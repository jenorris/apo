package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// version is set at build time via -ldflags "-X main.version=...".
var version = "dev"

// bearerTransport injects an Authorization header on every request, so the
// client can talk to an APO_MCP_AUTH=google-gated instance without the
// engine's own Python code needing to know anything about how the token got
// there.
type bearerTransport struct {
	token string
	base  http.RoundTripper
}

func (t *bearerTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("Authorization", "Bearer "+t.token)
	base := t.base
	if base == nil {
		base = http.DefaultTransport
	}
	return base.RoundTrip(req)
}

// apoSession wraps one connected MCP session against a running apo-engine
// server (streamable-HTTP transport, engine/mcp/server.py APO_MCP_TRANSPORT=http).
type apoSession struct {
	session *mcp.ClientSession
}

func connect(ctx context.Context, serverURL, token string) (*apoSession, error) {
	httpClient := &http.Client{Timeout: 60 * time.Second}
	if token != "" {
		httpClient.Transport = &bearerTransport{token: token}
	}

	transport := &mcp.StreamableClientTransport{
		Endpoint:   serverURL,
		HTTPClient: httpClient,
	}

	client := mcp.NewClient(&mcp.Implementation{
		Name:    "apo-remote",
		Version: version,
	}, nil)

	session, err := client.Connect(ctx, transport, nil)
	if err != nil {
		return nil, fmt.Errorf("connect to %s: %w", serverURL, err)
	}
	return &apoSession{session: session}, nil
}

func (s *apoSession) close() {
	_ = s.session.Close()
}

// listTools returns the tool names + descriptions the server currently
// exposes — used by `apo-remote tools`.
func (s *apoSession) listTools(ctx context.Context) ([]*mcp.Tool, error) {
	var out []*mcp.Tool
	cursor := ""
	for {
		res, err := s.session.ListTools(ctx, &mcp.ListToolsParams{Cursor: cursor})
		if err != nil {
			return nil, err
		}
		out = append(out, res.Tools...)
		if res.NextCursor == "" {
			break
		}
		cursor = res.NextCursor
	}
	return out, nil
}

// callResult is what every subcommand ultimately prints: either the tool's
// structured result (preferred) or its concatenated text content.
type callResult struct {
	raw     any
	isError bool
}

// call invokes one MCP tool by name with args already shaped as a JSON-able
// Go value (typically map[string]any), and normalizes the result down to a
// single JSON-able value regardless of whether the server returned
// StructuredContent or plain TextContent.
func (s *apoSession) call(ctx context.Context, tool string, args any) (*callResult, error) {
	res, err := s.session.CallTool(ctx, &mcp.CallToolParams{
		Name:      tool,
		Arguments: args,
	})
	if err != nil {
		return nil, fmt.Errorf("call %s: %w", tool, err)
	}

	if res.StructuredContent != nil {
		return &callResult{raw: res.StructuredContent, isError: res.IsError}, nil
	}

	// Fall back to concatenated text content, parsed as JSON when possible
	// (apo's MCP tools return JSON-serializable dicts; FastMCP renders that
	// as a single TextContent block when no output schema forces structured
	// content). If it doesn't parse as JSON, hand back the raw string(s).
	var text string
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			text += tc.Text
		}
	}
	var parsed any
	if json.Unmarshal([]byte(text), &parsed) == nil {
		return &callResult{raw: parsed, isError: res.IsError}, nil
	}
	return &callResult{raw: text, isError: res.IsError}, nil
}
