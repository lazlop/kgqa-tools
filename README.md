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
- **`diagnose(dataset, query, connect=False, sample_limit=3)`** — the tool for almost every
  query. Run a SPARQL `SELECT` query and diagnose it. Cheap even when the query already works
  (`ok: true`); when it doesn't, explains which triple pattern or `FILTER` is broken. By default
  also returns up to `sample_limit` rows of the query's own result (`sample_variables`/
  `sample_rows`, free since the full result is already computed to get the row count) — enough
  for most purposes that `query` isn't needed at all. Pass `connect=True` to also search the
  graph for a real connecting path and propose a corrected query — this part is **experimental**:
  it's slower, only looks within a fixed set of namespaces, not guaranteed to find or verify a
  real fix, and doesn't support `sample_limit`. Most agents get what they need from the default
  (`connect=False`) diagnosis and fix the query themselves from there.
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

`sparql-relax-rs` is pulled from [`lazlop/sparql-relax`](https://github.com/lazlop/sparql-relax)
(see `[tool.uv.sources]` in `pyproject.toml`) rather than a local path, so changes to the Rust
core there need to land upstream before `uv sync` here will pick them up.
