"""Vault-prefixed path refs for Apo tools (``vault_id:relative/path``).

Desk pointers already use this grammar for citations. Tool path/folder args
accept the same prefix so multi-vault sessions stay unambiguous. A prefix
attempt must name a vault in **this process registry** — never fall through
to a relative path on the default vault.
"""

from __future__ import annotations


class PathRefError(Exception):
    """Stable error for path-prefix resolution (mapped to OpsError in ops)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def looks_like_vault_prefix(raw: str) -> bool:
    """True when ``raw`` should be treated as a vault_id:rel attempt."""
    text = (raw or "").strip()
    if not text or text.startswith(("http://", "https://", "/")):
        return False
    if ":" not in text:
        return False
    left, _, _right = text.partition(":")
    if not left.strip() or "/" in left:
        return False
    return True


def peel_path_ref(raw: str, *, known: set[str] | frozenset[str]) -> tuple[str | None, str]:
    """Split optional ``vault_id:rel`` prefix.

    Returns ``(vault_id, rel)`` when prefixed with a **known** registry id,
    or ``(None, raw_stripped)`` when not a prefix attempt.

    Raises ``PathRefError(bad_vault)`` when the string looks like a vault prefix
    but the id is not in ``known`` (write/read gate for this MCP process).
    """
    text = (raw or "").strip()
    if not looks_like_vault_prefix(text):
        return None, text
    left, _, right = text.partition(":")
    vault_id = left.strip()
    rel = right.lstrip("/")  # allow atlas:/areas/... → areas/...
    if vault_id not in known:
        raise PathRefError(
            "bad_vault",
            f"unknown vault {vault_id!r}; available: {sorted(known)}",
        )
    return vault_id, rel


def merge_vault_arg(
    prefix_vault: str | None,
    vault_arg: str,
    *,
    default: str,
) -> str:
    """Combine path prefix with explicit ``vault=`` / process default.

    Raises ``PathRefError(bad_request)`` on conflict.
    """
    explicit = (vault_arg or "").strip()
    if prefix_vault:
        if explicit and explicit != prefix_vault:
            raise PathRefError(
                "bad_request",
                f"path prefix vault {prefix_vault!r} conflicts with vault={explicit!r}",
            )
        return prefix_vault
    return explicit or default


def qualified_path(vault: str, rel: str) -> str:
    """``vault_id:relative/path`` for agent copy-paste."""
    v = (vault or "").strip()
    r = (rel or "").strip().lstrip("/")
    if not v:
        return r
    if not r:
        return v
    return f"{v}:{r}"
