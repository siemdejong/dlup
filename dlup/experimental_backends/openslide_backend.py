# coding=utf-8
# Copyright (c) dlup contributors
from __future__ import annotations

from typing import cast

import numpy as np
import openslide
import PIL.Image

from dlup.experimental_backends.common import AbstractSlideBackend
from dlup.types import PathLike
from dlup.utils.image import check_if_mpp_is_valid


def open_slide(filename: PathLike) -> "OpenSlideSlide":
    """
    Read slide with openslide.

    Parameters
    ----------
    filename : PathLike
        Path to image.
    """
    return OpenSlideSlide(filename)


class OpenSlideSlide(openslide.OpenSlide, AbstractSlideBackend):
    """
    Backend for openslide.
    """

    def __init__(self, filename: PathLike):
        """
        Parameters
        ----------
        filename : PathLike
            Path to image.
        """
        super().__init__(str(filename))

        try:
            mpp_x = float(self.properties[openslide.PROPERTY_NAME_MPP_X])
            mpp_y = float(self.properties[openslide.PROPERTY_NAME_MPP_Y])
            self.spacing = (mpp_x, mpp_y)

        except KeyError:
            pass

    @property
    def spacing(self) -> tuple[float, float] | None:
        if not self._spacings:
            return None
        return self._spacings[0]

    @spacing.setter
    def spacing(self, value: tuple[float, float]) -> None:
        if not isinstance(value, tuple) and len(value) != 2:
            raise ValueError(f"`.spacing` has to be of the form (mpp_x, mpp_y).")

        mpp_x, mpp_y = value
        check_if_mpp_is_valid(mpp_x, mpp_y)
        mpp = np.array([mpp_y, mpp_x])
        self._spacings = [cast(tuple[float, float], tuple(mpp * downsample)) for downsample in self.level_downsamples]

    @property
    def magnification(self) -> int | None:
        """Returns the objective power at which the WSI was sampled."""
        value = self.properties.get(openslide.PROPERTY_NAME_OBJECTIVE_POWER, None)
        if value is not None:
            return int(value)
        return value

    @property
    def vendor(self) -> str:
        """Returns the scanner vendor."""
        return self.properties.properties[openslide.PROPERTY_NAME_VENDOR]

    def get_thumbnail(self, size: int | tuple[int, int]) -> PIL.Image.Image:
        """
        Return a PIL.Image as an RGB image with the thumbnail with maximum size given by size.
        Aspect ratio is preserved.

        Parameters
        ----------
        size : int or tuple[int, int]
            Output size of the thumbnail, will take the maximal value for the output and preserve aspect ratio.

        Returns
        -------
        PIL.Image
            The thumbnail.
        """
        if isinstance(size, int):
            size = (size, size)

        return super().get_thumbnail(size)
