################################################################
#予以废弃  效果训练的很差
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '2'
import torch
import torch.nn as nn
import torchvision.transforms as transforms
import torch.nn.functional as F
import sklearn
import torchvision
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import PIL
import json
from PIL import Image, ImageEnhance
import albumentations as A
import mmdet
import mmcv
from albumentations.pytorch import ToTensorV2
import glob
from pathlib import Path
import pycocotools
from pycocotools import mask
import numpy.random
import random
import cv2
import re
from mmdet.datasets import build_dataset
from mmdet.models import build_detector
from mmdet.apis import train_detector
from mmdet.apis import inference_detector, init_detector, show_result_pyplot, set_random_seed
import gc


IMG_WIDTH = 704
IMG_HEIGHT = 520



def rle_decode(mask_rle, shape):
    '''
    mask_rle: run-length as string formated (start length)
    shape: (height,width) of array to return 
    Returns numpy array, 1 - mask, 0 - background

    '''
    s = mask_rle.split()
    starts, lengths = [np.asarray(x, dtype=int) for x in (s[0:][::2], s[1:][::2])]
    starts -= 1
    ends = starts + lengths
    img = np.zeros(shape[0]*shape[1], dtype=np.uint8)
    for lo, hi in zip(starts, ends):
        img[lo:hi] = 1
    return img.reshape(shape)

def rle_encode(img):
    '''
    img: numpy array, 1 - mask, 0 - background
    Returns run length as string formated
    '''
    pixels = img.flatten()
    pixels = np.concatenate([[0], pixels, [0]])
    runs = np.where(pixels[1:] != pixels[:-1])[0] + 1
    runs[1::2] -= runs[::2]
    return ' '.join(str(x) for x in runs)



for fold_id in range(1,6):
    from mmcv import Config
    # cfg = Config.fromfile('/kaggle/working/mmdetection/configs/htc/htc_x101_64x4d_fpn_dconv_c3-c5_mstrain_400_1400_16x1_20e_coco.py')
    cfg = Config.fromfile('/home/shinewine/anaconda3/envs/detect3/lib/python3.8/site-packages/mmdet/.mim/configs/cascade_rcnn/cascade_mask_rcnn_x101_64x4d_fpn_20e_coco.py')
    # print(cfg.pretty_text)

    # print(cfg)

    cfg.dataset_type = 'CocoDataset'
    cfg.data_root = './'

    for head in cfg.model.roi_head.bbox_head:
        head.num_classes = 3
        
    # for head in cfg.model.roi_head.mask_head:
    #     head.num_classes = 3
        
    # cfg.model.roi_head.mask_head.semantic_head.num_classes=3
    cfg.model.roi_head.mask_head.num_classes=3


    # cfg.data.train = cfg.data.train.dataset
    cfg.data.train.type = 'CocoDataset'
    # cfg.data.train.ann_file = ['../data/mmd_annotations_train_fold1.json',
    #                           "../data/mmd_LIVECell_test.json",
    #                           "../data/mmd_LIVECell_train.json",
    #                           "../data/mmd_LIVECell_val.json"]
    # cfg.data.train.img_prefix = ["../data/",
    #                             "../data/LIVECell_dataset_2021/total/SHSY5Y/",
    #                             "../data/LIVECell_dataset_2021/total/SHSY5Y/",
    #                             "../data/LIVECell_dataset_2021/total/SHSY5Y/",
    #                             ]

    cfg.data.train.ann_file = '../data/mmd_annotations_train_fold{}.json'.format(fold_id)
    cfg.data.train.img_prefix = "../data/"

    # cfg.data.train.ann_file = "../input/cell-seg-3407-split/mmd_annotations_train_fold1.json"
    # cfg.data.train.img_prefix =  "../input/sartorius-cell-instance-segmentation/"
    cfg.data.train.classes = ('shsy5y', 'cort', 'astro')

    cfg.data.test.type = 'CocoDataset'
    cfg.data.test.ann_file = '../data/mmd_annotations_val_fold{}.json'.format(fold_id)
    cfg.data.test.img_prefix = "../data/"
    cfg.data.test.classes = ('shsy5y', 'cort', 'astro')


    cfg.data.val.type = 'CocoDataset'
    cfg.data.val.ann_file = '../data/mmd_annotations_val_fold{}.json'.format(fold_id)
    cfg.data.val.img_prefix = "../data/"
    cfg.data.val.classes = ('shsy5y', 'cort', 'astro')

    cfg.train_pipeline = [
        dict(type='LoadImageFromFile'),
        dict(type='LoadAnnotations', with_bbox=True, with_mask=True),
    #     dict(type='Resize', img_scale=[(440, 596), (480, 650), (520, 704), (580, 785), (620, 839)], multiscale_mode='value', keep_ratio=True),
    #     dict(type='Resize', img_scale=[(880, 1192), (960, 130), (1040, 1408), (1160, 1570), (1240, 1678)], multiscale_mode='value', keep_ratio=True),
    #     dict(type='Resize', img_scale=[(1333, 800), (1690, 960)]),
        dict(
                type='Resize',
                img_scale=[(1333, 640), (1333, 800)],
                multiscale_mode='range',
                keep_ratio=True),
        dict(type='RandomFlip',direction=['horizontal', 'vertical'], flip_ratio=0.5),
        dict(
            type='Normalize',
            mean=[123.675, 116.28, 103.53],
            std=[58.395, 57.12, 57.375],
            to_rgb=True),
        dict(type='Pad', size_divisor=32),
        dict(type='DefaultFormatBundle'), 
        dict(type='Collect', keys=['img', 'gt_bboxes', 'gt_masks', 'gt_labels'])
    ]

    cfg.val_pipeline = [
        dict(type='LoadImageFromFile'),
        dict(
            type='MultiScaleFlipAug',
    #         img_scale=[(880, 1192), (960, 130), (1040, 1408), (1160, 1570), (1240, 1678)],
    #         img_scale = [(1333, 800), (1690, 960)],
            img_scale=(1333, 800),
    #         img_scale = (520, 704),
            flip=False,
            transforms=[
                dict(type='Resize', keep_ratio=True),
    #             dict(type='RandomFlip'),
                dict(
                    type='Normalize',
                    mean=[123.675, 116.28, 103.53],
                    std=[58.395, 57.12, 57.375],
                    to_rgb=True),
                dict(type='Pad', size_divisor=32),
                dict(type='ImageToTensor', keys=['img']),
                dict(type='Collect', keys=['img'])
            ])
    ]


    cfg.test_pipeline =[
        dict(type='LoadImageFromFile'),
        dict(
            type='MultiScaleFlipAug',
    #         img_scale=[(880, 1192), (960, 130), (1040, 1408), (1160, 1570), (1240, 1678)],
    #         img_scale = [(1333, 800), (1690, 960)],
            img_scale=(1333, 800),
    #         img_scale = (520, 704),
            flip=False,
            transforms=[
                dict(type='Resize', keep_ratio=True),
    #             dict(type='RandomFlip'),
                dict(
                    type='Normalize',
                    mean=[123.675, 116.28, 103.53],
                    std=[58.395, 57.12, 57.375],
                    to_rgb=True),
                dict(type='Pad', size_divisor=32),
                dict(type='ImageToTensor', keys=['img']),
                dict(type='Collect', keys=['img'])
            ])
    ]

    cfg.data.train.pipeline = cfg.train_pipeline

    cfg.data.val.pipeline = cfg.val_pipeline
    cfg.data.test.pipeline = cfg.test_pipeline

    # cfg.load_from = '../input/htc-checkpoint-resnext101/htc_x101_64x4d_fpn_dconv_c3-c5_mstrain_400_1400_16x1_20e_coco_20200312-946fd751.pth'
    cfg.load_from = '/storage/Kaggle_Cell_Segmentation/model/cascade_mask_rcnn_x101_64x4d_fpn_20e_coco_20200512_161033-bdb5126a.pth'

    # 创建工作区
    os.makedirs(("../model/MaskRNN/test12/fold{}".format(fold_id)), exist_ok=True)
    cfg.work_dir = "../model/MaskRNN/test12/fold{}".format(fold_id)

    cfg.optimizer.lr = 0.01
    # cfg.optimizer.weight_decay = 0.001

    #### 修正 loss的权重  这道题 bbox的 loss 我们其实根本不关心
    # rpn _ loss 
    # cfg.model.rpn_head.loss_cls.loss_weight =  1.0
    # cfg.model.rpn_head.loss_bbox.loss_weight =  1.0

    # MultiStep
    cfg.lr_config = dict(
        policy='step',
        warmup='linear',
        warmup_iters=1000,
        warmup_ratio=0.001,
        step=[6, 16])

    # Poly
    # cfg.lr_config = dict(policy='poly',
    #                      power= 1.4, 
    #                      min_lr=cfg.optimizer.lr * 0.01, 
    #                      by_epoch=False,
    #                      warmup='linear',
    #                      warmup_iters= 2000,
    #                      warmup_ratio=0.001,
    #                     )

    cfg.data.samples_per_gpu = 2
    cfg.data.workers_per_gpu = 2

    cfg.evaluation.metric = 'segm'
    cfg.evaluation.interval = 1

    cfg.checkpoint_config.interval = 1
    cfg.runner.max_epochs = 20
    cfg.log_config.interval = 20

    # cfg.model.rpn_head.anchor_generator.base_sizes = [4, 9, 17, 31, 64]
    # cfg.model.rpn_head.anchor_generator.strides = [4, 8, 16, 32, 64]


    cfg.seed = 19970711
    set_random_seed(0, deterministic=False)
    cfg.gpu_ids = range(1)
    cfg.fp16 = dict(loss_scale=512.0)
    meta = dict()
    meta['config'] = cfg.pretty_text

    # roi _ head loss
    for head in cfg.model.roi_head.bbox_head:
        head.loss_cls.loss_weight = 1.5

    # print(f'Config:\n{cfg.pretty_text}')
    # balance_dataset = dict(
    #     type='ClassBalancedDataset',
    #     oversample_thr=1e-3,
    #     dataset = cfg.data.train
    # )


    datasets = [build_dataset(cfg.data.train)]
    print(datasets[0].CLASSES)
    model = build_detector(cfg.model, train_cfg=cfg.get('train_cfg'), test_cfg=cfg.get('test_cfg'))
    model.CLASSES = datasets[0].CLASSES
    mmcv.mkdir_or_exist(os.path.abspath(cfg.work_dir))
    train_detector(model, datasets, cfg, distributed=False, validate=True, meta=meta)


    del datasets,cfg,model
    gc.collect()

# 运行完成后 请马上执行 LocalCV 即可同时 