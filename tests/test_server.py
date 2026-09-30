"""In-process tests for the MCP server: drives it through a real `ClientSession` over
in-memory transports (no subprocess, no stdio), exercising the same call_tool path a
real MCP client would use.
"""

from __future__ import annotations

import re

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from rdflib import RDF, Graph, URIRef
from rdflib.compare import isomorphic

from sparql_relax_mcp.server import (
    TOOLSET_ENV_VAR,
    _datasets,
    _parse_toolset,
    _schema_summaries,
    _remove_inferred_superclass_types,
    _strip_ontology,
    _subclass_hierarchy,
    TOOLSETS,
    build_server,
    mcp,
    traverse,
)

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


CORE_TOOLS = {"load_dataset", "list_datasets", "summarize_schema", "run_query"}


@pytest.mark.asyncio
async def test_default_server_is_extended_toolset():
    async with create_connected_server_and_client_session(mcp) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == CORE_TOOLS | {"search"}
    assert "use search" in mcp.instructions and "traverse" not in mcp.instructions


def test_traverse_is_deprecated_and_in_no_toolset():
    assert all(traverse not in tools for tools in TOOLSETS.values())


@pytest.mark.asyncio
async def test_core_toolset_has_only_the_original_four_tools_and_instructions():
    core = build_server("core")
    async with create_connected_server_and_client_session(core) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == CORE_TOOLS
    # A core agent shouldn't be told about tools it doesn't have.
    assert "use search" not in core.instructions and "traverse" not in core.instructions


def test_build_server_rejects_unknown_toolset():
    with pytest.raises(ValueError):
        build_server("everything")


def test_toolset_selection_flag_then_env_then_default(monkeypatch):
    monkeypatch.delenv(TOOLSET_ENV_VAR, raising=False)
    assert _parse_toolset([]) == "extended"
    monkeypatch.setenv(TOOLSET_ENV_VAR, "core")
    assert _parse_toolset([]) == "core"
    assert _parse_toolset(["--toolset", "extended"]) == "extended"
    monkeypatch.setenv(TOOLSET_ENV_VAR, "bogus")
    with pytest.raises(SystemExit):
        _parse_toolset([])


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
async def test_run_query_without_loading_dataset_first_is_a_clear_error():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("run_query", {"dataset": "missing", "query": "SELECT * WHERE { ?s ?p ?o }"})
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
async def test_summarize_schema_member_counts_is_opt_in():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        without = _result_json(await client.call_tool("summarize_schema", {"dataset": "b223"}))
        assert "member_counts" not in without

        with_counts = _result_json(
            await client.call_tool("summarize_schema", {"dataset": "b223", "include_member_counts": True})
        )
        # class_graph groups {building223, zone1} into one class and {sensor1, sensor2} into
        # another (both 1-hop-identical pairs) -- member_counts should report 2 members each.
        assert with_counts["member_counts"] == {c: 2 for c in with_counts["member_counts"]}
        assert len(with_counts["member_counts"]) == 2
        assert all(curie.startswith("bs:") for curie in with_counts["member_counts"])

        # The flag only adds a field -- it doesn't change anything else about the summary.
        assert with_counts["class_graph"] == without["class_graph"]
        assert with_counts["compression_pct"] == without["compression_pct"]


ONTOLOGY_DATA_TTL = """
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ex: <http://example.org/bldg#> .
ex:vav1 a brick:VAV ; brick:feeds ex:zone1 ; brick:hasExternalReference [ brick:id "vav-1" ] .
ex:vav2 a brick:VAV ; brick:feeds ex:zone2 ; brick:hasExternalReference [ brick:id "vav-2" ] .
ex:zone1 a brick:Zone .
ex:zone2 a brick:Zone .
ex:pump1 a brick:Pump .
ex:temp1 a brick:Temperature_Sensor ; brick:measures brick:Temperature .
ex:site1 a brick:Site .
"""

# Data (`ex:`) lives in its own namespace, as it does in real building graphs; the ontology
# (`brick:`, `tag:`) is what gets stripped. `ex:site1` is named by a shape's sh:targetNode and
# nothing else in the data, and `ex:`'s own owl:Ontology header is removed -- yet `ex:` stays,
# since its real data outnumbers both.
ONTOLOGY_TTL = """
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ex: <http://example.org/bldg#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix tag: <https://brickschema.org/schema/BrickTag#> .
ex: a owl:Ontology ; owl:imports <https://brickschema.org/schema/Brick> .
<https://brickschema.org/schema/Brick> a owl:Ontology ; owl:imports <http://qudt.org/schema/qudt> .
brick:VAV a owl:Class, sh:NodeShape ;
    rdfs:subClassOf [ a owl:Restriction ; owl:onProperty brick:feeds ; owl:someValuesFrom brick:Zone ] ;
    sh:property [ sh:path brick:feeds ; sh:in ( ex:zone1 ex:zone2 ) ] ;
    brick:hasAssociatedTag tag:VAV .
tag:VAV a brick:Tag ; rdfs:label "VAV" .
brick:Zone a owl:Class .
brick:Terminal_Unit a owl:Class .
brick:VAV rdfs:subClassOf brick:Terminal_Unit .
brick:feeds a owl:ObjectProperty ; rdfs:domain brick:VAV .
brick:Metaclass rdfs:subClassOf rdfs:Class .
brick:Pump a brick:Metaclass .
brick:Temperature a brick:Quantity ; rdfs:label "Temperature" .
brick:NumericValue sh:or ( [ sh:datatype brick:float ] [ sh:datatype brick:int ] ) .
brick:SiteShape a sh:NodeShape ; sh:targetNode ex:site1 .
"""


# What a reasoner adds on top of ONTOLOGY_DATA_TTL, given ONTOLOGY_TTL's brick:VAV rdfs:subClassOf
# brick:Terminal_Unit.
INFERRED_TYPES_TTL = """
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ex: <http://example.org/bldg#> .
ex:vav1 a brick:Terminal_Unit .
ex:vav2 a brick:Terminal_Unit .
"""


def test_remove_inferred_superclass_types_keeps_only_the_most_specific_type():
    graph = Graph().parse(
        data="""
        @prefix ex: <http://example.org/bldg#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        ex:VAV rdfs:subClassOf ex:Terminal_Unit . ex:Terminal_Unit rdfs:subClassOf ex:Equipment .
        ex:Pump rdfs:subClassOf ex:Pumpe . ex:Pumpe rdfs:subClassOf ex:Pump .
        ex:vav1 a ex:VAV, ex:Terminal_Unit, ex:Equipment, ex:Tagged .
        ex:pump1 a ex:Pump, ex:Pumpe .
        """,
        format="turtle",
    )
    removed = _remove_inferred_superclass_types(graph, _subclass_hierarchy(graph))

    # Both ancestors go (even the one two levels up); an unrelated type stays; and classes that
    # are each other's subclass (a cycle -- effectively equivalent) don't remove each other.
    assert removed == 2
    ex = "http://example.org/bldg#"
    assert {str(o) for o in graph.objects(URIRef(ex + "vav1"), RDF.type)} == {ex + "VAV", ex + "Tagged"}
    assert {str(o) for o in graph.objects(URIRef(ex + "pump1"), RDF.type)} == {ex + "Pump", ex + "Pumpe"}


def test_strip_ontology_leaves_exactly_the_instance_data():
    graph = Graph()
    graph.parse(data=ONTOLOGY_DATA_TTL + ONTOLOGY_TTL, format="turtle")
    expected = Graph()
    expected.parse(data=ONTOLOGY_DATA_TTL, format="turtle")

    original_size = len(graph)
    removed = _strip_ontology(graph)

    # Restriction/property-shape/list blank nodes go with their class, and the ontology's
    # individuals (tag:VAV, brick:Temperature) with its namespaces; the data's own
    # hasExternalReference blank nodes, and data that uses ontology terms, stay.
    assert removed == original_size - len(expected)
    assert isomorphic(graph, expected)


@pytest.mark.asyncio
async def test_summarize_schema_exclude_ontology_is_opt_in_and_cached_separately():
    async with create_connected_server_and_client_session(mcp) as client:
        bundled = ONTOLOGY_DATA_TTL + INFERRED_TYPES_TTL + ONTOLOGY_TTL
        await client.call_tool("load_dataset", {"name": "bundled", "data": bundled})
        await client.call_tool("load_dataset", {"name": "data_only", "data": ONTOLOGY_DATA_TTL})

        full = _result_json(await client.call_tool("summarize_schema", {"dataset": "bundled"}))
        assert "ontology_triples_removed" not in full
        assert "sh:NodeShape" in full["class_graph"]

        stripped = _result_json(
            await client.call_tool("summarize_schema", {"dataset": "bundled", "exclude_ontology": True})
        )
        assert stripped["ontology_triples_removed"] > 0
        assert "owl:" not in stripped["class_graph"] and "sh:" not in stripped["class_graph"]
        assert "Removed" in stripped["message"]
        data_only = _result_json(await client.call_tool("summarize_schema", {"dataset": "data_only"}))
        assert "inferred" not in stripped["message"] and "inferred_types_removed" not in stripped
        assert "brick:Terminal_Unit" not in stripped["class_graph"]
        assert stripped["compression_pct"] == data_only["compression_pct"]

        # Each flag value is cached on its own; the default call still returns the unstripped summary.
        assert set(_schema_summaries["bundled"]) == {False, True}
        again = _result_json(await client.call_tool("summarize_schema", {"dataset": "bundled"}))
        assert again == full


INSTANCE_NAME_TTL = """
@prefix brick: <https://brickschema.org/schema/Brick#> .
brick:RTU01 a brick:AHU .
brick:RTU02 a brick:AHU .
brick:RTU03 a brick:AHU .
brick:RTU04 a brick:AHU .
"""


@pytest.mark.asyncio
async def test_summarize_schema_class_names_are_synthetic_not_real_instance_names():
    # create_bschema is called with use_original_names=False: each class is named after its
    # members' shared rdf:type (e.g. bs:AHU_version_1), never after one arbitrary real instance's
    # own IRI local name (e.g. bs:RTU01) -- the latter reads exactly like real data and could be
    # mistaken for (or literally collide with) an actual entity in the graph.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "rtus", "data": INSTANCE_NAME_TTL})
        summary = _result_json(await client.call_tool("summarize_schema", {"dataset": "rtus"}))
        assert "bs:RTU01" not in summary["class_graph"]
        assert re.search(r"bs:AHU\w*\s+a\s+brick:AHU", summary["class_graph"])


# bschema_rs binds its own default "brick" prefix to an *unversioned* Brick URI; a dataset that
# declares a *versioned* one (as real Brick data commonly does) collides on prefix name but not
# namespace.
COLLIDING_BRICK_TTL = """
@prefix brick: <https://brickschema.org/schema/1.1/Brick#> .
brick:AHU1 a brick:AHU .
brick:AHU1 brick:feeds brick:VAV1 .
brick:VAV1 a brick:VAV .
brick:VAV2 a brick:VAV .
brick:AHU1 brick:feeds brick:VAV2 .
"""


@pytest.mark.asyncio
async def test_summarize_schema_does_not_collapse_a_colliding_prefix_to_nsN():
    # The fill-in loop must still bind the dataset's own (versioned) Brick namespace -- under a
    # distinguishable prefix via rdflib's own collision handling -- rather than silently skip it
    # because the *name* "brick" is already taken, letting it fall through to rdflib's opaque
    # auto-generated ns1:/ns2: at serialize time.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "brick_v11", "data": COLLIDING_BRICK_TTL})
        summary = _result_json(await client.call_tool("summarize_schema", {"dataset": "brick_v11"}))

        prefix_match = re.search(r"@prefix (\w+): <https://brickschema\.org/schema/1\.1/Brick#>", summary["class_graph"])
        assert prefix_match is not None, summary["class_graph"]
        assert not prefix_match.group(1).startswith("ns")


@pytest.mark.asyncio
async def test_summarize_schema_without_loading_dataset_first_is_a_clear_error():
    async with create_connected_server_and_client_session(mcp) as client:
        result = await client.call_tool("summarize_schema", {"dataset": "missing"})
        assert result.isError
        text = "".join(block.text for block in result.content if block.type == "text")
        assert "no dataset named 'missing'" in text
        assert "load_dataset" in text


@pytest.mark.asyncio
async def test_run_query_reports_ok_and_returns_rows_on_a_working_query():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        result = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY}))
        assert result["ok"] is True
        assert result["form"] == "solutions"
        assert result["row_count"] == 2
        assert result["culprits"] == []
        assert result["filter_issues"] == []
        assert result["diagnosis_skipped"] is None
        # row_limit defaults to 3, so both of this query's rows come back.
        assert result["variables"] == ["s"]
        # URIs come back as CURIEs (prefix:local), not full URIs, using the ex: prefix
        # declared in both the dataset and the query.
        assert {row["s"]["value"] for row in result["rows"]} == {"ex:sensor1", "ex:sensor2"}
        assert all(row["s"]["type"] == "uri" for row in result["rows"])
        assert result["prefixes"]["ex"] == "https://brickschema.org/schema/Brick#"


@pytest.mark.asyncio
async def test_run_query_row_limit_caps_and_can_be_disabled():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        capped = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY, "row_limit": 1}))
        assert len(capped["rows"]) == 1
        assert capped["row_count"] == 2  # row_limit never affects the full count

        disabled = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY, "row_limit": 0}))
        assert disabled["variables"] == []
        assert disabled["rows"] == []


@pytest.mark.asyncio
async def test_run_query_with_connect_true_still_returns_rows():
    # diagnose_and_connect doesn't sample rows itself, so run_query executes the query
    # separately to fill them in -- connect=True must not cost the caller the results.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = _result_json(
            await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY, "connect": True, "row_limit": 3})
        )
        assert result["ok"] is True
        assert result["variables"] == ["s"]
        assert {row["s"]["value"] for row in result["rows"]} == {"ex:sensor1", "ex:sensor2"}


@pytest.mark.asyncio
async def test_run_query_reports_a_culprit_but_no_fix_by_default():
    # `connect` defaults to False: diagnosis is nearly free and always run, but the
    # (comparatively expensive, experimental) connection search only runs when asked
    # for explicitly -- see the `connect` docs on the `run_query` tool.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        diagnosis = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": BROKEN_QUERY}))
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
async def test_run_query_with_connect_true_suggests_a_fix():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        diagnosis = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": BROKEN_QUERY, "connect": True}))
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

        # The suggested connected_query should itself actually work via `run_query`.
        fixed = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": culprit["connected_query"]}))
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
async def test_run_query_suggests_a_verified_wrong_namespace_fix():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "ns", "data": NAMESPACE_TTL})

        diagnosis = _result_json(await client.call_tool("run_query", {"dataset": "ns", "query": WRONG_NAMESPACE_QUERY}))
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
        rerun = _result_json(await client.call_tool("run_query", {"dataset": "ns", "query": fix["fixed_query"], "row_limit": None}))
        assert len(rerun["rows"]) == fix["row_count_with_fix"]


@pytest.mark.asyncio
async def test_run_query_falls_back_to_a_case_typo_fix_when_no_namespace_match_exists():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "ns", "data": NAMESPACE_TTL})

        diagnosis = _result_json(await client.call_tool("run_query", {"dataset": "ns", "query": WRONG_CASE_QUERY}))
        assert diagnosis["ok"] is False
        culprit = diagnosis["culprits"][0]
        assert len(culprit["suggested_fixes"]) == 1

        fix = culprit["suggested_fixes"][0]
        assert fix["kind"] == "local_name_typo"
        assert fix["original_term"] == "s223:zone"
        assert fix["replacement_term"] == "s223:Zone"
        assert fix["row_count_with_fix"] == 2


@pytest.mark.asyncio
async def test_run_query_suggest_fixes_false_disables_the_search():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "ns", "data": NAMESPACE_TTL})

        diagnosis = _result_json(
            await client.call_tool("run_query", {"dataset": "ns", "query": WRONG_NAMESPACE_QUERY, "suggest_fixes": False})
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
async def test_run_query_skips_the_expensive_search_once_a_query_already_returns_rows():
    # ignore_cartesian_risk/expand_nonempty_results aren't exposed as MCP parameters (see
    # test_run_query_tool_schema_has_no_cartesian_or_expand_params) -- an MCP caller always gets
    # the search skipped once the query already returns at least one row.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "vals", "data": VALUE_TTL})

        result = _result_json(await client.call_tool("run_query", {"dataset": "vals", "query": NARROWED_QUERY}))
        assert result["row_count"] == 1
        assert result["ok"] is True
        assert result["filter_issues"] == []


@pytest.mark.asyncio
async def test_run_query_tool_schema_has_no_cartesian_or_expand_params():
    # README/docstring both say these aren't caller-settable over MCP -- the tool's advertised
    # schema shouldn't offer them either.
    async with create_connected_server_and_client_session(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        properties = tools["run_query"].inputSchema["properties"]
        assert "ignore_cartesian_risk" not in properties
        assert "expand_nonempty_results" not in properties


@pytest.mark.asyncio
async def test_run_query_row_limit_caps_solutions():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY, "row_limit": 1}))
        assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_run_query_default_row_limit_is_three():
    ttl = TTL + "\n".join(f"ex:sensor{i} a ex:TempSensor ." for i in range(3, 8))
    query_all_sensors = "PREFIX ex: <https://brickschema.org/schema/Brick#> SELECT ?s WHERE { ?s a ex:TempSensor }"
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": ttl})
        result = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": query_all_sensors}))
        assert len(result["rows"]) == 3


@pytest.mark.asyncio
async def test_run_query_ask_and_construct_forms():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        ask = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": "PREFIX ex: <https://brickschema.org/schema/Brick#> ASK { ex:sensor1 a ex:TempSensor }"}))
        assert ask["form"] == "boolean"
        assert ask["result"] is True
        assert ask["ok"] is True
        assert ask["row_count"] == 1

        construct = _result_json(
            await client.call_tool(
                "run_query",
                {
                    "dataset": "b223",
                    "query": "PREFIX ex: <https://brickschema.org/schema/Brick#> CONSTRUCT { ?s a ex:Thing } WHERE { ?s a ex:TempSensor }",
                },
            )
        )
        assert construct["form"] == "graph"
        assert construct["ok"] is True
        assert construct["row_count"] == 2
        assert len(construct["triples"]) == 2


@pytest.mark.asyncio
async def test_run_query_diagnoses_a_false_ask_and_an_empty_construct():
    # Same broken pattern as BROKEN_QUERY, wrapped in ASK/CONSTRUCT/DESCRIBE: the WHERE body
    # is diagnosed as a SELECT, so the culprit is reported just like it is for the SELECT.
    body = "{ ex:building223 ex:hasSensor ?sensor . ?sensor a ex:TempSensor . }"
    prefix = "PREFIX ex: <https://brickschema.org/schema/Brick#>\n"
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})

        for query in (
            f"{prefix}ASK {body}",
            # template contains a string with a brace in it, to exercise the rewrite's masking
            f'{prefix}CONSTRUCT {{ ?sensor ex:label "}}" }} WHERE {body}',
            f"{prefix}CONSTRUCT WHERE {body}",
            f"{prefix}DESCRIBE ?sensor WHERE {body}",
        ):
            result = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": query}))
            assert result["ok"] is False, query
            assert result["row_count"] == 0, query
            assert result["diagnosis_skipped"] is None, query
            assert result["culprits"][0]["triples"][0]["triple"] == "ex:building223 ex:hasSensor ?sensor", query
            if result["form"] == "boolean":
                assert result["result"] is False
            else:
                assert result["triples"] == [], query


@pytest.mark.asyncio
async def test_run_query_executes_a_bare_describe_without_diagnosing():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = _result_json(
            await client.call_tool(
                "run_query",
                {"dataset": "b223", "query": "PREFIX ex: <https://brickschema.org/schema/Brick#> DESCRIBE ex:sensor1", "row_limit": None},
            )
        )
        assert result["form"] == "graph"
        assert result["ok"] is True
        assert result["row_count"] is None
        assert {t["object"]["value"] for t in result["triples"]} == {"ex:TempSensor"}


@pytest.mark.asyncio
async def test_run_query_still_executes_a_query_it_cannot_diagnose():
    # An all-variable pattern has no BGP triples for the ablation search to work with --
    # the diagnosis is skipped, but the query ran fine, so that isn't reported as a failure.
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = _result_json(
            await client.call_tool("run_query", {"dataset": "b223", "query": "SELECT * WHERE { ?s ?p ?o }", "row_limit": None})
        )
        assert result["diagnosis_skipped"] is not None
        assert result["ok"] is True
        assert result["row_count"] is None
        assert len(result["rows"]) == 4


@pytest.mark.asyncio
async def test_run_query_surfaces_a_syntax_error():
    async with create_connected_server_and_client_session(mcp) as client:
        await client.call_tool("load_dataset", {"name": "b223", "data": TTL})
        result = await client.call_tool("run_query", {"dataset": "b223", "query": "SELEC ?s WHERE { ?s ?p ?o"})
        assert result.isError


@pytest.mark.asyncio
async def test_replacing_a_dataset_is_reflected_in_run_query_not_served_stale():
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
        first = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY}))
        assert first["row_count"] == 2  # forces the worker to actually fork with the original data loaded

        await client.call_tool("load_dataset", {"name": "b223", "data": replacement_ttl})
        second = _result_json(await client.call_tool("run_query", {"dataset": "b223", "query": WORKING_QUERY}))
        assert second["row_count"] == 1


# A small taxonomy with multiple inheritance (SAT_Sensor has two parents, which both
# lead to Sensor), plus instances with literals, a blank node, and a feeds cycle.
TAXONOMY_TTL = """
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <urn:ex#> .
brick:SAT_Sensor rdfs:subClassOf brick:Air_Temperature_Sensor, brick:Supply_Air_Sensor .
brick:Air_Temperature_Sensor rdfs:subClassOf brick:Sensor .
brick:Supply_Air_Sensor rdfs:subClassOf brick:Sensor .
brick:Sensor rdfs:subClassOf brick:Point ;
    rdfs:comment "Measures a physical quantity" .
ex:ahu1 a brick:AHU ; brick:feeds ex:vav1 ; rdfs:label "Rooftop unit 1" .
ex:vav1 a brick:VAV ; brick:feeds ex:ahu1 ; brick:hasPoint ex:sat1 .
ex:sat1 a brick:SAT_Sensor ; brick:hasUnit [ rdfs:label "degF" ] ; ex:bacnetName "AHU-1 SAT" .
"""


async def _load_taxonomy(client) -> None:
    _result_json(await client.call_tool("load_dataset", {"name": "tax", "data": TAXONOMY_TTL}))


def _traverse(**kwargs) -> dict:
    # traverse is deprecated and registered in no toolset, so it's tested as a plain function.
    return traverse(dataset="tax", **kwargs)


@pytest.mark.asyncio
async def test_traverse_multiple_inheritance_is_a_dag_not_duplicated_paths():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _traverse(start="brick:SAT_Sensor", predicates=["rdfs:subClassOf"], max_depth=5)
        levels = {lvl["depth"]: {n["node"]: n["via"] for n in lvl["nodes"]} for lvl in result["levels"]}
        assert set(levels[1]) == {"brick:Air_Temperature_Sensor", "brick:Supply_Air_Sensor"}
        # Sensor is reached from both parents: listed once, with both edges.
        assert sorted(levels[2]["brick:Sensor"]) == [
            ["brick:Air_Temperature_Sensor", "rdfs:subClassOf"],
            ["brick:Supply_Air_Sensor", "rdfs:subClassOf"],
        ]
        assert set(levels[3]) == {"brick:Point"}
        assert result["node_count"] == 4
        assert result["truncated"] is False and result["more_beyond_max_depth"] is False


@pytest.mark.asyncio
async def test_traverse_incoming_walks_down_a_taxonomy():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _traverse(start="brick:Sensor", direction="incoming", predicates=["rdfs:subClassOf"], max_depth=1)
        assert {n["node"] for n in result["levels"][1]["nodes"]} == {"brick:Air_Temperature_Sensor", "brick:Supply_Air_Sensor"}
        # SAT_Sensor is one level further down.
        assert result["more_beyond_max_depth"] is True


@pytest.mark.asyncio
async def test_traverse_terminates_on_cycles_and_records_the_back_edge():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _traverse(start="ex:ahu1", predicates=["brick:feeds"], max_depth=10)
        assert result["node_count"] == 1
        assert result["levels"][0]["nodes"][0]["via"] == [["ex:vav1", "brick:feeds"]]


@pytest.mark.asyncio
async def test_traverse_all_predicates_literals_are_leaves_and_blank_nodes_skipped():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _traverse(start="ex:sat1", max_depth=2)
        depth1 = {n["node"] for n in result["levels"][1]["nodes"]}
        assert depth1 == {"brick:SAT_Sensor", '"AHU-1 SAT"'}
        assert result["blank_node_edges_skipped"] == 1
        # The literal isn't expanded; the class is.
        assert {n["node"] for n in result["levels"][2]["nodes"]} == {"brick:Air_Temperature_Sensor", "brick:Supply_Air_Sensor"}


@pytest.mark.asyncio
async def test_traverse_max_nodes_truncates():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _traverse(start="brick:SAT_Sensor", predicates=["rdfs:subClassOf"], max_nodes=2)
        assert result["node_count"] == 1
        assert result["truncated"] is True


@pytest.mark.asyncio
async def test_traverse_rejects_unknown_prefix():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        with pytest.raises(ValueError):
            _traverse(start="brik:Sensor")


@pytest.mark.asyncio
async def test_search_bm25_splits_compound_names_and_filters_by_kind():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "air temperature sensor", "kind": "class"}))
        assert result["results"][0]["uri"] == "brick:Air_Temperature_Sensor"
        assert all("class" in r["kinds"] for r in result["results"])
        assert "total_matches" not in result

        preds = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "has point", "kind": "predicate"}))
        assert preds["results"][0]["uri"] == "brick:hasPoint"


@pytest.mark.asyncio
async def test_search_bm25_matches_labels_and_other_string_literals():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        rooftop = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "rooftop", "kind": "instance"}))
        assert rooftop["results"][0]["uri"] == "ex:ahu1"
        assert rooftop["results"][0]["label"] == "Rooftop unit 1"
        assert rooftop["results"][0]["types"] == ["brick:AHU"]

        physical = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "physical quantity"}))
        assert physical["results"][0]["uri"] == "brick:Sensor"


@pytest.mark.asyncio
async def test_search_regex_matches_names_before_literals_and_counts_all():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "(?i)ahu", "mode": "regex", "limit": 1}))
        assert result["total_matches"] == 3  # brick:AHU, ex:ahu1 by name; ex:sat1 by its "AHU-1 SAT" literal
        assert len(result["results"]) == 1

        literal_hit = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "AHU-1", "mode": "regex"}))
        assert literal_hit["results"] == [
            {"uri": "ex:sat1", "kinds": ["instance"], "types": ["brick:SAT_Sensor"], "matched_text": "AHU-1 SAT"}
        ]


@pytest.mark.asyncio
async def test_search_rejects_invalid_regex():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = await client.call_tool("search", {"dataset": "tax", "text": "(unclosed", "mode": "regex"})
        assert result.isError


@pytest.mark.asyncio
async def test_search_index_is_rebuilt_when_a_dataset_is_replaced():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        first = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "rooftop"}))
        assert first["results"]

        await client.call_tool("load_dataset", {"name": "tax", "data": TTL})
        second = _result_json(await client.call_tool("search", {"dataset": "tax", "text": "rooftop"}))
        assert second["results"] == []


@pytest.mark.asyncio
async def test_search_include_predicates_lists_every_requested_predicate():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _result_json(
            await client.call_tool(
                "search",
                {"dataset": "tax", "text": "physical quantity", "limit": 1,
                 "include_predicates": ["rdfs:comment", "rdfs:subClassOf", "rdfs:label"]},
            )
        )
        hit = result["results"][0]
        assert hit["uri"] == "brick:Sensor"
        assert hit["properties"] == {
            "rdfs:comment": ['"Measures a physical quantity"'],
            "rdfs:subClassOf": ["brick:Point"],
            "rdfs:label": [],
        }
        assert "cbd" not in hit

        bnode = _result_json(
            await client.call_tool(
                "search", {"dataset": "tax", "text": "AHU-1", "mode": "regex", "include_predicates": ["brick:hasUnit"]}
            )
        )
        assert bnode["results"][0]["properties"]["brick:hasUnit"][0].startswith("[] (blank node")


@pytest.mark.asyncio
async def test_search_include_cbd_follows_blank_nodes_as_turtle():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = _result_json(
            await client.call_tool("search", {"dataset": "tax", "text": "AHU-1", "mode": "regex", "include_cbd": True})
        )
        hit = result["results"][0]
        assert hit["uri"] == "ex:sat1"
        assert "cbd_truncated" not in hit
        # Prefix lines are stripped (the response's `prefixes` carries them); the blank node's
        # own triples are nested inline.
        assert "@prefix" not in hit["cbd"]
        parsed = Graph().parse(
            data="".join(f"@prefix {p}: <{ns}> .\n" for p, ns in result["prefixes"].items()) + hit["cbd"], format="turtle"
        )
        assert len(parsed) == 4  # type, bacnetName, hasUnit, and the unit's own rdfs:label
        assert '"degF"' in hit["cbd"]


@pytest.mark.asyncio
async def test_search_rejects_unknown_prefix_in_include_predicates():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        result = await client.call_tool("search", {"dataset": "tax", "text": "sensor", "include_predicates": ["rdfz:label"]})
        assert result.isError


@pytest.mark.asyncio
async def test_run_query_property_path_only_query_is_not_an_error():
    async with create_connected_server_and_client_session(mcp) as client:
        await _load_taxonomy(client)
        prefixes = "PREFIX brick: <https://brickschema.org/schema/Brick#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
        found = _result_json(
            await client.call_tool(
                "run_query",
                {"dataset": "tax", "query": prefixes + "SELECT ?anc WHERE { brick:SAT_Sensor rdfs:subClassOf+ ?anc }", "row_limit": None},
            )
        )
        assert found["ok"] is True
        assert found["diagnosis_skipped"] is not None
        assert {r["anc"]["value"] for r in found["rows"]} == {
            "brick:Air_Temperature_Sensor", "brick:Supply_Air_Sensor", "brick:Sensor", "brick:Point",
        }
        assert "not a problem with the query" in found["message"]

        empty = _result_json(
            await client.call_tool(
                "run_query", {"dataset": "tax", "query": prefixes + "SELECT ?anc WHERE { brick:Nope rdfs:subClassOf+ ?anc }"}
            )
        )
        assert empty["ok"] is False
        assert empty["rows"] == []
        assert "returned no results" in empty["message"]
