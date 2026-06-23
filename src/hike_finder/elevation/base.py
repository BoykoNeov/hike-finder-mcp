"""Elevation provider interface.

Both backends (API and local DEM) implement this, so the rest of the pipeline
doesn't care where elevations come from. You can also chain them (see
elevation/__init__.py:get_provider) to try local first and fall back to the API.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

Coord = tuple[float, float]


class ElevationProvider(ABC):
    @abstractmethod
    def lookup(self, points: list[Coord]) -> list[float]:
        """Return elevation in metres for each (lat, lon) point, same order.

        Implementations should batch internally and must preserve order and
        length. Raise ElevationError on unrecoverable failure so a fallback
        provider can take over.
        """
        raise NotImplementedError


class ElevationError(RuntimeError):
    pass
