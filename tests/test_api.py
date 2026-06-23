"""Offline tests for ApiElevationProvider.

The bug this guards against: the provider POSTed Open-Elevation's body shape to
OpenTopoData, which 400s every request (confirmed live 2026-06-23). These tests
pin the request body PER ENDPOINT and the shared response parsing, with no
network — requests.post is mocked.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from hike_finder.elevation.api import (
    DEFAULT_ENDPOINT,
    OPEN_ELEVATION_ENDPOINT,
    ApiElevationProvider,
)
from hike_finder.elevation.base import ElevationError


class FakeResp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _otd_payload(elevs):
    return {"results": [{"elevation": e, "location": {"lat": 0, "lng": 0}} for e in elevs]}


def _oe_payload(elevs):
    return {"results": [{"latitude": 0, "longitude": 0, "elevation": e} for e in elevs]}


def test_opentopodata_sends_pipe_string_body():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT)
    assert prov.api_format == "opentopodata"
    with patch("hike_finder.elevation.api.requests.post") as post:
        post.return_value = FakeResp(_otd_payload([100.0, 200.0]))
        out = prov.lookup([(50.73, 15.60), (50.74, 15.61)])
    body = post.call_args.kwargs["json"]
    # OpenTopoData wants ONE "lat,lon|lat,lon" string, not a list of dicts.
    assert body == {"locations": "50.73,15.6|50.74,15.61"}
    assert out == [100.0, 200.0]


def test_open_elevation_sends_dict_list_body():
    prov = ApiElevationProvider(endpoint=OPEN_ELEVATION_ENDPOINT)
    assert prov.api_format == "open-elevation"
    with patch("hike_finder.elevation.api.requests.post") as post:
        post.return_value = FakeResp(_oe_payload([100.0, 200.0]))
        out = prov.lookup([(50.73, 15.60), (50.74, 15.61)])
    body = post.call_args.kwargs["json"]
    assert body == {
        "locations": [
            {"latitude": 50.73, "longitude": 15.60},
            {"latitude": 50.74, "longitude": 15.61},
        ]
    }
    assert out == [100.0, 200.0]


def test_unknown_endpoint_defaults_to_open_elevation_format():
    prov = ApiElevationProvider(endpoint="https://elev.example.com/lookup")
    assert prov.api_format == "open-elevation"


def test_nodata_is_forward_filled_not_fatal():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT)
    with patch("hike_finder.elevation.api.requests.post") as post:
        # interior None -> previous; leading None -> first valid; trailing -> last
        post.return_value = FakeResp(_otd_payload([None, 100.0, None, 300.0, None]))
        out = prov.lookup([(0, 0)] * 5)
    assert out == [100.0, 100.0, 100.0, 300.0, 300.0]


def test_all_nodata_raises():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT)
    with patch("hike_finder.elevation.api.requests.post") as post:
        post.return_value = FakeResp(_otd_payload([None, None]))
        with pytest.raises(ElevationError):
            prov.lookup([(0, 0), (1, 1)])


def test_result_count_mismatch_raises():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT)
    with patch("hike_finder.elevation.api.requests.post") as post:
        post.return_value = FakeResp(_otd_payload([100.0]))  # asked for 2, got 1
        with pytest.raises(ElevationError):
            prov.lookup([(0, 0), (1, 1)])


def test_throttle_spaces_consecutive_requests():
    # The real bug behind the leftover 429s: requests weren't spaced ACROSS
    # batches/routes. With batch_size=1 and a positive interval, every batch
    # after the first must wait. time is mocked so the test is instant.
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT, batch_size=1, min_interval_s=10.0)
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.monotonic", return_value=0.0), \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.return_value = FakeResp(_otd_payload([1.0]))
        prov.lookup([(0, 0), (1, 1), (2, 2)])
    assert post.call_count == 3
    # First request doesn't wait; the next two do (monotonic frozen -> wait>0).
    assert sleep.call_count == 2
    assert all(c.args[0] == 10.0 for c in sleep.call_args_list)


def test_no_throttle_when_interval_zero():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT, batch_size=1, min_interval_s=0)
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.return_value = FakeResp(_otd_payload([1.0]))
        prov.lookup([(0, 0), (1, 1)])
    assert sleep.call_count == 0


# --- retry / backoff -------------------------------------------------------
# min_interval_s=0 in these so the only sleeps are backoff sleeps (the throttle
# is exercised separately above), and time.sleep is mocked so they're instant.


def test_retries_on_429_then_succeeds():
    prov = ApiElevationProvider(
        endpoint=DEFAULT_ENDPOINT, min_interval_s=0, max_retries=3, backoff_base_s=2.0
    )
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.side_effect = [
            FakeResp(None, status_code=429),
            FakeResp(_otd_payload([1.0, 2.0])),
        ]
        out = prov.lookup([(0, 0), (1, 1)])
    assert out == [1.0, 2.0]
    assert post.call_count == 2
    # One backoff between the two attempts: backoff_base * 2**0 = 2.0.
    assert sleep.call_count == 1
    assert sleep.call_args.args[0] == 2.0


def test_retries_on_503_then_succeeds():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT, min_interval_s=0)
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep"):
        post.side_effect = [
            FakeResp(None, status_code=503),
            FakeResp(_otd_payload([1.0])),
        ]
        out = prov.lookup([(0, 0)])
    assert out == [1.0]
    assert post.call_count == 2


def test_honors_retry_after_header():
    prov = ApiElevationProvider(
        endpoint=DEFAULT_ENDPOINT, min_interval_s=0, max_retries=1, backoff_base_s=1.0
    )
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.side_effect = [
            FakeResp(None, status_code=429, headers={"Retry-After": "5"}),
            FakeResp(_otd_payload([1.0])),
        ]
        out = prov.lookup([(0, 0)])
    assert out == [1.0]
    # Retry-After (5 s) wins over the smaller exponential delay (1 s).
    assert sleep.call_args.args[0] == 5.0


def test_large_retry_after_gives_up_instead_of_stalling():
    # A daily-quota 429 carries Retry-After = seconds-until-reset (can be an
    # hour). Honouring it unbounded would freeze the search (and a web request).
    # Above max_backoff_s we stop and degrade to n/a — no multi-minute sleep.
    prov = ApiElevationProvider(
        endpoint=DEFAULT_ENDPOINT, min_interval_s=0, max_retries=3, max_backoff_s=30.0
    )
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.return_value = FakeResp(None, status_code=429, headers={"Retry-After": "3600"})
        with pytest.raises(ElevationError):
            prov.lookup([(0, 0)])
    # Gave up on the first 429 — no extra rejected calls, no hour-long sleep.
    assert post.call_count == 1
    assert sleep.call_count == 0


def test_gives_up_after_max_retries_on_persistent_429():
    prov = ApiElevationProvider(
        endpoint=DEFAULT_ENDPOINT, min_interval_s=0, max_retries=2, backoff_base_s=2.0
    )
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.return_value = FakeResp(None, status_code=429)
        with pytest.raises(ElevationError):
            prov.lookup([(0, 0)])
    # max_retries=2 -> 3 attempts; backoff only BETWEEN attempts (not after last).
    assert post.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [2.0, 4.0]


def test_does_not_retry_on_400():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT, min_interval_s=0)
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep") as sleep:
        post.return_value = FakeResp(None, status_code=400)
        with pytest.raises(ElevationError):
            prov.lookup([(0, 0)])
    # 400 is deterministic: fail immediately, no retry, no backoff.
    assert post.call_count == 1
    assert sleep.call_count == 0


def test_retries_on_network_error_then_succeeds():
    prov = ApiElevationProvider(endpoint=DEFAULT_ENDPOINT, min_interval_s=0)
    with patch("hike_finder.elevation.api.requests.post") as post, \
            patch("hike_finder.elevation.api.time.sleep"):
        post.side_effect = [
            requests.ConnectionError("boom"),
            FakeResp(_otd_payload([1.0])),
        ]
        out = prov.lookup([(0, 0)])
    assert out == [1.0]
    assert post.call_count == 2
