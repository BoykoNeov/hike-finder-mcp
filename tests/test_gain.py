from hike_finder.elevation.gain import cumulative_gain_loss, smooth


def test_pure_noise_rejected():
    # Flat trail at 1000 m with +/-5 m jitter => 10 m peak-to-peak.
    # KEY PRINCIPLE: threshold must exceed PEAK-TO-PEAK noise, not half of it.
    # A 10 m threshold sits exactly on the boundary and would count the sawtooth;
    # 12 m correctly rejects it. (Smoothing also helps; disabled here to isolate.)
    elev = [1000 + (5 if i % 2 else -5) for i in range(200)]
    gain, loss = cumulative_gain_loss(elev, threshold_m=12.0, smooth_window=1)
    assert gain < 5
    assert loss < 5


def test_monotonic_climb_captured():
    # Steady climb 0 -> 100 in 1 m steps. Real gain ~100 m.
    elev = [float(i) for i in range(101)]
    gain, loss = cumulative_gain_loss(elev, threshold_m=10.0, smooth_window=1)
    assert 90 <= gain <= 100
    assert loss == 0.0


def test_up_then_down_symmetric():
    elev = [float(i) for i in range(101)] + [float(i) for i in range(99, -1, -1)]
    gain, loss = cumulative_gain_loss(elev, threshold_m=10.0, smooth_window=1)
    assert 90 <= gain <= 100
    assert 90 <= loss <= 100


def test_naive_would_overcount_but_threshold_does_not():
    # Climb of 50 m real, buried under heavy +/-8 m sawtooth noise.
    base = [i * 0.5 for i in range(100)]  # 0 -> 49.5 gradual
    noisy = [b + (8 if i % 2 else -8) for i, b in enumerate(base)]
    gain, _ = cumulative_gain_loss(noisy, threshold_m=12.0, smooth_window=5)
    # True gain ~50 m; naive sum-of-positive-diffs would report hundreds.
    assert 35 <= gain <= 75


def test_smooth_reduces_variance():
    elev = [1000 + (10 if i % 2 else -10) for i in range(50)]
    s = smooth(elev, window=5)
    assert max(s) - min(s) < max(elev) - min(elev)
