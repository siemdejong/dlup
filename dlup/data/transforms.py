# coding=utf-8
# Copyright (c) dlup contributors
# pylint: disable=unsubscriptable-object
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import cv2
import numpy as np
import PIL.Image
import shapely

import dlup.annotations
from dlup._exceptions import AnnotationError

_AnnotationsTypes = dlup.annotations.Point | dlup.annotations.Polygon


def convert_annotations(
    annotations: Iterable[_AnnotationsTypes],
    region_size: tuple[int, int],
    index_map: dict[str, int],
    roi_name: str | None = None,
    default_value: int = 0,
) -> tuple[dict, np.ndarray, np.ndarray | None]:
    """
    Convert the polygon and point annotations as output of a dlup dataset class, where:
    - In case of points the output is dictionary mapping the annotation name to a list of locations.
    - In case of polygons these are converted into a mask according to `index_map`.

    *BE AWARE*: the polygon annotations are processed sequentially and later annotations can overwrite earlier ones.
    This is for instance useful when you would annotate "tumor associated stroma" on top of "stroma".
    The dlup Annotation classes return the polygons with area from large to small.

    When the polygon has holes, the previous written annotation is used to fill the holes.

    TODO
    ----
    - Convert segmentation index map to an Enum
    - Do we need to return PIL images here? If we load a tif mask the mask will be returned as a PIL image, so
      for consistency it might be relevant to do the same here.

    Parameters
    ----------
    annotations
    region_size : tuple[int, int]
    index_map : dict[str, int]
        Map mapping annotation name to index number in the output.
    roi_name : str
        Name of the region-of-interest key.
    default_value : int
        The mask will be initialized with this value.

    Returns
    -------
    dict, np.ndarray, np.ndarray or None
        Dictionary of points, mask and roi_mask.

    """
    mask = np.empty(region_size, dtype=np.int32)
    mask[:] = default_value
    points: dict[str, list] = defaultdict(list)

    roi_mask = np.zeros(region_size, dtype=np.int32)

    for curr_annotation in annotations:
        holes_mask = None
        if isinstance(curr_annotation, dlup.annotations.Point):
            points[curr_annotation.label] += tuple(curr_annotation.coords)
            continue

        if roi_name and curr_annotation.label == roi_name:
            cv2.fillPoly(
                roi_mask,
                [np.asarray(curr_annotation.exterior.coords).round().astype(np.int32)],
                1,
            )
            continue

        if not (curr_annotation.label in index_map):
            continue

        original_values = None
        interiors = [np.asarray(pi.coords).round().astype(np.int32) for pi in curr_annotation.interiors]
        if interiors is not []:
            original_values = mask.copy()
            holes_mask = np.zeros(region_size, dtype=np.int32)
            # Get a mask where the holes are
            cv2.fillPoly(holes_mask, interiors, 1)

        cv2.fillPoly(
            mask,
            [np.asarray(curr_annotation.exterior.coords).round().astype(np.int32)],
            index_map[curr_annotation.label],
        )
        if interiors is not []:
            # TODO: This is a bit hacky to ignore mypy here, but I don't know how to fix it.
            mask = np.where(holes_mask == 1, original_values, mask)  # type: ignore

    return dict(points), mask, roi_mask if roi_name else None


class ConvertAnnotationsToMask:
    """Transform which converts polygons to masks. Will overwrite the annotations key"""

    def __init__(self, *, roi_name: str | None, index_map: dict[str, int], default_value: int = 0):
        """
        Parameters
        ----------
        roi_name : str, optional
            Name of the ROI key.
        index_map : dict
            Dictionary mapping the label to the integer in the output.
        default_value : int
            The mask will be initialized with this value.
        """
        self._roi_name = roi_name
        self._index_map = index_map
        self._default_value = default_value

    def __call__(self, sample):
        if "annotations" not in sample:
            return sample

        _annotations = sample["annotations"]
        points, mask, roi = convert_annotations(
            _annotations,
            sample["image"].size[::-1],
            roi_name=self._roi_name,
            index_map=self._index_map,
            default_value=self._default_value,
        )
        sample["annotation_data"] = {
            "points": points,
            "mask": mask,
        }
        if roi is not None:
            sample["annotation_data"]["roi"] = roi

        return sample


class RenameLabels:
    """Remap the label names"""

    def __init__(self, remap_labels: dict[str, str]):
        """

        Parameters
        ----------
        remap_labels : dict
            Dictionary mapping old name to new name.
        """
        self._remap_labels = remap_labels

    def __call__(self, sample):
        _annotations = sample["annotations"]

        output_annotations = []
        for annotation in _annotations:
            label = annotation.label
            if label not in self._remap_labels:
                output_annotations.append(annotation)
                continue

            if isinstance(annotation, dlup.annotations.Polygon):
                output_annotations.append(dlup.annotations.Polygon(annotation, label=self._remap_labels[label]))

            elif isinstance(annotation, dlup.annotations.Point):
                output_annotations.append(dlup.annotations.Point(annotation, label=self._remap_labels[label]))
            else:
                raise AnnotationError(f"Unsupported annotation type {type(annotation)}")

        sample["annotations"] = output_annotations
        return sample


class MajorityClassToLabel:
    """Transform which the majority class in the annotations to a label.

    The function works as follows:
    - The total area for each label in the sample is computed, the label with the maximum area is determined.
    - The total area *not* covered by the ROI is computed.
    - If the area the roi doesn't cover is larger than the label with the maximum area the image is masked on the ROI.
    - The label is added to the output dictionary in ["labels"]["majority_label"]
    """

    def __init__(self, *, roi_name: str | None, index_map: dict[str, int]):
        """
        Parameters
        ----------
        roi_name : str
            Name of the ROI key.
        index_map : dict
            Dictionary mapping the label to the integer in the output.
        """
        self._roi_name = roi_name
        self._index_map = index_map

    def __call__(self, sample):
        if "annotations" not in sample:
            return sample

        if "labels" not in sample:
            sample["labels"] = {}

        areas = defaultdict(int)
        keys = list(self._index_map.keys())
        if self._roi_name:
            keys.append(self._roi_name)

        for annotation in sample["annotations"]:
            if annotation.label in keys:
                areas[annotation.label] += annotation.area

        tile_area = np.prod(sample["image"].size)
        roi_non_cover = 0.0
        if self._roi_name:
            roi_non_cover = (tile_area - areas[self._roi_name]) / tile_area
            del areas[self._roi_name]

        max_key = max(areas, key=lambda x: areas[x])
        max_proportion = areas[max_key] / tile_area

        if roi_non_cover > max_proportion:
            # In this case we cannot be certain about the label as the non-covering part of the ROI is larger than the
            # majority class.
            # In this case we mask the image.
            _, _, roi = convert_annotations(
                sample["annotations"], sample["image"].size[::-1], roi_name=self._roi_name, index_map={}
            )
            masked_image = np.asarray(sample["image"]) * roi[..., np.newaxis]
            sample["image"] = PIL.Image.fromarray(masked_image.astype(np.uint8), mode=sample["image"].mode)

        sample["labels"].update({"majority_class": self._index_map[max_key]})
        return sample


class ContainsPolygonToLabel:
    """Transform which transforms annotations into a sample-level label whether the label is present above a threshold.

    The area of the label within the ROI (if given) is first computed. If the proportion of this label in the
    image itself is above the threshold, the ["labels"]["has <label>"] is set to True, otherwise False.

    """

    def __init__(self, *, roi_name: str | None, label: str, threshold: float):
        """
        Parameters
        ----------
        roi_name : str
            Name of the ROI key.
        label : str
            Which label to test.
        threshold : float
            Threshold as number between 0 and 1 that denotes when we should consider the label to be present.
        """
        self._roi_name = roi_name
        self._label = label
        self._threshold = threshold

    def __call__(self, sample):
        if "annotations" not in sample:
            return sample

        if "labels" not in sample:
            sample["labels"] = {}

        requested_polygons = [_ for _ in sample["annotations"] if _.label == self._label]

        if self._roi_name:
            roi = shapely.geometry.MultiPolygon([_ for _ in sample["annotations"] if _.label == self._roi_name])
        else:
            roi = shapely.geometry.box(0, 0, *(sample["image"].size[::-1]))

        multi_polygon = shapely.geometry.MultiPolygon(requested_polygons)
        if not multi_polygon.is_valid:
            multi_polygon = shapely.make_valid(multi_polygon)
        label_area = multi_polygon.intersection(roi).area

        proportion = label_area / roi.area
        sample["labels"].update({f"has {self._label}": proportion > self._threshold})
        return sample
