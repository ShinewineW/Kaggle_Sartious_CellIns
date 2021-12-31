# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# Modified by Sangrok Lee and Youngwan Lee (ETRI), 2020. All Rights Reserved.
# Modified by Christoffer Edlund (Sartorius), 2020. All Rights Reserved.
import types
import contextlib
import copy
import io
import itertools
import json
import logging
import numpy as np
import os
import pickle
from collections import OrderedDict
import pycocotools.mask as mask_util
import torch
from fvcore.common.file_io import PathManager
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
from tabulate import tabulate

import detectron2.utils.comm as comm
from detectron2.data import MetadataCatalog,DatasetCatalog
from detectron2.data.datasets.coco import convert_to_coco_json
from detectron2.structures import Boxes, BoxMode, pairwise_iou
from detectron2.utils.logger import create_small_table
import pandas as pd

from detectron2.evaluation.evaluator import DatasetEvaluator
#from detectron2.evaluation.fast_eval_api import COCOeval_opt
_use_fast_impl = True
try:
    from fast_coco_eval import COCOeval_fast as COCOeval_opt
except ImportError:
    print(f"Could not find fast coco implementation")
    _use_fast_impl = False


def precision_at(threshold, iou):
    """
    Computes the precision at a given threshold.

    Args:
        threshold (float): Threshold.
        iou (np array [n_truths x n_preds]): IoU matrix.

    Returns:
        int: Number of true positives,
        int: Number of false positives,
        int: Number of false negatives.
    """
    matches = iou > threshold
    true_positives = np.sum(matches, axis=1) >= 1  # Correct objects
    false_negatives = np.sum(matches, axis=1) == 0  # Missed objects
    false_positives = np.sum(matches, axis=0) == 0  # Extra objects
    tp, fp, fn = (
        np.sum(true_positives),
        np.sum(false_positives),
        np.sum(false_negatives),
    )
    return tp, fp, fn


def iou_map(ious,verbose=0):
    """
    Computes the metric for the competition.
    Masks contain the segmented pixels where each object has one value associated,
    and 0 is the background.

    Args:
        truths (list of masks): Ground truths.
        preds (list of masks): Predictions.
        verbose (int, optional): Whether to print infos. Defaults to 0.

    Returns:
        float: mAP.
    """

    if verbose:
        print("Thresh\tTP\tFP\tFN\tPrec.")

    prec = []
    for t in np.arange(0.5, 1.0, 0.05):
        tps, fps, fns = 0, 0, 0
        tp, fp, fn = precision_at(t, ious)
        tps += tp
        fps += fp
        fns += fn

        p = tps / (tps + fps + fns)
        prec.append(p)

        if verbose:
            print("{:1.3f}\t{}\t{}\t{}\t{:1.3f}".format(t, tps, fps, fns, p))

    if verbose:
        print("AP\t-\t-\t-\t{:1.3f}".format(np.mean(prec)))

    return np.mean(prec)


class COCOEvaluator(DatasetEvaluator):
    """
    Evaluate object proposal, instance detection/segmentation, keypoint detection
    outputs using COCO's metrics and APIs.
    """

    def __init__(self, dataset_name, cfg, distributed, output_dir=None ,TOPK_TYPE = 'livecell'):
        """
        Args:
            dataset_name (str): name of the dataset to be evaluated.
                It must have either the following corresponding metadata:
                    "json_file": the path to the COCO format annotation
                Or it must be in detectron2's standard dataset format
                so it can be converted to COCO format automatically.
            cfg (CfgNode): config instance
            distributed (True): if True, will collect results from all ranks for evaluation.
                Otherwise, will evaluate the results in the current process.
            output_dir (str): optional, an output directory to dump all
                results predicted on the dataset. The dump contains two files:
                1. "instance_predictions.pth" a file in torch serialization
                   format that contains all the raw original predictions.
                2. "coco_instances_results.json" a json file in COCO's result
                   format.
            TOPK_TYPE: 'default' 或者 'livecell'
                当为 default 时， 积分阈值为 .15,.3,.55
                当为 livecell时， 积分阈值为 .25,.45,.65
        """
        print("__init__")
        self._tasks = self._tasks_from_config(cfg)
        self._distributed = distributed
        self._output_dir = output_dir

        self._cpu_device = torch.device("cpu")
        self._logger = logging.getLogger(__name__)

        self._metadata = MetadataCatalog.get(dataset_name)
        if not hasattr(self._metadata, "json_file"):
            self._logger.warning(
                f"json_file was not found in MetaDataCatalog for '{dataset_name}'."
                " Trying to convert it to COCO format ..."
            )

            cache_path = os.path.join(output_dir, f"{dataset_name}_coco_format.json")
            self._metadata.json_file = cache_path
            convert_to_coco_json(dataset_name, cache_path)

        json_file = PathManager.get_local_path(self._metadata.json_file)
        with contextlib.redirect_stdout(io.StringIO()):
            self._coco_api = COCO(json_file)

        self._kpt_oks_sigmas = cfg.TEST.KEYPOINT_OKS_SIGMAS
        # Test set json files do not contain annotations (evaluation must be
        # performed using the COCO evaluation server).
        self._do_evaluation = "annotations" in self._coco_api.dataset

        # ShineWine Update:
        if TOPK_TYPE == 'default':
            self._SCORE_THRESHOLD = [0.15,0.30,0.55]
        if TOPK_TYPE == 'livecell':
            self._SCORE_THRESHOLD = [0.25,0.45,0.65]
        self._MIN_PIXELS = [60,140,75]

        dataset_dicts = DatasetCatalog.get(dataset_name)
        self._annotations_cache = {item['image_id']:item['annotations'] for item in dataset_dicts}

    def reset(self):
        self._predictions = []
        self._scores = []

    def _tasks_from_config(self, cfg):
        """
        Returns:
            tuple[str]: tasks that can be evaluated under the given configuration.
        """
        print("_tasks_from_config")
        # tasks = ("bbox",)
        if cfg.MODEL.MASK_ON:
            tasks = ("segm",)
        if cfg.MODEL.KEYPOINT_ON:
            tasks = tasks + ("keypoints",)
        print(f"tasks: {tasks}")
        return tasks

    def process(self, inputs, outputs):
        """
        Args:
            inputs: the inputs to a COCO model (e.g., GeneralizedRCNN).
                It is a list of dict. Each dict corresponds to an image and
                contains keys like "height", "width", "file_name", "image_id".
            outputs: the outputs of a COCO model. It is a list of dicts with key
                "instances" that contains :class:`Instances`.
        """

        for input, output in zip(inputs, outputs):
            prediction = {"image_id": input["image_id"]}
            ###################################################################################
            # Insert: 将阈值优化部分写入 
            
            pred_classes = output['instances'].pred_classes.cpu().numpy().tolist()
            pred_class = max(set(pred_classes), key=pred_classes.count)
            take_mojorities = output['instances'].pred_classes == pred_class
            take = output['instances'].scores >= self._SCORE_THRESHOLD[pred_class]
            take = take & take_mojorities

            # 将 MIN_PIXELS优化部分写入
            # print(output['instances'].pred_masks.sum(dim = 1).sum(dim = 1).shape)
            take_sum = output['instances'].pred_masks.sum(dim = 1).sum(dim = 1) >= self._MIN_PIXELS[pred_class]
            assert(len(take_mojorities) == len(take_sum))
            take = take & take_sum
            
            # 修改 output中的值
            output['instances'] = output['instances'][take]
            ###################################################################################


            # TODO this is ugly
            if "instances" in output:
                instances = output["instances"].to(self._cpu_device)
                prediction["instances"] = instances_to_coco_json(instances, input["image_id"])
            if "proposals" in output:
                prediction["proposals"] = output["proposals"].to(self._cpu_device)
            self._predictions.append(prediction)

    def evaluate(self):

        print("evaluate")

        if self._distributed:
            comm.synchronize()
            predictions = comm.gather(self._predictions, dst=0)
            predictions = list(itertools.chain(*predictions))

            print("Distributed Open! The total num of eval is {}".format(len(predictions)))

            if not comm.is_main_process():
                return {}
        else:
            predictions = self._predictions

        if len(predictions) == 0:
            self._logger.warning("[COCOEvaluator] Did not receive valid predictions.")
            return {}

        if self._output_dir:
            PathManager.mkdirs(self._output_dir)
            file_path = os.path.join(self._output_dir, "instances_predictions.pth")
            with PathManager.open(file_path, "wb") as f:
                torch.save(predictions, f)

        self._results = OrderedDict()
        if "proposals" in predictions[0]:
            self._eval_box_proposals(predictions)
        if "instances" in predictions[0]:
            #########################################################################################################################################################
            # 如果要完全加入 原有的方式 就在这里修正
            # 传入的 predections 是一个列表  列表中的每一项为  一个字典  表示单张图片的结果
            # 这个字典只有两个键值   instances  和   image_id
            # 然后 instances中保存的为列表， 列表的每一项为预测的 每一个结果
            for per_img_dic in predictions:
                targ = self._annotations_cache[per_img_dic["image_id"]]
                enc_targs = list(map(lambda x:x['segmentation'], targ))

                enc_preds = []
                for pred_instance in per_img_dic["instances"]:
                    enc_preds.append(pred_instance["segmentation"])
                if enc_preds != []:
                    ious = mask_util.iou(enc_preds, enc_targs, [0]*len(enc_targs))
                    self._scores.append(iou_map(ious))
                else:
                    self._scores.append(0)
            print("Eval nums is {}".format(len(self._scores)))
            ################################################################################################################################################
            # self._eval_predictions(set(self._tasks), predictions)
        # Copy so the caller can do whatever with results
        return {"MaP IoU": np.mean(self._scores)}
        # return copy.deepcopy(self._results)

    def _eval_predictions(self, tasks, predictions):
        """
        Evaluate predictions on the given tasks.
        Fill self._results with the metrics of the tasks.
        """

        print("_eval_predictions")
        print(f"use_fast_impl: {_use_fast_impl}")

        self._logger.info("Preparing results for COCO format ...")
        coco_results = list(itertools.chain(*[x["instances"] for x in predictions]))

        # unmap the category ids for COCO
        if hasattr(self._metadata, "thing_dataset_id_to_contiguous_id"):
            reverse_id_mapping = {
                v: k for k, v in self._metadata.thing_dataset_id_to_contiguous_id.items()
            }
            for result in coco_results:
                category_id = result["category_id"]
                assert (
                        category_id in reverse_id_mapping
                ), "A prediction has category_id={}, which is not available in the dataset.".format(
                    category_id
                )
                result["category_id"] = reverse_id_mapping[category_id]

        if self._output_dir:
            file_path = os.path.join(self._output_dir, "coco_instances_results.json")
            self._logger.info("Saving results to {}".format(file_path))
            with PathManager.open(file_path, "w") as f:
                f.write(json.dumps(coco_results))
                f.flush()

        if not self._do_evaluation:
            self._logger.info("Annotations are not available for evaluation.")
            return

        self._logger.info("Evaluating predictions ...")
        for task in sorted(tasks):
            coco_eval = (
                _evaluate_predictions_on_coco(
                    self._coco_api, coco_results, task, kpt_oks_sigmas=self._kpt_oks_sigmas
                )
                if len(coco_results) > 0
                else None  # cocoapi does not handle empty results very well
            )
            # print(self._metadata.get("thing_classes"))
            # 这里 dataset中的 thing_classes已经正确导入
            res = self._derive_coco_results(
                coco_eval, task, class_names= self._metadata.get("thing_classes")
            )
            self._results[task] = res

    def _eval_box_proposals(self, predictions):
        """
        Evaluate the box proposals in predictions.
        Fill self._results with the metrics for "box_proposals" task.
        """
        print("_eval_box_proposals")
        if self._output_dir:
            # Saving generated box proposals to file.
            # Predicted box_proposals are in XYXY_ABS mode.
            bbox_mode = BoxMode.XYXY_ABS.value
            ids, boxes, objectness_logits = [], [], []
            for prediction in predictions:
                ids.append(prediction["image_id"])
                boxes.append(prediction["proposals"].proposal_boxes.tensor.numpy())
                objectness_logits.append(prediction["proposals"].objectness_logits.numpy())

            proposal_data = {
                "boxes": boxes,
                "objectness_logits": objectness_logits,
                "ids": ids,
                "bbox_mode": bbox_mode,
            }
            with PathManager.open(os.path.join(self._output_dir, "box_proposals.pkl"), "wb") as f:
                pickle.dump(proposal_data, f)

        if not self._do_evaluation:
            self._logger.info("Annotations are not available for evaluation.")
            return

        self._logger.info("Evaluating bbox proposals ...")
        res = {}
        areas = {"all": "", "small": "s", "medium": "m", "large": "l"}
        for limit in [100, 1000]:
            for area, suffix in areas.items():
                stats = _evaluate_box_proposals(predictions, self._coco_api, area=area, limit=limit)
                key = "AR{}@{:d}".format(suffix, limit)
                res[key] = float(stats["ar"].item() * 100)
        self._logger.info("Proposal metrics: \n" + create_small_table(res))
        self._results["box_proposals"] = res

    def _derive_coco_results(self, coco_eval, iou_type, class_names=None):
        """
        Derive the desired score numbers from summarized COCOeval.
        Args:
            coco_eval (None or COCOEval): None represents no predictions from model.
            iou_type (str):
            class_names (None or list[str]): if provided, will use it to predict
                per-category AP.
        Returns:
            a dict of {metric name: score}
        """
        print("_derive_coco_results")

        metrics = {
            "bbox": ["AP", "AP50", "AP75", "APs", "APm", "APl"],
            "segm": ["AP", "AP50", "AP75", "APs", "APm", "APl"],
            "keypoints": ["AP", "AP50", "AP75", "APm", "APl"],
        }[iou_type]

        if coco_eval is None:
            self._logger.warn("No predictions from the model!")
            return {metric: float("nan") for metric in metrics}

        # the standard metrics
        results = {
            metric: float(coco_eval.stats[idx] * 100 if coco_eval.stats[idx] >= 0 else "nan")
            for idx, metric in enumerate(metrics)
        }
        self._logger.info(
            "Evaluation results for {}: \n".format(iou_type) + create_small_table(results)
        )
        if not np.isfinite(sum(results.values())):
            self._logger.info("Note that some metrics cannot be computed.")

        if class_names is None or len(class_names) <= 1:
            return results
        # Compute per-category AP
        # from https://github.com/facebookresearch/Detectron/blob/a6a835f5b8208c45d0dce217ce9bbda915f44df7/detectron/datasets/json_dataset_evaluator.py#L222-L252 # noqa
        precisions = coco_eval.eval["precision"]

        # print("precisions:::: {}".format(precisions.shape))
        # print(precisions)
        # precision has dims (iou, recall, cls, area range, max dets)
        assert len(class_names) == precisions.shape[2]

        results_per_category = []
        for idx, name in enumerate(class_names):
            # area range index 0: all area ranges
            # max dets index -1: typically 100 per image
            precision = precisions[:, :, idx, 0, -1]
            precision = precision[precision > -1]
            ap = np.mean(precision) if precision.size else float("nan")
            results_per_category.append(("{}".format(name), float(ap * 100)))

        # tabulate it
        N_COLS = min(6, len(results_per_category) * 2)
        results_flatten = list(itertools.chain(*results_per_category))
        results_2d = itertools.zip_longest(*[results_flatten[i::N_COLS] for i in range(N_COLS)])
        table = tabulate(
            results_2d,
            tablefmt="pipe",
            floatfmt=".3f",
            headers=["category", "AP"] * (N_COLS // 2),
            numalign="left",
        )
        self._logger.info("Per-category {} AP: \n".format(iou_type) + table)

        results.update({"AP-" + name: ap for name, ap in results_per_category})
        return results


def instances_to_coco_json(instances, img_id):
    """
    Dump an "Instances" object to a COCO-format json that's used for evaluation.
    Args:
        instances (Instances):
        img_id (int): the image id
    Returns:
        list[dict]: list of json annotations in COCO format.
    """

    num_instance = len(instances)
    if num_instance == 0:
        return []

    boxes = instances.pred_boxes.tensor.numpy()
    boxes = BoxMode.convert(boxes, BoxMode.XYXY_ABS, BoxMode.XYWH_ABS)
    boxes = boxes.tolist()
    scores = instances.scores.tolist()
    classes = instances.pred_classes.tolist()

    has_mask = instances.has("pred_masks")
    has_mask_scores = instances.has("mask_scores")
    if has_mask:
        # use RLE to encode the masks, because they are too large and takes memory
        # since this evaluator stores outputs of the entire dataset
        rles = [
            mask_util.encode(np.array(mask[:, :, None], order="F", dtype="uint8"))[0]
            for mask in instances.pred_masks
        ]
        for rle in rles:
            # "counts" is an array encoded by mask_util as a byte-stream. Python3's
            # json writer which always produces strings cannot serialize a bytestream
            # unless you decode it. Thankfully, utf-8 works out (which is also what
            # the pycocotools/_mask.pyx does).
            rle["counts"] = rle["counts"].decode("utf-8")

        if has_mask_scores:
            mask_scores = instances.mask_scores.tolist()

    has_keypoints = instances.has("pred_keypoints")
    if has_keypoints:
        keypoints = instances.pred_keypoints

    results = []
    for k in range(num_instance):
        result = {
            "image_id": img_id,
            "category_id": classes[k],
            "bbox": boxes[k],
            "score": scores[k],
        }
        if has_mask:
            result["segmentation"] = rles[k]
            if has_mask_scores:
                result["mask_score"] = mask_scores[k]

        if has_keypoints:
            # In COCO annotations,
            # keypoints coordinates are pixel indices.
            # However our predictions are floating point coordinates.
            # Therefore we subtract 0.5 to be consistent with the annotation format.
            # This is the inverse of data loading logic in `datasets/coco.py`.
            keypoints[k][:, :2] -= 0.5
            result["keypoints"] = keypoints[k].flatten().tolist()
        results.append(result)
    return results


# inspired from Detectron:
# https://github.com/facebookresearch/Detectron/blob/a6a835f5b8208c45d0dce217ce9bbda915f44df7/detectron/datasets/json_dataset_evaluator.py#L255 # noqa
def _evaluate_box_proposals(dataset_predictions, coco_api, thresholds=None, area="all", limit=None):
    """
    Evaluate detection proposal recall metrics. This function is a much
    faster alternative to the official COCO API recall evaluation code. However,
    it produces slightly different results.
    """
#    print("_evaluate_box_proposals")
    # Record max overlap value for each gt box
    # Return vector of overlap values
    areas = {
        "all": 0,
        "small": 1,
        "medium": 2,
        "large": 3,
        "96-128": 4,
        "128-256": 5,
        "256-512": 6,
        "512-inf": 7,
    }


    area_ranges = [
        [0 ** 2, 1e5 ** 2],  # all
        [0 ** 2, 18 ** 2],  # small org: 0 - 32
        [18 ** 2, 31 ** 2],  # medium org: 32 - 96
        [31 ** 2, 1e5 ** 2],  # large org: 96 - 1e5
        [31 ** 2, 128 ** 2],  # org: 96-128
        [128 ** 2, 256 ** 2],  # 128-256
        [256 ** 2, 512 ** 2],  # 256-512
        [512 ** 2, 1e5 ** 2],
    ]  # 512-inf

    """
    area_ranges = [
        [0 ** 2, 1e5 ** 2],  # all
        [0 ** 2, 28 ** 2],  # small org: 0 - 32
        [28 ** 2, 94 ** 2],  # medium org: 32 - 96
        [94 ** 2, 1e5 ** 2],  # large org: 96 - 1e5 - our 64
        [94 ** 2, 128 ** 2],  #  org: 96-128
        [128 ** 2, 256 ** 2],  # 128-256
        [256 ** 2, 512 ** 2],  # 256-512
        [512 ** 2, 1e5 ** 2],
    ]  # 512-inf
    """
    assert area in areas, "Unknown area range: {}".format(area)
    area_range = area_ranges[areas[area]]
    gt_overlaps = []
    num_pos = 0

    for prediction_dict in dataset_predictions:
        predictions = prediction_dict["proposals"]

        # sort predictions in descending order
        # TODO maybe remove this and make it explicit in the documentation
        inds = predictions.objectness_logits.sort(descending=True)[1]
        predictions = predictions[inds]

        ann_ids = coco_api.getAnnIds(imgIds=prediction_dict["image_id"])
        anno = coco_api.loadAnns(ann_ids)
        gt_boxes = [
            BoxMode.convert(obj["bbox"], BoxMode.XYWH_ABS, BoxMode.XYXY_ABS)
            for obj in anno
            if obj["iscrowd"] == 0
        ]
        gt_boxes = torch.as_tensor(gt_boxes).reshape(-1, 4)  # guard against no boxes
        gt_boxes = Boxes(gt_boxes)
        gt_areas = torch.as_tensor([obj["area"] for obj in anno if obj["iscrowd"] == 0])

        if len(gt_boxes) == 0 or len(predictions) == 0:
            continue

        valid_gt_inds = (gt_areas >= area_range[0]) & (gt_areas <= area_range[1])
        gt_boxes = gt_boxes[valid_gt_inds]

        num_pos += len(gt_boxes)

        if len(gt_boxes) == 0:
            continue

        if limit is not None and len(predictions) > limit:
            predictions = predictions[:limit]

        overlaps = pairwise_iou(predictions.proposal_boxes, gt_boxes)

        _gt_overlaps = torch.zeros(len(gt_boxes))
        for j in range(min(len(predictions), len(gt_boxes))):
            # find which proposal box maximally covers each gt box
            # and get the iou amount of coverage for each gt box
            max_overlaps, argmax_overlaps = overlaps.max(dim=0)

            # find which gt box is 'best' covered (i.e. 'best' = most iou)
            gt_ovr, gt_ind = max_overlaps.max(dim=0)
            assert gt_ovr >= 0
            # find the proposal box that covers the best covered gt box
            box_ind = argmax_overlaps[gt_ind]
            # record the iou coverage of this gt box
            _gt_overlaps[j] = overlaps[box_ind, gt_ind]
            assert _gt_overlaps[j] == gt_ovr
            # mark the proposal box and the gt box as used
            overlaps[box_ind, :] = -1
            overlaps[:, gt_ind] = -1

        # append recorded iou coverage level
        gt_overlaps.append(_gt_overlaps)
    gt_overlaps = torch.cat(gt_overlaps, dim=0)
    gt_overlaps, _ = torch.sort(gt_overlaps)

    if thresholds is None:
        step = 0.05
        thresholds = torch.arange(0.5, 0.95 + 1e-5, step, dtype=torch.float32)
    recalls = torch.zeros_like(thresholds)
    # compute recall for each iou threshold
    for i, t in enumerate(thresholds):
        recalls[i] = (gt_overlaps >= t).float().sum() / float(num_pos)
    # ar = 2 * np.trapz(recalls, thresholds)
    ar = recalls.mean()
    return {
        "ar": ar,
        "recalls": recalls,
        "thresholds": thresholds,
        "gt_overlaps": gt_overlaps,
        "num_pos": num_pos,
    }


def _evaluate_predictions_on_coco(coco_gt, coco_results, iou_type, kpt_oks_sigmas=None, use_fast_impl=False):
    """
    Evaluate the coco results using COCOEval API.
    """
#    print("_evaluate_predictions_on_coco")
    assert len(coco_results) > 0

    #Insert this code to increase the number of detections possible /Christoffer :

    def summarize_2(self, all_prec=False):
            '''
            Compute and display summary metrics for evaluation results.
            Note this functin can *only* be applied on the default parameter setting
            '''

            print("In method")
            def _summarize(ap=1, iouThr=None, areaRng='all', maxDets=2000):
                p = self.params
                iStr = ' {:<18} {} @[ IoU={:<9} | area={:>6s} | maxDets={:>3d} ] = {:0.3f}'
                titleStr = 'Average Precision' if ap == 1 else 'Average Recall'
                typeStr = '(AP)' if ap == 1 else '(AR)'
                iouStr = '{:0.2f}:{:0.2f}'.format(p.iouThrs[0], p.iouThrs[-1]) \
                    if iouThr is None else '{:0.2f}'.format(iouThr)

                aind = [i for i, aRng in enumerate(p.areaRngLbl) if aRng == areaRng]
                mind = [i for i, mDet in enumerate(p.maxDets) if mDet == maxDets]
                if ap == 1:
                    # dimension of precision: [TxRxKxAxM]
                    s = self.eval['precision']
                    # IoU
                    if iouThr is not None:
                        t = np.where(iouThr == p.iouThrs)[0]
                        s = s[t]
                    s = s[:, :, :, aind, mind]
                else:
                    # dimension of recall: [TxKxAxM]
                    s = self.eval['recall']
                    if iouThr is not None:
                        t = np.where(iouThr == p.iouThrs)[0]
                        s = s[t]
                    s = s[:, :, aind, mind]
                if len(s[s > -1]) == 0:
                    mean_s = -1
                else:
                    mean_s = np.mean(s[s > -1])
                print(iStr.format(titleStr, typeStr, iouStr, areaRng, maxDets, mean_s))
                return mean_s

            def _summarizeDets():

                stats = np.zeros((12,))
                stats[0] = _summarize(1, maxDets=self.params.maxDets[2])
                stats[1] = _summarize(1, iouThr=.5, maxDets=self.params.maxDets[2])
                stats[2] = _summarize(1, iouThr=.75, maxDets=self.params.maxDets[2])
                stats[3] = _summarize(1, areaRng='small', maxDets=self.params.maxDets[2])
                stats[4] = _summarize(1, areaRng='medium', maxDets=self.params.maxDets[2])
                stats[5] = _summarize(1, areaRng='large', maxDets=self.params.maxDets[2])
                stats[6] = _summarize(0, maxDets=self.params.maxDets[0])
                stats[7] = _summarize(0, maxDets=self.params.maxDets[1])
                stats[8] = _summarize(0, maxDets=self.params.maxDets[2])
                stats[9] = _summarize(0, areaRng='small', maxDets=self.params.maxDets[2])
                stats[10] = _summarize(0, areaRng='medium', maxDets=self.params.maxDets[2])
                stats[11] = _summarize(0, areaRng='large', maxDets=self.params.maxDets[2])
                return stats


            def _summarizeKps():
                stats = np.zeros((10,))
                stats[0] = _summarize(1, maxDets=self.params.maxDets[2])
                stats[1] = _summarize(1, maxDets=self.params.maxDets[2], iouThr=.5)
                stats[2] = _summarize(1, maxDets=self.params.maxDets[2], iouThr=.75)
                stats[3] = _summarize(1, maxDets=self.params.maxDets[2], areaRng='medium')
                stats[4] = _summarize(1, maxDets=self.params.maxDets[2], areaRng='large')
                stats[5] = _summarize(0, maxDets=self.params.maxDets[2])
                stats[6] = _summarize(0, maxDets=self.params.maxDets[2], iouThr=.5)
                stats[7] = _summarize(0, maxDets=self.params.maxDets[2], iouThr=.75)
                stats[8] = _summarize(0, maxDets=self.params.maxDets[2], areaRng='medium')
                stats[9] = _summarize(0, maxDets=self.params.maxDets[2], areaRng='large')
                return stats

            if not self.eval:
                raise Exception('Please run accumulate() first')
            iouType = self.params.iouType
            if iouType == 'segm' or iouType == 'bbox':
                summarize = _summarizeDets
            elif iouType == 'keypoints':
                summarize = _summarizeKps
            self.stats = summarize()


    if iou_type == "segm":
        coco_results = copy.deepcopy(coco_results)
        # When evaluating mask AP, if the results contain bbox, cocoapi will
        # use the box area as the area of the instance, instead of the mask area.
        # This leads to a different definition of small/medium/large.
        # We remove the bbox field to let mask AP use mask area.
        # We also replace `score` with `mask_score` when using mask scoring.
        has_mask_scores = "mask_score" in coco_results[0]

        for c in coco_results:
            c.pop("bbox", None)
            if has_mask_scores:
                c["score"] = c["mask_score"]
                del c["mask_score"]

    coco_dt = coco_gt.loadRes(coco_results)
    coco_eval = (COCOeval_opt if _use_fast_impl else COCOeval)(coco_gt, coco_dt, iou_type)
    # Use the COCO default keypoint OKS sigmas unless overrides are specified
    if kpt_oks_sigmas:
        coco_eval.params.kpt_oks_sigmas = np.array(kpt_oks_sigmas)

    if iou_type == "keypoints":
        num_keypoints = len(coco_results[0]["keypoints"]) // 3
        assert len(coco_eval.params.kpt_oks_sigmas) == num_keypoints, (
            "[COCOEvaluator] The length of cfg.TEST.KEYPOINT_OKS_SIGMAS (default: 17) "
            "must be equal to the number of keypoints. However the prediction has {} "
            "keypoints! For more information please refer to "
            "http://cocodataset.org/#keypoints-eval.".format(num_keypoints)
        )

    # coco_eval.params.catIds = [1]
    # coco_eval.params.useCats = 1
    coco_eval.params.maxDets = [100, 500, 2000]

    coco_eval.params.areaRng = [[0 ** 2, 1e5 ** 2], [0 ** 2, 18 ** 2], [18 ** 2, 31 ** 2], [31 ** 2, 1e5 ** 2]]
    coco_eval.params.areaRngLbl = ['all', 'small', 'medium', 'large']

    print(f"Size parameters: {coco_eval.params.areaRng}")

    coco_eval.summarize = types.MethodType(summarize_2, coco_eval)

    coco_eval.evaluate()
    coco_eval.accumulate()
    coco_eval.summarize()

    """
    Added code to produce precision and recall for all iou levels / Chris
    """
    # precisions = coco_eval.eval['precision']
    # recalls = coco_eval.eval['recall']

    # # IoU threshold | instances | Categories | areas | max dets
    # pre_per_iou = [precisions[iou_idx, :, :, 0, -1].mean() for iou_idx in precisions.shape[0]]
    # rec_pre_iou = [recalls[iou_idx, :, :, 0, -1].mean() for iou_idx in recalls.shape[0]]

    # print(f"Precision and Recall per iou: {coco_eval.params.iouThrs}")
    # print(np.round(np.array(pre_per_iou), 4))
    # print(np.round(np.array(rec_pre_iou), 4))

    return coco_eval