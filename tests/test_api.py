"""Offline tests for ApiElevationProvider.

The bug this guards against: the provider POSTed Open-Elevation's body shape to
OpenTopoData, which 400s every request (confirmed live 2026-06-23). These tests
pin the request body PER ENDPOINT and the shared response parsing, with no
network — requests.post is mocked.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from hike_finder.elevation.api import (
    DEFAULT_ENDPOINT,
    OPEN_ELEVATION_ENDPOINT,
    ApiElevationProvider,
)
from hike_finder.elevation.base import ElevationError


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

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
