#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# 
# Modification: enable loading image sequences.

import os
import random
import tarfile
import io
import glob
import torch
import torch.utils.data
from fvcore.common.file_io import PathManager

import corr_net.utils.logging as logging

from . import decoder as decoder
from . import utils as utils
from . import video_container as container
from .build import DATASET_REGISTRY

logger = logging.get_logger(__name__)


@DATASET_REGISTRY.register()
class Kinetics(torch.utils.data.Dataset):
    """
    Kinetics video loader. Construct the Kinetics video loader, then sample
    clips from the videos. For training and validation, a single clip is
    randomly sampled from every video with random cropping, scaling, and
    flipping. For testing, multiple clips are uniformaly sampled from every
    video with uniform cropping. For uniform cropping, we take the left, center,
    and right crop if the width is larger than height, or take top, center, and
    bottom crop if the height is larger than the width.
    """

    def __init__(self, cfg, mode, num_retries=10):
        """
        Construct the Kinetics video loader with a given csv file. 
        If data files are videos, the format of the csv file should be:
        ```
        path_to_video_1 label_1
        path_to_video_2 label_2
        ...
        path_to_video_N label_N
        ```
        If data files are tar-wrapped frame sequences, the format of the csv file should be:
        ```
        path_to_tar_1 label_1 fps_1(float) num_frames_1(int) duration_1(int/"None")
        ...
        path_to_tar_N label_N fps_N(float) num_frames_N(int) duration_N(int/"None")
        ```
        Args:
            cfg (CfgNode): configs.
            mode (string): Options includes `train`, `val`, or `test` mode.
                For the train and val mode, the data loader will take data
                from the train or val set, and sample one clip per video.
                For the test mode, the data loader will take data from test set,
                and sample multiple clips per video.
            num_retries (int): number of retries.
        """
        # Only support train, val, and test mode.
        assert mode in [
            "train",
            "val",
            "test",
        ], "Split '{}' not supported for Kinetics".format(mode)
        self.mode = mode
        self.cfg = cfg

        self._video_meta = {}
        self._num_retries = num_retries
        # For training or validation mode, one single clip is sampled from every
        # video. For testing, NUM_ENSEMBLE_VIEWS clips are sampled from every
        # video. For every clip, NUM_SPATIAL_CROPS is cropped spatially from
        # the frames.
        if self.mode in ["train", "val", "test"]:
            self._num_clips = 1
        #elif self.mode in ["test"]:
            #self._num_clips = (
                #cfg.TEST.NUM_ENSEMBLE_VIEWS * cfg.TEST.NUM_SPATIAL_CROPS
            #)

        logger.info("Constructing Kinetics {}...".format(mode))
        self._construct_loader()

    def _construct_loader(self):
        """
        Construct the video loader.
        """
        if self.cfg.DATA.USE_FRAME_SEQUENCES:
            path_to_file = os.path.join(
                self.cfg.DATA.PATH_TO_DATA_DIR, "BharatDSL_{}.csv".format(self.mode)
            )
        else:
            path_to_file = os.path.join(
                self.cfg.DATA.PATH_TO_DATA_DIR, "{}.csv".format(self.mode)
            )
        assert PathManager.exists(path_to_file), "{} dir not found".format(
            path_to_file
        )

        self._path_to_videos = []
        self._labels = []
        self._spatial_temporal_idx = []
        with PathManager.open(path_to_file, "r") as f:
            for clip_idx, path_label in enumerate(f.read().splitlines()):
                #print('path_label = ', path_label)
                #assert (
                    #len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR))
                    #== 2 or
                    #len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 5
                #)
                if self.cfg.DATA.USE_FRAME_SEQUENCES:
                    set_name, class_name, class_sample, num_frames, label = path_label.strip().split(
                        self.cfg.DATA.PATH_LABEL_SEPARATOR)
                    path = os.path.join('BharatDSL_dataset', set_name, class_name, class_sample)
                else:
                    path, label = path_label.split(
                        self.cfg.DATA.PATH_LABEL_SEPARATOR
                    )

                for idx in range(self._num_clips):
                    self._path_to_videos.append(
                        os.path.join(self.cfg.DATA.PATH_PREFIX, path)
                    )
                    self._labels.append(int(label))
                    self._spatial_temporal_idx.append(idx)
                    self._video_meta[clip_idx * self._num_clips + idx] = {}
                    if self.cfg.DATA.USE_FRAME_SEQUENCES:
                        #self._video_meta[clip_idx * self._num_clips + idx]['fps'] = float(fps)
                        self._video_meta[clip_idx * self._num_clips + idx]['num_frames'] = int(num_frames)
                        #self._video_meta[clip_idx * self._num_clips + idx]['duration'] = \
                            #None if duration == "None" else int(duration)
        assert (
            len(self._path_to_videos) > 0
        ), "Failed to load Kinetics split {} from {}".format(
            self.mode, path_to_file
        )
        logger.info(
            "Constructing kinetics dataloader (size: {}) from {}".format(
                len(self._path_to_videos), path_to_file
            )
        )

    def __getitem__(self, index):
        """
        Given the video index, return the list of frames, label, and video
        index if the video can be fetched and decoded successfully, otherwise
        repeatly find a random video that can be decoded as a replacement.
        Args:
            index (int): the video index provided by the pytorch sampler.
        Returns:
            frames (tensor): the frames of sampled from the video. The dimension
                is `channel` x `num frames` x `height` x `width`.
            label (int): the label of the current video.
            index (int): if the video provided by pytorch sampler can be
                decoded, then return the index of the video. If not, return the
                index of the video replacement that can be decoded.
        """
        short_cycle_idx = None
        # When short cycle is used, input index is a tupple.
        if isinstance(index, tuple):
            index, short_cycle_idx = index

        if self.mode in ["train", "val"]:
            # -1 indicates random sampling.
            temporal_sample_index = -1
            spatial_sample_index = -1
            min_scale = self.cfg.DATA.TRAIN_JITTER_SCALES[0]
            max_scale = self.cfg.DATA.TRAIN_JITTER_SCALES[1]
            crop_size = self.cfg.DATA.TRAIN_CROP_SIZE
            if short_cycle_idx in [0, 1]:
                crop_size = int(
                    round(
                        self.cfg.MULTIGRID.SHORT_CYCLE_FACTORS[short_cycle_idx]
                        * self.cfg.MULTIGRID.DEFAULT_S
                    )
                )
            if self.cfg.MULTIGRID.DEFAULT_S > 0:
                # Decreasing the scale is equivalent to using a larger "span"
                # in a sampling grid.
                min_scale = int(
                    round(
                        float(min_scale)
                        * crop_size
                        / self.cfg.MULTIGRID.DEFAULT_S
                    )
                )
        elif self.mode in ["test"]:
            temporal_sample_index = (
                self._spatial_temporal_idx[index]
                // self.cfg.TEST.NUM_SPATIAL_CROPS
            )
            # spatial_sample_index is in [0, 1, 2]. Corresponding to left,
            # center, or right if width is larger than height, and top, middle,
            # or bottom if height is larger than width.
            spatial_sample_index = (
                (
                    self._spatial_temporal_idx[index]
                    % self.cfg.TEST.NUM_SPATIAL_CROPS
                )
                if self.cfg.TEST.NUM_SPATIAL_CROPS > 1
                else 1
            )
            min_scale, max_scale, crop_size = (
                [self.cfg.DATA.TEST_CROP_SIZE] * 3
                if self.cfg.TEST.NUM_SPATIAL_CROPS > 1
                else [self.cfg.DATA.TRAIN_JITTER_SCALES[0]] * 2
                + [self.cfg.DATA.TEST_CROP_SIZE]
            )
            # The testing is deterministic and no jitter should be performed.
            # min_scale, max_scale, and crop_size are expect to be the same.
            assert len({min_scale, max_scale}) == 1
        else:
            raise NotImplementedError(
                "Does not support {} mode".format(self.mode)
            )
        sampling_rate = utils.get_random_sampling_rate(
            self.cfg.MULTIGRID.LONG_CYCLE_SAMPLING_RATE,
            self.cfg.DATA.SAMPLING_RATE,
        )
        # Try to decode and sample a clip from a video. If the video can not be
        # decoded, repeatly find a random video replacement that can be decoded.
        for i_try in range(self._num_retries):
            if self.cfg.DATA.USE_FRAME_SEQUENCES:
                # decode selected frame jpegs
                try:
                    #print('index = ', index)
                    tar_handler = self._path_to_videos[index]
                    #print('tar_handler = ', tar_handler)
                    frame_list = glob.glob(os.path.join(tar_handler+'_*.png'))
                    #print('frame_list =', frame_list)
                    if len(frame_list) != self._video_meta[index]['num_frames']:
                        raise Exception("Unmatched num of frames and len of sequence")
                    elif len(frame_list) < 5:
                        raise Exception("Too few frames, video might be corrupted")
                except Exception as e:
                    logger.info(
                        "Failed to load tar file from {} with error: {}".format(
                            self._path_to_videos[index])
                    )
                    tar_handler = None

                # Select a random tar if the current tar was not able to access.
                if tar_handler is None:
                    logger.warning(
                        "Failed to meta load tar file from {}; trial {}".format(
                            self._path_to_videos[index], i_try
                        )
                    )
                    if (
                        self.mode not in ["test"]
                        and i_try > self._num_retries // 2
                    ):
                        # let's try another one
                        index = random.randint(0, len(self._path_to_videos) - 1)
                    continue

                # temporarily select and decode frames
                # frames: Tensor of (num_frames, h, w, c)
                frames = decoder.decode_seq(
                    tar_handler,
                    sampling_rate,
                    frame_list,
                    self._video_meta[index],
                    self.cfg.DATA.NUM_FRAMES,
                )
                
            else:
                # decode videos
                video_container = None
                try:
                    video_container = container.get_video_container(
                        self._path_to_videos[index],
                        self.cfg.DATA_LOADER.ENABLE_MULTI_THREAD_DECODE,
                        self.cfg.DATA.DECODING_BACKEND,
                    )
                except Exception as e:
                    logger.info(
                        "Failed to load video from {} with error {}".format(
                            self._path_to_videos[index], e
                        )
                    )
                # Select a random video if the current video was not able to access.
                if video_container is None:
                    logger.warning(
                        "Failed to meta load video idx {} from {}; trial {}".format(
                            index, self._path_to_videos[index], i_try
                        )
                    )
                    if (
                        self.mode not in ["test"]
                        and i_try > self._num_retries // 2
                    ):
                        # let's try another one
                        index = random.randint(0, len(self._path_to_videos) - 1)
                    continue

                # Decode video. Meta info is used to perform selective decoding.
                # frames: Tensor of (num_frames, h, w, c)
                frames = decoder.decode(
                    video_container,
                    sampling_rate,
                    self.cfg.DATA.NUM_FRAMES,
                    temporal_sample_index,
                    self.cfg.TEST.NUM_ENSEMBLE_VIEWS,
                    video_meta=self._video_meta[index],
                    target_fps=self.cfg.DATA.TARGET_FPS,
                    backend=self.cfg.DATA.DECODING_BACKEND,
                    max_spatial_scale=min_scale,
                )

            # If decoding failed (wrong format, video is too short, and etc),
            # select another video.
            if frames is None:
                logger.warning(
                    "Failed to decode {} idx {} from {}; trial {}".format(
                        "tar file" if self.cfg.DATA.USE_FRAME_SEQUENCES else "video",
                        index, self._path_to_videos[index], i_try
                    )
                )
                if (
                    self.mode not in ["test"]
                    and i_try > self._num_retries // 2
                ):
                    # let's try another one
                    index = random.randint(0, len(self._path_to_videos) - 1)
                continue

            # Perform color normalization.
            frames = utils.tensor_normalize(
                frames, self.cfg.DATA.MEAN, self.cfg.DATA.STD
            )
            # T H W C -> C T H W.
            frames = frames.permute(3, 0, 1, 2)
            # Perform data augmentation.
            frames = utils.spatial_sampling(
                frames,
                spatial_idx=spatial_sample_index,
                min_scale=min_scale,
                max_scale=max_scale,
                crop_size=crop_size,
                random_horizontal_flip=self.cfg.DATA.RANDOM_FLIP,
                inverse_uniform_sampling=self.cfg.DATA.INV_UNIFORM_SAMPLE,
            )

            label = self._labels[index]
            frames = utils.pack_pathway_output(self.cfg, frames)
            return frames, label, index, {}
        else:
            raise RuntimeError(
                "Failed to fetch video after {} retries.".format(
                    self._num_retries
                )
            )

    def __len__(self):
        """
        Returns:
            (int): the number of videos in the dataset.
        """
        return len(self._path_to_videos)
