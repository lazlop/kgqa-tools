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

For the single most common broken-triple cause -- right local name, wrong namespace,
or a mis-cased local name -- `diagnose` doesn't just explain it: by default it also
looks for another URI in the graph with the same local name, substitutes it in, and
reruns, reporting the result in that culprit's `suggested_fixes` only once verified to
actually return rows. See `_suggest_fixes_for_culprit`. This is unrelated to and much
cheaper than `connect`, and runs regardless of it.

Every URI any tool returns is abbreviated to `prefix:local` (e.g. `s223:Zone`) rather
than a full URI, using the dataset's own declared prefixes plus common defaults --
see `DEFAULT_PREFIXES`/`_dataset_prefixes` -- so results read the way an agent
actually writes SPARQL and don't burn context on repeated namespace strings.
"""

from __future__ import annotations

import multiprocessing as mp
import re
from dataclasses import dataclass, field
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
        "connect off unless you specifically want to try it. Separately, and by default, diagnose "
        "also checks each broken triple for the single most common mistake -- right local name, "
        "wrong namespace, or a mis-cased local name -- and reports a verified fix (query rerun and "
        "confirmed to return rows) in that culprit's suggested_fixes when one exists; this is "
        "unrelated to and much cheaper than connect. Every URI any tool returns is abbreviated to "
        "prefix:local (e.g. s223:Zone) using the dataset's declared prefixes plus common defaults "
        "-- each response's own `prefixes` field lists exactly which bindings were used."
    ),
)


@dataclass
class _Dataset:
    store: Store
    data: str
    format: str
    triple_count: int
    prefixes: dict[str, str] = field(default_factory=dict)
    """Namespace prefix -> URI, used to render URIs as CURIEs (`prefix:local`) in
    every tool's output instead of raw `http://...` strings, which are harder for an
    agent to read and to match back against a query it just wrote. Combines this
    dataset's own `@prefix`/`PREFIX` declarations (extracted from its source text,
    which take priority since they're what a query against it would actually use)
    with `DEFAULT_PREFIXES` as a fallback for common ontologies the source doesn't
    declare itself. See `_uri_to_curie`/`_abbreviate_sparql_text`."""


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


# ==============================================================================
#  PREFIXES / CURIEs
# ==============================================================================
#
# `Store` (Oxigraph via sparql_relax's Rust bindings) has no concept of
# namespaces -- it only ever hands back bare URI strings. Left alone, every
# tool's output would be full of `http://data.ashrae.org/standard223#Zone`
# instead of `s223:Zone`, which is harder for an agent to read, harder to
# visually match back against the prefixed form it just wrote in its own
# query, and burns extra context for no benefit. Everything below exists to
# turn full URIs back into the CURIEs an agent actually thinks and writes
# queries in, using the dataset's own declared prefixes (most accurate --
# it's what a query against that exact data would use) plus a fallback list
# of common building/semantic-web ontologies for datasets that don't declare
# their own (e.g. n-triples has no prefixes at all).

DEFAULT_PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "sh": "http://www.w3.org/ns/shacl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sosa": "http://www.w3.org/ns/sosa/",
    "prov": "http://www.w3.org/ns/prov#",
    "dcterms": "http://purl.org/dc/terms/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "vcard": "http://www.w3.org/2006/vcard/ns#",
    "sdo": "http://schema.org/",
    "quantitykind": "http://qudt.org/vocab/quantitykind/",
    "qudt": "http://qudt.org/schema/qudt/",
    "unit": "http://qudt.org/vocab/unit/",
    "brick": "https://brickschema.org/schema/Brick#",
    "ref": "https://brickschema.org/schema/Brick/ref#",
    "tag": "https://brickschema.org/schema/BrickTag#",
    "bsh": "https://brickschema.org/schema/BrickShape#",
    "rec": "https://w3id.org/rec#",
    "s223": "http://data.ashrae.org/standard223#",
    "bob": "http://data.ashrae.org/standard223/si-builder#",
    "bacnet": "http://data.ashrae.org/bacnet/2020#",
    "g36": "http://data.ashrae.org/standard223/1.0/extensions/g36#",
    "s4bldg": "https://saref.etsi.org/saref4bldg#",
    "s4ener": "https://saref.etsi.org/saref4ener#",
    "saref": "https://saref.etsi.org/core#",
    "bs": "urn:bschema#",
}
"""Fallback prefix bindings for common building-automation/semantic-web ontologies,
used to abbreviate URIs from namespaces a dataset doesn't declare a prefix for
itself. A dataset's own declared prefixes (see `_extract_declared_prefixes`) always
take priority over these when both bind a URI in the same namespace."""

_PREFIX_DECL_RE = re.compile(r"@prefix\s+([\w.-]*):\s*<([^>]+)>\s*\.", re.IGNORECASE)
_PREFIX_SPARQL_RE = re.compile(r"PREFIX\s+([\w.-]*):\s*<([^>]+)>", re.IGNORECASE)


def _extract_declared_prefixes(text: str) -> dict[str, str]:
    """Pulls every `@prefix p: <uri> .` (Turtle/TriG) or `PREFIX p: <uri>` (SPARQL)
    declaration out of `text` via regex, without a full parse. Used both on a
    dataset's raw source text (so `query`/`diagnose` output can use exactly the
    prefixes that source already declares) and on a query string on its own (so a
    query using prefixes the dataset doesn't declare still round-trips). N-Triples/
    N-Quads/RDF-XML have no such lines and just yield an empty dict here, falling
    back entirely to `DEFAULT_PREFIXES`."""
    found: dict[str, str] = {}
    for regex in (_PREFIX_DECL_RE, _PREFIX_SPARQL_RE):
        for prefix, uri in regex.findall(text):
            found.setdefault(prefix, uri)
    return found


def _dataset_prefixes(data: str, extra_query: Optional[str] = None) -> dict[str, str]:
    """Builds the combined prefix map for a dataset: `DEFAULT_PREFIXES`, overridden
    by whatever `data` (and, if given, `extra_query` -- a SPARQL query that may
    itself declare prefixes the dataset's own source didn't) declares explicitly."""
    combined = dict(DEFAULT_PREFIXES)
    combined.update(_extract_declared_prefixes(data))
    if extra_query:
        combined.update(_extract_declared_prefixes(extra_query))
    return combined


def _uri_to_curie(uri: str, prefixes: dict[str, str]) -> tuple[str, Optional[str]]:
    """Abbreviates `uri` to `prefix:local` using the longest matching namespace in
    `prefixes`, returning `(abbreviated_or_original, prefix_used)`. `prefix_used` is
    `None` when no namespace matched, in which case `uri` is returned unchanged.

    Ties (two prefixes bound to the exact same namespace, e.g. a dataset's own
    declared prefix for a namespace `DEFAULT_PREFIXES` also has a default name for)
    go to whichever was iterated last -- `prefixes` is built (see `_dataset_prefixes`)
    so declared prefixes are always merged in after, and thus win over, defaults.
    """
    best_prefix: Optional[str] = None
    best_ns = ""
    for prefix, ns in prefixes.items():
        if uri.startswith(ns) and len(ns) >= len(best_ns):
            local = uri[len(ns):]
            if local and "/" not in local and "#" not in local:
                best_prefix, best_ns = prefix, ns
    if best_prefix is None:
        return uri, None
    return f"{best_prefix}:{uri[len(best_ns):]}", best_prefix


_URI_TOKEN_RE = re.compile(r"<([^<>\s]+)>")


def _abbreviate_sparql_text(text: str, prefixes: dict[str, str], used: set[str]) -> str:
    """Replaces every `<full uri>` token in a chunk of SPARQL text (a triple pattern,
    a whole query, ...) with its CURIE where `prefixes` has a match, recording which
    prefixes were actually used in `used` so callers can report a minimal, accurate
    legend. Tokens with no matching namespace are left as `<full uri>` -- still valid
    SPARQL, just not abbreviated."""

    def _sub(match: "re.Match[str]") -> str:
        curie, prefix = _uri_to_curie(match.group(1), prefixes)
        if prefix is None:
            return match.group(0)
        used.add(prefix)
        return curie

    return _URI_TOKEN_RE.sub(_sub, text)


def _prefix_declarations(prefix_names: set[str], prefixes: dict[str, str]) -> str:
    """Renders `PREFIX p: <uri>` lines for `prefix_names`, for prepending to a
    standalone query string so it stays directly runnable after abbreviation."""
    return "\n".join(f"PREFIX {p}: <{prefixes[p]}>" for p in sorted(prefix_names) if p in prefixes)


def _make_runnable(query_text: Optional[str], prefixes: dict[str, str], used: set[str]) -> Optional[str]:
    """Abbreviates a full, standalone query string (e.g. `diagnose`'s
    `connected_query`) and prepends the `PREFIX` lines it needs, so the result can be
    pasted straight into `query`/`diagnose` without the caller having to reconstruct
    which prefixes it relies on. Unlike a bare triple/expression fragment, a
    standalone query is meaningless without its own prefixes attached.

    Skips prepending a `PREFIX` line for any name `query_text` already declares
    itself -- relevant when this text is a lightly-modified version of a query that
    already had its own `PREFIX` block (e.g. a namespace-fix suggestion, see
    `_suggest_fixes_for_culprit`), where blindly prepending everything used would
    duplicate declarations the text already has. Safe to skip: `prefixes` is always
    built (see `_dataset_prefixes`) so a name the query declares itself maps to
    exactly the namespace that declaration gives it.
    """
    if query_text is None:
        return None
    local_used: set[str] = set()
    abbreviated = _abbreviate_sparql_text(query_text, prefixes, local_used)
    used.update(local_used)
    already_declared = set(_extract_declared_prefixes(query_text))
    decls = _prefix_declarations(local_used - already_declared, prefixes)
    return f"{decls}\n{abbreviated}" if decls else abbreviated


# ==============================================================================
#  NAMESPACE-FIX SUGGESTIONS
# ==============================================================================
#
# The most common reason a triple pattern turns up as a `diagnose` culprit isn't a
# structural mistake -- it's that the query used the right local name under the
# wrong namespace (`brick:hasPoint` when the graph actually uses `s223:hasPoint`),
# or the right namespace with a typo'd/mis-cased local name (`s223:zone` instead of
# `s223:Zone`). An agent that already knows the graph's real predicate/class names
# wouldn't make this mistake in the first place, and `connect=True`'s graph-edge
# search doesn't target it specifically (it searches for any connecting path, not
# specifically a same-local-name swap, and is namespace-restricted/experimental).
#
# This is cheap and safe enough to run by default (unlike `connect`): candidates
# come from simple, single-triple-pattern SPARQL queries (no join, no cartesian
# risk at all) run directly against the dataset's `Store` -- same as the plain
# `query` tool, not routed through the diagnose watchdog worker -- and every
# candidate is only ever reported after empirically verifying it by substituting it
# into the user's actual query and rerunning that modified query for real. Nothing
# here is a guess dressed up as a fix.

RDF_TYPE_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

MAX_FIX_ATTEMPTS_PER_DIAGNOSE = 6
"""Global cap, across every culprit/triple/term in one `diagnose` call, on how many
candidate substitutions get rerun against the dataset to verify. Each rerun is a
real SPARQL query with the same worst-case cost profile as the user's own query
(bounded individually by `FIX_VERIFY_TIMEOUT`), so this exists to keep a single
`diagnose` call's total added latency bounded even when several culprits each have
several plausible-looking candidates -- most of which, for a graph with lots of
near-miss local names, will fail verification and each cost up to the full timeout
to rule out."""

MAX_FIXES_PER_CULPRIT = 2
"""Cap on how many verified fixes get reported per culprit -- past this, more
`suggested_fixes` entries add clutter without adding much value; an agent acting on
the first verified fix is the common case."""

FIX_VERIFY_TIMEOUT = 5.0
"""Per-attempt timeout (seconds), tighter than `Store.query`'s own 10s default,
since these are speculative reruns -- worth failing fast on rather than spending a
full 10s each to rule out, given `MAX_FIX_ATTEMPTS_PER_DIAGNOSE` attempts might run."""

FIX_VERIFY_ROW_LIMIT = 1000
"""Row cap for a verification rerun -- enough to report a meaningful
`row_count_with_fix` without risking a slow full evaluation of a genuinely-fixed
query that turns out to match a huge fraction of the graph."""

_CANDIDATE_LIMIT = 3
"""Max replacement URIs `_find_term_candidates` returns per broken term -- each one
that comes back may go on to consume a rerun from `MAX_FIX_ATTEMPTS_PER_DIAGNOSE`,
so this is deliberately small."""

_TRIPLE_TERM_RE = re.compile(r"^<([^<>]+)>$")


def _local_name(uri: str) -> str:
    """The part of `uri` after its last `#` or `/` -- the conventional CURIE local
    name. Empty if `uri` ends in a separator (a bare namespace URI used as a node),
    which has no meaningful local name to search the graph for."""
    hash_idx = uri.rfind("#")
    if hash_idx != -1:
        return uri[hash_idx + 1 :]
    slash_idx = uri.rfind("/")
    if slash_idx != -1:
        return uri[slash_idx + 1 :]
    return uri


def _parse_triple_pattern(triple_text: str) -> Optional[tuple[str, str, str]]:
    """Splits a culprit's raw triple-pattern text (spargebra's `Display` for
    `TriplePattern`, e.g. `?zone <http://...#hasPoint> ?sensor`) into `(subject,
    predicate, object)`. Splits on the first two spaces only: subject/predicate can
    never contain whitespace (they're always a URI, blank node, or variable -- never
    a literal, the one term type that can), so whatever's left after two splits is
    the whole object term even if it's a literal containing spaces."""
    parts = triple_text.split(" ", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _bound_uri(term_text: str) -> Optional[str]:
    """The URI inside a triple term written as `<uri>`, or `None` for a variable
    (`?x`), blank node (`_:x`), or literal -- only a bound URI term can be a
    wrong-namespace/wrong-local-name mistake."""
    match = _TRIPLE_TERM_RE.match(term_text)
    return match.group(1) if match else None


def _sparql_string_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _find_term_candidates(store: Store, position: str, local_name: str, exclude_uri: str, case_insensitive: bool) -> list[str]:
    """Looks in `store` for other URIs actually used in triple `position`
    ("subject", "predicate", "object", or "type_object" for the object of an
    `rdf:type` triple specifically, which is both cheaper and more relevant to
    search than a plain object-position scan when the broken term is itself meant to
    be a class) whose local name matches `local_name` -- exactly, or
    case-insensitively when `case_insensitive` is set (a typo/capitalization
    mistake, tried as a fallback only when an exact match finds nothing). Every
    query here is a single triple pattern with two free variables -- no join, so no
    cartesian risk -- capped with `LIMIT` to `_CANDIDATE_LIMIT`."""
    if position == "predicate":
        pattern = "?s ?x ?o"
    elif position == "subject":
        pattern = "?x ?p ?o"
    elif position == "type_object":
        pattern = "?s a ?x"
    else:
        pattern = "?s ?p ?x"

    name_expr = 'REPLACE(STR(?x), "^.*[#/]", "")'
    escaped = _sparql_string_escape(local_name)
    if case_insensitive:
        name_expr = f"LCASE({name_expr})"
        target = f'"{escaped.lower()}"'
    else:
        target = f'"{escaped}"'

    query_text = (
        f"SELECT DISTINCT ?x WHERE {{ {pattern} . "
        f"FILTER(isURI(?x) && ?x != <{exclude_uri}> && {name_expr} = {target}) }} "
        f"LIMIT {_CANDIDATE_LIMIT}"
    )
    try:
        result = store.query(query_text, row_limit=_CANDIDATE_LIMIT)
    except Exception:
        return []
    if result.form != "solutions":
        return []
    return [row[0].value for row in result.rows if row and row[0] is not None]


def _substitute_uri_in_query(query_text: str, old_uri: str, new_uri: str) -> Optional[str]:
    """Rewrites every reference to `old_uri` in `query_text` -- whether written as a
    bare `<old_uri>` or as `prefix:local` under any prefix `query_text` itself
    declares for that namespace -- to `<new_uri>`, so the result can be rerun to
    test a candidate fix. Returns `None` if `old_uri` isn't referenced in a form
    this can find (should be rare in practice, since `old_uri` always comes from a
    triple this exact query parsed to in the first place)."""
    replaced = False
    result = query_text

    bracket_form = f"<{old_uri}>"
    if bracket_form in result:
        result = result.replace(bracket_form, f"<{new_uri}>")
        replaced = True

    local_name = _local_name(old_uri)
    if local_name and old_uri.endswith(local_name):
        ns = old_uri[: -len(local_name)]
        for prefix, bound_ns in _extract_declared_prefixes(query_text).items():
            if bound_ns != ns:
                continue
            pattern = re.compile(rf"(?<![\w:]){re.escape(prefix)}:{re.escape(local_name)}\b")
            new_result, n = pattern.subn(f"<{new_uri}>", result)
            if n:
                result = new_result
                replaced = True

    return result if replaced else None


class _FixAttemptBudget:
    """Mutable counter shared across every culprit in one `diagnose` call, so
    `MAX_FIX_ATTEMPTS_PER_DIAGNOSE` bounds the *total* number of verification
    reruns rather than being applied independently per culprit."""

    def __init__(self, total: int) -> None:
        self.remaining = total

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _suggest_fixes_for_culprit(
    store: Store, original_query: str, raw_triples: list[str], budget: _FixAttemptBudget
) -> list[dict[str, Any]]:
    """For each bound URI term across `raw_triples` (one culprit's broken triple
    pattern(s), in full-URI form straight from the diagnose report -- called before
    any CURIE abbreviation, since the analysis here needs real URIs), looks for
    another URI in `store` sharing its local name and, if one turns up, verifies it
    by substituting it into `original_query` and actually rerunning that modified
    query. Only substitutions confirmed to return at least one row are returned.
    Predicate position is tried before subject/object since a wrong predicate is by
    far the most common version of this mistake.
    """
    fixes: list[dict[str, Any]] = []
    for raw_triple in raw_triples:
        parsed = _parse_triple_pattern(raw_triple)
        if parsed is None:
            continue
        subj, pred, obj = parsed
        is_type = _bound_uri(pred) == RDF_TYPE_URI
        for term_text, position in (
            (pred, "predicate"),
            (obj, "type_object" if is_type else "object"),
            (subj, "subject"),
        ):
            if len(fixes) >= MAX_FIXES_PER_CULPRIT or budget.remaining <= 0:
                return fixes
            uri = _bound_uri(term_text)
            if uri is None:
                continue
            local = _local_name(uri)
            if not local:
                continue

            exact = _find_term_candidates(store, position, local, uri, case_insensitive=False)
            candidates = [(c, "wrong_namespace") for c in exact]
            if not exact:
                candidates += [
                    (c, "local_name_typo") for c in _find_term_candidates(store, position, local, uri, case_insensitive=True)
                ]

            for candidate_uri, kind in candidates:
                if not budget.take():
                    return fixes
                modified = _substitute_uri_in_query(original_query, uri, candidate_uri)
                if modified is None:
                    continue
                try:
                    result = store.query(modified, row_limit=FIX_VERIFY_ROW_LIMIT, timeout=FIX_VERIFY_TIMEOUT)
                except Exception:
                    continue
                row_count = len(result.bindings) if result.form == "solutions" else 0
                if row_count > 0:
                    fixes.append(
                        {
                            "kind": kind,
                            "original_term": uri,
                            "replacement_term": candidate_uri,
                            "fixed_query": modified,
                            "row_count_with_fix": row_count,
                        }
                    )
                    break  # this term's fixed; no need to try its other candidates
    return fixes


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


def _term_to_json(term: Optional[Term], prefixes: dict[str, str], used: set[str]) -> Optional[dict[str, Any]]:
    if term is None:
        return None
    value = term.value
    if term.kind == "uri":
        value, prefix = _uri_to_curie(value, prefixes)
        if prefix is not None:
            used.add(prefix)
    out: dict[str, Any] = {"type": term.kind, "value": value}
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

    `diagnose`/`query` abbreviate every URI they return to `prefix:local` rather than a full URI,
    using this dataset's own declared prefixes plus common defaults for ontologies it doesn't
    declare (each response's own `prefixes` field says exactly which of those were used). The
    `declared_prefixes` returned here is just the dataset's own -- worth a glance up front so you
    know which short names are already meaningful to write in your own queries.
    """
    if (data is None) == (path is None):
        raise ValueError("pass exactly one of `data` or `path`, not both")
    if path is not None:
        data = Path(path).read_text()
    assert data is not None
    store = Store(data, format=format)
    count_result = store.query("SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }")
    triple_count = int(count_result.rows[0][0].value)  # type: ignore[union-attr,index]
    declared_prefixes = _extract_declared_prefixes(data)
    prefixes = {**DEFAULT_PREFIXES, **declared_prefixes}
    _datasets[name] = _Dataset(store=store, data=data, format=format, triple_count=triple_count, prefixes=prefixes)
    _invalidate_diagnose_worker()
    _schema_summaries.pop(name, None)
    return {"name": name, "format": format, "triple_count": triple_count, "declared_prefixes": declared_prefixes}


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
    # bschema_rs already binds its own broad default prefix list (rdf, s223, sh, ...) on
    # class_graph -- fill in whatever's left (dataset-specific namespaces like a data file's own
    # `ex1:`) from this dataset's own declared prefixes, without clobbering bschema_rs's picks for
    # namespaces it already recognized.
    existing_namespaces = {str(ns) for _, ns in class_graph.namespaces()}
    existing_prefixes = {str(p) for p, _ in class_graph.namespaces()}
    for prefix, ns in _extract_declared_prefixes(ds.data).items():
        if ns in existing_namespaces or prefix in existing_prefixes:
            continue
        class_graph.bind(prefix, ns)
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
    suggest_fixes: bool = True,
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

    Every URI in the result -- in `sample_rows`, `culprits`, `connected_query`,
    `fallback_query_with_broken_triples_removed`, everywhere -- is abbreviated to `prefix:local`
    (e.g. `s223:Zone`) rather than returned in full, using this dataset's own declared prefixes
    plus common defaults for ontologies it doesn't declare. `connected_query`/
    `fallback_query_with_broken_triples_removed` are still directly runnable as-is: each has its
    own needed `PREFIX` lines prepended. The top-level `prefixes` field lists exactly which
    prefix -> URI bindings were used anywhere in this response.

    `suggest_fixes` (default `True`) targets the single most common reason a triple pattern is
    broken: the query used the right local name under the wrong namespace (`brick:hasPoint` when
    the graph actually has `s223:hasPoint`), or the right namespace with a mis-cased local name
    (`s223:zone` instead of `s223:Zone`). For each culprit, it looks for another URI in the graph
    sharing the broken term's local name and *verifies* it by actually substituting it into your
    query and rerunning -- nothing appears in a culprit's `suggested_fixes` unless that rerun
    confirmed it returns rows. Each entry's `fixed_query` is directly runnable (own `PREFIX` lines
    included, same as `connected_query`). This is unrelated to `connect`, much cheaper, and runs
    regardless of it -- `connect`'s graph-edge search looks for a different failure mode entirely
    (a genuinely wrong/missing edge, not a namespace mismatch) and doesn't target this one
    specifically. Pass `False` to skip it if you don't want the extra (small, timeout-bounded)
    query reruns.
    """
    _require_dataset(dataset)  # fail fast with a clear error before involving the watchdog worker at all
    entry = _datasets[dataset]
    store = entry.store
    prefixes = _dataset_prefixes(entry.data, extra_query=query)
    used_prefixes: set[str] = set()
    fix_budget = _FixAttemptBudget(MAX_FIX_ATTEMPTS_PER_DIAGNOSE)

    def _abbrev(text: Optional[str]) -> Optional[str]:
        return None if text is None else _abbreviate_sparql_text(text, prefixes, used_prefixes)

    def _runnable(text: Optional[str]) -> Optional[str]:
        return _make_runnable(text, prefixes, used_prefixes)

    def _display_uri(uri: str) -> str:
        curie, prefix = _uri_to_curie(uri, prefixes)
        if prefix is not None:
            used_prefixes.add(prefix)
        return curie

    def _fixes_for(raw_triples: list[str]) -> list[dict[str, Any]]:
        if not suggest_fixes:
            return []
        raw_fixes = _suggest_fixes_for_culprit(store, query, raw_triples, fix_budget)
        return [
            {
                "kind": f["kind"],
                "original_term": _display_uri(f["original_term"]),
                "replacement_term": _display_uri(f["replacement_term"]),
                "fixed_query": _runnable(f["fixed_query"]),
                "row_count_with_fix": f["row_count_with_fix"],
            }
            for f in raw_fixes
        ]

    worker = _get_diagnose_worker()
    if connect:
        report = worker.call(dataset, "diagnose_and_connect", query, ignore_cartesian_risk=ignore_cartesian_risk)
        culprits = [
            {
                "depth": result.found_at_depth,
                "triples": [{"triple": _abbrev(t.triple), "discovered_path": _abbrev(t.path_text)} for t in result.triples],
                "fixed": result.fixed,
                "connected_query": _runnable(result.connected_query),
                "row_count_with_fix": result.row_count,
                "fallback_query_with_broken_triples_removed": _runnable(result.pruned_query),
                "fallback_row_count": result.pruned_row_count,
                "suggested_fixes": _fixes_for([t.triple for t in result.triples]),
            }
            for result in report.results
        ]
        filter_issues = [
            {"expression": _abbrev(f.expression), "row_count_without_filter": f.row_count_without_filter}
            for f in report.filter_results
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
                "triples": [{"triple": _abbrev(t), "discovered_path": None} for t in c.triples],
                "fixed": False,
                "connected_query": None,
                "row_count_with_fix": None,
                "fallback_query_with_broken_triples_removed": None,
                "fallback_row_count": None,
                "suggested_fixes": _fixes_for(list(c.triples)),
            }
            for c in report.culprits
        ]
        filter_issues = [
            {"expression": _abbrev(f.expression), "row_count_without_filter": f.row_count_without_filter}
            for f in report.filter_culprits
        ]
        cartesian_risks = report.cartesian_risks
        sample_variables = report.sample_variables
        sample_rows = [
            {var: _term_to_json(term, prefixes, used_prefixes) for var, term in zip(sample_variables, row)}
            for row in report.sample_rows
        ]

    cartesian_risks_skipped = [
        {"triples": [_abbrev(t) for t in r.triples], "depth": r.depth} for r in cartesian_risks
    ]

    fixed_culprit_count = sum(1 for c in culprits if c["suggested_fixes"])

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
        if fixed_culprit_count:
            message = (
                f"Query is broken, but {fixed_culprit_count} culprit(s) have a verified fix in their own "
                "`suggested_fixes` -- each `fixed_query` there was actually rerun and confirmed to return rows."
            )
        elif connect:
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
        "prefixes": {p: prefixes[p] for p in sorted(used_prefixes)},
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

    Every URI in the result is abbreviated to `prefix:local` (e.g. `s223:Zone`) rather than
    returned in full, using this dataset's own declared prefixes plus common defaults for
    ontologies it doesn't declare -- match these against the prefixes you wrote in `query` itself.
    The `prefixes` field on the response lists exactly which prefix -> URI bindings were actually
    used, so nothing is ambiguous even for a namespace the dataset didn't declare.
    """
    store = _require_dataset(dataset)
    entry = _datasets[dataset]
    prefixes = _dataset_prefixes(entry.data, extra_query=query)
    used_prefixes: set[str] = set()
    result: QueryResult = store.query(query, row_limit=row_limit)

    if result.form == "boolean":
        return {"form": "boolean", "result": result.boolean}
    if result.form == "solutions":
        return {
            "form": "solutions",
            "variables": result.variables,
            "rows": [
                {var: _term_to_json(term, prefixes, used_prefixes) for var, term in row.items()}
                for row in result.bindings
            ],
            "prefixes": {p: prefixes[p] for p in sorted(used_prefixes)},
        }
    return {
        "form": "graph",
        "triples": [
            {
                "subject": _term_to_json(s, prefixes, used_prefixes),
                "predicate": _term_to_json(p, prefixes, used_prefixes),
                "object": _term_to_json(o, prefixes, used_prefixes),
            }
            for s, p, o in (result.triples or [])
        ],
        "prefixes": {p: prefixes[p] for p in sorted(used_prefixes)},
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
