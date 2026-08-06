"""In-process tests for the MCP server: drives it through a real `ClientSession` over
in-memory transports (no subprocess, no stdio), exercising the same call_tool path a
real MCP client would use.
"""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from sparql_relax_mcp.server import _datasets, _schema_summaries, mcp

# Uses the Brick namespace (rather than an arbitrary made-up one) because diagnose's
# connection path search defaults to Brick/223P/RDFS/QUDT predicates only (see
# DEFAULT_CONNECT_NAMESPACES) -- a fix outside those namespaces would never be found,
# which would make test_diagnose_explains_a_broken_query_and_suggests_a_fix below
# fail for a reason unrelated to what it's actually checking.
TTL = """
@prefix ex: <https://brickschema.org/schema/Brick#> .
ex:building223 ex:hasPart ex:zone1 .
ex:zone1 ex:hasSensor ex:sensor1 .
ex:sensor1 a ex:TempSensor .
ex:sensor2 a ex:TempSensor .
"""

WORKING_QUERY = "PREFIX ex: <https://brickschema.org/schema/Brick#> SELECT ?s WHERE { ?s a ex:TempSensor }"
BROKEN_QUERY = """
PREFIX ex: <https://brickschema.org/schema/Brick#>
SELECT ?sensor WHERE {
    ex:building223 ex:hasSensor ?sensor .
    ?sensor a ex:TempSensor .
}
"""


@pytest.fixture(autouse=True)
def _clear_datasets():
    """Datasets are process-global module state; reset between tests so they don't leak."""
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
async def test_lists_all_five_tools():
    async with create_connected_server_and_client_session(mcp) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {"load_dataset", "list_datasets", "summarize_schema", "diagnose", "query"}


@pytest.mark.asyncio
async def test_load_then_list_datasets():
    async with create_connected_server_and_client_session(mcp) as client:
        loaded = _result_json(await client.call_tool("load_dataset", {"name": "b223", "data": TTL}))
        assert loaded["name"] == "b223"
        assert loaded["format"] == "turtle"
        assert loaded["triple_count"] == 4
        # TTL declares its own `ex:` prefix.
        assert loaded["declared_prefixes"]["ex"] == "https://brickschema.org/schema/Brick#"

        listed = _result_json(await client.call_tool("list_datasets", {}))
        assert listed["result"] == [{"name": "b223", "format": "turtle", "triple_count": 4}]


@pytest.mark.asyncio
async def test_load_dataset_rejects_both_data_and_path():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("load_dataset", {"name": "x", "data": TTL, "path": "/tmp/nonexistent.ttl"})
        assert result.isError


@pytest.mark.asyncio
async def test_query_without_loading_dataset_first_is_a_clear_error():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("query", {"dataset": "missing", "query": "SELECT * WHERE { ?s ?p ?o }"})
        assert result.isError
        text = "".join(block.text for block in result.content if block.type == "text")
        assert "no dataset named 'missing'" in text
        assert "load_dataset" in text


@pytest.mark.asyncio
async def test_summarize_schema_returns_class_graph_and_caches():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        summary = _result_json(await client.call_tool("summarize_schema", {"dataset": "b223"}))
        assert "bs:" in summary["class_graph"] or "urn:bschema#" in summary["class_graph"]
        assert isinstance(summary["compression_pct"], (int, float))
        assert isinstance(summary["iterations_run"], int)

        # Cached: a second call returns the exact same result without recomputing.
        again = _result_json(await client.call_tool("summarize_schema", {"dataset": "b223"}))
        assert again == summary

        # Reloading the dataset invalidates the cache for that name.
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        assert "b223" not in _schema_summaries


@pytest.mark.asyncio
async def test_summarize_schema_without_loading_dataset_first_is_a_clear_error():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("summarize_schema", {"dataset": "missing"})
        assert result.isError
        text = "".join(block.text for block in result.content if block.type == "text")
        assert "no dataset named 'missing'" in text
        assert "load_dataset" in text


@pytest.mark.asyncio
async def test_diagnose_reports_ok_on_a_working_query_then_query_fetches_full_results():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        diagnosis = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": WORKING_QUERY}))
        assert diagnosis["ok"] is True
        assert diagnosis["row_count"] == 2
        assert diagnosis["culprits"] == []
        assert diagnosis["filter_issues"] == []
        # sample_limit defaults to 3, so both of this query's rows come back for free.
        assert diagnosis["sample_variables"] == ["s"]
        # URIs come back as CURIEs (prefix:local), not full URIs, using the ex: prefix
        # declared in both the dataset and the query.
        assert {row["s"]["value"] for row in diagnosis["sample_rows"]} == {"ex:sensor1", "ex:sensor2"}
        assert diagnosis["prefixes"]["ex"] == "https://brickschema.org/schema/Brick#"

        result = _result_json(await client.call_tool("query", {"dataset": "b223", "query": WORKING_QUERY}))
        assert result["form"] == "solutions"
        assert result["variables"] == ["s"]
        values = {row["s"]["value"] for row in result["rows"]}
        assert values == {"ex:sensor1", "ex:sensor2"}
        assert all(row["s"]["type"] == "uri" for row in result["rows"])


@pytest.mark.asyncio
async def test_diagnose_sample_limit_caps_and_can_be_disabled():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        capped = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": WORKING_QUERY, "sample_limit": 1}))
        assert len(capped["sample_rows"]) == 1

        disabled = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": WORKING_QUERY, "sample_limit": 0}))
        assert disabled["sample_variables"] == []
        assert disabled["sample_rows"] == []


@pytest.mark.asyncio
async def test_diagnose_with_connect_true_has_no_sample_rows():
    # diagnose_and_connect doesn't support sample_limit, regardless of what's passed.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        diagnosis = _result_json(
            await client.call_tool("diagnose", {"dataset": "b223", "query": WORKING_QUERY, "connect": True, "sample_limit": 3})
        )
        assert diagnosis["sample_variables"] == []
        assert diagnosis["sample_rows"] == []


@pytest.mark.asyncio
async def test_diagnose_reports_a_culprit_but_no_fix_by_default():
    # `connect` defaults to False: diagnosis is nearly free and always run, but the
    # (comparatively expensive, experimental) connection search only runs when asked
    # for explicitly -- see the `connect` docs on the `diagnose` tool.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        diagnosis = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": BROKEN_QUERY}))
        assert diagnosis["ok"] is False
        assert diagnosis["row_count"] == 0
        assert len(diagnosis["culprits"]) == 1

        culprit = diagnosis["culprits"][0]
        assert culprit["triples"][0]["triple"] == "ex:building223 ex:hasSensor ?sensor"
        assert culprit["fixed"] is False
        assert culprit["connected_query"] is None
        assert culprit["row_count_with_fix"] is None
        # TTL has no other namespace/casing for "hasSensor" to suggest -- nothing to find.
        assert culprit["suggested_fixes"] == []


@pytest.mark.asyncio
async def test_diagnose_with_connect_true_suggests_a_fix():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        diagnosis = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": BROKEN_QUERY, "connect": True}))
        assert diagnosis["ok"] is False
        assert diagnosis["row_count"] == 0
        assert len(diagnosis["culprits"]) == 1

        culprit = diagnosis["culprits"][0]
        assert culprit["triples"][0]["triple"] == "ex:building223 ex:hasSensor ?sensor"
        assert culprit["fixed"] is True
        assert culprit["connected_query"] is not None
        assert culprit["row_count_with_fix"] > 0

        # connected_query is abbreviated to CURIEs but still directly runnable: it
        # carries its own PREFIX lines rather than relying on the caller to supply them.
        assert "PREFIX" in culprit["connected_query"]

        # The suggested connected_query should itself actually work via `query`.
        fixed = _result_json(await client.call_tool("query", {"dataset": "b223", "query": culprit["connected_query"]}))
        assert fixed["form"] == "solutions"
        assert len(fixed["rows"]) == culprit["row_count_with_fix"]


# s223:Zone exists; queries below deliberately use the wrong namespace (rec:) or the
# wrong case (s223:zone) for the same local name, to exercise diagnose's
# suggest_fixes -- both are common real mistakes an agent unfamiliar with the
# graph's exact schema would make.
NAMESPACE_TTL = """
@prefix s223: <http://data.ashrae.org/standard223#> .
s223:zone1 a s223:Zone .
s223:zone2 a s223:Zone .
"""
WRONG_NAMESPACE_QUERY = """
PREFIX rec: <https://w3id.org/rec#>
SELECT ?z WHERE { ?z a rec:Zone . }
"""
WRONG_CASE_QUERY = """
PREFIX s223: <http://data.ashrae.org/standard223#>
SELECT ?z WHERE { ?z a s223:zone . }
"""


@pytest.mark.asyncio
async def test_diagnose_suggests_a_verified_wrong_namespace_fix():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "ns", "data": NAMESPACE_TTL})

        diagnosis = _result_json(await client.call_tool("diagnose", {"dataset": "ns", "query": WRONG_NAMESPACE_QUERY}))
        assert diagnosis["ok"] is False
        culprit = diagnosis["culprits"][0]
        assert len(culprit["suggested_fixes"]) == 1

        fix = culprit["suggested_fixes"][0]
        assert fix["kind"] == "wrong_namespace"
        assert fix["original_term"] == "rec:Zone"
        assert fix["replacement_term"] == "s223:Zone"
        assert fix["row_count_with_fix"] == 2
        assert "PREFIX s223:" in fix["fixed_query"]

        # The fix is verified, not just guessed: rerunning fixed_query for real
        # returns exactly what it claims.
        rerun = _result_json(await client.call_tool("query", {"dataset": "ns", "query": fix["fixed_query"], "row_limit": None}))
        assert len(rerun["rows"]) == fix["row_count_with_fix"]


@pytest.mark.asyncio
async def test_diagnose_falls_back_to_a_case_typo_fix_when_no_namespace_match_exists():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "ns", "data": NAMESPACE_TTL})

        diagnosis = _result_json(await client.call_tool("diagnose", {"dataset": "ns", "query": WRONG_CASE_QUERY}))
        assert diagnosis["ok"] is False
        culprit = diagnosis["culprits"][0]
        assert len(culprit["suggested_fixes"]) == 1

        fix = culprit["suggested_fixes"][0]
        assert fix["kind"] == "local_name_typo"
        assert fix["original_term"] == "s223:zone"
        assert fix["replacement_term"] == "s223:Zone"
        assert fix["row_count_with_fix"] == 2


@pytest.mark.asyncio
async def test_diagnose_suggest_fixes_false_disables_the_search():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "ns", "data": NAMESPACE_TTL})

        diagnosis = _result_json(
            await client.call_tool("diagnose", {"dataset": "ns", "query": WRONG_NAMESPACE_QUERY, "suggest_fixes": False})
        )
        assert diagnosis["culprits"][0]["suggested_fixes"] == []


# sensor1's value fails the FILTER, sensor2's passes it, so this query already returns
# one row (sensor2) even though the FILTER is quietly excluding sensor1's.
VALUE_TTL = """
@prefix ex: <https://brickschema.org/schema/Brick#> .
ex:zone1 ex:hasSensor ex:sensor1 .
ex:sensor1 ex:hasValue 72 .
ex:zone1 ex:hasSensor ex:sensor2 .
ex:sensor2 ex:hasValue 2000 .
"""
NARROWED_QUERY = """
PREFIX ex: <https://brickschema.org/schema/Brick#>
SELECT ?sensor ?value WHERE {
    ex:zone1 ex:hasSensor ?sensor .
    ?sensor ex:hasValue ?value .
    FILTER(?value > 1000)
}
"""


@pytest.mark.asyncio
async def test_expand_nonempty_results_defaults_to_skipping_the_search():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "vals", "data": VALUE_TTL})

        skipped = _result_json(await client.call_tool("diagnose", {"dataset": "vals", "query": NARROWED_QUERY}))
        assert skipped["row_count"] == 1
        assert skipped["ok"] is True
        assert skipped["filter_issues"] == []

        expanded = _result_json(
            await client.call_tool("diagnose", {"dataset": "vals", "query": NARROWED_QUERY, "expand_nonempty_results": True})
        )
        assert expanded["row_count"] == 1
        assert expanded["ok"] is False
        assert len(expanded["filter_issues"]) == 1
        assert expanded["filter_issues"][0]["row_count_without_filter"] == 2


@pytest.mark.asyncio
async def test_query_row_limit_caps_solutions():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = _result_json(await client.call_tool("query", {"dataset": "b223", "query": WORKING_QUERY, "row_limit": 1}))
        assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_query_default_row_limit_is_three():
    ttl = TTL + "\n".join(f"ex:sensor{i} a ex:TempSensor ." for i in range(3, 8))
    query_all_sensors = "PREFIX ex: <https://brickschema.org/schema/Brick#> SELECT ?s WHERE { ?s a ex:TempSensor }"
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": ttl})
        result = _result_json(await client.call_tool("query", {"dataset": "b223", "query": query_all_sensors}))
        assert len(result["rows"]) == 3


@pytest.mark.asyncio
async def test_query_ask_and_construct_forms():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        ask = _result_json(await client.call_tool("query", {"dataset": "b223", "query": "PREFIX ex: <https://brickschema.org/schema/Brick#> ASK { ex:sensor1 a ex:TempSensor }"}))
        assert ask == {"form": "boolean", "result": True}

        construct = _result_json(
            await client.call_tool(
                "query",
                {
                    "dataset": "b223",
                    "query": "PREFIX ex: <https://brickschema.org/schema/Brick#> CONSTRUCT { ?s a ex:Thing } WHERE { ?s a ex:TempSensor }",
                },
            )
        )
        assert construct["form"] == "graph"
        assert len(construct["triples"]) == 2


@pytest.mark.asyncio
async def test_diagnose_rejects_non_select_queries():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = await client.call_tool("diagnose", {"dataset": "b223", "query": "ASK { ?s ?p ?o }"})
        assert result.isError


@pytest.mark.asyncio
async def test_replacing_a_dataset_is_reflected_in_diagnose_not_served_stale():
    # diagnose routes through a persistent forked worker (see DiagnoseWorker in
    # server.py) that's only supposed to be replaced -- picking up the new data --
    # when load_dataset changes something. This is the test for that wiring: if
    # load_dataset forgot to invalidate the worker, the second diagnose call below
    # would still see the first fork's copy of "b223" (2 TempSensors) instead of
    # the replacement (1).
    replacement_ttl = """
    @prefix ex: <https://brickschema.org/schema/Brick#> .
    ex:sensor3 a ex:TempSensor .
    """
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        first = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": WORKING_QUERY}))
        assert first["row_count"] == 2  # forces the worker to actually fork with the original data loaded

        await client.call_tool("load_dataset", {"name": "b223", "data": replacement_ttl})
        second = _result_json(await client.call_tool("diagnose", {"dataset": "b223", "query": WORKING_QUERY}))
        assert second["row_count"] == 1
