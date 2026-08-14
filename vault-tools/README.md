# Apo vault-tools

Contract-gated batch mutators for Apo vaults. **Invoke from outside the vault**
so Cursor agents do not use a vault root as their edit cwd (mutate chokepoint).

## Layout

```
vault-tools/
  lib/           # vault_env + preflight
  tools/okf/     # OKF lint / fix / linkify / export (pilot)
  justfile
```

## Requirements

Each tool declares `requires_contracts` in `tools/<id>/tool.yaml`. Preflight
discovers contracts the same way Apo does:

- Preferred: `<vault>/system/contracts/*.yaml`
- Legacy: `<vault>/system/config/*-contract.schema.yaml` and `okf-profile.schema.yaml`

OKF tools require an OKF contract (`okf-contract` or legacy `okf-profile`).

CLI path arguments must resolve **under** `VAULT_ROOT` (`..` / absolute escapes → exit 2).

## Invoke

```bash
# Direct
python3 vault-tools/tools/okf/okf_lint.py --vault ~/Notes/Atlas
VAULT_ROOT=~/Notes/Atlas python3 vault-tools/tools/okf/okf_lint.py --strict

# vault-tools justfile
just -f vault-tools/justfile okf-lint --vault ~/Notes/Atlas

# Vault thin binder (preferred for humans/agents)
just --justfile ~/Notes/Atlas/justfile okf lint
# Override toolkit path (worktrees):
APO_VAULT_TOOLS=~/Code/apo-worktrees/vault-tools/vault-tools \
  just --justfile ~/Notes/Atlas/justfile okf lint
```

Default `APO_VAULT_TOOLS` (when unset): `~/Code/apo/vault-tools`.

## Agent policy

Do not attach the agent root to `~/Notes/<vault>` for freeform editor writes.
Use Apo MCP for concept notes; use these tools only via trusted `just` recipes.
