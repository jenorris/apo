"""OKF write-path stamp / validate — vault-contract-driven, optional.

When no contract is configured or found, writes pass through unchanged (engine
stays convention-agnostic). Meta vault ships ``system/contracts/okf-contract.schema.yaml``
(legacy ``system/config/`` and ``okf-profile.schema.yaml`` still accepted).

Layout:

* :mod:`.contract` — contract loading, path rules, glob matching
* :mod:`.frontmatter` — scalar and structured frontmatter views
* :mod:`.model` — :class:`ConceptMeta`, the version-agnostic concept view
* :mod:`.v0_1` / :mod:`.v0_2` — per-version readers
* :mod:`.stamp` — ``process_concept`` / ``validate_concept``

See Meta ``system/config/apo-okf-write-contract.md`` and Apo ``docs/contracts/okf-bundle.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apo_engine.okf import v0_1, v0_2
from apo_engine.okf.contract import (
    _CONTRACT_CACHE,
    _CONTRACT_LOCK,
    _GLOB_RE_CACHE,
    OkfContract,
    PathRule,
    _compile_glob,
    clear_contract_cache,
    enforcement_override,
    generated_policy_override,
    generated_updates,
    get_contract,
    load_contract,
    match_rule,
    path_glob_match,
    resolve_contract_path,
    spec_type_policy_override,
    spec_type_updates,
    utc_now,
)
from apo_engine.okf.frontmatter import (
    _FM_RE,
    _H1_RE,
    _SCALAR_RE,
    _first_h1,
    _has_frontmatter,
    _parse_scalars,
    _set_fields,
    body_of,
    first_h1,
    has_frontmatter,
    parse_mapping,
    parse_scalars,
    set_fields,
    set_structured_fields,
)
from apo_engine.okf.model import (
    Actor,
    Attribution,
    ConceptMeta,
    SourceRef,
    UsageWindow,
    parse_actor,
)
from apo_engine.okf.stamp import (
    OkfResult,
    ValidationReport,
    _effective_enforcement,
    _infer_okf_type,
    process_concept,
    validate_concept,
)

#: Spec versions this engine can read.
SUPPORTED_VERSIONS = (v0_1.VERSION, v0_2.VERSION)

_READERS = {v0_1.VERSION: v0_1, v0_2.VERSION: v0_2}


def detect_version(frontmatter: dict[str, Any], body: str = "") -> str:
    """Best-effort spec version for a single concept.

    v0.2 wins when any v0.2-only key is present; otherwise v0.1. SPEC §12 puts
    the declared ``okf_version`` on the *bundle root* index only, so a concept
    is classified by the shape of its own frontmatter.
    """
    if v0_2.detect(frontmatter, body):
        return v0_2.VERSION
    return v0_1.VERSION


def read_concept(
    content: str,
    rel_path: str = "",
    *,
    version: str | None = None,
) -> ConceptMeta:
    """Parse a note into a :class:`ConceptMeta`, reading both spec versions.

    Both readers always run (v0.2 first, then v0.1 fills what is still empty),
    which is what makes the fallbacks in SPEC §13.1 work: ``generated.at`` else
    ``timestamp``, ``sources`` else a ``# Citations`` body list. Pass
    ``version`` to read strictly as one version.
    """
    fm = parse_mapping(content, rel_path)
    body = body_of(content, rel_path)

    meta = ConceptMeta(rel_path=rel_path)
    meta.detected_version = version or detect_version(fm, body)

    # Core fields are unchanged between versions.
    meta.type = _text(fm.get("type"))
    meta.okf_type = _text(fm.get("okf_type"))
    meta.title = _text(fm.get("title"))
    meta.description = _text(fm.get("description"))
    meta.resource = _text(fm.get("resource"))
    tags = fm.get("tags")
    if isinstance(tags, list):
        meta.tags = [str(t).strip() for t in tags if str(t).strip()]
    elif isinstance(tags, str) and tags.strip():
        meta.tags = [t.strip() for t in tags.split(",") if t.strip()]

    if version is None:
        v0_2.parse(fm, body, meta=meta)
        v0_1.parse(fm, body, meta=meta)
    else:
        reader = _READERS.get(version)
        if reader is None:
            raise ValueError(f"unsupported OKF version: {version!r}")
        reader.parse(fm, body, meta=meta)

    return meta


def _text(val: Any) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    return text or None


# Back-compat aliases (pre-contracts rename)
resolve_profile_path = resolve_contract_path
load_profile = load_contract
get_profile = get_contract
clear_profile_cache = clear_contract_cache
OkfProfile = OkfContract

__all__ = [
    "Actor",
    "Attribution",
    "ConceptMeta",
    "OkfContract",
    "OkfProfile",
    "OkfResult",
    "PathRule",
    "SUPPORTED_VERSIONS",
    "SourceRef",
    "UsageWindow",
    "ValidationReport",
    "body_of",
    "clear_contract_cache",
    "clear_profile_cache",
    "detect_version",
    "enforcement_override",
    "first_h1",
    "generated_policy_override",
    "generated_updates",
    "get_contract",
    "get_profile",
    "has_frontmatter",
    "load_contract",
    "load_profile",
    "match_rule",
    "parse_actor",
    "parse_mapping",
    "parse_scalars",
    "path_glob_match",
    "process_concept",
    "read_concept",
    "resolve_contract_path",
    "resolve_profile_path",
    "set_fields",
    "set_structured_fields",
    "spec_type_policy_override",
    "spec_type_updates",
    "utc_now",
    "v0_1",
    "v0_2",
    "validate_concept",
]
