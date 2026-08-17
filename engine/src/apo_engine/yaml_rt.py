"""Round-trip YAML — comment- and formatting-preserving load/dump (yq-v4 semantics).

Every YAML write site in the engine (standalone ``.yaml`` catalog notes, Markdown
frontmatter fences, YAML patch ops) routes through here so an edit to one key does
not reflow or strip the rest of the document.

Semantics:

- Comments on an *unrelated* key are byte-identical after editing a different key.
- Comments on an *edited* key survive: the value is replaced and the trailing
  comment is re-emitted at its original column.
- Deleting a key drops its line, its inline comment, and any comment block that
  *follows* it — in ruamel's model a comment belongs to the key above it, so the
  block between the deleted key and the next one goes with the deletion. A block
  comment written *above* the deleted key belongs to the preceding key and stays.
- Blank lines, quoting style and flow style are preserved as loaded. Block
  indentation is sniffed from the source (:func:`_sniff_indent`) because ruamel
  emits with a fixed indent config rather than the document's own.

Cross-loader safety: the rest of the engine still reads frontmatter with PyYAML
(YAML 1.1). ruamel emits YAML 1.2, where ``yes``/``no``/``on``/``off`` are plain
strings — emitting those unquoted would make PyYAML read them back as booleans.
:class:`_CompatRepresenter` quotes any string PyYAML's resolver would not resolve
as a string, which also keeps invalid-timestamp strings (``2017-00-00``) quoted.

PyYAML stays as the fallback: when ruamel refuses a document (or produces a value
the catalog cannot carry, e.g. an unknown ``!tag``), :func:`load` returns ``None``
and callers keep their existing PyYAML path.
"""

from __future__ import annotations

import datetime as _dt
import io
import re
from typing import Any

import yaml as _pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.representer import RoundTripRepresenter

__all__ = [
    "CommentedMap",
    "load",
    "dump",
    "set_field_at_path",
    "delete_field_at_path",
]

# Match the historical PyYAML emitter settings so freshly built (non-round-tripped)
# mappings keep producing the same bytes they always have.
_WIDTH = 1000

# PyYAML's block defaults: nested maps indent 2, sequence dashes flush with the key.
_DEFAULT_INDENT = (2, 2, 0)  # (mapping, sequence, offset)

# Where a sniffed layout is stashed on the loaded mapping so dump() can restore it.
_INDENT_ATTR = "_apo_rt_indent"

_STR_TAG = "tag:yaml.org,2002:str"

_PLAIN_TYPES = (
    str,
    int,
    float,
    bool,
    type(None),
    _dt.date,
    _dt.datetime,
    _dt.time,
)


def _tolerant_timestamp(constructor: Any, node: Any) -> Any:
    """Load invalid YAML 1.1 timestamps (``2017-00-00``) as plain scalars.

    Mirrors ``note_format._construct_yaml_timestamp`` for ruamel's round-trip
    constructor — without it a note with ``effective_date: 2017-00-00`` fails to
    parse instead of round-tripping as a string.
    """
    try:
        return RoundTripConstructor.construct_yaml_timestamp(constructor, node)
    except (ValueError, OverflowError, TypeError):
        return constructor.construct_scalar(node)


class _TolerantRTConstructor(RoundTripConstructor):
    """RoundTripConstructor that tolerates invalid YAML 1.1 timestamp scalars."""


# add_constructor copies the class-level table onto the subclass, so this does not
# leak into ruamel's global RoundTripConstructor.
_TolerantRTConstructor.add_constructor(
    "tag:yaml.org,2002:timestamp",
    _tolerant_timestamp,
)


def _pyyaml_would_retag(value: str) -> bool:
    """True when PyYAML (YAML 1.1) would resolve this plain scalar as a non-string."""
    try:
        resolved = _pyyaml.resolver.Resolver().resolve(
            _pyyaml.nodes.ScalarNode, value, (True, False)
        )
    except Exception:  # pragma: no cover - the resolver is total in practice
        return True
    return resolved != _STR_TAG


def _represent_compat_str(representer: Any, data: str) -> Any:
    if _pyyaml_would_retag(data):
        return representer.represent_scalar(_STR_TAG, data, style="'")
    return RoundTripRepresenter.represent_str(representer, data)


def _represent_compat_none(representer: Any, data: Any) -> Any:
    """Emit ``null``, not an empty value — ruamel's rt default writes ``key:``.

    Every ``null`` already in the vault was written by PyYAML, so spelling it the
    same way keeps unrelated keys byte-identical through an edit.
    """
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


class _CompatRepresenter(RoundTripRepresenter):
    """RoundTripRepresenter that keeps plain strings unambiguous for PyYAML readers."""


# Registered for ``str`` only — ruamel's ScalarString subclasses (preserved quotes,
# literal/folded blocks) keep their own representers and are untouched.
_CompatRepresenter.add_representer(str, _represent_compat_str)
_CompatRepresenter.add_representer(type(None), _represent_compat_none)


def _yaml(indent: tuple[int, int, int] = _DEFAULT_INDENT) -> YAML:
    """A fresh round-trip YAML instance (these are stateful, so never shared)."""
    mapping, sequence, offset = indent
    y = YAML(typ="rt")
    y.Constructor = _TolerantRTConstructor
    y.Representer = _CompatRepresenter
    y.preserve_quotes = True
    y.width = _WIDTH
    y.allow_unicode = True
    y.default_flow_style = False
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    return y


_KEY_RE = re.compile(r"^(\s*)(?:[^\s#-]|-\S)[^:#]*:(\s|$)")
_DASH_RE = re.compile(r"^(\s*)-(\s|$)")


def _sniff_indent(text: str) -> tuple[int, int, int]:
    """Guess the document's block layout as ``(mapping, sequence, offset)``.

    ruamel emits with a fixed indent configuration, so without this an edit to one
    key silently re-indents every block sequence in the file (``  - a`` → ``- a``).
    Only the first nested mapping and the first block sequence are sampled; a
    document mixing styles keeps whichever it uses first, and anything implausible
    falls back to the PyYAML-compatible default.
    """
    mapping, sequence, offset = _DEFAULT_INDENT
    got_mapping = False
    got_sequence = False
    prev_key_indent: int | None = None

    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        dash = _DASH_RE.match(raw)
        if dash is not None:
            if not got_sequence and prev_key_indent is not None:
                cand = len(dash.group(1)) - prev_key_indent
                if 0 <= cand <= 6:
                    offset = cand
                    sequence = cand + 2
                    got_sequence = True
            continue
        key = _KEY_RE.match(raw)
        if key is None:
            continue
        indent = len(key.group(1))
        if not got_mapping and prev_key_indent is not None and indent > prev_key_indent:
            cand = indent - prev_key_indent
            if 1 <= cand <= 8:
                mapping = cand
                got_mapping = True
        prev_key_indent = indent
        if got_mapping and got_sequence:
            break

    if sequence <= offset:  # ruamel requires dash offset < sequence indent
        return _DEFAULT_INDENT
    return mapping, sequence, offset


def _indent_of(data: Any) -> tuple[int, int, int]:
    value = getattr(data, _INDENT_ATTR, None)
    if isinstance(value, tuple) and len(value) == 3:
        return value
    return _DEFAULT_INDENT


def _plain_safe(node: Any, depth: int = 0) -> bool:
    """Reject documents carrying values the catalog cannot represent.

    ruamel keeps unknown ``!tag`` scalars as ``TaggedScalar`` and sets as
    ``CommentedSet``; PyYAML's SafeLoader refuses those outright. Returning
    ``False`` routes such a document to the PyYAML fallback so behavior is
    unchanged from before this module existed.
    """
    if depth > 64:
        return False
    if isinstance(node, dict):
        return all(
            isinstance(k, _PLAIN_TYPES) and _plain_safe(v, depth + 1)
            for k, v in node.items()
        )
    if isinstance(node, list):
        return all(_plain_safe(v, depth + 1) for v in node)
    return isinstance(node, _PLAIN_TYPES)


def load(text: str | None) -> CommentedMap | None:
    """Round-trip parse a YAML mapping. Non-mapping / unparseable → ``None``.

    ``None`` means "caller should fall back to PyYAML" — it is never an assertion
    that the document is invalid.
    """
    if not text or not text.strip():
        return None
    try:
        data = _yaml().load(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not _plain_safe(data):
        return None
    try:
        setattr(data, _INDENT_ATTR, _sniff_indent(text))
    except Exception:  # pragma: no cover - CommentedMap accepts attributes
        pass
    return data


def dump(data: Any) -> str:
    """Emit a mapping as YAML text (trailing newline). ``None``/empty → ``{}``."""
    if data is None or (isinstance(data, dict) and not data):
        return "{}\n"
    buf = io.StringIO()
    try:
        _yaml(_indent_of(data)).dump(data, buf)
        body = buf.getvalue()
    except Exception:
        body = _pyyaml.safe_dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=_WIDTH,
        )
    if not body.endswith("\n"):
        body += "\n"
    return body


def set_field_at_path(data: Any, field: str, value: Any) -> None:
    """``fm_path.set_at_path`` against a round-trip mapping (comments survive)."""
    from apo_engine.fm_path import set_at_path

    set_at_path(data, field, value)


def delete_field_at_path(data: Any, field: str) -> None:
    """``fm_path.delete_at_path`` against a round-trip mapping.

    The deleted key takes its own comments with it (inline, plus any block that
    follows it) — see the module docstring.
    """
    from apo_engine.fm_path import delete_at_path

    delete_at_path(data, field)
