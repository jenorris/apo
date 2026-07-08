<?php

namespace App\Mcp\Tools;

use Illuminate\Contracts\JsonSchema\JsonSchema;
use Illuminate\Support\Facades\Process;
use Illuminate\Support\Str;
use Laravel\Mcp\Request;
use Laravel\Mcp\Response;
use Laravel\Mcp\Server\Attributes\Description;
use Laravel\Mcp\Server\Tool;

#[Description('Semantic search over the personal markdown vault. Returns the most relevant note chunks with their path, heading breadcrumb, and a snippet.')]
class SearchNotesTool extends Tool
{
    public function handle(Request $request): Response
    {
        $v = $request->validate([
            'query' => ['required', 'string'],
            'k' => ['sometimes', 'integer', 'min:1', 'max:50'],
            'exclude' => ['sometimes', 'array'],
            'exclude.*' => ['string'],
        ]);

        $bin = env('APO_ENGINE_BIN', base_path('../engine/.venv/bin/apo-engine'));

        $args = [$bin, 'search', $v['query'], '-k', (string) ($v['k'] ?? 8), '--json'];
        if (! empty($v['exclude'])) {
            $args[] = '--exclude';
            array_push($args, ...$v['exclude']);
        }

        $result = Process::timeout(60)->run($args);
        if (! $result->successful()) {
            return Response::error('Engine search failed: '.trim($result->errorOutput() ?: $result->output()));
        }

        $hits = json_decode(trim($result->output()), true);
        if (! is_array($hits) || $hits === []) {
            return Response::text('No results.');
        }

        $lines = [];
        foreach ($hits as $i => $h) {
            $crumb = ! empty($h['heading']) ? '  ⟩ '.$h['heading'] : '';
            $snippet = (string) Str::of($h['text'] ?? '')->squish()->limit(280);
            $score = number_format((float) ($h['score'] ?? 0), 3);
            $lines[] = ($i + 1).". [{$score}] {$h['path']}{$crumb}\n   {$snippet}";
        }

        return Response::text(implode("\n\n", $lines));
    }

    /**
     * @return array<string, JsonSchema>
     */
    public function schema(JsonSchema $schema): array
    {
        return [
            'query' => $schema->string()
                ->description('Natural-language query over the vault.')
                ->required(),
            'k' => $schema->integer()
                ->description('Number of results to return (default 8).'),
            'exclude' => $schema->array()
                ->description('Optional vault-relative path globs to drop from results, e.g. "private/*".'),
        ];
    }
}
