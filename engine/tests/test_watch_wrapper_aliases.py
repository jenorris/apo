from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _manual_status_run(
    tmp_path: Path,
    *,
    canonical: str = "",
    legacy: str = "",
    canonical_reconcile: str = "",
    legacy_reconcile: str = "",
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "watch.sh"
    script.write_text((ROOT / "watch.sh").read_text(encoding="utf-8"))
    env = os.environ.copy()
    for key in (
        "APO_WATCH_INTERVAL",
        "WATCH_INTERVAL",
        "APO_WATCH_RECONCILE_INTERVAL",
        "WATCH_RECONCILE_INTERVAL",
    ):
        env.pop(key, None)
    for key, value in (
        ("APO_WATCH_INTERVAL", canonical),
        ("WATCH_INTERVAL", legacy),
        ("APO_WATCH_RECONCILE_INTERVAL", canonical_reconcile),
        ("WATCH_RECONCILE_INTERVAL", legacy_reconcile),
    ):
        if value:
            env[key] = value
    env["WATCH_PID_DIR"] = str(tmp_path / "run")
    return subprocess.run(
        ["bash", "-x", str(script), "status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _launchd_run(
    tmp_path: Path,
    *,
    canonical: str = "",
    legacy: str = "",
    canonical_reconcile: str = "",
    legacy_reconcile: str = "",
    with_env_example: bool = False,
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "launchd-watch.sh"
    script.write_text((ROOT / "launchd-watch.sh").read_text(encoding="utf-8"))
    if with_env_example:
        (tmp_path / ".env").write_text(
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    engine = tmp_path / "apo-engine"
    engine.write_text(
        "#!/bin/sh\n"
        'printf "args=%s\\n" "$*"\n'
        'printf "canonical=%s\\n" "${APO_WATCH_INTERVAL-}"\n'
        'printf "legacy=%s\\n" "${WATCH_INTERVAL-}"\n'
        'printf "canonical_reconcile=%s\\n" "${APO_WATCH_RECONCILE_INTERVAL-}"\n'
        'printf "legacy_reconcile=%s\\n" "${WATCH_RECONCILE_INTERVAL-}"\n',
        encoding="utf-8",
    )
    engine.chmod(0o755)
    notes = tmp_path / "notes"
    notes.mkdir()

    env = os.environ.copy()
    for key in (
        "APO_WATCH_INTERVAL",
        "WATCH_INTERVAL",
        "APO_WATCH_RECONCILE_INTERVAL",
        "WATCH_RECONCILE_INTERVAL",
    ):
        env.pop(key, None)
    for key, value in (
        ("APO_WATCH_INTERVAL", canonical),
        ("WATCH_INTERVAL", legacy),
        ("APO_WATCH_RECONCILE_INTERVAL", canonical_reconcile),
        ("WATCH_RECONCILE_INTERVAL", legacy_reconcile),
    ):
        if value:
            env[key] = value
    env.update(
        {
            "APO_ENGINE_BIN": str(engine),
            "APO_NOTES_ROOT": str(notes),
            "APO_INDEX": str(tmp_path / "index.db"),
            "APO_EMBED_BACKEND": "fastembed",
            "WATCH_PID_DIR": str(tmp_path / "run"),
        }
    )
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("canonical", "legacy", "expected_canonical", "expected_legacy"),
    [
        ("17", "", "17", ""),
        ("", "18", "", "18"),
        ("19", "19", "19", "19"),
    ],
)
def test_launchd_interval_aliases_reach_cli_without_real_watcher(
    tmp_path: Path,
    canonical: str,
    legacy: str,
    expected_canonical: str,
    expected_legacy: str,
) -> None:
    result = _launchd_run(tmp_path, canonical=canonical, legacy=legacy)
    assert result.returncode == 0, result.stderr
    assert f"args=watch --interval {canonical or legacy}" in result.stdout
    assert f"canonical={expected_canonical}" in result.stdout
    assert f"legacy={expected_legacy}" in result.stdout


def test_env_example_preserves_legacy_only_values_during_migration(
    tmp_path: Path,
) -> None:
    result = _launchd_run(
        tmp_path,
        legacy="18",
        legacy_reconcile="901",
        with_env_example=True,
    )
    assert result.returncode == 0, result.stderr
    assert "args=watch --interval 18" in result.stdout
    assert "canonical=18" in result.stdout
    assert "legacy=18" in result.stdout
    assert "canonical_reconcile=901" in result.stdout
    assert "legacy_reconcile=901" in result.stdout


@pytest.mark.parametrize(
    ("canonical", "legacy", "expected"),
    [("17", "", "17"), ("", "18", "18"), ("19", "19", "19")],
)
def test_manual_wrapper_resolves_interval_without_starting_watcher(
    tmp_path: Path, canonical: str, legacy: str, expected: str
) -> None:
    result = _manual_status_run(tmp_path, canonical=canonical, legacy=legacy)
    assert result.returncode == 0
    assert "Watcher STOPPED" in result.stdout
    assert f"+ WATCH_INTERVAL_VALUE={expected}" in result.stderr


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        (
            {"canonical": "17", "legacy": "18"},
            ("APO_WATCH_INTERVAL", "WATCH_INTERVAL"),
        ),
        (
            {"canonical_reconcile": "17", "legacy_reconcile": "18"},
            ("APO_WATCH_RECONCILE_INTERVAL", "WATCH_RECONCILE_INTERVAL"),
        ),
    ],
)
def test_launchd_rejects_conflicting_aliases(
    tmp_path: Path, kwargs: dict[str, str], names: tuple[str, str]
) -> None:
    result = _launchd_run(tmp_path, **kwargs)
    assert result.returncode != 0
    assert names[0] in result.stderr
    assert names[1] in result.stderr
    assert "conflict" in result.stderr


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        (
            {"canonical": "17", "legacy": "18"},
            ("APO_WATCH_INTERVAL", "WATCH_INTERVAL"),
        ),
        (
            {"canonical_reconcile": "17", "legacy_reconcile": "18"},
            ("APO_WATCH_RECONCILE_INTERVAL", "WATCH_RECONCILE_INTERVAL"),
        ),
    ],
)
def test_manual_wrapper_rejects_conflicting_aliases_without_starting(
    tmp_path: Path, kwargs: dict[str, str], names: tuple[str, str]
) -> None:
    result = _manual_status_run(tmp_path, **kwargs)
    assert result.returncode != 0
    assert names[0] in result.stderr
    assert names[1] in result.stderr
    assert "conflict" in result.stderr


def test_manual_wrapper_and_example_use_canonical_names() -> None:
    watch = (ROOT / "watch.sh").read_text(encoding="utf-8")
    launchd = (ROOT / "launchd-watch.sh").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for script in (watch, launchd):
        assert 'WATCH_INTERVAL_VALUE="${APO_WATCH_INTERVAL:-${WATCH_INTERVAL:-30}}"' in script
        assert "APO_WATCH_INTERVAL conflicts with deprecated WATCH_INTERVAL" in script
        assert (
            "APO_WATCH_RECONCILE_INTERVAL conflicts with deprecated "
            "WATCH_RECONCILE_INTERVAL"
        ) in script
        assert 'WATCH_INTERVAL="${WATCH_INTERVAL:-30}"' not in script

    assert (
        'APO_WATCH_INTERVAL="${APO_WATCH_INTERVAL:-${WATCH_INTERVAL:-30}}"'
        in example
    )
    assert (
        "APO_WATCH_RECONCILE_INTERVAL="
        '"${APO_WATCH_RECONCILE_INTERVAL:-${WATCH_RECONCILE_INTERVAL:-900}}"'
        in example
    )
