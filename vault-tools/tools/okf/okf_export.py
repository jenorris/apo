#!/usr/bin/env python3
"""
okf_export.py — Export a portable OKF subset of a vault.

Requires vault OKF contract. Pass --vault / VAULT_ROOT.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import bootstrap, split_vault_args

import okf_common
from okf_common import (
    PARA_ROOTS,
    iter_markdown,
    rel_to_vault,
    set_scalar_in_frontmatter,
    split_frontmatter,
    utc_now,
)

DEFAULT_INCLUDE = list(PARA_ROOTS)


def export_tree(dest: Path, roots: list[str], okf_version: str = "0.1") -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    # Bundle root index
    root_index = dest / "index.md"
    root_index.write_text(
        "\n".join(
            [
                "---",
                f'okf_version: "{okf_version}"',
                f'title: "OKF export"',
                f'description: "Exported from vault at {utc_now()}"',
                f'timestamp: "{utc_now()}"',
                "---",
                "",
                "# OKF export",
                "",
                f"Exported {utc_now()} from vault.",
                "",
                "## Roots",
                "",
                *[
                    f"* [{name}]({name}/)"
                    for name in roots
                    if (okf_common.VAULT_ROOT / name).is_dir()
                ],
                "",
            ]
        ),
        encoding="utf-8",
    )

    for root_name in roots:
        src_root = okf_common.VAULT_ROOT / root_name
        if not src_root.is_dir():
            continue
        for path in iter_markdown(src_root):
            rel = rel_to_vault(path)
            # Skip huge / noisy trees
            if any(
                part in path.parts
                for part in ("agent-client", "node_modules", ".obsidian", "__pycache__")
            ):
                skipped += 1
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scalars, _, has_fm = split_frontmatter(text)
            reserved = path.name in {"index.md", "log.md"}
            if not reserved and not has_fm:
                skipped += 1
                continue
            if not reserved and not (scalars.get("okf_type") or scalars.get("type")):
                skipped += 1
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if reserved:
                shutil.copy2(path, out)
            else:
                # SPEC §11.2 — the exported bundle must carry a non-empty
                # `type`. Vaults whose native field is `okf_type` would
                # otherwise export a non-conformant bundle.
                native = (scalars.get("okf_type") or "").strip()
                if not (scalars.get("type") or "").strip() and native:
                    text = set_scalar_in_frontmatter(text, {"type": native})
                out.write_text(text, encoding="utf-8")
            copied += 1

    # Ship the OKF contract so the exported bundle is self-validating
    # (`okf-lint --vault <export> --profile=okf` works standalone).
    for name in ("okf-contract.schema.yaml", "okf-profile.schema.yaml"):
        src_contract = okf_common.VAULT_ROOT / "system" / "contracts" / name
        if src_contract.is_file():
            out_contract = dest / "system" / "contracts" / name
            out_contract.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_contract, out_contract)
            break

    manifest = dest / "export-manifest.txt"
    manifest.write_text(
        f"exported_at={utc_now()}\ncopied={copied}\nskipped={skipped}\nroots={','.join(roots)}\n",
        encoding="utf-8",
    )
    return {"copied": copied, "skipped": skipped, "dest": str(dest)}


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    root, raw = bootstrap(raw)
    _, raw = split_vault_args(raw)

    parser = argparse.ArgumentParser(description="Export vault OKF subset")
    parser.add_argument(
        "destination",
        nargs="?",
        default=None,
        help="Output directory or .tar.gz path (default: /tmp/meta-okf-export-YYYYMMDD)",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Write a .tar.gz archive (destination may be a .tgz path)",
    )
    parser.add_argument(
        "--roots",
        default=",".join(DEFAULT_INCLUDE),
        help="Comma-separated PARA roots to include",
    )
    parser.add_argument(
        "--okf-version",
        choices=("0.1", "0.2"),
        default="0.1",
        help="okf_version declared in the exported bundle root index.md",
    )
    args = parser.parse_args(raw)

    roots = [r.strip() for r in args.roots.split(",") if r.strip()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if args.destination:
        dest_arg = Path(args.destination)
        if not dest_arg.is_absolute():
            dest_arg = Path.cwd() / dest_arg
    else:
        dest_arg = Path(f"/tmp/okf-export-{stamp}")

    if args.archive:
        archive_path = dest_arg if dest_arg.suffixes else Path(str(dest_arg) + ".tar.gz")
        staging = Path(f"/tmp/meta-okf-export-staging-{stamp}")
        if staging.exists():
            shutil.rmtree(staging)
        stats = export_tree(staging, roots, args.okf_version)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(staging, arcname=archive_path.stem.replace(".tar", ""))
        shutil.rmtree(staging)
        print(f"archive: {archive_path}")
        print(f"copied {stats['copied']} file(s); skipped {stats['skipped']}")
        return 0

    if dest_arg.exists() and dest_arg.is_dir() and any(dest_arg.iterdir()):
        # Write into destination as-is (idempotent overwrite of files)
        pass
    stats = export_tree(dest_arg, roots, args.okf_version)
    print(f"export: {stats['dest']}")
    print(f"copied {stats['copied']} file(s); skipped {stats['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
