from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apo_engine import config

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    ("canonical", "deprecated"),
    [
        ("APO_YAML_MAX_CHARS", "APO_MAX_CHARS"),
        ("APO_YAML_OVERLAP", "APO_OVERLAP"),
        ("APO_OKF_CONTRACT", "APO_OKF_PROFILE"),
        ("APO_WATCH_INTERVAL", "WATCH_INTERVAL"),
        ("APO_WATCH_RECONCILE_INTERVAL", "WATCH_RECONCILE_INTERVAL"),
    ],
)
def test_config_aliases_accept_matching_values_and_reject_conflicts(
    canonical: str, deprecated: str
) -> None:
    env = os.environ.copy()
    env.pop(canonical, None)
    env.pop(deprecated, None)
    env[canonical] = "17"
    env[deprecated] = "17"
    matched = subprocess.run(
        [sys.executable, "-c", "import apo_engine.config"],
        cwd=_ENGINE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert matched.returncode == 0, matched.stderr

    env[deprecated] = "18"
    conflicted = subprocess.run(
        [sys.executable, "-c", "import apo_engine.config"],
        cwd=_ENGINE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert conflicted.returncode != 0
    assert canonical in conflicted.stderr
    assert deprecated in conflicted.stderr
