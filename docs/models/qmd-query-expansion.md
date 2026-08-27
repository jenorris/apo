# Query-expansion model (`APO_QUERY_EXPAND=1`)

`core.expand_query()` uses qmd (github.com/tobi/qmd)'s own fine-tuned model —
`tobil/qmd-query-expansion-1.7B` (Qwen3-1.7B, LoRA SFT'd on `lex:`/`vec:`/`hyde:`
line output) — not a general chat model prompted for JSON. It's small (~1.3GB
Q4_K_M GGUF) and its own SFT training makes it emit plain `lex:`/`vec:`/`hyde:`
lines directly, unlike a general instruct/reasoning model (`qwen3.5:4b` was tried
first — it needed a `format: "json"` + `think: false` workaround and was still less
reliable).

## One-time setup

```sh
ollama pull hf.co/tobil/qmd-query-expansion-1.7B-gguf:Q4_K_M
ollama create apo-query-expand -f docs/models/qmd-query-expansion.Modelfile
```

Or `just setup-query-expand-model` does both. `apo-query-expand` is
`config.QUERY_EXPAND_MODEL`'s default — override with `APO_QUERY_EXPAND_MODEL` to
use a different Ollama model name (e.g. if you build the Modelfile under another
name, or want to fall back to a general chat model).

## The Modelfile

[`qmd-query-expansion.Modelfile`](qmd-query-expansion.Modelfile) is copied verbatim
from qmd's own `finetune/Modelfile` (`FROM` line repointed at the `ollama pull`
above instead of qmd's local build output path). The `TEMPLATE` is load-bearing —
it's what turns a raw query into the exact prompt shape
(`/no_think Expand this search query: {query}`) the model was SFT'd on;
`core.expand_query()` sends only the raw query text, relying on this template to
wrap it.

## Output format and parsing

The model emits plain text, not JSON:

```
lex: how to troubleshoot
lex: what causes search
vec: how to troubleshoot when searches yield no results
vec: what causes search queries to have no matches
hyde: If you encounter problems with search returning no results, first check your logging configuration.
```

Unlike a single JSON object, multiple `lex:`/`vec:` lines are common — each becomes
its own typed sub-query entry. `core._parse_qmd_expansion()` scans every line for a
`lex:`/`vec:`/`hyde:` prefix and ignores everything else, rather than trying to
strip a `<think>...</think>` block — despite `/no_think`, a stray (sometimes
unclosed) `<think>` line was observed in testing; scanning defensively for the typed
prefixes sidesteps that instead of depending on well-formed think-tag output.

## Latency

Measured on this host (2× consumer GPU, shared with other Ollama/ComfyUI
consumers): a cold load of this model took 7–15s, and — notably — a per-request
`keep_alive` (`APO_QUERY_EXPAND_KEEP_ALIVE`, default `5m`) did **not** reliably keep
it warm across back-to-back calls in testing; something else on the GPU (this
host's watcher doing its own embedding calls, or ComfyUI) can evict it between
calls even inside the keep-alive window. `APO_QUERY_EXPAND_TIMEOUT` defaults to
`20`s to comfortably cover a cold load; if your host doesn't share the GPU this
aggressively, you can lower it.
