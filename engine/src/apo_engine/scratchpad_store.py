"""Ephemeral scratchpad spill under ``~/.apo/scratchpads/<session_id>/``."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Format = Literal["yaml", "json"]
State = Literal["ACTIVE", "STAGED", "PROMOTED"]

DEFAULT_TTL_S = 24 * 60 * 60
_BUFFER_TRUNCATE = 8 * 1024


def scratchpads_root() -> Path:
    override = os.environ.get("APO_SCRATCHPADS_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".apo" / "scratchpads"


@dataclass
class ScratchpadMeta:
    session_id: str
    format: Format
    state: State = "ACTIVE"
    vault: str | None = None
    destination_path: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ttl_s: int = DEFAULT_TTL_S
    promoted_path: str | None = None

    def expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.updated_at) > self.ttl_s

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScratchpadMeta:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def _session_dir(session_id: str) -> Path:
    return scratchpads_root() / session_id


def buffer_filename(fmt: Format) -> str:
    return "buffer.json" if fmt == "json" else "buffer.yaml"


def new_session_id() -> str:
    return str(uuid.uuid4())


def save_session(meta: ScratchpadMeta, content: str) -> None:
    root = _session_dir(meta.session_id)
    root.mkdir(parents=True, exist_ok=True)
    meta.touch()
    buf_path = root / buffer_filename(meta.format)
    meta_path = root / "meta.json"
    meta_text = json.dumps(meta.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp_buf = root / f".{buf_path.name}.tmp"
    tmp_meta = root / ".meta.json.tmp"
    tmp_buf.write_text(content, encoding="utf-8")
    os.replace(tmp_buf, buf_path)
    tmp_meta.write_text(meta_text, encoding="utf-8")
    os.replace(tmp_meta, meta_path)


def load_session(session_id: str) -> tuple[ScratchpadMeta, str] | None:
    root = _session_dir(session_id)
    meta_path = root / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = ScratchpadMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if meta.expired():
        discard_session(session_id)
        return None
    buf = root / buffer_filename(meta.format)
    if not buf.is_file():
        return None
    return meta, buf.read_text(encoding="utf-8")


def discard_session(session_id: str) -> bool:
    root = _session_dir(session_id)
    if not root.is_dir():
        return False
    for child in root.iterdir():
        try:
            child.unlink()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return True


def status_envelope(meta: ScratchpadMeta, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "session_id": meta.session_id,
        "state": meta.state,
        "format": meta.format,
        "vault": meta.vault,
        "destination_path": meta.destination_path,
        "promoted_path": meta.promoted_path,
        "ttl_s": meta.ttl_s,
        "updated_at": meta.updated_at,
    }
    out.update(extra)
    return out


def read_buffer_payload(content: str) -> dict[str, Any]:
    """Return buffer text for read action; truncate large payloads."""
    if len(content.encode("utf-8")) > _BUFFER_TRUNCATE:
        return {
            "buffer": content[:_BUFFER_TRUNCATE],
            "tip": "buffer truncated (>8KiB); patch surgically with set_field",
        }
    return {"buffer": content}
