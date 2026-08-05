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
reads `<vault>/system/contracts/*.schema.yaml` (and bare `.yaml`). Missing
contract → non-zero exit with a clear message.

OKF tools require **`okf-contract`** (file `okf-contract.schema.yaml`).

## Invoke

```bash
# Direct
python3 vault-tools/tools/okf/okf_lint.py --vault ~/Notes/Meta
VAULT_ROOT=~/Notes/Meta python3 vault-tools/tools/okf/okf_lint.py --strict

# vault-tools justfile
just -f vault-tools/justfile okf-lint --vault ~/Notes/Meta

# Vault thin binder (preferred for humans/agents)
just --justfile ~/Notes/Meta/justfile okf lint
# Override toolkit path (worktrees):
APO_VAULT_TOOLS=~/Code/apo-worktrees/vault-tools/vault-tools \
  just --justfile ~/Notes/Meta/justfile okf lint
```

Default `APO_VAULT_TOOLS` (when unset): `~/Code/apo/vault-tools`.

## Agent policy

Do not attach the agent root to `~/Notes/<vault>` for freeform editor writes.
Use Apo MCP for concept notes; use these tools only via trusted `just` recipes.
