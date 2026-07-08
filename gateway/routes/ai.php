<?php

use App\Mcp\Servers\ApoServer;
use Laravel\Mcp\Facades\Mcp;

// Local stdio transport — for Claude Code / Cursor:
//   php artisan mcp:start apo
Mcp::local('apo', ApoServer::class);

// OAuth 2.1 discovery + dynamic client registration for remote clients (claude.ai):
//   /.well-known/oauth-authorization-server, /.well-known/oauth-protected-resource, POST /oauth/register
// Backed by Passport (authorize/token endpoints) — answers the memsearch-remote-mcp open question.
Mcp::oauthRoutes();

// Web (streamable-http) transport, protected by Passport bearer auth (scope: mcp:use).
// Front with Caddy (TLS) for claude.ai; the phone is only the UI — calls originate from Anthropic.
Mcp::web('/mcp/apo', ApoServer::class)
    ->middleware('auth:api');
