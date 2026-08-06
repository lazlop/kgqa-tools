# kgqa-tools

MCP ([Model Context Protocol](https://modelcontextprotocol.io)) tooling for AI agents working
with RDF/SPARQL knowledge graphs. Ships an MCP server combining
[`sparql-relax`](https://github.com/lazlop/sparql-relax) (SPARQL query execution and diagnosis)
with [`bschema`](https://github.com/lazlop/bschema) (structural graph summarization), for agents
that need to understand and query a knowledge graph.

## Tools

- **`load_dataset(name, data=None, path=None, format="turtle")`** — load RDF text (or a local
  file) into memory under `name`. Replaces any dataset already loaded under that name.
- **`list_datasets()`** — list loaded datasets with their format and triple count.
- **`summarize_schema(dataset, iterations=10, similarity_threshold=0.3)`** — summarize the
  dataset's structure into a compact `bs:`-namespaced class graph (via bschema), so an agent can
  see the graph's repeated patterns before writing any SPARQL against it. Call this **once** per
  dataset, right after `load_dataset`; the result is cached, so a repeat call is free but won't
  reflect changes until `load_dataset` reloads that name. `similarity_threshold` defaults to a
  lenient `0.3` (group subjects whose 1-hop patterns overlap by at least that much) rather than
  requiring an exact match, since real graphs rarely have perfectly identical instance patterns.
- **`diagnose(dataset, query, connect=False, sample_limit=3, suggest_fixes=True)`** — the tool
  for almost every query. Run a SPARQL `SELECT` query and diagnose it. Cheap even when the query
  already works (`ok: true`); when it doesn't, explains which triple pattern or `FILTER` is
  broken. By default also returns up to `sample_limit` rows of the query's own result
  (`sample_variables`/`sample_rows`, free since the full result is already computed to get the
  row count) — enough for most purposes that `query` isn't needed at all. For each broken triple,
  `suggest_fixes` (on by default, cheap) looks for the single most common cause — the query used
  the right local name under the wrong namespace, or the right namespace with a mis-cased local
  name — and reports it in that culprit's `suggested_fixes` only after actually substituting it
  in and confirming the rerun returns rows; nothing is ever reported as fixed without being
  verified first. Pass `connect=True` to *additionally* search the graph for a real connecting
  path and propose a corrected query for a different class of problem (a genuinely wrong/missing
  edge, not a namespace mismatch) — this part is **experimental**: it's slower, only looks within
  a fixed set of namespaces, not guaranteed to find or verify a real fix, and doesn't support
  `sample_limit`. Most agents get what they need from the default (`connect=False`) diagnosis —
  `suggest_fixes` runs either way — and fix anything else themselves from there.
- **`query(dataset, query, row_limit=3)`** — run any SPARQL query form (`SELECT`, `ASK`,
  `CONSTRUCT`, `DESCRIBE`) and return the actual results. A fallback, not the default next step
  after `diagnose` — reach for it when you need more rows than `diagnose`'s sample, when you're
  after something specific (a particular room or VAV, say) that isn't in the sample and isn't
  easily pinned down with a FILTER/VALUES clause of your own, or for `ASK`/`CONSTRUCT`/`DESCRIBE`,
  which `diagnose` doesn't support at all. Defaults to just 3 rows — enough to confirm the query
  returns what's expected without spending context on a full result set; pass a higher `row_limit`
  (or `null`) once you actually need more.

**Intended workflow:** `load_dataset`, then `summarize_schema` once to understand the graph's
shape. From there, `diagnose` is the tool for almost every query — call it before trusting a
query's result, even when you expect it to succeed; it's nearly free when the query works, tells
you exactly what's wrong when it doesn't, and its `sample_rows` usually make a separate `query`
call unnecessary. Reach for `query` only as a fallback (see above). Leave `connect` off by
default; it's there for cases where an automatic suggested fix is worth the extra cost, not as
the first thing to reach for.

## What the output looks like

Every URI a tool returns is abbreviated to `prefix:local` (e.g. `s223:Zone`) instead of a full
`http://...` URI, using the dataset's own declared `@prefix`/`PREFIX` bindings first and falling
back to a built-in list of common building-automation/semantic-web ontologies for anything the
dataset doesn't declare itself. Any response containing URIs also carries its own `prefixes`
field — exactly the bindings actually used in that response — so nothing is ambiguous even for a
namespace the dataset never declared.

Run `uv run python scripts/demo.py` to see every tool's input and output end to end against a
tiny built-in sample graph, or point it at a real one: `uv run python scripts/demo.py
path/to/graph.ttl`. A few excerpts from a run against a tiny two-`Zone` S223 graph:

`load_dataset` reports the dataset's own declared prefixes:

```json
{
  "name": "demo",
  "format": "turtle",
  "triple_count": 8,
  "declared_prefixes": {
    "s223": "http://data.ashrae.org/standard223#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#"
  }
}
```

`query` returns CURIEs, not full URIs, plus the legend that resolves them:

```json
{
  "form": "solutions",
  "variables": ["zone"],
  "rows": [
    {"zone": {"type": "uri", "value": "s223:zone2"}},
    {"zone": {"type": "uri", "value": "s223:zone1"}}
  ],
  "prefixes": {"s223": "http://data.ashrae.org/standard223#"}
}
```

`diagnose` explains what's broken with the same abbreviated URIs the query itself used — and with
`connect=True`, the suggested fix is still directly runnable even though it's abbreviated, because
it carries its own `PREFIX` lines:

```json
{
  "ok": false,
  "culprits": [
    {
      "triples": [{"triple": "?zone brick:hasPoint ?sensor", "discovered_path": null}],
      "fixed": false,
      "row_count_with_fix": 0,
      "fallback_query_with_broken_triples_removed": "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\nPREFIX s223: <http://data.ashrae.org/standard223#>\nSELECT ?zone ?sensor WHERE { ?zone rdf:type s223:Zone . } LIMIT 50000",
      "fallback_row_count": 2
    }
  ],
  "prefixes": {
    "brick": "https://brickschema.org/schema/Brick#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "s223": "http://data.ashrae.org/standard223#"
  },
  "message": "Query is broken. See `culprits`/`filter_issues` for what's wrong, and `connected_query` on any culprit where a fix was found."
}
```

`fallback_query_with_broken_triples_removed`/`connected_query` are meant to be pasted straight
back into `query`/`diagnose`, not reconstructed by hand.

For the single most common kind of broken triple — right local name, wrong namespace (or a
mis-cased local name) — `diagnose` doesn't just explain it, it looks in the graph for the term you
probably meant and verifies the fix by actually rerunning your query with it substituted in.
Querying an S223 graph (which defines `s223:Zone`) for `rec:Zone` instead:

```json
{
  "ok": false,
  "culprits": [
    {
      "triples": [{"triple": "?zone rdf:type rec:Zone", "discovered_path": null}],
      "suggested_fixes": [
        {
          "kind": "wrong_namespace",
          "original_term": "rec:Zone",
          "replacement_term": "s223:Zone",
          "fixed_query": "PREFIX s223: <http://data.ashrae.org/standard223#>\nPREFIX rec: <https://w3id.org/rec#> SELECT ?zone WHERE { ?zone a s223:Zone }",
          "row_count_with_fix": 2
        }
      ]
    }
  ],
  "message": "Query is broken, but 1 culprit(s) have a verified fix in their own `suggested_fixes` -- each `fixed_query` there was actually rerun and confirmed to return rows."
}
```

The other `kind` of verified fix is `local_name_typo`: a mis-cased local name in an otherwise
correct namespace, tried only when no different-namespace candidate exists at all. Querying b59
(a real ASHRAE 223P building graph, which defines `s223:Zone`) for `s223:zone`:

```json
{
  "kind": "local_name_typo",
  "original_term": "s223:zone",
  "replacement_term": "s223:Zone",
  "fixed_query": "PREFIX s223: <http://data.ashrae.org/standard223#> SELECT ?z WHERE { ?z a s223:Zone . }",
  "row_count_with_fix": 51
}
```

`suggested_fixes` is only ever populated with fixes that were actually verified this way — never
a guess. It's empty whenever no same-local-name candidate exists in the data (as for the
`brick:hasPoint` example above, since that graph doesn't define anything called `hasPoint` under
any namespace) or none of the candidates that do exist actually fix the query. (A third failure
mode — the query used the right predicate but in the wrong direction, e.g. subject/object swapped
— was considered but left out: unlike a namespace swap, correctly relocating a triple pattern
within arbitrary user-authored query text needs real SPARQL rewriting, not a safe string
substitution.)

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and a Rust toolchain (to build the `sparql-relax-rs`
extension the first time — cached by uv/maturin afterwards).

### Option A: run straight from GitHub (no clone)

```sh
uvx --from "git+https://github.com/lazlop/kgqa-tools" sparql-relax-mcp
```

This is the easiest way for collaborators to get the server without checking out the repo. `uv`
clones it, resolves the `sparql-relax-rs` dependency, and builds the extension for you (cached
after the first run).

### Option B: from a local clone

```sh
uv sync
```

> We may publish `sparql-relax-mcp` to PyPI (or ship prebuilt wheels) in the future so this
> doesn't require a local Rust toolchain. For now, installing from GitHub is the supported path.

### Register with Claude Code

Pointed at a local clone:

```sh
claude mcp add sparql-relax -- uv --directory /absolute/path/to/kgqa-tools run sparql-relax-mcp
```

or by hand, in `.mcp.json`, using the GitHub install directly (no local path needed):

```json
{
  "mcpServers": {
    "sparql-relax": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/lazlop/kgqa-tools", "sparql-relax-mcp"]
    }
  }
}
```

or pointed at a local clone instead:

```json
{
  "mcpServers": {
    "sparql-relax": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/kgqa-tools", "run", "sparql-relax-mcp"]
    }
  }
}
```

### Register with Claude Desktop

Add the same block to `claude_desktop_config.json` (Settings → Developer → Edit Config).

### Run directly (for testing)

```sh
uv run sparql-relax-mcp
```

Talks stdio — it will sit waiting for MCP protocol messages on stdin, not print a prompt. Use the
[MCP Inspector](https://modelcontextprotocol.io/legacy/tools/inspector) to poke at it manually:

```sh
npx @modelcontextprotocol/inspector uv run sparql-relax-mcp
```

## Development

```sh
uv sync --group dev
uv run pytest
```

`tests/test_real_buildings.py` additionally exercises the tools against a real building graph
(`BuildingQA/eval_buildings/b59.ttl`, checked out as a sibling of this repo) rather than
`test_server.py`'s tiny synthetic fixture — real graphs have messiness (many declared prefixes, a
dataset-specific namespace, even a known-wrong prefix declaration in b59 itself) a 4-triple graph
can't exercise. It's skipped automatically if that sibling checkout isn't present.

`uv run python scripts/demo.py` (see "What the output looks like" above) is the fastest way to
eyeball a tool's actual input/output after changing `server.py`, without going through a real MCP
client.

`sparql-relax-rs` is pulled from [`lazlop/sparql-relax`](https://github.com/lazlop/sparql-relax)
(see `[tool.uv.sources]` in `pyproject.toml`) rather than a local path, so changes to the Rust
core there need to land upstream before `uv sync` here will pick them up.
