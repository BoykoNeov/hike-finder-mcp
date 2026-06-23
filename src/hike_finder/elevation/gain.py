"""Cumulative elevation gain/loss with noise rejection.

This is THE module that determines whether your numbers are trustworthy.
Naive "sum every positive diff" overcounts badly because DEM/API elevation
data is noisy. We apply two defences:

  1. Optional moving-average smoothing of the elevation series.
  2. A hysteresis threshold: a climb is only counted once it exceeds `threshold`
     metres above the last committed reference point. Small oscillations inside
     the band are treated as noise and ignored, while genuine gradual climbs are
     still fully captured (the reference doesn't advance until the band is
     crossed, so the whole delta is committed in chunks).

Tune `threshold` to match your elevation source: API data (often pre-smoothed)
tolerates ~8-10 m; raw SRTM may want 12-15 m.
"""
from __future__ import annotations


def smooth(elevations: list[float], window: int = 3) -> list[float]:
    """Centered moving average. window=1 disables smoothing."""
    if window <= 1 or len(elevations) <= window:
        return list(elevations)
    half = window // 2
    out: list[float] = []
    for i in range(len(elevations)):
        lo = max(0, i - half)
        hi = min(len(elevations), i + half + 1)
        out.append(sum(elevations[lo:hi]) / (hi - lo))
    return out


def cumulative_gain_loss(
    elevations: list[float],
    threshold_m: float = 10.0,
    smooth_window: int = 3,
) -> tuple[float, float]:
    """Return (gain_m, loss_m) using smoothing + hysteresis thresholding."""
    series = smooth(elevations, smooth_window)
    if len(series) < 2:
        return 0.0, 0.0

    gain = 0.0
    loss = 0.0
    ref = series[0]
    for e in series[1:]:
        diff = e - ref
        if diff >= threshold_m:
            gain += diff
            ref = e
        elif diff <= -threshold_m:
            loss += -diff
            ref = e
        # else: inside the noise band — do not move the reference.
    return gain, loss
