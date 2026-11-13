package main

import (
	"strings"
	"testing"
)

func TestSplitFlags(t *testing.T) {
	for _, key := range []string{"APO_MCP_URL", "APO_SERVER_URL", "APO_MCP_TOKEN", "APO_TOKEN"} {
		t.Setenv(key, "")
	}
	tests := []struct {
		name      string
		args      []string
		wantURL   string
		wantToken string
		wantRest  []string
		wantErr   string
	}{
		{"canonical", []string{"query", "--server-url", "https://x/mcp", "--bearer-token=t"}, "https://x/mcp", "t", []string{"query"}, ""},
		{"deprecated", []string{"--server=https://x/mcp", "--token", "t", "query"}, "https://x/mcp", "t", []string{"query"}, ""},
		{"matching dual", []string{"--server-url", "x", "--server=x", "--bearer-token", "t", "--token=t"}, "x", "t", nil, ""},
		{"conflicting endpoint", []string{"--server-url", "x", "--server", "y"}, "", "", nil, "conflicting endpoint"},
		{"missing endpoint", []string{"--server-url"}, "", "", nil, "requires a value"},
		{"empty token", []string{"--bearer-token="}, "", "", nil, "requires a non-empty value"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, rest, err := splitFlags(tt.args)
			if tt.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("error = %v, want substring %q", err, tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got.serverURL != tt.wantURL || got.token != tt.wantToken {
				t.Fatalf("flags = %#v, want url=%q token=%q", got, tt.wantURL, tt.wantToken)
			}
			if strings.Join(rest, "\x00") != strings.Join(tt.wantRest, "\x00") {
				t.Fatalf("rest = %#v, want %#v", rest, tt.wantRest)
			}
		})
	}
}

func TestSplitFlagsEnvironmentAliases(t *testing.T) {
	tests := []struct {
		name    string
		newURL  string
		oldURL  string
		wantURL string
		wantErr bool
	}{
		{"canonical", "new", "", "new", false},
		{"deprecated", "", "old", "old", false},
		{"matching", "same", "same", "same", false},
		{"conflict", "new", "old", "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("APO_MCP_URL", tt.newURL)
			t.Setenv("APO_SERVER_URL", tt.oldURL)
			t.Setenv("APO_MCP_TOKEN", "")
			t.Setenv("APO_TOKEN", "")
			got, _, err := splitFlags(nil)
			if (err != nil) != tt.wantErr {
				t.Fatalf("error = %v, wantErr=%v", err, tt.wantErr)
			}
			if err == nil && got.serverURL != tt.wantURL {
				t.Fatalf("url = %q, want %q", got.serverURL, tt.wantURL)
			}
		})
	}
}

func TestPartitionRejectsMissingOrMalformedValues(t *testing.T) {
	for _, args := range [][]string{
		{"query", "--limit"},
		{"query", "--limit="},
		{"query", "--folder", "--limit", "2"},
	} {
		if _, _, err := partition(args, map[string]bool{"limit": true, "folder": true}); err == nil {
			t.Fatalf("partition(%#v) unexpectedly succeeded", args)
		}
	}
}
