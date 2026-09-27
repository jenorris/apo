// Command apo-remote is a thin MCP client for a running apo-engine server
// (streamable-HTTP transport, engine/mcp/server.py APO_MCP_TRANSPORT=http).
//
// It has no local vault access and no engine dependencies (duckdb,
// sqlite-vec, watchdog, ...) — every command is a single MCP tool call over
// the wire. For the same commands run in-process against a local vault with
// no server required, see the apo-local console script (apo_engine.cli_ops,
// Python, part of the apo-engine package) — or the unified `apo` entry
// point's bare note verbs, which call the same backend.
//
// Named apo-remote, not apo: naming-consolidation pass (0.29) gave the
// Python-side unified CLI the `apo` console script name (apo_engine.cli_apo,
// dispatching in-process to the apo-engine/apo-local backends). This binary
// had reserved `apo` for itself since 0.27.0 but has essentially no
// adoption yet, so it gives the name up rather than the other way around.
//
// Any tool the server exposes can be reached via `apo-remote call <tool>
// --args '<json>'` — the generic path is the source of truth, so this
// binary never goes stale when the server adds or changes a tool's schema.
// `read` and `search` exist as convenience wrappers around the two most
// common, stable-shaped calls.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

const defaultServerURL = "http://127.0.0.1:8878/mcp"

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "apo-remote: "+err.Error())
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		usage()
		return nil
	}

	cmd, rest := args[0], args[1:]
	if cmd == "-h" || cmd == "--help" || cmd == "help" {
		usage()
		return nil
	}
	if cmd == "-V" || cmd == "--version" || cmd == "version" {
		fmt.Println("apo-remote " + version)
		return nil
	}

	switch cmd {
	case "tools":
		return cmdTools(rest)
	case "call":
		return cmdCall(rest)
	case "search":
		return cmdSearch(rest)
	case "read":
		return cmdRead(rest)
	case "append":
		return cmdAppend(rest)
	default:
		return fmt.Errorf("unknown command %q — run `apo-remote help`", cmd)
	}
}

func usage() {
	fmt.Print(`apo-remote — thin MCP client for a running apo-engine server

Usage:
  apo-remote tools                                  list tools the server exposes
  apo-remote call <tool> [--args '<json>']          call any tool by name (reads stdin if --args omitted)
  apo-remote search <query> [--folder F] [--vault V] [--limit N]
  apo-remote read <path> [--heading H]
  cmd | apo-remote append <path> [--heading H] [--chunk-hash C] [--vault V] [--position start|end] [--create]
                                              pipe stdout straight into a note (append_note; errors if the
                                              note doesn't exist unless --create is given)

Global flags (any subcommand):
  --server URL   apo-engine MCP endpoint (default $APO_SERVER_URL or ` + defaultServerURL + `)
  --token TOK    bearer token for APO_MCP_AUTH=google instances (default $APO_TOKEN)

Every MCP tool the server exposes is reachable via 'call', regardless of
whether this binary has a dedicated subcommand for it — e.g.:
  apo-remote call write_note --args '{"path":"areas/threads/x.md","content":"..."}'
`)
}

// serverFlags are accepted by every subcommand, ahead of or after its own
// flags — parsed manually so subcommands can still take positional args
// freely (stdlib flag.FlagSet requires flags before positionals).
type serverFlags struct {
	serverURL string
	token     string
}

// splitFlags pulls --server/--token out of args (in any position) and
// returns them plus the remaining args for the subcommand's own parsing.
func splitFlags(args []string) (serverFlags, []string) {
	sf := serverFlags{
		serverURL: envOr("APO_SERVER_URL", defaultServerURL),
		token:     os.Getenv("APO_TOKEN"),
	}
	var rest []string
	for i := 0; i < len(args); i++ {
		head, val, hasEq := splitEq(args[i])
		switch head {
		case "--server":
			if hasEq {
				sf.serverURL = val
			} else if i+1 < len(args) {
				sf.serverURL = args[i+1]
				i++
			}
		case "--token":
			if hasEq {
				sf.token = val
			} else if i+1 < len(args) {
				sf.token = args[i+1]
				i++
			}
		default:
			rest = append(rest, args[i])
		}
	}
	return sf, rest
}

// splitEq splits a "--name=value" token into ("--name", "value", true); any
// other token (including a bare "--name" with no "=") comes back unchanged
// as (tok, "", false). Lets every flag accept either "--name value" or
// "--name=value" — the latter is the form an agent unfamiliar with this
// binary's specific parsing is more likely to reach for by default.
func splitEq(tok string) (head, value string, hasEq bool) {
	if i := strings.Index(tok, "="); i >= 0 && strings.HasPrefix(tok, "--") {
		return tok[:i], tok[i+1:], true
	}
	return tok, "", false
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func cmdTools(args []string) error {
	sf, _ := splitFlags(args)
	ctx := context.Background()
	sess, err := connect(ctx, sf.serverURL, sf.token)
	if err != nil {
		return err
	}
	defer sess.close()

	tools, err := sess.listTools(ctx)
	if err != nil {
		return err
	}
	for _, t := range tools {
		desc := strings.SplitN(t.Description, "\n", 2)[0]
		fmt.Printf("%-18s %s\n", t.Name, desc)
	}
	return nil
}

func cmdCall(args []string) error {
	sf, rest := splitFlags(args)
	positionals, flags := partition(rest, map[string]bool{"args": true})
	if len(positionals) < 1 {
		return fmt.Errorf("usage: apo-remote call <tool> [--args '<json>']")
	}
	tool := positionals[0]

	raw := flags["args"]
	if raw == "" {
		raw = "{}"
		if stat, _ := os.Stdin.Stat(); stat != nil && (stat.Mode()&os.ModeCharDevice) == 0 {
			b, err := io.ReadAll(os.Stdin)
			if err != nil {
				return fmt.Errorf("read stdin: %w", err)
			}
			if s := strings.TrimSpace(string(b)); s != "" {
				raw = s
			}
		}
	}
	var toolArgs map[string]any
	if err := json.Unmarshal([]byte(raw), &toolArgs); err != nil {
		return fmt.Errorf("--args is not a JSON object: %w", err)
	}

	return callAndPrint(sf, tool, toolArgs)
}

func cmdSearch(args []string) error {
	sf, rest := splitFlags(args)
	positionals, flags := partition(rest, map[string]bool{"folder": true, "vault": true, "limit": true})
	if len(positionals) < 1 {
		return fmt.Errorf("usage: apo-remote search <query> [--folder F] [--vault V] [--limit N]")
	}
	query := strings.Join(positionals, " ")

	toolArgs := map[string]any{"query": query}
	if v := flags["folder"]; v != "" {
		toolArgs["folder"] = v
	}
	if v := flags["vault"]; v != "" {
		toolArgs["vault"] = v
	}
	if v := flags["limit"]; v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return fmt.Errorf("--limit must be an integer: %w", err)
		}
		toolArgs["limit"] = n
	}
	return callAndPrint(sf, "search_notes", toolArgs)
}

func cmdRead(args []string) error {
	sf, rest := splitFlags(args)
	positionals, flags := partition(rest, map[string]bool{"heading": true})
	if len(positionals) < 1 {
		return fmt.Errorf("usage: apo-remote read <path> [--heading H]")
	}
	toolArgs := map[string]any{"path": positionals[0]}
	if v := flags["heading"]; v != "" {
		toolArgs["heading"] = v
	}
	return callAndPrint(sf, "read_note", toolArgs)
}

// cmdAppend wraps append_note for the common "pipe a command's output into a
// note" case. Unlike cmdCall (JSON args from stdin), stdin here is the note
// text itself — no JSON envelope, so `some-command | apo-remote append path.md`
// works with zero ceremony. create defaults to false, matching append_note's
// own default: this never conjures a new note out of a typo'd path unless
// asked to.
func cmdAppend(args []string) error {
	sf, rest := splitFlags(args)
	create, rest := hasBoolFlag(rest, "create")
	positionals, flags := partition(rest, map[string]bool{"heading": true, "chunk-hash": true, "vault": true, "position": true})
	if len(positionals) < 1 {
		return fmt.Errorf("usage: cmd | apo-remote append <path> [--heading H] [--chunk-hash C] [--vault V] [--position start|end] [--create]")
	}
	path := positionals[0]

	stat, _ := os.Stdin.Stat()
	if stat == nil || (stat.Mode()&os.ModeCharDevice) != 0 {
		return fmt.Errorf("apo-remote append reads note text from stdin — pipe something in, e.g. `echo hi | apo-remote append %s`", path)
	}
	b, err := io.ReadAll(os.Stdin)
	if err != nil {
		return fmt.Errorf("read stdin: %w", err)
	}
	text := string(b)
	if strings.TrimSpace(text) == "" {
		return fmt.Errorf("stdin was empty — nothing to append")
	}

	toolArgs := map[string]any{"path": path, "text": text}
	if v := flags["heading"]; v != "" {
		toolArgs["heading"] = v
	}
	if v := flags["chunk-hash"]; v != "" {
		toolArgs["chunk_hash"] = v
	}
	if v := flags["vault"]; v != "" {
		toolArgs["vault"] = v
	}
	if v := flags["position"]; v != "" {
		toolArgs["position"] = v
	}
	if create {
		toolArgs["create"] = true
	}
	return callAndPrint(sf, "append_note", toolArgs)
}

// hasBoolFlag reports whether a bare "--name" flag (no value) is present
// anywhere in args, returning the args with every occurrence removed. For
// flags like --create that are either present or not — never "--name value".
func hasBoolFlag(args []string, name string) (bool, []string) {
	target := "--" + name
	found := false
	rest := make([]string, 0, len(args))
	for _, a := range args {
		if a == target {
			found = true
			continue
		}
		rest = append(rest, a)
	}
	return found, rest
}

// partition splits args into positionals and flag values, tolerating any
// interleaving of the two (unlike stdlib flag.FlagSet, which stops parsing
// flags at the first positional) and both "--name value" and "--name=value"
// forms (see splitEq). valueFlags names which "--name" flags consume a
// value; every other "--"-prefixed token or bare token is treated as
// positional.
func partition(args []string, valueFlags map[string]bool) (positionals []string, flags map[string]string) {
	flags = map[string]string{}
	for i := 0; i < len(args); i++ {
		a := args[i]
		head, val, hasEq := splitEq(a)
		if name, ok := strings.CutPrefix(head, "--"); ok && valueFlags[name] {
			if hasEq {
				flags[name] = val
				continue
			}
			if i+1 < len(args) {
				flags[name] = args[i+1]
				i++
				continue
			}
		}
		positionals = append(positionals, a)
	}
	return positionals, flags
}

func callAndPrint(sf serverFlags, tool string, toolArgs map[string]any) error {
	ctx := context.Background()
	sess, err := connect(ctx, sf.serverURL, sf.token)
	if err != nil {
		return err
	}
	defer sess.close()

	result, err := sess.call(ctx, tool, toolArgs)
	if err != nil {
		return err
	}

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(result.raw); err != nil {
		return fmt.Errorf("encode result: %w", err)
	}
	if result.isError {
		os.Exit(1)
	}
	return nil
}
