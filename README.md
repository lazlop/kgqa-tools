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
- **`summarize_schema(dataset, iterations=10, similarity_threshold=0.3,
  exclude_ontology=False)`** — summarize the dataset's structure into a compact
  `bs:`-namespaced class graph (via bschema), so an agent can see the graph's repeated patterns
  before writing any SPARQL against it. Call this **once** per dataset, right after
  `load_dataset`; the result is cached, so a repeat call is free but won't reflect changes until
  `load_dataset` reloads that name. `similarity_threshold` defaults to a lenient `0.3` (group
  subjects whose 1-hop patterns overlap by at least that much) rather than requiring an exact
  match, since real graphs rarely have perfectly identical instance patterns. Pass `exclude_ontology=True` when the dataset bundles its
  ontology (Brick, 223P, ...) alongside the instance data: classes (`owl:Class`/`rdfs:Class`, or
  a metaclass subclassing one, like `s223:Class`), properties, SHACL shapes and ontology headers
  are dropped before summarizing, along with the blank nodes hanging off them (restrictions,
  property shapes, rules, lists), and then every other subject in the ontology's namespaces
  (e.g. Brick's tags, quantities and substances), so the summary shows the data's patterns rather
  than the ontology's. A namespace counts as the ontology's when most of its subjects are
  ontology terms (or referenced only by them), so the data's own namespace stays even if it holds
  an `owl:Ontology` header, as long as the data doesn't share a namespace with the ontology.
  Instance data typed with those classes is kept, and only the summary is affected (`run_query`
  still sees everything). **This goes beyond strictly removing the ontology:** it also drops
  inferred superclass types from the instance data (`ex:vav1 a brick:Terminal_Unit` when
  `ex:vav1 a brick:VAV` is there too), since a reasoner adds those unevenly and they only add
  noise to a summary. The `rdfs:subClassOf` hierarchy bundled in the dataset is used, plus the
  hierarchy of every ontology shipped in `src/sparql_relax_mcp/ontologies/` (223P and Brick
  1.4.4), since graphs often carry a reasoner's supertypes without the hierarchy itself; a type
  whose subclass link is in neither stays. Drop another `.ttl` into that folder to cover it too. This part is left out of
  the tool's own description and response, so it doesn't distract the agent. The response adds
  `ontology_triples_removed`.

  Called from Python (`sparql_relax_mcp.server.summarize_schema`), it also takes
  `include_member_graph=False`: pass `True` to add `member_graph`, bschema's Turtle graph linking each
  derived class to every real instance it collapsed (`bs:VAV_version_1 rdfs:member ex:vav1, ...`).
  This is left out of the MCP tool. It's as long as the list of the data's subjects, and agents
  turn on whatever optional flag they're shown: the tool used to have `include_member_counts`,
  and in a benchmark run Gemma passed it on ~97% of calls without any sign it helped.

  What counts as "ontology" here rests on assumptions drawn from how Brick and 223P are defined:
  terms are typed as OWL/RDFS classes and properties or SHACL shapes (or with a metaclass, like
  `s223:Class`, declared a subclass of `rdfs:Class`), their supporting structure hangs off them
  as blank nodes, and the ontology lives in its own namespaces, separate from the instance data.
  It was checked against Brick- and 223P-based building graphs (see
  `tests/test_strip_ontology_real.py`). Other ontologies that are defined differently, or data
  that shares a namespace with its ontology, may not be handled correctly, and these rules may be
  updated in the future.
- **`run_query(dataset, query, row_limit=3, connect=False, suggest_fixes=True)`** — the one tool
  for running queries, of any form (`SELECT`, `ASK`, `CONSTRUCT`, `DESCRIBE`). Returns the
  query's results (`variables`/`rows`, `result`, or `triples`, by `form`) *and* diagnoses it in
  the same call. Cheap even when the query already works (`ok: true`); when it doesn't, explains
  which triple pattern or `FILTER` is broken. Returns just `row_limit` rows by default (3 —
  enough to confirm the query returns what's expected without spending context on a full result
  set); pass a higher value (or `null`) once you actually need more, or `0` for the diagnosis
  alone. `row_count` is always the full count. To avoid excessive results, use `LIMIT`/`OFFSET`
  with `row_limit=null`. A query that pages itself with a top-level `OFFSET` is diagnosed without
  its `LIMIT`/`OFFSET`, so a page past the last row comes back `ok: true` and empty instead of
  being blamed on a triple. `row_count` is still that page's own count, and `total_row_count` is
  the count before `LIMIT`/`OFFSET`. For `ASK`/`CONSTRUCT`/`DESCRIBE`, the WHERE body
  is diagnosed as `SELECT * WHERE { ... }` first — so a `false` ASK or an empty CONSTRUCT is
  explained exactly like an empty SELECT — and then the original query is executed; a bare
  `DESCRIBE <uri>` with no WHERE clause is just executed. If the diagnosis itself can't run (e.g.
  a pattern of only all-variable triples like `?s ?p ?o`, or only property paths like
  `?c rdfs:subClassOf+ brick:Point`), the query still runs, the reason is reported in
  `diagnosis_skipped`, and it isn't treated as an error — `ok` is then just whether the query
  returned anything. For each broken triple, `suggest_fixes` (on by default,
  cheap) looks for the single most common cause — the query used the right local name under the
  wrong namespace, or the right namespace with a mis-cased local name — and reports it in that
  culprit's `suggested_fixes` only after actually substituting it in and confirming the rerun
  returns rows; nothing is ever reported as fixed without being verified first. Pass
  `connect=True` to *additionally* search the graph for a real connecting path and propose a
  corrected query for a different class of problem (a genuinely wrong/missing edge, not a
  namespace mismatch) — this part is **experimental**: it's slower, only looks within a fixed set
  of namespaces, and not guaranteed to find or verify a real fix. Most agents get what they need
  from the default (`connect=False`) diagnosis — `suggest_fixes` runs either way — and fix
  anything else themselves from there. Everything runs in a watchdog-guarded worker: a
  pathologically stuck query is hard-killed after 30s and the worker is automatically restarted —
  you'll see this as an error naming the timeout, not a silent hang.

The default `extended` toolset (see [Choosing a toolset](#choosing-a-toolset)) adds one tool for
exploring a graph before or between queries:

- **`search(dataset, text, mode="bm25", kind="any", limit=None, include_predicates=None,
  include_cbd=False, include_cbd_incoming=False)`** — find the URI for a concept
  instead of guessing it. `mode="bm25"` ranks nodes by keyword relevance over their local names
  (split into words, so `supply air temp` matches `Supply_Air_Temperature_Sensor` and `has point`
  matches `hasPoint`), their `rdf:type`s' names, and their string literals (labels, comments,
  definitions, BACnet names, ...). `mode="regex"` matches a Python regex against each node's URI,
  CURIE and string literals, and also returns `total_matches`. `kind` narrows results to
  `class`, `predicate` or `instance`. The index is built once per dataset on first use (a few
  seconds for a ~16MB graph) and rebuilt when `load_dataset` replaces it. Classes are only as
  searchable as what's loaded: a data graph that references `brick:` classes without including
  the Brick ontology has no definitions or hierarchy for them, so load the ontology into the
  same dataset when that matters.

  Three options return more about each hit, for telling candidates apart without a follow-up
  query. They're listed cheapest first and combine freely:

  | Option | Adds | Use it for |
  |---|---|---|
  | `include_predicates=[...]` | `properties`: just those predicates' values | labels, definitions, parents across many hits |
  | `include_cbd=True` | `cbd`: what the hit says about itself | a class's tags, constraints and full definition; an instance's own edges |
  | `include_cbd_incoming=True` | `incoming`: what points at the hit | subclasses, an enumeration kind's members, the shapes that constrain it |

  Since a Brick or 223P class's CBD can be dozens of triples, `limit` defaults to 3 hits
  (instead of 10) whenever `include_cbd` or `include_cbd_incoming` is set; pass `limit`
  explicitly to override. Measured over Brick and 223P, a keyword search returning a CBD per hit
  is ~1–1.5k tokens at 3 hits and ~4–5k at 10.

  `include_predicates` (e.g. `["rdfs:label", "skos:definition", "rdfs:subClassOf"]`) adds a
  `properties` map with each hit's values for just those predicates; every requested predicate
  is listed, empty if the hit has none.

  `include_cbd=True` adds a `cbd`: the hit's
  [concise bounded description](https://www.w3.org/submission/CBD/) — every triple with it as
  subject, plus the same recursively for any blank node objects (so Brick's `sh:rule` tag blocks
  and 223P's property shapes are included) — as Turtle, using the response's `prefixes`. It's
  capped at 200 triples (`cbd_truncated` says when that cut it short).

  `include_cbd_incoming=True` adds an `incoming`: what points *at* each hit. It's kept separate
  from `cbd` so "what this is" and "what references it" stay apart; pass both to get the hit's
  *symmetric* CBD.
  - `incoming.direct` maps each predicate to the named nodes using it on the hit (a class's
    subclasses and instances, `brick:feeds` from upstream equipment, ...), at most 20 each and
    then `"... and N more"` — in a building graph, `rdf:type` on a class can be every instance.
  - `incoming.referenced_in` has one entry per nested structure that references the hit from
    inside blank nodes: a SHACL `sh:property [ sh:path ...; sh:class X ]`, an OWL restriction or
    `owl:AllDisjointClasses` list. Each entry has:
    - `owner`: the named node the structure hangs off (`null` when nothing names it);
    - `turtle`: the structure itself, shown *whole* from its owner down, so the `sh:path` and
      `sh:message` that explain the reference come along. The one exception is a list that
      leads to the hit (an `sh:or`'s alternatives): its blank-node members that don't
      reference the hit are dropped and the list relinked;
    - `pruned` (only when that happened): notes like
      `"sh:or: 1 of 2 members omitted (it doesn't reference the hit)"`.

    Whole entries are added until a 200-triple budget is spent, and `referenced_in_omitted`
    counts any that didn't fit.

**Intended workflow:** `load_dataset`, then `summarize_schema` once to understand the graph's
shape. From there, `run_query` for every query — it's nearly free when the query works, tells you
exactly what's wrong when it doesn't, and raising `row_limit` is all it takes to get the full
results. Leave `connect` off by default; it's there for cases where an automatic suggested fix is
worth the extra cost, not as the first thing to reach for.

## Ontologies

Data graphs usually reference ontology terms without including their definitions, so load the
ontology into the same dataset (`load_dataset` with `path` after downloading, or pass its text as
`data`) when you need class hierarchies, definitions or searchable labels:

- **ASHRAE 223P** (`s223:`): <https://open223.info/223p.ttl>
- **Brick** (`brick:`): <https://brickschema.org/schema/1.4.4/Brick.ttl>

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

`run_query` returns CURIEs, not full URIs, plus the legend that resolves them (diagnosis fields
trimmed here):

```json
{
  "ok": true,
  "form": "solutions",
  "variables": ["zone"],
  "rows": [
    {"zone": {"type": "uri", "value": "s223:zone2"}},
    {"zone": {"type": "uri", "value": "s223:zone1"}}
  ],
  "row_count": 2,
  "prefixes": {"s223": "http://data.ashrae.org/standard223#"},
  "message": "Query's pattern matched 2 row(s) with no issues found."
}
```

On a broken query, `run_query` explains what's broken with the same abbreviated URIs the query itself used — and with
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
back into `run_query`, not reconstructed by hand.

For the single most common kind of broken triple — right local name, wrong namespace (or a
mis-cased local name) — `run_query` doesn't just explain it, it looks in the graph for the term you
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

Pointed at github, do this in two steps the first time. `claude mcp add` gives a stdio server only
~30s to complete its startup handshake (Claude Code's default `MCP_TIMEOUT`), but the *first* run
of the `uvx` command below has to clone the repo and compile the `sparql-relax-rs`/`bschema-rs`
Rust extensions from scratch, which can easily take longer than that -- and shows up as a
"failed to connect"/timeout error that has nothing to do with the URL or your setup. Run the same
command directly once first, so `uv` builds and caches the extensions outside of that timeout:

```sh
uvx --from git+https://github.com/lazlop/kgqa-tools sparql-relax-mcp
```

It talks stdio, so once the build finishes it will just sit there waiting for input -- that means
it's ready. Press Ctrl-C to stop it, then register it (fast now, since the build is cached):

```sh
claude mcp add sparql-relax -- uvx --from git+https://github.com/lazlop/kgqa-tools sparql-relax-mcp
```

Pointed at a local clone:

```sh
claude mcp add sparql-relax -- uv --directory /absolute/path/to/kgqa-tools run sparql-relax-mcp
```

or by hand, in `.mcp.json`, using the GitHub install directly (no local path needed -- run the
priming step above first here too, since Claude Code applies the same startup timeout when it
loads `.mcp.json`):

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

### Choosing a toolset

Every tool's description is sent to the agent on every turn, so each tool costs context. The
server exposes one of two toolsets:

- **`extended`** (default): all five tools — the core four plus `search`.
- **`core`**: just `load_dataset`, `list_datasets`, `summarize_schema` and `run_query`.

See [Why there's a `core` and an `extended` toolset](docs/toolsets.md) for what each is for and
how they compare on a KGQA benchmark.

Pick one with `--toolset` after the command, or with the `SPARQL_RELAX_TOOLSET` environment
variable (the flag wins if both are set). The server's instructions to the agent change to match,
so a `core` agent is never told about tools it doesn't have.

```sh
claude mcp add sparql-relax -- uvx --from git+https://github.com/lazlop/kgqa-tools sparql-relax-mcp --toolset core
# or
claude mcp add sparql-relax -e SPARQL_RELAX_TOOLSET=core -- uvx --from git+https://github.com/lazlop/kgqa-tools sparql-relax-mcp
```

In `.mcp.json`, append `"--toolset", "core"` to `args`.

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

## Deprecated: `traverse`

`traverse(dataset, start, direction="outgoing", predicates=None, max_depth=3, max_nodes=100)` — a
breadth-first walk from a node along chosen predicates, returned as a per-level DAG — is
deprecated. Its code (and tests) stay in `server.py`, but no toolset registers it, so no MCP
client sees it.

It was pulled after reviewing how an agent (Gemma 4, via the `kgqa-agent` BuildingQA benchmark,
two runs × zero/one-shot, 744 questions over four building graphs) actually used it — 257 calls:

- **Mostly a taxonomy lookup that usually had no taxonomy to walk.** 75% of calls followed
  `rdfs:subClassOf` ("is VAV a Terminal_Unit?", "which Temperature_Setpoint subclasses exist?").
  Only one of the four building graphs bundled the Brick class hierarchy; the others had none (or
  42 stray triples), so those walks could only come back empty.
- **Silent empty results that caused loops.** 25% of calls returned nothing, and the message
  ("check the start term (search can find it)") couldn't tell "no such node" from "node exists
  but has no such edges". For a class used in the data but not defined in the loaded ontology
  (`brick:Electric_Meter`), `search` found the term, so the agent re-ran the same failing
  `traverse` — 26 calls were exact repeats within a question.
- **Noisy instance walks.** Every truncated result (24) was an unrestricted walk from an
  instance, filled to the node cap by redundant 223P inverse edges (`cnx`, `connected`,
  `connectedTo`/`From`, ...) and `rdf:type` hops into ontology clutter (`sh:NodeShape`, class
  comments).
- **Nothing it answered that `search` + one query couldn't.** Questions where the agent used it
  scored lower and cost ~50% more tokens (confounded — it was reached for when already stuck —
  but no case showed it answering something the other tools couldn't). A class's ancestors *with*
  their shape are one query (`X rdfs:subClassOf* ?c . ?c rdfs:subClassOf ?p`), and it skipped
  blank nodes, so it couldn't show Brick tags (`sh:rule`) or 223P constraints (`sh:property`) —
  the parts that matter most when picking a class or an `EnumerationKind`.

What replaces it: `search`'s `include_predicates`/`include_cbd` options return a hit's
definition, parents, tags and constraints in the same call, and `run_query` no longer treats a
property-path-only query (which it can't diagnose) as an error. A future ontology tool is more
likely to describe one class fully (definition, parents/children, deprecation and replacement,
tags, SHACL constraints) than to walk the graph.

## Skill: mapping points to 223P and Brick

`skills/building-point-classes/` is a Claude Code skill for a common job these tools support:
turning a point list (BACnet objects, a BMS export, Haystack tags) into 223P or Brick classes.
It contains:
- the workflow: find the standard term, check extension shapes, extend minimally, keep
  provenance, validate against a baseline;
- 223P and Brick reference notes, including a table mapping common BACnet state texts to 223P
  enumeration kinds;
- tested SPARQL templates for browsing the taxonomies;
- a SHACL validation script that diffs results against a baseline model.

### Install

Link the skill into your personal skills folder so it's available in every project:

```bash
ln -s "$PWD/skills/building-point-classes" ~/.claude/skills/building-point-classes
```

To share it with everyone working in one project instead, link or copy it into that project's
`.claude/skills/` folder. Claude Code picks up new skills when a session starts, so start a new
session after installing.

The skill works best with:
- **This MCP server registered** (see [Register with Claude Code](#register-with-claude-code)).
  The skill uses `search` with `include_cbd` and `include_cbd_incoming` to inspect candidate terms, and
  `run_query` for its SPARQL templates. Without the server it falls back to rdflib.
- **The ontologies on disk**: 223P and Brick (see [Ontologies](#ontologies)), plus any
  extension whose shapes your model must satisfy, such as ASHRAE's G36 extension for 223P.
- **For validation, a Python environment with
  [BuildingMOTIF](https://github.com/NatLabRockies/BuildingMOTIF/tree/gtf-buildingmotif)
  (`gtf-buildingmotif` branch) installed.** The validation script then uses the pyshifty
  engine, which took about 20 seconds on a 4,300-triple 223P model. Otherwise it falls back to
  TopQuadrant (`brick-tq-shacl`, needs Java; about 50 seconds) and then pyshacl (too slow for
  223P).

### Use

You don't need to call the skill by name. Claude loads it when you ask for something it covers,
for example:

- "Here's the BACnet export from our VAV controller, with state texts in `states.csv`. Make a
  223P model of the points, using standard enumeration kinds wherever they fit."
- "What Brick classes should these AHU points get? SA-T, CHW-ST, SF-S, SF-C, ZN-T-SP"
- "Our controller has a multistate 'Economizer State' with five states. Model it in 223P with
  whatever extension it needs."

You can also invoke it directly with `/building-point-classes`.

Include the state texts of binary and multi-state objects when you have them (the
inactive/active text, or the `state-text` array). They decide the enumeration kind, and the
skill will ask for them rather than guess. Expect back:
- the model or class choices;
- any extension terms, as Turtle;
- a mapping table with one row per point;
- a report listing what was reused, what was extended, the judgement calls and what wasn't
  modelled.

The validation script can also be run on its own:

```bash
python skills/building-point-classes/scripts/validate.py model.ttl extension.ttl \
    --ontology 223p.ttl --ontology g36.ttl \
    --ontology VOCAB_QUDT-UNITS-ALL.ttl --ontology VOCAB_QUDT-QUANTITY-KINDS-ALL.ttl \
    --baseline previous_model.ttl --focus
```

With `--baseline`, it prints only the results that appeared or disappeared since the previous
model. That's what matters after a change, since 223P reports many advisory warnings on any
partial model. `--engine` forces `pyshifty`, `topquadrant` or `pyshacl`.

If you build models with BuildingMOTIF, use this skill alongside BuildingMOTIF's own agent
skill. That skill runs the build, validate and repair loop. This one decides which class,
enumeration kind and extension terms each point gets, which repair proposals can't do: they
check type, not meaning.
