"""Integration tests against real building graphs, not the tiny synthetic TTL
`test_server.py` uses -- real graphs have the messiness (many declared prefixes, a
dataset-specific namespace with no default binding, SHACL/ontology noise alongside
the instance data, even a wrong/nonstandard prefix declaration in one file) that
`test_server.py`'s 4-triple fixture can't exercise. These specifically guard the
CURIE-abbreviation behavior added so tool output doesn't confuse an agent with raw
URIs -- see DEFAULT_PREFIXES/_dataset_prefixes/_uri_to_curie in server.py.

Skipped entirely if the sibling BuildingQA checkout isn't present (e.g. a fresh
clone of just this repo) -- these files live in a separate repo and are too large
(1.5-16MB) to vendor copies of here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from sparql_relax_mcp.server import _datasets, _schema_summaries, mcp

EVAL_BUILDINGS_DIR = Path(__file__).resolve().parents[2] / "BuildingQA" / "eval_buildings"
B59 = EVAL_BUILDINGS_DIR / "b59.ttl"

pytestmark = pytest.mark.skipif(not B59.exists(), reason=f"sibling BuildingQA checkout not found at {EVAL_BUILDINGS_DIR}")

_FULL_URI_RE = re.compile(r"^https?://")


@pytest.fixture(autouse=True)
def _clear_datasets():
    _datasets.clear()
    _schema_summaries.clear()
    yield
    _datasets.clear()
    _schema_summaries.clear()


def _result_json(call_tool_result) -> dict:
    assert not call_tool_result.isError, call_tool_result.content
    assert call_tool_result.structuredContent is not None
    return call_tool_result.structuredContent


@pytest.mark.asyncio
async def test_query_on_b59_returns_curies_not_full_uris():
    async with create_connected_server_and_client_session(mcp) as client:
        loaded = _result_json(await client.call_tool("load_dataset", {"name": "b59", "path": str(B59)}))
        assert loaded["triple_count"] > 0
        # b59 declares s223/sh/rdfs/... itself; these should show up as declared,
        # without needing DEFAULT_PREFIXES at all.
        assert loaded["declared_prefixes"]["s223"] == "http://data.ashrae.org/standard223#"

        result = _result_json(
            await client.call_tool(
                "query",
                {
                    "dataset": "b59",
                    "query": "SELECT ?s ?p ?o WHERE { ?s ?p ?o . FILTER(isURI(?s) && isURI(?o)) }",
                    "row_limit": 25,
                },
            )
        )
        assert result["rows"], "expected at least one URI-URI-URI triple in b59"
        for row in result["rows"]:
            for term in row.values():
                if term["type"] == "uri":
                    assert not _FULL_URI_RE.match(term["value"]), f"expected a CURIE, got a full URI: {term['value']}"


@pytest.mark.asyncio
async def test_diagnose_on_b59_broken_query_abbreviates_culprit_and_connected_query():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b59", "path": str(B59)})

        # brick:hasPoint doesn't appear in b59 (it's an S223 graph, not Brick) --
        # a reliable way to force a culprit without depending on b59's exact instance data.
        diagnosis = _result_json(
            await client.call_tool(
                "diagnose",
                {
                    "dataset": "b59",
                    "query": (
                        "PREFIX s223: <http://data.ashrae.org/standard223#>\n"
                        "PREFIX brick: <https://brickschema.org/schema/Brick#>\n"
                        "SELECT ?zone ?sensor WHERE { ?zone a s223:Zone . ?zone brick:hasPoint ?sensor . }"
                    ),
                    "connect": True,
                },
            )
        )
        assert diagnosis["ok"] is False
        assert diagnosis["culprits"], "expected diagnose to isolate the brick:hasPoint triple as broken"

        culprit = diagnosis["culprits"][0]
        triple_text = culprit["triples"][0]["triple"]
        assert "brick:hasPoint" in triple_text
        assert "https://brickschema.org" not in triple_text

        fallback = culprit["fallback_query_with_broken_triples_removed"]
        assert fallback is not None
        assert "PREFIX s223:" in fallback  # abbreviated queries carry their own PREFIX lines
        assert "http://data.ashrae.org" not in fallback.split("\n", 10)[-1]  # body itself is abbreviated

        # brick:hasPoint has no same-local-name match anywhere in b59 (it's not a
        # Brick graph) -- suggest_fixes must not invent a fix that doesn't exist.
        assert culprit["suggested_fixes"] == []


@pytest.mark.asyncio
async def test_diagnose_suggests_a_verified_case_fix_on_b59():
    # b59 defines s223:Zone; a query that mis-cases it as s223:zone is exactly the
    # kind of mistake suggest_fixes exists for -- verified end to end on real data,
    # not just the tiny synthetic graph test_server.py uses.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b59", "path": str(B59)})

        diagnosis = _result_json(
            await client.call_tool(
                "diagnose",
                {
                    "dataset": "b59",
                    "query": "PREFIX s223: <http://data.ashrae.org/standard223#> SELECT ?z WHERE { ?z a s223:zone . }",
                },
            )
        )
        assert diagnosis["ok"] is False
        culprit = diagnosis["culprits"][0]
        assert len(culprit["suggested_fixes"]) == 1

        fix = culprit["suggested_fixes"][0]
        assert fix["kind"] == "local_name_typo"
        assert fix["replacement_term"] == "s223:Zone"
        assert fix["row_count_with_fix"] > 0

        rerun = _result_json(await client.call_tool("query", {"dataset": "b59", "query": fix["fixed_query"], "row_limit": None}))
        assert len(rerun["rows"]) == fix["row_count_with_fix"]


@pytest.mark.asyncio
async def test_summarize_schema_on_b59_binds_dataset_specific_namespace():
    # b59's own `ex1:` (a dataset-specific namespace with no DEFAULT_PREFIXES entry
    # and no bschema_rs default) should be preserved by name in the class_graph
    # rather than serialized under an arbitrary rdflib-assigned ns1:/ns2:.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b59", "path": str(B59)})
        summary = _result_json(await client.call_tool("summarize_schema", {"dataset": "b59"}))
        assert "ex1:" in summary["class_graph"]

        # b59 famously declares `ref:` pointing at a nonstandard namespace (a known
        # data quirk -- see bschema-rs/eval/remove_ontology.py's "Ref namespace wrong
        # in b59" note). The fill-in (from b59's own declared prefixes) must not let
        # that clobber bschema_rs's own correct `ref: .../Brick/ref#` binding.
        ref_binding = re.search(r"@prefix ref: <([^>]+)>", summary["class_graph"])
        if ref_binding is not None:
            assert ref_binding.group(1) == "https://brickschema.org/schema/Brick/ref#"
