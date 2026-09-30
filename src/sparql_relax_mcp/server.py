"""MCP server exposing `sparql_relax`'s SPARQL query execution and diagnosis, and
`bschema`'s structural graph summarization, over in-memory RDF graphs, for AI agents.

Intended agent workflow: `load_dataset` once, then `summarize_schema` -- also just
once, it's cached -- to see the graph's repeated structural patterns before writing
any SPARQL against it. From there, `run_query` is the one tool for running queries,
of any form (SELECT/ASK/CONSTRUCT/DESCRIBE): it returns the query's results (capped by
`row_limit`), confirms the row count, and explains *why* a broken query returns nothing
or too few rows -- which triple or FILTER is at fault. For non-SELECT queries, it
diagnoses the WHERE body (rewritten as `SELECT *`) and then executes the original
query. `run_query`'s `connect=True` option additionally searches the graph's real
edges for a corrected query, but that search is experimental (slower,
namespace-restricted, and not guaranteed to find or verify a real fix) -- most agents
are better served by the default diagnosis and fixing the query themselves from its
explanation.

For the single most common broken-triple cause -- right local name, wrong namespace,
or a mis-cased local name -- `run_query` doesn't just explain it: by default it also
looks for another URI in the graph with the same local name, substitutes it in, and
reruns, reporting the result in that culprit's `suggested_fixes` only once verified to
actually return rows. See `_suggest_fixes_for_culprit`. This is unrelated to and much
cheaper than `connect`, and runs regardless of it.

The default `extended` toolset (see TOOLSETS) adds two exploration tools on top of
those four: `search` (BM25 or regex over node names and string literals, for finding a
URI before querying it) and `traverse` (a breadth-first walk from a node along chosen
predicates, returned as a per-level DAG). `--toolset core` exposes only the original
four, for when the extra tool descriptions aren't worth their context cost.

Every URI any tool returns is abbreviated to `prefix:local` (e.g. `s223:Zone`) rather
than a full URI, using the dataset's own declared prefixes plus common defaults --
see `DEFAULT_PREFIXES`/`_dataset_prefixes` -- so results read the way an agent
actually writes SPARQL and don't burn context on repeated namespace strings.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from bschema_rs import create_bschema
from mcp.server.fastmcp import FastMCP
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH
from sparql_relax import QueryResult, Store, Term

_CORE_INSTRUCTIONS = (
    "Tools for understanding and querying SPARQL/RDF graphs. Load a graph with "
    "load_dataset, then call summarize_schema ONCE to see the graph's repeated structural "
    "patterns before writing any SPARQL against it -- it's cached, so calling it again is "
    "free but adds nothing new. From there, run_query is the one tool for running any query "
    "(SELECT/ASK/CONSTRUCT/DESCRIBE). It returns the query's results -- just 3 rows by "
    "default; raise row_limit (or pass null) when you need more -- and, in the same call, "
    "diagnoses it: cheap when the query works (ok=true), and when it doesn't, it explains "
    "exactly which triple or FILTER is broken. run_query's connect=True option additionally "
    "tries to search the graph for a corrected query, but that search is experimental and "
    "its suggestions should be verified, not trusted outright -- leave connect off unless you "
    "specifically want to try it. Separately, and by default, run_query also checks each "
    "broken triple for the single most common mistake -- right local name, wrong namespace, "
    "or a mis-cased local name -- and reports a verified fix (query rerun and confirmed to "
    "return rows) in that culprit's suggested_fixes when one exists; this is unrelated to and "
    "much cheaper than connect. Every URI any tool returns is abbreviated to prefix:local "
    "(e.g. s223:Zone) using the dataset's declared prefixes plus common defaults -- each "
    "response's own `prefixes` field lists exactly which bindings were used. Ontology "
    "files: ASHRAE 223P at https://open223.info/223p.ttl, Brick at "
    "https://brickschema.org/schema/1.4.4/Brick.ttl."
)

_EXTENDED_INSTRUCTIONS = _CORE_INSTRUCTIONS + (
    " When you don't yet know the URI for a concept (a class, a predicate, or a specific "
    "instance), use search to find it -- by keyword (mode='bm25', over local names, labels, "
    "comments and other string literals) or by regex -- instead of guessing names and letting "
    "run_query catch the guess. To walk a hierarchy or a chain of relations (up or down an "
    "rdfs:subClassOf taxonomy, downstream along brick:feeds, ...), use traverse with a direction "
    "and a predicate list; it returns the reachable structure level by level, with every edge "
    "into each node, which a SPARQL property path's flat result doesn't show."
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

_schema_summaries: dict[str, dict[bool, dict[str, Any]]] = {}
"""Cache of `summarize_schema` results, keyed by dataset name and then by its
`exclude_ontology` flag (the two produce different summaries) -- computing a bschema
class graph is real work (iterative graph relabeling), and the point of the tool is
to be called once per dataset, so a repeat call should be free rather than
recomputing. Cleared for a name whenever `load_dataset` replaces it."""

_search_indexes: dict[str, "_SearchIndex"] = {}
"""Cache of `search`'s per-dataset index, keyed by dataset name -- built lazily on
the first `search` call against a dataset (one full scan of its triples), then
reused. Cleared for a name whenever `load_dataset` replaces it."""

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
    dataset's raw source text (so `run_query` output can use exactly the
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
    """Abbreviates a full, standalone query string (e.g. `run_query`'s
    `connected_query`) and prepends the `PREFIX` lines it needs, so the result can be
    pasted straight back into `run_query` without the caller having to reconstruct
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
# The most common reason a triple pattern turns up as a `run_query` culprit isn't a
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
# risk at all) run directly against the dataset's `Store` in-process, not routed
# through the diagnose watchdog worker -- and every
# candidate is only ever reported after empirically verifying it by substituting it
# into the user's actual query and rerunning that modified query for real. Nothing
# here is a guess dressed up as a fix.

RDF_TYPE_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

MAX_FIX_ATTEMPTS_PER_DIAGNOSE = 6
"""Global cap, across every culprit/triple/term in one `run_query` call, on how many
candidate substitutions get rerun against the dataset to verify. Each rerun is a
real SPARQL query with the same worst-case cost profile as the user's own query
(bounded individually by `FIX_VERIFY_TIMEOUT`), so this exists to keep a single
`run_query` call's total added latency bounded even when several culprits each have
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
    """Mutable counter shared across every culprit in one `run_query` call, so
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
# `run_query` also executes queries through this same worker (the worker just
# dispatches `Store.query` like any other method), so plain execution of a
# SELECT under `connect=True`, or of an ASK/CONSTRUCT/DESCRIBE, gets the same
# hard-timeout protection as the diagnosis itself.

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


def load_dataset(name: str, data: Optional[str] = None, path: Optional[str] = None, format: str = "turtle") -> dict[str, Any]:
    """Load RDF data into memory as a named dataset for `run_query` to run against.

    Pass exactly one of `data` (the RDF text itself) or `path` (an absolute path to a local RDF
    file to read) -- not both. `format` is one of "turtle" (default), "ntriples", "nquads",
    "rdfxml", or "trig".

    Loading a dataset under a `name` that's already loaded replaces it.

    `run_query` abbreviates every URI it returns to `prefix:local` rather than a full URI,
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
    _search_indexes.pop(name, None)
    return {"name": name, "format": format, "triple_count": triple_count, "declared_prefixes": declared_prefixes}


def list_datasets() -> list[dict[str, Any]]:
    """List every dataset currently loaded via `load_dataset`, with its format and triple count."""
    return [{"name": name, "format": ds.format, "triple_count": ds.triple_count} for name, ds in sorted(_datasets.items())]


_ONTOLOGY_TYPES: frozenset[URIRef] = frozenset({
    RDFS.Class, OWL.Class, RDFS.Datatype,
    RDF.Property, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty, OWL.OntologyProperty,
    OWL.FunctionalProperty, OWL.InverseFunctionalProperty, OWL.TransitiveProperty, OWL.SymmetricProperty,
    OWL.AsymmetricProperty, OWL.ReflexiveProperty, OWL.IrreflexiveProperty,
    OWL.Ontology,
    SH.Shape, SH.NodeShape, SH.PropertyShape,
})
"""`rdf:type`s marking a subject as ontology rather than instance data, for
`summarize_schema(exclude_ontology=True)` -- see `_strip_ontology`."""

_ONTOLOGY_PREDICATES: frozenset[URIRef] = frozenset({
    RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain, RDFS.range,
    OWL.imports, OWL.deprecated, OWL.equivalentClass, OWL.equivalentProperty, OWL.inverseOf,
})
"""Predicates only an ontology term is ever the subject of, marking it as ontology even when
it's missing an `rdf:type` from `_ONTOLOGY_TYPES` -- e.g. Brick's deprecated terms (only
`owl:deprecated`) or an `owl:imports` header with no `owl:Ontology` type. Any predicate in the
`sh:` namespace counts too (a shape declared only by its `sh:or`, say); see `_strip_ontology`."""


def _namespace(uri: str) -> str:
    """`uri` up to and including its last `#` or `/` -- `ex:` for `ex:vav1`."""
    return uri[: max(uri.rfind("#"), uri.rfind("/")) + 1]


def _subclass_hierarchy(graph: Graph) -> dict[Any, set[Any]]:
    """Each class's direct `rdfs:subClassOf` parents in `graph`, for
    `_remove_inferred_superclass_types` -- read before `_strip_ontology` removes them."""
    parents_of: dict[Any, set[Any]] = {}
    for s, o in graph.subject_objects(RDFS.subClassOf):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and s != o:
            parents_of.setdefault(s, set()).add(o)
    return parents_of


def _remove_inferred_superclass_types(graph: Graph, parents_of: dict[Any, set[Any]]) -> int:
    """Remove `?s a ?parent` wherever `?s` is also typed with a strict subclass of `?parent`
    (per `parents_of`, from `_subclass_hierarchy`), returning how many triples were removed.

    Not ontology removal: these are instance-data triples, typically materialized by a reasoner
    (`ex:vav1 a brick:VAV, brick:Terminal_Unit, brick:HVAC_Equipment, brick:Equipment`). For a
    schema summary they only add noise -- the most specific type already implies the rest -- and
    they split otherwise-identical subjects into different patterns whenever inference was applied
    unevenly. Only the hierarchy the graph itself bundles is known, so a type whose subclass link
    lives in an ontology that wasn't loaded stays. "Strict" means a type is only dropped for a
    subclass it isn't also a subclass of, so classes declared equivalent via a subclass cycle
    never knock each other out.
    """
    types_of: dict[Any, set[Any]] = {}
    for s, o in graph.subject_objects(RDF.type):
        types_of.setdefault(s, set()).add(o)

    ancestors_cache: dict[Any, set[Any]] = {}

    def ancestors(cls: Any) -> set[Any]:
        cached = ancestors_cache.get(cls)
        if cached is None:
            cached = set()
            frontier = list(parents_of.get(cls, ()))
            while frontier:
                parent = frontier.pop()
                if parent not in cached:
                    cached.add(parent)
                    frontier.extend(parents_of.get(parent, ()))
            ancestors_cache[cls] = cached
        return cached

    redundant = [
        (subject, RDF.type, t)
        for subject, types in types_of.items()
        if len(types) > 1
        for t in types
        if any(t in ancestors(other) and other not in ancestors(t) for other in types if other != t)
    ]
    for triple in redundant:
        graph.remove(triple)
    return len(redundant)


def _strip_ontology(graph: Graph) -> int:
    """Remove ontology definitions from `graph` in place, returning how many triples were removed.

    A subject counts as ontology if it's typed with one of `_ONTOLOGY_TYPES` (or with a metaclass
    the graph itself declares as a subclass of one, like 223P's `s223:Class rdfs:subClassOf
    rdfs:Class`), or is the subject of one of `_ONTOLOGY_PREDICATES` or of any `sh:` predicate.

    Ontologies also define individuals that aren't classes, properties or shapes -- Brick's
    `brick:Quantity`s and `brick:Substance`s, or its `tag:` namespace of `brick:Tag`s, which only
    the removed classes point to. So the ontology's *namespaces* go too: a namespace whose
    subjects are mostly ontology -- the terms above, plus URIs referenced only by them -- is
    removed wholesale. "Mostly" is what keeps instance data safe: the data's own namespace can
    hold its `owl:Ontology` header or a node some shape names in `sh:targetNode`, but those are
    outnumbered by the actual data, so that namespace stays.

    Every triple *about* a removed subject goes, plus the blank nodes reachable from it --
    `owl:Restriction`s, `sh:property` shapes, `sh:rule`s, RDF lists -- since those only exist to
    describe it. A blank node reached that way but still referenced by some kept subject is kept
    (along with everything under it), so shared structure never leaves instance data dangling.
    Triples that merely *use* an ontology term (`ex:vav1 a brick:VAV`) are untouched.
    """

    # One pass over the graph into plain dicts: per-node lookups against the store (Oxigraph's
    # especially) cost far more than the dict lookups below, and a Brick-sized graph needs
    # hundreds of thousands of them.
    objects_of: dict[Any, list[Any]] = {}
    referrers_of: dict[Any, set[Any]] = {}
    typed: dict[Any, list[Any]] = {}
    subclasses_of: dict[Any, list[Any]] = {}
    shacl_ns = str(SH)
    seeds: set[Any] = set()
    for s, p, o in graph:
        objects_of.setdefault(s, []).append(o)
        referrers_of.setdefault(o, set()).add(s)
        if p == RDF.type:
            typed.setdefault(o, []).append(s)
        elif p == RDFS.subClassOf:
            subclasses_of.setdefault(o, []).append(s)
        if p in _ONTOLOGY_PREDICATES or p.startswith(shacl_ns):
            seeds.add(s)

    def with_blank_nodes(seeds: set[Any]) -> set[Any]:
        reached: set[Any] = set()
        frontier = list(seeds)
        while frontier:
            for obj in objects_of.get(frontier.pop(), ()):
                if isinstance(obj, BNode) and obj not in seeds and obj not in reached:
                    reached.add(obj)
                    frontier.append(obj)
        removed = seeds | reached
        changed = True
        while changed:
            changed = False
            for node in [n for n in reached if n in removed]:
                if not referrers_of[node] <= removed:
                    removed.discard(node)
                    changed = True
        return removed

    ontology_types = set(_ONTOLOGY_TYPES)
    frontier = list(ontology_types)
    while frontier:
        for subclass in subclasses_of.get(frontier.pop(), ()):
            if isinstance(subclass, URIRef) and subclass not in ontology_types:
                ontology_types.add(subclass)
                frontier.append(subclass)
    for t in ontology_types:
        seeds.update(typed.get(t, ()))
    removed = with_blank_nodes(seeds)

    referenced_only_by_ontology = {
        obj
        for node in removed
        for obj in objects_of.get(node, ())
        if isinstance(obj, URIRef)
        and obj not in removed
        and obj in objects_of
        and referrers_of[obj] - {obj} <= removed
    }
    ontology_count: Counter[str] = Counter()
    other_count: Counter[str] = Counter()
    uri_subjects = [s for s in objects_of if isinstance(s, URIRef)]
    for subject in uri_subjects:
        counter = ontology_count if subject in removed or subject in referenced_only_by_ontology else other_count
        counter[_namespace(subject)] += 1
    ontology_namespaces = {ns for ns, n in ontology_count.items() if n > other_count[ns]}
    seeds.update(s for s in uri_subjects if _namespace(s) in ontology_namespaces)
    removed = with_blank_nodes(seeds)

    before = len(graph)
    for node in removed:
        graph.remove((node, None, None))
    return before - len(graph)


def summarize_schema(
    dataset: str,
    iterations: int = 10,
    similarity_threshold: Optional[float] = 0.3,
    include_member_counts: bool = False,
    exclude_ontology: bool = False,
) -> dict[str, Any]:
    """Summarize `dataset`'s structure into a compact class graph (via bschema), so you can see
    its repeated patterns before writing SPARQL against it.

    Call this ONCE per dataset, right after `load_dataset` and before your first `run_query`
    call -- knowing the graph's shape up front is what makes it possible to write a
    plausible query on the first try instead of guessing at predicates and class names. The
    result is cached, so calling it again for the same dataset is free but returns the same
    summary; it won't reflect changes until `load_dataset` reloads that name.

    The returned `class_graph` (Turtle) groups subjects that share the same 1-hop structural
    pattern into a single derived `bs:`-namespaced class -- read it the way you'd read a schema,
    not as data to query directly. Each class is named after its members' shared `rdf:type` (e.g.
    `bs:VAV_version_1 a brick:VAV`), or `bs:Resource_version_N` when the group has none -- never
    after a specific real instance, so nothing here could be mistaken for an actual entity to
    query. `compression_pct` (class graph size / original graph size) gives a rough sense of how
    repetitive the data is: a low percentage means most entities collapsed into a few patterns
    and the summary is trustworthy; a percentage close to 100 means the data didn't compress much
    (e.g. it's already schema-like, or every entity is distinct) and the summary is less useful.

    `similarity_threshold` (0-1, default 0.3) groups subjects whose patterns overlap above that
    ratio rather than requiring an exact match -- real building/knowledge graphs rarely have
    perfectly identical 1-hop patterns across instances, so a lenient default finds more of the
    graph's repeated structure than exact isomorphism would. Pass `None` to require an exact
    match instead (more classes, each more homogeneous), or a higher ratio for something in
    between.

    `include_member_counts` (default `False`) adds a `member_counts` field: a mapping from each
    derived class's CURIE (as it appears in `class_graph`) to how many real instances it
    collapsed, e.g. `{"bs:VAV_version_1": 50, "bs:AHU_version_1": 4}` -- lets you tell how many
    of a given pattern actually exist (4 AHUs? 51 zones?) without spending a separate
    `run_query` round trip on a `COUNT` query just to find out. Off by default to keep the
    common-case response small; pass `True` when that count matters for what you're about to
    query.

    `exclude_ontology` (default `False`) drops ontology definitions -- every class
    (`owl:Class`/`rdfs:Class`, or a metaclass declared as a subclass of one, like `s223:Class`),
    property (`rdf:Property` and the OWL property kinds), SHACL shape, and ontology header, along
    with the blank-node structure hanging off them (restrictions, property shapes, rules, lists)
    -- before summarizing, and then every other subject in the ontology's namespaces (Brick's
    tags, quantities and substances, say). Pass `True` when the dataset bundles its ontology
    (e.g. Brick or 223P loaded alongside the instance data), so the summary shows the instance
    data's patterns instead of the ontology's. Only the summary is affected: the loaded dataset
    itself, and so `run_query`, still sees every triple. Instance data typed with those classes
    (`ex:vav1 a brick:VAV`) is kept, and so is the data's own namespace, as long as the data
    isn't in the same namespace as the ontology it's bundled with.
    """
    cached = _schema_summaries.get(dataset, {}).get(exclude_ontology)
    if cached is None:
        ds = _datasets.get(dataset)
        if ds is None:
            available = ", ".join(sorted(_datasets)) or "(none loaded)"
            raise ValueError(f"no dataset named {dataset!r} is loaded. Loaded datasets: {available}. Call load_dataset first.")

        rdflib_format = _RDFLIB_FORMATS[ds.format]
        data_graph = Graph(store="Oxigraph")
        data_graph.parse(data=ds.data, format=rdflib_format)
        ontology_triples_removed = 0
        if exclude_ontology:
            # Going beyond strictly removing the ontology, this also drops inferred superclass
            # types from the instance data (see _remove_inferred_superclass_types and the
            # README). Deliberately left out of this tool's docstring and response: it's a
            # summary-quality detail that would only distract an agent reading them.
            hierarchy = _subclass_hierarchy(data_graph)
            ontology_triples_removed = _strip_ontology(data_graph)
            _remove_inferred_superclass_types(data_graph, hierarchy)

        # use_original_names=False: name each derived class after its members' shared rdf:type
        # (e.g. bs:VAV_version_1) instead of one arbitrary member's own IRI local name (e.g.
        # bs:RTU01) -- the latter reads exactly like real instance data and could be mistaken for
        # (or literally collide with) an actual entity in the graph.
        class_graph, member_graph, iterations_run = create_bschema(
            data_graph, iterations=iterations, similarity_threshold=similarity_threshold, use_original_names=False
        )
        # bschema_rs already binds its own broad default prefix list (rdf, s223, sh, ...) on
        # class_graph -- fill in whatever's left (dataset-specific namespaces like a data file's
        # own `ex1:`) from this dataset's own declared prefixes, without clobbering bschema_rs's
        # picks for namespaces it already recognized. Only skip a prefix whose *namespace* is
        # already bound -- if the *name* merely collides with a different namespace (e.g.
        # bschema_rs's default `brick:` is an unversioned URI, but the dataset declares a
        # versioned one), still bind it: rdflib's own `bind()` auto-suffixes the new prefix
        # (`brick1:`) in that case rather than silently dropping it, so the dataset's real
        # vocabulary never falls through to a serialize-time `nsN:`.
        existing_namespaces = {str(ns) for _, ns in class_graph.namespaces()}
        for prefix, ns in _extract_declared_prefixes(ds.data).items():
            if ns in existing_namespaces:
                continue
            class_graph.bind(prefix, ns)
        class_graph_text = class_graph.serialize(format="turtle")
        original_size = len(data_graph)
        compression_pct = (len(class_graph) / original_size * 100) if original_size else 0.0

        # member_graph maps each derived class to every real instance it collapsed via
        # rdfs:member triples -- create_bschema computes it regardless of include_member_counts,
        # so counting it here costs a groupby, not a new graph traversal. Always compute and
        # cache it (keyed by prefixes matching class_graph's own, post-fill) so a later call with
        # include_member_counts=True doesn't need a second bschema run.
        prefixes = {**DEFAULT_PREFIXES, **_extract_declared_prefixes(ds.data)}
        member_counts = {
            _uri_to_curie(str(cls), prefixes)[0]: sum(1 for _ in member_graph.objects(cls, RDFS.member))
            for cls in sorted(member_graph.subjects(RDFS.member, None, unique=True))
        }

        cached = {
            "class_graph": class_graph_text,
            "compression_pct": round(compression_pct, 2),
            "iterations_run": iterations_run,
            "member_counts": member_counts,
            "original_size": original_size,
            "class_graph_size": len(class_graph),
            "ontology_triples_removed": ontology_triples_removed,
        }
        _schema_summaries.setdefault(dataset, {})[exclude_ontology] = cached

    message = (
        f"Compressed {cached['original_size']} triples to {cached['class_graph_size']} "
        f"({cached['compression_pct']:.1f}%) in {cached['iterations_run']} iteration(s). Use "
        "class_graph to understand the graph's structure, then call run_query on your queries."
    )
    result = {
        "class_graph": cached["class_graph"],
        "compression_pct": cached["compression_pct"],
        "iterations_run": cached["iterations_run"],
    }
    if exclude_ontology:
        result["ontology_triples_removed"] = cached["ontology_triples_removed"]
        message += (
            f" Removed {cached['ontology_triples_removed']} ontology triple(s) (class, property and "
            "SHACL shape definitions) before summarizing; the triple counts above exclude them."
        )
    if include_member_counts:
        result["member_counts"] = cached["member_counts"]
        message += " member_counts has each class's real instance count."
    result["message"] = message
    return result


_IRIREF_RE = re.compile(r'<[^<>"{}|^`\\\x00-\x20]*>')
_STRING_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""'
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'",
    re.S,
)
_QUERY_FORM_RE = re.compile(r"(?<![\w:?$])(SELECT|ASK|CONSTRUCT|DESCRIBE)(?![\w:\-])", re.I)

_UNLIMITED_ROWS = 2**63 - 1
"""Stand-in for "no limit" when handing `row_limit=None` to `Store.diagnose`, whose
`sample_limit` is a Rust `usize` and doesn't accept `None`."""


def _mask_sparql(text: str) -> str:
    """Returns `text` with every string literal, IRIREF and comment blanked out to spaces
    (same length, so indices still line up with `text`) -- lets `_query_form`/
    `_as_select_over_where_body` find keywords and braces with plain regex/brace matching
    without tripping over a `{` inside a literal or a `#` inside an IRI."""
    out = list(text)
    i = 0
    while i < len(text):
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            j = len(text) if j == -1 else j
        elif c in "\"'":
            m = _STRING_RE.match(text, i)
            j = m.end() if m else i + 1
        elif c == "<" and (m := _IRIREF_RE.match(text, i)):
            j = m.end()
        else:
            i += 1
            continue
        out[i:j] = " " * (j - i)
        i = j
    return "".join(out)


def _match_brace(masked: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _query_form(query: str) -> Optional[str]:
    """`"SELECT"`/`"ASK"`/`"CONSTRUCT"`/`"DESCRIBE"`, or `None` if none is found (left for
    the query engine itself to reject with a real parse error)."""
    m = _QUERY_FORM_RE.search(_mask_sparql(query))
    return m.group(1).upper() if m else None


def _as_select_over_where_body(query: str) -> Optional[str]:
    """Rewrites an ASK/CONSTRUCT/DESCRIBE query into `SELECT * WHERE { <its WHERE body> }`
    (same prologue, same trailing solution modifiers), so `Store.diagnose` -- SELECT-only --
    can explain why that body matches nothing. `None` when there's no body to diagnose (a
    bare `DESCRIBE <uri>`) or the query's shape isn't recognized. The dataset clauses
    (`FROM`) of CONSTRUCT/DESCRIBE are dropped -- this server's stores have a single default
    graph anyway."""
    masked = _mask_sparql(query)
    m = _QUERY_FORM_RE.search(masked)
    if m is None:
        return None
    form = m.group(1).upper()
    prologue = query[: m.start()]
    if form == "ASK":
        return prologue + "SELECT *" + query[m.end() :]
    if form not in ("CONSTRUCT", "DESCRIBE"):
        return None
    pos = m.end()
    if form == "CONSTRUCT":
        template_start = len(masked[pos:]) - len(masked[pos:].lstrip()) + pos
        if masked.startswith("{", template_start):
            template_end = _match_brace(masked, template_start)
            if template_end < 0:
                return None
            pos = template_end + 1
    body_start = masked.find("{", pos)
    if body_start < 0:
        return None
    return prologue + "SELECT * WHERE " + query[body_start:]


def run_query(
    dataset: str,
    query: str,
    row_limit: Optional[int] = 3,
    connect: bool = False,
    suggest_fixes: bool = True,
) -> dict[str, Any]:
    """Run any SPARQL query (SELECT/ASK/CONSTRUCT/DESCRIBE) against `dataset`, returning its
    results *and* a diagnosis of why it returns nothing when it's broken. This is the one tool
    for running queries -- there's no separate "just execute" tool, and none is needed: on a
    working query the diagnosis is nearly free and comes back as `ok: true` with no culprits.

    Results come back shaped by the query's form (`form`): `"solutions"` (SELECT) with
    `variables`/`rows`, `"boolean"` (ASK) with `result`, or `"graph"` (CONSTRUCT/DESCRIBE) with
    `triples`. `row_limit` (default 3) caps how many rows/triples are returned -- enough to
    confirm the query returns what you expect without spending context on a full result set.
    Pass a higher value, or `null` for no limit, once you actually need the results (e.g. to hand
    them back to the user), or `0` for the diagnosis alone. It has no effect on ASK, and never
    affects `row_count`, which is always the full count.

    `row_count` counts solutions of the query's WHERE pattern: for SELECT, that's its own rows;
    for ASK/CONSTRUCT/DESCRIBE, the query is diagnosed as `SELECT * WHERE { <its WHERE body> }`
    first (so an ASK that's `false`, or a CONSTRUCT that builds nothing, is explained the same
    way an empty SELECT is), then the original query itself is executed for `result`/`triples`.
    A bare `DESCRIBE <uri>` with no WHERE clause has nothing to diagnose and is just executed
    (`row_count: null`). If the diagnosis itself can't run -- e.g. the pattern is only
    all-variable triples like `?s ?p ?o`, which there's nothing to ablate in -- the query is still
    executed and the reason is reported in `diagnosis_error`.

    On a query whose pattern matches nothing, or fewer rows than expected, `culprits`/
    `filter_issues` explain *why* -- which BGP triple(s) or FILTER(s) are responsible. Once the
    pattern already matches at least one row, the (combinatorial, by far the most expensive)
    triple/filter search is skipped entirely, so a working query costs one query run.

    `suggest_fixes` (default `True`) targets the single most common reason a triple pattern is
    broken: the query used the right local name under the wrong namespace (`brick:hasPoint` when
    the graph actually has `s223:hasPoint`), or the right namespace with a mis-cased local name
    (`s223:zone` instead of `s223:Zone`). For each culprit, it looks for another URI in the graph
    sharing the broken term's local name and *verifies* it by actually substituting it into your
    query and rerunning -- nothing appears in a culprit's `suggested_fixes` unless that rerun
    confirmed it returns rows. Each entry's `fixed_query` is directly runnable (own `PREFIX` lines
    included). Pass `False` to skip it if you don't want the extra (small, timeout-bounded) reruns.

    If `connect=True`, it also searches the graph's actual edges for a real connecting path,
    often finding a corrected query that actually returns rows (see `connected_query` on each
    culprit). This is experimental: slower, and restricted to predicates in the Brick, ASHRAE
    223P, RDFS and QUDT namespaces (a real fix outside those won't be found, though the diagnosis
    of *which* triple is broken is unaffected). For AI agents it's usually more effective to leave
    `connect` off and correct the query yourself from the diagnosis.

    Some triple combinations would force the query engine to materialize a full N x M cross
    product before yielding a single row -- this call always skips those instead of checking them
    (reported separately in `cartesian_risks_skipped`, not proof either way). Everything runs in a
    watchdog-guarded worker process, so a query that hangs the engine is killed after 30s and
    reported as an error rather than hanging the server.

    Every URI in the result -- in `rows`, `triples`, `culprits`, `connected_query`,
    `fallback_query_with_broken_triples_removed`, everywhere -- is abbreviated to `prefix:local`
    (e.g. `s223:Zone`) rather than returned in full, using this dataset's own declared prefixes
    plus common defaults for ontologies it doesn't declare. `connected_query`/`fixed_query`/
    `fallback_query_with_broken_triples_removed` are still directly runnable as-is: each has its
    own needed `PREFIX` lines prepended. The top-level `prefixes` field lists exactly which
    prefix -> URI bindings were used anywhere in this response.
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

    form = _query_form(query)
    is_select = form == "SELECT"
    diagnosed_query = query if is_select else _as_select_over_where_body(query)
    rows_limit = _UNLIMITED_ROWS if row_limit is None else row_limit

    def _fixes_for(raw_triples: list[str]) -> list[dict[str, Any]]:
        # Fixes are verified by rerunning the query with a term substituted, which needs a
        # SELECT -- so for a non-SELECT query they're found and reported against the rewritten
        # `SELECT * WHERE { ... }` form (still runnable, just not the original query form).
        if not suggest_fixes or diagnosed_query is None:
            return []
        raw_fixes = _suggest_fixes_for_culprit(store, diagnosed_query, raw_triples, fix_budget)
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
    report = None
    diagnosis_error: Optional[str] = None
    culprits: list[dict[str, Any]] = []
    filter_issues: list[dict[str, Any]] = []
    cartesian_risks: list[Any] = []
    sampled: Optional[tuple[list[str], list[list[Optional[Term]]]]] = None

    # MCP callers never opt into ignoring cartesian risk or expanding a nonempty result's search
    # -- both are hardcoded here rather than exposed as parameters (see the docstring).
    if diagnosed_query is not None:
        try:
            if connect:
                report = worker.call(dataset, "diagnose_and_connect", diagnosed_query, ignore_cartesian_risk=False)
            else:
                report = worker.call(
                    dataset,
                    "diagnose",
                    diagnosed_query,
                    ignore_cartesian_risk=False,
                    sample_limit=rows_limit if is_select else 0,
                    expand_nonempty_results=False,
                )
        except RuntimeError as exc:
            diagnosis_error = str(exc)

    if report is not None and connect:
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
    elif report is not None:
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
        if is_select:
            # diagnose already ran the query in full to count it, so its sample *is* the result.
            sampled = (report.sample_variables, report.sample_rows)

    result: dict[str, Any]
    if sampled is not None:
        variables, raw_rows = sampled
        result = {
            "form": "solutions",
            "variables": variables,
            "rows": [
                {var: _term_to_json(term, prefixes, used_prefixes) for var, term in zip(variables, row)}
                for row in raw_rows
            ],
        }
    else:
        executed: QueryResult = worker.call(dataset, "query", query, row_limit=rows_limit)
        if executed.form == "boolean":
            result = {"form": "boolean", "result": executed.boolean}
        elif executed.form == "solutions":
            result = {
                "form": "solutions",
                "variables": executed.variables,
                "rows": [
                    {var: _term_to_json(term, prefixes, used_prefixes) for var, term in row.items()}
                    for row in executed.bindings
                ],
            }
        else:
            result = {
                "form": "graph",
                "triples": [
                    {
                        "subject": _term_to_json(s, prefixes, used_prefixes),
                        "predicate": _term_to_json(p, prefixes, used_prefixes),
                        "object": _term_to_json(o, prefixes, used_prefixes),
                    }
                    for s, p, o in (executed.triples or [])
                ],
            }

    cartesian_risks_skipped = [
        {"triples": [_abbrev(t) for t in r.triples], "depth": r.depth} for r in cartesian_risks
    ]
    row_count = report.original_row_count if report is not None else None
    fixed_culprit_count = sum(1 for c in culprits if c["suggested_fixes"])

    if report is None:
        ok = diagnosis_error is None
        if diagnosed_query is None:
            message = "Query executed; it has no WHERE pattern to diagnose."
        else:
            message = f"Query executed, but couldn't be diagnosed ({diagnosis_error}) -- check its results yourself."
    else:
        ok = report.original_row_count > 0 and not culprits and not filter_issues
        if ok:
            message = f"Query's pattern matched {report.original_row_count} row(s) with no issues found."
            if result["form"] != "boolean" and row_limit is not None and report.original_row_count > row_limit:
                message += f" Only {row_limit} returned (row_limit) -- raise it, or pass null, if you need more."
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
                "Query's pattern matched 0 rows and no broken triple/filter could be isolated, but "
                f"{len(cartesian_risks_skipped)} combination(s) were skipped rather than checked (see "
                "`cartesian_risks_skipped`) to avoid materializing a full cross product -- the real "
                "culprit may be among them."
            )
        else:
            message = (
                "Query's pattern matched 0 rows and no single broken triple/filter could be isolated -- the "
                "issue may be structural (e.g. two jointly-broken triples beyond the search depth, or "
                "an unbound variable) rather than one clear culprit."
            )

    return {
        "ok": ok,
        **result,
        "row_count": row_count,
        "culprits": culprits,
        "filter_issues": filter_issues,
        "cartesian_risks_skipped": cartesian_risks_skipped,
        "diagnosis_error": diagnosis_error,
        "prefixes": {p: prefixes[p] for p in sorted(used_prefixes)},
        "message": message,
    }


def query(dataset: str, query: str, row_limit: Optional[int] = 3) -> dict[str, Any]:
    """Run any SPARQL query (SELECT/ASK/CONSTRUCT/DESCRIBE) against `dataset` in-process and
    return its actual results, with no diagnosis.

    No longer exposed as an MCP tool -- `run_query` covers everything this does (all four query
    forms, any `row_limit`) plus the diagnosis, and runs behind the watchdog. Kept as a plain
    function for direct Python callers.

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


# ==============================================================================
#  TERM RESOLUTION (shared by traverse/search)
# ==============================================================================

RDFS_SUBCLASS_OF_URI = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
XSD_STRING_URI = "http://www.w3.org/2001/XMLSchema#string"
RDF_LANGSTRING_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"

_CURIE_RE = re.compile(r"^([A-Za-z_][\w.-]*)?:(.*)$")
_IRI_FORBIDDEN_RE = re.compile(r'[\s<>"{}|\\^`]')


def _resolve_term(text: str, prefixes: dict[str, str]) -> str:
    """Turns a term an agent wrote -- `prefix:local` (using the dataset's own
    prefixes plus defaults, same as every tool's output), `<full uri>`, a bare full
    URI, or `a` for `rdf:type` -- into the full URI to put in a query. Raises on an
    unknown prefix rather than treating it as a URI scheme, since a mistyped prefix
    (`brik:`) would otherwise silently match nothing."""
    text = text.strip()
    if text == "a":
        return RDF_TYPE_URI
    if text.startswith("<") and text.endswith(">"):
        uri = text[1:-1]
    else:
        match = _CURIE_RE.match(text)
        prefix = (match.group(1) or "") if match else None
        if match and prefix in prefixes:
            uri = prefixes[prefix] + match.group(2)
        elif "://" in text or text.startswith("urn:"):
            uri = text
        else:
            raise ValueError(
                f"can't resolve {text!r} to a URI: use prefix:local with a prefix the dataset declares "
                "(or a common default like brick:, s223:, rdfs:), or a full URI"
            )
    if not uri or _IRI_FORBIDDEN_RE.search(uri):
        raise ValueError(f"{text!r} isn't a valid URI")
    return uri


def _literal_display(term: Term, prefixes: dict[str, str], used: set[str]) -> str:
    """Renders a literal compactly in Turtle-like form: `"text"`, `"text"@en`, or
    `"72.5"^^xsd:double` -- plain strings get no datatype, matching how they're
    usually written."""
    text = '"' + term.value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if term.language:
        return f"{text}@{term.language}"
    if term.datatype and term.datatype not in (XSD_STRING_URI, RDF_LANGSTRING_URI):
        curie, prefix = _uri_to_curie(term.datatype, prefixes)
        if prefix is not None:
            used.add(prefix)
            return f"{text}^^{curie}"
        return f"{text}^^<{curie}>"
    return text


# ==============================================================================
#  TRAVERSE
# ==============================================================================
#
# SPARQL property paths (`?x rdfs:subClassOf* ?y`, `?a brick:feeds+ ?b`) answer
# "what's reachable", but flatten it into pairs: no depth, no record of which edge
# reached which node, and no sense of the shape. For a taxonomy with multiple
# inheritance (Brick has plenty), or an HVAC `feeds` graph with loops, that shape is
# exactly what an agent exploring the graph wants. `traverse` does a breadth-first
# walk and returns the reachable subgraph as a DAG grouped by depth: each node
# appears once, at the depth it was first reached, with *every* edge from an
# expanded node into it listed in `via` -- so multiple parents show up as multiple
# `via` entries rather than as duplicated paths (whose count can grow
# combinatorially), and cycles terminate naturally since a visited node is never
# expanded twice. Each depth is one SPARQL query with the whole frontier in a
# `VALUES` block, not one query per node.

TRAVERSE_EDGE_LIMIT_FACTOR = 10
"""Per-level cap on edges fetched, as a multiple of `max_nodes` -- enough headroom
that edges into already-seen nodes (multiple parents, cycles) don't crowd out new
ones in the common case, while still bounding a hub node (say, `brick:Point`
walked `incoming` over `rdf:type`, with thousands of instances)."""


def _traverse_step_query(frontier: list[str], direction: str, predicate_uris: Optional[list[str]], limit: Optional[int]) -> str:
    values = " ".join(f"<{u}>" for u in frontier)
    pred_values = f"VALUES ?p {{ {' '.join(f'<{u}>' for u in predicate_uris)} }} " if predicate_uris else ""
    pattern = "?src ?p ?dst" if direction == "outgoing" else "?dst ?p ?src"
    limit_clause = f" LIMIT {limit}" if limit is not None else ""
    return f"SELECT ?src ?p ?dst WHERE {{ VALUES ?src {{ {values} }} {pred_values}{pattern} . }} ORDER BY ?src ?p ?dst{limit_clause}"


def traverse(
    dataset: str,
    start: str,
    direction: Literal["outgoing", "incoming"] = "outgoing",
    predicates: Optional[list[str]] = None,
    max_depth: int = 3,
    max_nodes: int = 100,
) -> dict[str, Any]:
    """Walk the graph breadth-first from `start` and return what's reachable, level by level.

    `direction="outgoing"` follows `start pred ?next` edges; `"incoming"` follows `?next pred
    start`. `predicates` (e.g. `["rdfs:subClassOf"]`, `["brick:feeds"]`) restricts which edges are
    followed; omit it to follow every predicate. For a taxonomy: outgoing `rdfs:subClassOf` walks
    up to superclasses, incoming walks down to subclasses.

    Each node appears once, at the depth first reached, with `via` listing every
    `[previous_node, predicate]` edge that reaches it -- several entries mean several parents.
    Literals are leaves; blank nodes are skipped. `truncated` means `max_nodes` or the per-level
    edge cap was hit; `more_beyond_max_depth` means the last level has further edges.
    """
    store = _require_dataset(dataset)
    prefixes = _datasets[dataset].prefixes
    used_prefixes: set[str] = set()
    if direction not in ("outgoing", "incoming"):
        raise ValueError("direction must be 'outgoing' or 'incoming'")
    if max_depth < 1 or max_nodes < 1:
        raise ValueError("max_depth and max_nodes must both be at least 1")
    start_uri = _resolve_term(start, prefixes)
    predicate_uris = [_resolve_term(p, prefixes) for p in predicates] if predicates else None

    def _display(term: Term) -> str:
        if term.kind == "literal":
            return _literal_display(term, prefixes, used_prefixes)
        curie, prefix = _uri_to_curie(term.value, prefixes)
        if prefix is not None:
            used_prefixes.add(prefix)
        return curie

    start_display, start_prefix = _uri_to_curie(start_uri, prefixes)
    if start_prefix is not None:
        used_prefixes.add(start_prefix)

    # Keyed by display form: CURIEs/URIs and literals (which always start with `"`)
    # can't collide, and two distinct URIs never abbreviate to the same CURIE.
    depth_of: dict[str, int] = {start_display: 0}
    via: dict[str, list[list[str]]] = {start_display: []}
    order: list[str] = [start_display]
    frontier = [start_uri]
    truncated = False
    blank_node_edges_skipped = 0
    edge_limit = max_nodes * TRAVERSE_EDGE_LIMIT_FACTOR

    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        result = store.query(_traverse_step_query(frontier, direction, predicate_uris, edge_limit + 1))
        rows = result.rows
        if len(rows) > edge_limit:
            truncated = True
            rows = rows[:edge_limit]
        next_frontier: list[str] = []
        for src, pred, dst in rows:
            if dst.kind == "bnode":
                blank_node_edges_skipped += 1
                continue
            key = _display(dst)
            if key not in depth_of:
                if len(order) >= max_nodes:
                    truncated = True
                    continue
                depth_of[key] = depth
                via[key] = []
                order.append(key)
                if dst.kind == "uri":
                    next_frontier.append(dst.value)
            via[key].append([_display(src), _display(pred)])
        frontier = next_frontier

    more_beyond_max_depth = False
    if frontier:
        probe = store.query(_traverse_step_query(frontier, direction, predicate_uris, 1))
        more_beyond_max_depth = bool(probe.rows)

    levels: list[dict[str, Any]] = []
    for key in order:
        depth = depth_of[key]
        if depth == len(levels):
            levels.append({"depth": depth, "nodes": []})
        levels[depth]["nodes"].append({"node": key, "via": via[key]})

    node_count = len(order) - 1
    pred_note = f" via {', '.join(predicates)}" if predicates else ""
    if node_count == 0:
        other = "incoming" if direction == "outgoing" else "outgoing"
        message = (
            f"No {direction} edges{pred_note} from {start_display}. Check the start term (search can find it), "
            f"or try direction='{other}'."
        )
    else:
        message = f"Reached {node_count} node(s){pred_note} across {len(levels) - 1} level(s)."
        if truncated:
            message += " Truncated -- narrow `predicates` or raise `max_nodes` to see more."
        if more_beyond_max_depth:
            message += " More levels exist beyond max_depth -- raise it, or traverse again from a last-level node."

    return {
        "start": start_display,
        "direction": direction,
        "predicates": [_display_curie(u, prefixes, used_prefixes) for u in predicate_uris] if predicate_uris else None,
        "levels": levels,
        "node_count": node_count,
        "truncated": truncated,
        "more_beyond_max_depth": more_beyond_max_depth,
        "blank_node_edges_skipped": blank_node_edges_skipped,
        "prefixes": {p: prefixes[p] for p in sorted(used_prefixes)},
        "message": message,
    }


def _display_curie(uri: str, prefixes: dict[str, str], used: set[str]) -> str:
    curie, prefix = _uri_to_curie(uri, prefixes)
    if prefix is not None:
        used.add(prefix)
    return curie


# ==============================================================================
#  SEARCH
# ==============================================================================
#
# Nothing else here answers "what's the URI for X?" -- `summarize_schema` shows the
# graph's shape and `run_query` needs terms the agent already knows. `search`
# indexes every named (non-blank) node once per dataset as a small text document:
# its local name split into words (`Supply_Air_Temperature_Sensor`, `hasPoint`,
# `AHU01` -> supply air temperature sensor / has point / ahu 01 -- without this
# split BM25 matches almost nothing in building graphs, whose names are mostly
# compound identifiers), the local names of its `rdf:type`s, and every short string
# literal attached to it (labels, comments, definitions, BACnet object names, ...).
# The local name counts double, since it's the most reliable signal of what a node
# is. BM25 is implemented inline -- it's a few lines of arithmetic, not worth a
# dependency.
#
# Each node also gets a `kinds` set: `class` (used as an `rdf:type` object, on
# either side of `rdfs:subClassOf`, or typed owl:Class/rdfs:Class), `predicate`
# (used in predicate position, or typed as a property), else `instance`.

BM25_K1 = 1.5
BM25_B = 0.75
SEARCH_MAX_LITERAL_CHARS = 1000
"""String literals longer than this aren't indexed -- long blobs (embedded
documents, serialized JSON) would dominate a node's document length and add noise,
not findability."""

_CLASS_TYPE_URIS = {
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/2000/01/rdf-schema#Class",
}
_PROPERTY_TYPE_URIS = {
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
    "http://www.w3.org/2002/07/owl#ObjectProperty",
    "http://www.w3.org/2002/07/owl#DatatypeProperty",
    "http://www.w3.org/2002/07/owl#AnnotationProperty",
}
_LABEL_PREDICATE_URIS = (
    "http://www.w3.org/2000/01/rdf-schema#label",
    "http://www.w3.org/2004/02/skos/core#prefLabel",
)

_WORD_CHUNK_RE = re.compile(r"[^\W_]+")
_CAMEL_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _tokenize(text: str) -> list[str]:
    """Lowercased word tokens, splitting on punctuation/underscores *and* camelCase/
    letter-digit boundaries. Chunks the camelCase regex can't handle (non-ASCII
    words) are kept whole rather than dropped."""
    tokens: list[str] = []
    for chunk in _WORD_CHUNK_RE.findall(text):
        parts = _CAMEL_TOKEN_RE.findall(chunk)
        if "".join(parts) == chunk:
            tokens.extend(part.lower() for part in parts)
        else:
            tokens.append(chunk.lower())
    return tokens


@dataclass
class _SearchEntry:
    uri: str
    kinds: set[str] = field(default_factory=set)
    types: list[str] = field(default_factory=list)
    label: Optional[str] = None
    texts: list[str] = field(default_factory=list)


@dataclass
class _SearchIndex:
    entries: list[_SearchEntry]
    postings: dict[str, list[tuple[int, int]]]
    """token -> [(entry index, term frequency in that entry's document)]"""
    doc_lengths: list[int]
    avg_doc_length: float


def _build_search_index(store: Store) -> _SearchIndex:
    by_uri: dict[str, _SearchEntry] = {}

    def _entry(uri: str) -> _SearchEntry:
        entry = by_uri.get(uri)
        if entry is None:
            entry = by_uri[uri] = _SearchEntry(uri=uri)
        return entry

    result = store.query("SELECT ?s ?p ?o WHERE { ?s ?p ?o }", timeout=120.0)
    labels: dict[str, tuple[int, str]] = {}
    for s, p, o in result.rows:
        if p is None or o is None or s is None:
            continue
        _entry(p.value).kinds.add("predicate")
        if s.kind != "uri":
            continue
        subject = _entry(s.value)
        if o.kind == "uri":
            obj = _entry(o.value)
            if p.value == RDF_TYPE_URI:
                obj.kinds.add("class")
                subject.types.append(o.value)
                if o.value in _CLASS_TYPE_URIS:
                    subject.kinds.add("class")
                elif o.value in _PROPERTY_TYPE_URIS:
                    subject.kinds.add("predicate")
            elif p.value == RDFS_SUBCLASS_OF_URI:
                subject.kinds.add("class")
                obj.kinds.add("class")
        elif o.kind == "literal" and o.datatype in (None, XSD_STRING_URI, RDF_LANGSTRING_URI):
            if len(o.value) <= SEARCH_MAX_LITERAL_CHARS:
                subject.texts.append(o.value)
            if p.value in _LABEL_PREDICATE_URIS:
                # Prefer rdfs:label over skos:prefLabel, and an English/untagged label over others.
                rank = _LABEL_PREDICATE_URIS.index(p.value) * 2 + (0 if o.language in (None, "en") else 1)
                if s.value not in labels or rank < labels[s.value][0]:
                    labels[s.value] = (rank, o.value)

    entries = sorted(by_uri.values(), key=lambda e: e.uri)
    postings: dict[str, list[tuple[int, int]]] = {}
    doc_lengths: list[int] = []
    for idx, entry in enumerate(entries):
        if not entry.kinds:
            entry.kinds.add("instance")
        if entry.uri in labels:
            entry.label = labels[entry.uri][1]
        name_tokens = _tokenize(_local_name(entry.uri))
        tokens = name_tokens * 2
        for type_uri in entry.types:
            tokens.extend(_tokenize(_local_name(type_uri)))
        for text in entry.texts:
            tokens.extend(_tokenize(text))
        counts = Counter(tokens)
        for token, tf in counts.items():
            postings.setdefault(token, []).append((idx, tf))
        doc_lengths.append(len(tokens))
    avg = (sum(doc_lengths) / len(doc_lengths)) if doc_lengths else 0.0
    return _SearchIndex(entries=entries, postings=postings, doc_lengths=doc_lengths, avg_doc_length=avg)


def _get_search_index(dataset: str) -> _SearchIndex:
    store = _require_dataset(dataset)
    index = _search_indexes.get(dataset)
    if index is None:
        index = _search_indexes[dataset] = _build_search_index(store)
    return index


def _bm25_scores(index: _SearchIndex, text: str) -> dict[int, float]:
    n_docs = len(index.entries)
    scores: dict[int, float] = {}
    for token in set(_tokenize(text)):
        postings = index.postings.get(token)
        if not postings:
            continue
        idf = math.log((n_docs - len(postings) + 0.5) / (len(postings) + 0.5) + 1.0)
        for idx, tf in postings:
            norm = BM25_K1 * (1 - BM25_B + BM25_B * index.doc_lengths[idx] / (index.avg_doc_length or 1.0))
            scores[idx] = scores.get(idx, 0.0) + idf * tf * (BM25_K1 + 1) / (tf + norm)
    return scores


def search(
    dataset: str,
    text: str,
    mode: Literal["bm25", "regex"] = "bm25",
    kind: Literal["any", "class", "predicate", "instance"] = "any",
    limit: int = 10,
) -> dict[str, Any]:
    """Find nodes in `dataset` by keyword or regex -- use it to get the URI for a concept
    before writing SPARQL against it.

    `mode="bm25"` (default) ranks nodes by keyword relevance over their local names (split into
    words, so "supply air temp" matches `Supply_Air_Temperature_Sensor`), their types' names, and
    their string literals (labels, comments, definitions). `mode="regex"` matches a Python regex
    against each node's full URI, CURIE, and string literals (case-sensitive; prefix `(?i)` to
    ignore case); `total_matches` counts every regex match, not just the `limit` returned. `kind`
    restricts results to classes, predicates, or instances. Follow up with `traverse` to explore
    a hit.
    """
    index = _get_search_index(dataset)
    prefixes = _datasets[dataset].prefixes
    used_prefixes: set[str] = set()
    if limit < 1:
        raise ValueError("limit must be at least 1")

    def _keep(entry: _SearchEntry) -> bool:
        return kind == "any" or kind in entry.kinds

    matched_text: dict[int, str] = {}
    if mode == "bm25":
        scores = _bm25_scores(index, text)
        ranked = sorted(
            (idx for idx in scores if _keep(index.entries[idx])),
            key=lambda idx: (-scores[idx], index.entries[idx].uri),
        )
    elif mode == "regex":
        try:
            pattern = re.compile(text)
        except re.error as exc:
            raise ValueError(f"invalid regex {text!r}: {exc}") from exc
        name_hits: list[int] = []
        text_hits: list[int] = []
        for idx, entry in enumerate(index.entries):
            if not _keep(entry):
                continue
            curie, _ = _uri_to_curie(entry.uri, prefixes)
            if pattern.search(entry.uri) or pattern.search(curie):
                name_hits.append(idx)
                continue
            for literal in entry.texts:
                if pattern.search(literal):
                    matched_text[idx] = literal if len(literal) <= 120 else literal[:117] + "..."
                    text_hits.append(idx)
                    break
        ranked = name_hits + text_hits  # name matches first; each group is already in URI order
        scores = {}
    else:
        raise ValueError("mode must be 'bm25' or 'regex'")

    results = []
    for idx in ranked[:limit]:
        entry = index.entries[idx]
        hit: dict[str, Any] = {
            "uri": _display_curie(entry.uri, prefixes, used_prefixes),
            "kinds": sorted(entry.kinds),
            "types": [_display_curie(t, prefixes, used_prefixes) for t in sorted(set(entry.types))[:3]],
        }
        if entry.label is not None:
            hit["label"] = entry.label
        if mode == "bm25":
            hit["score"] = round(scores[idx], 3)
        elif idx in matched_text:
            hit["matched_text"] = matched_text[idx]
        results.append(hit)

    response: dict[str, Any] = {"results": results}
    if mode == "regex":
        # A BM25 "match" is any node sharing even one word with `text`, so a count of them
        # says nothing useful; a regex match count does (e.g. how many AHUs there are).
        response["total_matches"] = len(ranked)
    response["prefixes"] = {p: prefixes[p] for p in sorted(used_prefixes)}
    if not results:
        response["message"] = "No matches. Try other keywords or synonyms, a looser regex, or kind='any'."
    elif mode == "regex":
        response["message"] = f"{len(ranked)} match(es); showing {len(results)}."
    else:
        response["message"] = f"Top {len(results)} by keyword relevance."
    return response


# ==============================================================================
#  TOOLSETS
# ==============================================================================
#
# Every tool's description is sent to the agent on every turn, so each tool costs
# context whether or not it's used. The toolset picks which tools get registered:
# `core` is the original four; `extended` (the default) adds `search` and
# `traverse`. The server-level instructions change with it, so a `core` agent is
# never told about tools it doesn't have.

TOOLSETS: dict[str, tuple[Callable[..., Any], ...]] = {
    "core": (load_dataset, list_datasets, summarize_schema, run_query),
    "extended": (load_dataset, list_datasets, summarize_schema, run_query, search, traverse),
}
_TOOLSET_INSTRUCTIONS = {"core": _CORE_INSTRUCTIONS, "extended": _EXTENDED_INSTRUCTIONS}
DEFAULT_TOOLSET = "extended"
TOOLSET_ENV_VAR = "SPARQL_RELAX_TOOLSET"


def build_server(toolset: str = DEFAULT_TOOLSET) -> FastMCP:
    """A FastMCP server registering exactly `toolset`'s tools, with instructions to match.
    All servers built here share this module's loaded datasets and caches."""
    if toolset not in TOOLSETS:
        raise ValueError(f"unknown toolset {toolset!r}; choose one of: {', '.join(sorted(TOOLSETS))}")
    server = FastMCP(name="sparql-relax", instructions=_TOOLSET_INSTRUCTIONS[toolset])
    for fn in TOOLSETS[toolset]:
        server.add_tool(fn)
    return server


mcp = build_server()


def _parse_toolset(argv: Optional[list[str]] = None) -> str:
    """`--toolset` if given, else `$SPARQL_RELAX_TOOLSET`, else `DEFAULT_TOOLSET`."""
    parser = argparse.ArgumentParser(prog="sparql-relax-mcp", description="MCP server for exploring and debugging SPARQL/RDF graphs.")
    parser.add_argument(
        "--toolset",
        choices=sorted(TOOLSETS),
        default=None,
        help=f"which tools to expose (default: ${TOOLSET_ENV_VAR} if set, else {DEFAULT_TOOLSET!r}). "
        "'core' is load_dataset/list_datasets/summarize_schema/run_query; 'extended' adds search and traverse.",
    )
    args = parser.parse_args(argv)
    toolset = args.toolset or os.environ.get(TOOLSET_ENV_VAR) or DEFAULT_TOOLSET
    if toolset not in TOOLSETS:
        parser.error(f"${TOOLSET_ENV_VAR}={toolset!r} isn't a known toolset; choose one of: {', '.join(sorted(TOOLSETS))}")
    return toolset


def main(argv: Optional[list[str]] = None) -> None:
    toolset = _parse_toolset(argv)
    server = mcp if toolset == DEFAULT_TOOLSET else build_server(toolset)
    try:
        server.run(transport="stdio")
    finally:
        # `daemon=True` already ensures the worker (if any) dies with this
        # process even without this, but shutting it down explicitly first
        # gives it a chance to exit cleanly rather than being SIGKILL'd.
        if _diagnose_worker is not None:
            _diagnose_worker.shutdown()


if __name__ == "__main__":
    main()
