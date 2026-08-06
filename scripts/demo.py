#!/usr/bin/env python3
"""Calls each `sparql_relax_mcp` tool in turn and pretty-prints its input and output,
so you can eyeball what an agent actually sees -- CURIEs instead of full URIs, the
`prefixes` legend, a `diagnose --connect` fix that's still directly runnable, a
verified `suggest_fixes` namespace correction, etc.

Calls the tool functions directly (they're plain Python functions under the
`@mcp.tool()` decorator -- see server.py) rather than going through a real MCP
client/transport, since the point here is just to look at the return values.

Usage:
    uv run python scripts/demo.py                    # tiny built-in sample graph
    uv run python scripts/demo.py path/to/graph.ttl   # a real graph instead

Long string fields (e.g. summarize_schema's class_graph on a big graph) are
truncated in the printed output only -- the tools themselves return the full text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from sparql_relax_mcp import server as s

SAMPLE_TTL = """
@prefix s223: <http://data.ashrae.org/standard223#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

s223:zone1 a s223:Zone ;
    rdfs:label "Zone 1" ;
    s223:hasProperty s223:temp1 .
s223:zone2 a s223:Zone ;
    rdfs:label "Zone 2" ;
    s223:hasProperty s223:temp2 .
s223:temp1 a s223:QuantifiableObservableProperty .
s223:temp2 a s223:QuantifiableObservableProperty .
"""
"""A tiny graph with just enough repeated structure (two Zones, each with one
QuantifiableObservableProperty) for summarize_schema to show a real compression,
and no brick: data at all -- so the brick:hasPoint query below reliably breaks,
demonstrating diagnose's culprit output regardless of what graph you point this at."""

TRUNCATE_AT = 700


def _truncate(obj: Any) -> Any:
    if isinstance(obj, str):
        if len(obj) > TRUNCATE_AT:
            return obj[:TRUNCATE_AT] + f"\n... [truncated -- {len(obj)} chars total]"
        return obj
    if isinstance(obj, dict):
        return {k: _truncate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate(v) for v in obj]
    return obj


def _call(label: str, fn, **kwargs: Any) -> dict:
    args_preview = ", ".join(
        f"{k}={kwargs[k]!r}" if k != "data" else f"data=<{len(kwargs[k])} chars of inline Turtle>" for k in kwargs
    )
    print(f"\n{'=' * 88}\n{label}({args_preview})\n{'=' * 88}")
    result = fn(**kwargs)
    print(json.dumps(_truncate(result), indent=2))
    return result


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).resolve()
        _call("load_dataset", s.load_dataset, name="demo", path=str(path))
    else:
        _call("load_dataset", s.load_dataset, name="demo", data=SAMPLE_TTL)

    _call("summarize_schema", s.summarize_schema, dataset="demo")

    working_query = "PREFIX s223: <http://data.ashrae.org/standard223#> SELECT ?zone WHERE { ?zone a s223:Zone }"
    _call("diagnose (working query)", s.diagnose, dataset="demo", query=working_query)

    broken_query = (
        "PREFIX s223: <http://data.ashrae.org/standard223#>\n"
        "PREFIX brick: <https://brickschema.org/schema/Brick#>\n"
        "SELECT ?zone ?sensor WHERE { ?zone a s223:Zone . ?zone brick:hasPoint ?sensor . }"
    )
    _call("diagnose (broken query)", s.diagnose, dataset="demo", query=broken_query)
    _call("diagnose (broken query, connect=True)", s.diagnose, dataset="demo", query=broken_query, connect=True)

    # brick:hasPoint above has no same-local-name match anywhere in the sample graph
    # (it's not a Brick graph), so suggest_fixes correctly finds nothing for it. This
    # one instead makes the exact mistake suggest_fixes targets: s223:Zone exists,
    # but the query used rec:Zone (right local name, wrong namespace).
    wrong_namespace_query = "PREFIX rec: <https://w3id.org/rec#> SELECT ?zone WHERE { ?zone a rec:Zone }"
    _call("diagnose (wrong namespace -- suggest_fixes)", s.diagnose, dataset="demo", query=wrong_namespace_query)

    _call("query", s.query, dataset="demo", query=working_query)


if __name__ == "__main__":
    main()
