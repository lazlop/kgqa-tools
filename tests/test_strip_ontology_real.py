"""`_strip_ontology` against real building graphs that bundle their ontology, checked against
hand-cleaned copies of the same graphs with the ontology removed.

The pairs live in the sibling kgqa-agent checkout: `data/eval_buildings/<name>.ttl` (as
published, ontology included) and `data/eval_buildings/without-ontology/<name>.ttl` (cleaned by
bschema-rs/eval/remove_ontology.py). Skipped if that checkout isn't present.

Graphs are compared after RDFC-1.0 canonicalization (via pyoxigraph -- rdflib's own
`isomorphic` takes minutes on bldg11's ~900 blank nodes), which also treats `"x"` and
`"x"^^xsd:string` as the same literal, as RDF 1.1 does; the cleaned files were re-serialized
with the explicit datatype on every string.

The graphs go through what `summarize_schema(exclude_ontology=True)` does: strip the ontology,
then drop inferred superclass types using the bundled hierarchy plus the shipped ontologies' (223P, Brick).
Caveats on "identical":
- remove_ontology.py dropped inferred superclass types against the *full* ontology files. b59
  bundles only a slice of 223P, which misses the links above ~950 of them (s223:Equipment,
  s223:Connectable, ...); the shipped 223P has to cover those. That script also renamed
  b59's nodes, so that (`_rename_b59_nodes`, copied from it) is applied before comparing.
- The other way round, bldg11's bundled Brick does link the 10 `brick:Command`s the reference
  kept to a more specific type of theirs (`brick:Heating_Command`), so the same cleanup is
  applied to the reference too, and exactly those 10 are asserted to be the difference.
"""

from __future__ import annotations

from pathlib import Path

import pyoxigraph
import pytest
from rdflib import RDF, RDFS, Graph, Namespace

from sparql_relax_mcp.server import (
    _remove_inferred_superclass_types,
    _strip_ontology,
    _subclass_hierarchy,
    _with_known_hierarchy,
)

KGQA_AGENT_DATA = Path(__file__).resolve().parents[2] / "kgqa-agent" / "data"
EVAL_BUILDINGS_DIR = KGQA_AGENT_DATA / "eval_buildings"

pytestmark = pytest.mark.skipif(
    not (EVAL_BUILDINGS_DIR / "without-ontology").exists(),
    reason=f"sibling kgqa-agent checkout not found at {EVAL_BUILDINGS_DIR}",
)

S223 = Namespace("http://data.ashrae.org/standard223#")


def _canonical(graph: Graph) -> pyoxigraph.Dataset:
    triples = graph.serialize(format="nt", encoding="utf-8")
    dataset = pyoxigraph.Dataset(pyoxigraph.parse(triples, format=pyoxigraph.RdfFormat.N_TRIPLES))
    dataset.canonicalize(pyoxigraph.CanonicalizationAlgorithm.RDFC_1_0)
    return dataset


def _rename_b59_nodes(graph: Graph) -> None:
    ex = Namespace("http://data.ashrae.org/standard223/data/lbnl-example-2#")
    api_reference = Namespace("https://brickschema.org/schema/Brick/").APIReference
    for s in list(graph.subjects()):
        if str(ex) not in s:
            continue
        label = graph.value(s, RDFS.label)
        if label is None or (s, RDF.type, api_reference) in graph or (s, RDF.type, S223.Connection) in graph:
            continue
        new_uri = ex[f"{label.replace(' ', '_').replace('(', '').replace(')', '')}{str(s).split('#')[-1]}"]
        for p, o in list(graph.predicate_objects(s)):
            graph.remove((s, p, o))
            graph.add((new_uri, p, o))
        for s2, p2 in list(graph.subject_predicates(s)):
            graph.remove((s2, p2, s))
            graph.add((s2, p2, new_uri))


@pytest.mark.parametrize("building", ["bldg11.ttl", "b59.ttl", "TUC_building.ttl", "dflexlibs_multizone.ttl"])
def test_strip_ontology_matches_hand_cleaned_graph(building):
    graph = Graph().parse(EVAL_BUILDINGS_DIR / building)
    hierarchy = _with_known_hierarchy(_subclass_hierarchy(graph))
    removed = _strip_ontology(graph)
    _remove_inferred_superclass_types(graph, hierarchy)
    if building == "b59.ttl":
        _rename_b59_nodes(graph)
    expected = Graph().parse(EVAL_BUILDINGS_DIR / "without-ontology" / building)
    missed_by_reference = _remove_inferred_superclass_types(expected, hierarchy)
    assert missed_by_reference == (10 if building == "bldg11.ttl" else 0)

    # bldg11 bundles Brick and b59 a slice of 223P; TUC and dflexlibs have no ontology at all,
    # so nothing may be removed from them (including their many data blank nodes).
    assert (removed > 0) == (building in ("bldg11.ttl", "b59.ttl"))
    assert len(graph) == len(expected)
    assert _canonical(graph) == _canonical(expected)
