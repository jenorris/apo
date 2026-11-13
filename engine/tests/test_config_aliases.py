from __future__ import annotations

import os

import pytest

from apo_engine import config


def test_canonical_runtime_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APO_RUNTIME_COLLECTION", "desk")
    monkeypatch.delenv("APO_COLLECTION", raising=False)
    assert config.env_alias("APO_RUNTIME_COLLECTION", "APO_COLLECTION", "default") == "desk"


def test_matching_runtime_collection_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APO_RUNTIME_COLLECTION", "desk")
    monkeypatch.setenv("APO_COLLECTION", "desk")
    assert config.env_alias("APO_RUNTIME_COLLECTION", "APO_COLLECTION", "default") == "desk"


def test_conflicting_runtime_collection_aliases_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APO_RUNTIME_COLLECTION", "new")
    monkeypatch.setenv("APO_COLLECTION", "old")
    with pytest.raises(ValueError, match="APO_RUNTIME_COLLECTION.*APO_COLLECTION"):
        config.env_alias("APO_RUNTIME_COLLECTION", "APO_COLLECTION", "default")
