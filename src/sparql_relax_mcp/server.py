"""MCP server exposing `sparql_relax`'s SPARQL query execution and diagnosis, and
`bschema`'s structural graph summarization, over in-memory RDF graphs, for AI agents.

Intended agent workflow: `load_dataset` once, then `summarize_schema` -- also just
once, it's cached -- to see the graph's repeated structural patterns before writing
any SPARQL against it. From there, `diagnose` is the tool for almost every query: it
confirms the row count, explains *why* a broken query returns nothing or too few rows
-- which triple or FILTER is at fault -- and, since it samples a few rows of the
query's own result for free, usually removes the need to call `query` at all. Reach
for `query` only as a fallback: when a query needs more rows than the sample, or is
after something specific -- a particular room or VAV, say -- that isn't among the
sampled rows and isn't easily pinned down with a FILTER/VALUES clause added to the
query itself. `diagnose`'s `connect=True` option additionally searches the graph's
real edges for a corrected query, but that search is experimental (slower,
namespace-restricted, and not guaranteed to find or verify a real fix) -- most agents
are better served by the default diagnosis and fixing the query themselves from its
explanation.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from bschema_rs import create_bschema
from mcp.server.fastmcp import FastMCP
from rdflib import Graph
from sparql_relax import QueryResult, Store, Term

mcp = FastMCP(
    name="sparql-relax",
    instructions=(
        "Tools for understanding and debugging SPARQL/RDF graphs. Load a graph with "
        "load_dataset, then call summarize_schema ONCE to see the graph's repeated structural "
        "patterns before writing any SPARQL against it -- it's cached, so calling it again is "
        "free but adds nothing new. From there, diagnose is the tool for almost every query -- "
        "ALWAYS call it before trusting a query's result. It's cheap even when the query already "
        "works, explains exactly which triple or FILTER is broken when it doesn't, and by "
        "default also samples a few rows of the query's own result for free -- for most purposes "
        "that sample is enough, and you don't need query at all. Only reach for query as a "
        "fallback: when you need more rows than the sample, or are after something specific (a "
        "particular room or VAV, say) that isn't in the sample and isn't easily pinned down by "
        "adding a FILTER/VALUES clause to the query yourself. diagnose's connect=True option "
        "additionally tries to search the graph for a corrected query, but that search is "
        "experimental and its suggestions should be verified, not trusted outright -- leave "
        "connect off unless you specifically want to try it."
    ),
)


@dataclass
class _Dataset:
    store: Store
    data: str
    format: str
    triple_count: int


_datasets: dict[str, _Dataset] = {}

_schema_summaries: dict[str, dict[str, Any]] = {}
"""Cache of `summarize_schema` results, keyed by dataset name -- computing a bschema
class graph is real work (iterative graph relabeling), and the point of the tool is
to be called once per dataset, so a repeat call should be free rather than
recomputing. Cleared for a name whenever `load_dataset` replaces it."""

_RDFLIB_FORMATS = {
    "turtle": "turtle",
    "ntriples": "nt",
    "nquads": "nquads",
    "rdfxml": "xml",
    "trig": "trig",
}
"""Maps `load_dataset`'s `format` values (sparql_relax/Oxigraph naming) to the format
names rdflib's `Graph.parse` expects, which differ for a couple of these."""


def _require_dataset(name: str) -> Store:
    dataset = _datasets.get(name)
    if dataset is None:
        available = ", ".join(sorted(_datasets)) or "(none loaded)"
        raise ValueError(f"no dataset named {name!r} is loaded. Loaded datasets: {available}. Call load_dataset first.")
    return dataset.store


# ==============================================================================
#  WATCHDOG
# ==============================================================================
#
# `Store.diagnose`/`diagnose_and_connect` run a Rust-side ablation search that,
# for a disconnected BGP, can make Oxigraph's query engine materialize a full
# N x M cross product without ever checking its own cancellation token --
# see sparql-relax-core/src/diagnose.rs's module docs, and eval/run_eval.py's
# own watchdog (which this mirrors) for a measured case that took over 200
# seconds. Because that stuck evaluation runs on rayon's shared global thread
# pool, it doesn't just make one call slow -- it permanently occupies a
# worker thread for the rest of this process's life, since nothing on the
# Python side can force a native thread to stop, and every subsequent
# diagnose call submits more work onto that same, increasingly saturated
# pool.
#
# Unlike eval/run_eval.py -- a batch script where killing a disposable
# per-row worker costs nothing -- this server is a single long-lived process
# holding every dataset an agent has loaded for the whole session in
# `_datasets`. Losing that on every diagnose call the way run_eval.py's
# per-row workers do would be far more disruptive than losing one row's
# result. So diagnose/diagnose_and_connect calls are routed through one
# persistent worker process instead, only replaced -- killed and respawned --
# when `load_dataset` changes what's loaded, or when a call times out;
# datasets are expected to change rarely within a session (often just
# once), so this stays cheap in the common case.
#
# The worker is started with multiprocessing's "spawn" method, not "fork".
# fork was tried first and empirically deadlocks every diagnose call, not
# just pathological ones: this server's stdio transport wraps sys.stdin via
# anyio.wrap_file, which offloads blocking reads to a worker thread in
# anyio's thread pool -- so by the time any tool call handler runs, a
# background thread is essentially always alive, parked mid-syscall waiting
# on the next line of stdin. Forking while that thread exists is exactly the
# hazard CPython's own multiprocessing docs warn about: os.fork() only
# duplicates the calling thread, so the child inherits a frozen copy of
# whatever lock that reader thread happened to be holding (import lock,
# allocator lock, rayon/Oxigraph's global thread-pool init lock, ...) with
# no thread left alive to ever release it -- and the child deadlocks the
# first time anything in it touches that lock. This diagnosis is backed by:
# a bare fork()-plus-Pipe test works fine in isolation; forking after
# building a Store and running a query (touching Oxigraph/rayon directly)
# *also* works fine in isolation; but every diagnose call through the real,
# deployed server -- launched as a subprocess talking real stdio, exactly
# like a real MCP client would -- deadlocks for its full hard timeout, even
# on the most trivial possible query, while this repo's own in-process
# tests (mcp.shared.memory's in-memory transport, no stdio, no background
# reader thread) all pass. The one difference between "deployed server" and
# "in-process test" is exactly that stdio reader thread. Switching to spawn
# makes the deadlock disappear entirely, which starts a brand-new
# interpreter with no inherited threads or locks at all.
#
# The tradeoff: spawn gives the child no copy-on-write access to this
# process's live `_datasets`, so the worker can no longer look a dataset's
# raw text up in that global itself the way it could when forked. Instead,
# `DiagnoseWorker.call()` -- which runs here in the parent, where
# `_datasets` is real -- looks up the entry itself and sends its `data`/
# `.format` explicitly alongside every request; the worker only ever
# re-parses that into its own fresh `Store` the first time it sees a given
# dataset name, cached locally for the rest of its life, exactly as before.
# This also means the worker builds its `Store` from inert Python text, not
# an already-touched native object shared from the parent -- which was the
# original motivation for parsing fresh in the child even back when this
# used fork, and remains true now for a different reason (spawn simply
# can't share the object at all).
#
# This deliberately does *not* also wrap the plain `query` tool: `query`
# doesn't run the automatic ablation search that's the actual mechanism
# behind a genuine Rust-side hang -- a hand-crafted disconnected query
# passed to `query` directly is comparatively rare, and adding worker-
# process overhead to the tool that's supposed to be the cheap, ordinary
# path isn't worth guarding against it.

DIAGNOSE_HARD_TIMEOUT_SECONDS = 30.0
"""Wall-clock cap per diagnose/diagnose_and_connect call, enforced by killing and
replacing the worker process if exceeded. Well above DEFAULT_ABLATION_TIMEOUT's/
DEFAULT_CONNECT_TIMEOUT's own 5-second (soft, Rust-side) budgets -- this is only
meant to catch the rare case where that Rust-side deadline itself isn't honored
(see the module docs above), not to second-guess an ordinary, successful search."""


def _diagnose_worker_loop(conn: "mp.connection.Connection") -> None:
    """Runs in the spawned worker: services one `(dataset_name, data, format,
    method_name, args, kwargs)` request at a time, blocking on `conn.recv()`
    between them. `data`/`format` are sent explicitly by `DiagnoseWorker.call()`
    on every request rather than read from this module's `_datasets` global --
    a spawned process starts a fresh interpreter with no copy-on-write access
    to the parent's memory, so `_datasets` here would just be empty. `data is
    None` signals the caller didn't find that dataset name loaded. Builds its
    own fresh `Store` per dataset name the first time it's asked for, cached
    locally (`local_stores`) for the rest of this worker's life. Exits when
    the parent closes its end of the pipe or sends the `None` shutdown
    sentinel."""
    local_stores: dict[str, Store] = {}
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        dataset_name, data, fmt, method_name, args, kwargs = msg
        try:
            if data is None:
                raise ValueError(f"no dataset named {dataset_name!r} is loaded. Call load_dataset first.")
            if dataset_name not in local_stores:
                local_stores[dataset_name] = Store(data, format=fmt)
            store = local_stores[dataset_name]
            result = getattr(store, method_name)(*args, **kwargs)
            conn.send(("ok", result))
        except Exception as exc:
            conn.send(("error", str(exc)))


class DiagnoseWorker:
    """A persistent spawned worker process plus the hard-timeout watchdog
    around it, for `diagnose`/`diagnose_and_connect` specifically -- see the
    module docs above for why (and why spawn, not fork). `call()` looks like
    a plain function call from the caller's side, but under the hood: look up
    `dataset`'s raw text/format from this module's live `_datasets` (spawn
    gives the worker no way to see that itself), send the request, wait up to
    `hard_timeout` seconds for a reply, and if that expires -- or the worker
    dies outright -- kill whatever's left of it and start a replacement
    before reporting the call as failed. A replacement worker starts with an
    empty local Store cache, so the next call for any dataset re-parses it
    from whatever `_datasets` looks like *now* -- nothing loaded after the
    dead worker was started is lost.

    `worker_loop`/`hard_timeout` are only ever overridden by tests (to inject
    a fast, deterministic stand-in for a real hang rather than waiting on
    one); real callers should just use the defaults.
    """

    def __init__(
        self, worker_loop: Callable[["mp.connection.Connection"], None] = _diagnose_worker_loop, hard_timeout: float = DIAGNOSE_HARD_TIMEOUT_SECONDS
    ) -> None:
        self._worker_loop = worker_loop
        self._hard_timeout = hard_timeout
        self._ctx = mp.get_context("spawn")
        self._conn: Optional["mp.connection.Connection"] = None
        self._proc: Optional[mp.process.BaseProcess] = None
        self._spawn()

    def _spawn(self) -> None:
        parent_conn, child_conn = self._ctx.Pipe()
        proc = self._ctx.Process(target=self._worker_loop, args=(child_conn,), daemon=True)
        proc.start()
        child_conn.close()
        self._conn = parent_conn
        self._proc = proc

    def _kill_and_respawn(self) -> None:
        assert self._proc is not None and self._conn is not None
        try:
            self._proc.kill()
        except Exception:
            pass
        self._proc.join(timeout=5)
        self._conn.close()
        self._spawn()

    def invalidate(self) -> None:
        """Replaces the worker with a fresh one, so its next call re-parses
        whatever `_datasets` looks like right now. Call this whenever
        `load_dataset` loads or replaces a dataset -- otherwise the worker's
        local `Store` cache would keep serving diagnose/diagnose_and_connect
        calls against stale data under that name."""
        self._kill_and_respawn()

    def call(self, dataset: str, method_name: str, *args: Any, **kwargs: Any) -> Any:
        assert self._conn is not None
        entry = _datasets.get(dataset)
        data = entry.data if entry is not None else None
        fmt = entry.format if entry is not None else None
        try:
            self._conn.send((dataset, data, fmt, method_name, args, kwargs))
        except (BrokenPipeError, OSError):
            self._kill_and_respawn()
            raise RuntimeError("diagnose worker died before this call could be sent; it has been restarted")

        if not self._conn.poll(self._hard_timeout):
            self._kill_and_respawn()
            raise RuntimeError(
                f"{method_name} exceeded its {self._hard_timeout:.0f}s hard timeout (likely a disconnected-BGP "
                "combination the query engine got stuck materializing) and was killed; the worker has been restarted"
            )

        try:
            status, payload = self._conn.recv()
        except (EOFError, OSError):
            self._kill_and_respawn()
            raise RuntimeError("diagnose worker died while processing this call; it has been restarted")

        if status == "error":
            raise RuntimeError(payload)
        return payload

    def shutdown(self) -> None:
        if self._conn is None or self._proc is None:
            return
        try:
            self._conn.send(None)
        except Exception:
            pass
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join(timeout=5)
        self._conn.close()


_diagnose_worker: Optional[DiagnoseWorker] = None


def _get_diagnose_worker() -> DiagnoseWorker:
    global _diagnose_worker
    if _diagnose_worker is None:
        _diagnose_worker = DiagnoseWorker()
    return _diagnose_worker


def _invalidate_diagnose_worker() -> None:
    """Called whenever `load_dataset` changes what's loaded. No-op if no worker
    has been created yet (it'll fork fresh from the current `_datasets` on its
    own first use, so there's nothing stale to replace)."""
    if _diagnose_worker is not None:
        _diagnose_worker.invalidate()


def _term_to_json(term: Optional[Term]) -> Optional[dict[str, Any]]:
    if term is None:
        return None
    out: dict[str, Any] = {"type": term.kind, "value": term.value}
    if term.datatype is not None:
        out["datatype"] = term.datatype
    if term.language is not None:
        out["lang"] = term.language
    return out


@mcp.tool()
def load_dataset(name: str, data: Optional[str] = None, path: Optional[str] = None, format: str = "turtle") -> dict[str, Any]:
    """Load RDF data into memory as a named dataset for `diagnose`/`query` to run against.

    Pass exactly one of `data` (the RDF text itself) or `path` (an absolute path to a local RDF
    file to read) -- not both. `format` is one of "turtle" (default), "ntriples", "nquads",
    "rdfxml", or "trig".

    Loading a dataset under a `name` that's already loaded replaces it.
    """
    if (data is None) == (path is None):
        raise ValueError("pass exactly one of `data` or `path`, not both")
    if path is not None:
        data = Path(path).read_text()
    assert data is not None
    store = Store(data, format=format)
    count_result = store.query("SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }")
    triple_count = int(count_result.rows[0][0].value)  # type: ignore[union-attr,index]
    _datasets[name] = _Dataset(store=store, data=data, format=format, triple_count=triple_count)
    _invalidate_diagnose_worker()
    _schema_summaries.pop(name, None)
    return {"name": name, "format": format, "triple_count": triple_count}


@mcp.tool()
def list_datasets() -> list[dict[str, Any]]:
    """List every dataset currently loaded via `load_dataset`, with its format and triple count."""
    return [{"name": name, "format": ds.format, "triple_count": ds.triple_count} for name, ds in sorted(_datasets.items())]


@mcp.tool()
def summarize_schema(dataset: str, iterations: int = 10, similarity_threshold: Optional[float] = 0.3) -> dict[str, Any]:
    """Summarize `dataset`'s structure into a compact class graph (via bschema), so you can see
    its repeated patterns before writing SPARQL against it.

    Call this ONCE per dataset, right after `load_dataset` and before your first `diagnose`/
    `query` call -- knowing the graph's shape up front is what makes it possible to write a
    plausible query on the first try instead of guessing at predicates and class names. The
    result is cached, so calling it again for the same dataset is free but returns the same
    summary; it won't reflect changes until `load_dataset` reloads that name.

    The returned `class_graph` (Turtle) groups subjects that share the same 1-hop structural
    pattern into a single derived `bs:`-namespaced class -- read it the way you'd read a schema,
    not as data to query directly. `compression_pct` (class graph size / original graph size)
    gives a rough sense of how repetitive the data is: a low percentage means most entities
    collapsed into a few patterns and the summary is trustworthy; a percentage close to 100 means
    the data didn't compress much (e.g. it's already schema-like, or every entity is distinct)
    and the summary is less useful.

    `similarity_threshold` (0-1, default 0.3) groups subjects whose patterns overlap above that
    ratio rather than requiring an exact match -- real building/knowledge graphs rarely have
    perfectly identical 1-hop patterns across instances, so a lenient default finds more of the
    graph's repeated structure than exact isomorphism would. Pass `None` to require an exact
    match instead (more classes, each more homogeneous), or a higher ratio for something in
    between.
    """
    cached = _schema_summaries.get(dataset)
    if cached is not None:
        return cached

    ds = _datasets.get(dataset)
    if ds is None:
        available = ", ".join(sorted(_datasets)) or "(none loaded)"
        raise ValueError(f"no dataset named {dataset!r} is loaded. Loaded datasets: {available}. Call load_dataset first.")

    rdflib_format = _RDFLIB_FORMATS[ds.format]
    data_graph = Graph(store="Oxigraph")
    data_graph.parse(data=ds.data, format=rdflib_format)

    class_graph, _member_graph, iterations_run = create_bschema(
        data_graph, iterations=iterations, similarity_threshold=similarity_threshold
    )
    class_graph_text = class_graph.serialize(format="turtle")
    original_size = len(data_graph)
    compression_pct = (len(class_graph) / original_size * 100) if original_size else 0.0

    result = {
        "class_graph": class_graph_text,
        "compression_pct": round(compression_pct, 2),
        "iterations_run": iterations_run,
        "message": (
            f"Compressed {original_size} triples to {len(class_graph)} ({compression_pct:.1f}%) in "
            f"{iterations_run} iteration(s). Use class_graph to understand the graph's structure, "
            "then call diagnose on your queries."
        ),
    }
    _schema_summaries[dataset] = result
    return result


@mcp.tool()
def diagnose(
    dataset: str,
    query: str,
    connect: bool = False,
    ignore_cartesian_risk: bool = True,
    sample_limit: int = 3,
    expand_nonempty_results: bool = False,
) -> dict[str, Any]:
    """Run a SPARQL SELECT query against `dataset` and diagnose it. This is the tool to reach for
    for almost every query -- call it before trusting a query's result, even when you expect it
    to succeed.

    On a working query this is nearly free: it just confirms the row count (`ok: true`) and, since
    `sample_limit > 0` by default, includes a preview of the actual result rows in
    `sample_variables`/`sample_rows` at no extra cost -- for most purposes that preview is enough
    to confirm the query returns what you expect, and you don't need `query` at all. On a query
    that returns nothing, or fewer rows than expected, this explains *why* instead -- which BGP
    triple(s) or FILTER(s) are responsible. If `connect=True`, it also searches the graph's actual
    edges for a real connecting path, often finding a corrected query that actually returns rows
    (see `connected_query` on each culprit).

    Note: Connection is experimental. For AI agents, it is often more effective to use
    diagnose with `connect=False`, then allow the agent to correct the query itself based
    on the diagnosis.

    Only SELECT queries can be diagnosed (ASK/CONSTRUCT/DESCRIBE aren't supported here -- use
    `query` directly for those). Reach for `query` instead of relying on `sample_rows` only when
    you need more rows than `sample_limit`, or you're after something specific -- a particular
    room or VAV, say -- that isn't among the sampled rows and isn't easily pinned down by adding
    a FILTER/VALUES clause to this query yourself.

    `sample_limit` (default 3) caps how many rows of the query's own result are included in
    `sample_variables`/`sample_rows`, shaped like `query`'s own `variables`/`rows` -- free to
    include since the full result is already computed here to get the row count anyway. Pass `0`
    to skip it. Only honored when `connect=False`; `diagnose_and_connect` doesn't support it, so
    `sample_variables`/`sample_rows` are always empty when `connect=True`.

    `expand_nonempty_results` controls whether the (combinatorial, and by far the most expensive
    part of this call) triple/filter search runs at all once the query already returned at least
    one row. Defaults to `False`: the common case is diagnosing a query that returned nothing, so
    once a query is known to already return something, this skips the search entirely and comes
    back immediately with `ok=true` and no culprits -- `row_count`/`sample_rows` are unaffected,
    since they only ever cost the one query run this call always makes anyway. Pass `True` to also
    search a nonempty result for triples/filters that are quietly narrowing it further -- useful
    if you suspect a query is returning fewer rows than it should, not just checking it returned
    anything at all. Only honored when `connect=False`; `diagnose_and_connect` always runs the full
    search regardless, since a caller reaching for `connect` already wants a fix searched for.

    When `connect=True`, path search defaults to predicates in the Brick, ASHRAE 223P, RDFS,
    and QUDT namespaces (this tool's usual building-automation domain) -- a real fix outside
    those namespaces won't be found, though the diagnosis of *which* triple is broken
    is unaffected.

    Some triple combinations would force the query engine to materialize a full N x M cross
    product before yielding a single row if checked -- by default (`ignore_cartesian_risk=True`)
    they're checked anyway, since nothing can force a stuck check to give up early and this is
    usually worth the (small, measured) risk to actually isolate the culprit rather than miss it.
    Pass `ignore_cartesian_risk=False` to skip those combinations instead (reported separately in
    `cartesian_risks_skipped`, not proof either way) if the query is large/untrusted enough that a
    stuck evaluation isn't an acceptable risk here.
    """
    _require_dataset(dataset)  # fail fast with a clear error before involving the watchdog worker at all
    worker = _get_diagnose_worker()
    if connect:
        report = worker.call(dataset, "diagnose_and_connect", query, ignore_cartesian_risk=ignore_cartesian_risk)
        culprits = [
            {
                "depth": result.found_at_depth,
                "triples": [{"triple": t.triple, "discovered_path": t.path_text} for t in result.triples],
                "fixed": result.fixed,
                "connected_query": result.connected_query,
                "row_count_with_fix": result.row_count,
                "fallback_query_with_broken_triples_removed": result.pruned_query,
                "fallback_row_count": result.pruned_row_count,
            }
            for result in report.results
        ]
        filter_issues = [
            {"expression": f.expression, "row_count_without_filter": f.row_count_without_filter} for f in report.filter_results
        ]
        cartesian_risks = report.cartesian_risks
        sample_variables: list[str] = []
        sample_rows: list[dict[str, Any]] = []
    else:
        report = worker.call(
            dataset,
            "diagnose",
            query,
            ignore_cartesian_risk=ignore_cartesian_risk,
            sample_limit=sample_limit,
            expand_nonempty_results=expand_nonempty_results,
        )
        culprits = [
            {
                "depth": c.depth,
                "triples": [{"triple": t, "discovered_path": None} for t in c.triples],
                "fixed": False,
                "connected_query": None,
                "row_count_with_fix": None,
                "fallback_query_with_broken_triples_removed": None,
                "fallback_row_count": None,
            }
            for c in report.culprits
        ]
        filter_issues = [
            {"expression": f.expression, "row_count_without_filter": f.row_count_without_filter} for f in report.filter_culprits
        ]
        cartesian_risks = report.cartesian_risks
        sample_variables = report.sample_variables
        sample_rows = [
            {var: _term_to_json(term) for var, term in zip(sample_variables, row)} for row in report.sample_rows
        ]

    cartesian_risks_skipped = [{"triples": list(r.triples), "depth": r.depth} for r in cartesian_risks]

    ok = report.original_row_count > 0 and not culprits and not filter_issues
    if ok:
        if sample_rows:
            message = (
                f"Query returned {report.original_row_count} row(s) with no issues found. "
                f"sample_rows has {len(sample_rows)} of them for a quick check -- call `query` only if "
                "you need more rows, or a specific value these samples don't include."
            )
        else:
            message = f"Query returned {report.original_row_count} row(s) with no issues found. Call `query` to fetch the results."
    elif culprits or filter_issues:
        if connect:
            message = (
                "Query is broken. See `culprits`/`filter_issues` for what's wrong, and `connected_query` "
                "on any culprit where a fix was found."
            )
        else:
            message = (
                "Query is broken. See `culprits`/`filter_issues` for what's wrong. Call again with "
                "`connect=true` to search for a corrected query."
            )
    elif cartesian_risks_skipped:
        message = (
            "Query returned 0 rows and no broken triple/filter could be isolated, but "
            f"{len(cartesian_risks_skipped)} combination(s) were skipped rather than checked (see "
            "`cartesian_risks_skipped`) because `ignore_cartesian_risk=false` was passed -- the real "
            "culprit may be among them. Call again without `ignore_cartesian_risk=false` (it defaults "
            "to `true`) to force those combinations to actually be checked."
        )
    else:
        message = (
            "Query returned 0 rows and no single broken triple/filter could be isolated -- the "
            "issue may be structural (e.g. two jointly-broken triples beyond the search depth, or "
            "an unbound variable) rather than one clear culprit."
        )

    return {
        "ok": ok,
        "row_count": report.original_row_count,
        "sample_variables": sample_variables,
        "sample_rows": sample_rows,
        "culprits": culprits,
        "filter_issues": filter_issues,
        "cartesian_risks_skipped": cartesian_risks_skipped,
        "message": message,
    }


@mcp.tool()
def query(dataset: str, query: str, row_limit: Optional[int] = 3) -> dict[str, Any]:
    """Run any SPARQL query (SELECT/ASK/CONSTRUCT/DESCRIBE) against `dataset` and return its
    actual results.

    This is a fallback, not the default next step after `diagnose` -- `diagnose`'s own
    `sample_rows` already gives you a free peek at a working SELECT query's results, which is
    enough for most purposes. Reach for `query` instead when you need more rows than the sample,
    when you're after something specific (a particular room or VAV, say) that isn't in the sample
    and isn't easily pinned down by adding a FILTER/VALUES clause to the query yourself, or for
    ASK/CONSTRUCT/DESCRIBE queries, which `diagnose` doesn't support at all. Still call `diagnose`
    first on any new SELECT query -- it's cheap even when the query works, and it catches broken
    queries with an actionable explanation instead of a bare empty result.

    `row_limit` caps how many rows a SELECT/CONSTRUCT/DESCRIBE result may return (default 3 --
    enough to confirm the query returns what you expect without spending context on a full result
    set); has no effect on ASK. Pass a higher value, or `null` for no limit, once you actually need
    more rows than that (e.g. to hand real results back to the user).
    """
    store = _require_dataset(dataset)
    result: QueryResult = store.query(query, row_limit=row_limit)

    if result.form == "boolean":
        return {"form": "boolean", "result": result.boolean}
    if result.form == "solutions":
        return {
            "form": "solutions",
            "variables": result.variables,
            "rows": [{var: _term_to_json(term) for var, term in row.items()} for row in result.bindings],
        }
    return {
        "form": "graph",
        "triples": [
            {"subject": _term_to_json(s), "predicate": _term_to_json(p), "object": _term_to_json(o)}
            for s, p, o in (result.triples or [])
        ],
    }


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        # `daemon=True` already ensures the worker (if any) dies with this
        # process even without this, but shutting it down explicitly first
        # gives it a chance to exit cleanly rather than being SIGKILL'd.
        if _diagnose_worker is not None:
            _diagnose_worker.shutdown()


if __name__ == "__main__":
    main()
