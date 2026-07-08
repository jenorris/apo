<?php

namespace App\Mcp\Servers;

use App\Mcp\Tools\SearchNotesTool;
use Laravel\Mcp\Server;
use Laravel\Mcp\Server\Attributes\Instructions;
use Laravel\Mcp\Server\Attributes\Name;
use Laravel\Mcp\Server\Attributes\Version;

#[Name('Apo')]
#[Version('0.1.0')]
#[Instructions(
    'Apo is the gateway to a personal markdown knowledge base (PARA/OKF Obsidian vault). '.
    'Use search_notes to find relevant notes semantically before answering questions about '.
    'the user\'s projects, devices, household, or past decisions, and to avoid creating duplicates. '.
    'Pass exclude globs (e.g. "private/*") to omit employer-mixed material.'
)]
class ApoServer extends Server
{
    protected array $tools = [
        SearchNotesTool::class,
    ];

    protected array $resources = [
        //
    ];

    protected array $prompts = [
        //
    ];
}
