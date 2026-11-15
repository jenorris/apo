from __future__ import annotations

import os
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from apo_engine import config, ops


def test_unset_means_system_local():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("APO_TIMEZONE", None)
        assert config.local_tzinfo() is None


def test_env_names_an_iana_zone():
    with mock.patch.dict(os.environ, {"APO_TIMEZONE": "Asia/Tokyo"}):
        assert config.local_tzinfo() == ZoneInfo("Asia/Tokyo")


def test_unknown_zone_names_the_variable():
    with mock.patch.dict(os.environ, {"APO_TIMEZONE": "Not/AZone"}):
        with pytest.raises(ValueError, match="APO_TIMEZONE"):
            config.local_tzinfo()


def test_history_date_bound_follows_apo_timezone():
    with mock.patch.dict(os.environ, {"APO_TIMEZONE": "UTC"}):
        ts, err = ops._parse_history_bound("2026-01-02", end_of_day=False)
    assert err is None
    assert ts == 1767312000.0  # 2026-01-02T00:00:00Z
    with mock.patch.dict(os.environ, {"APO_TIMEZONE": "Asia/Tokyo"}):
        ts, _ = ops._parse_history_bound("2026-01-02", end_of_day=False)
    assert ts == 1767312000.0 - 9 * 3600
