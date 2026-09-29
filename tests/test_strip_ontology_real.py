"""`_strip_ontology` against real building graphs that bundle their ontology, checked against
hand-cleaned copies of the same graphs with the ontology removed.

The pairs live in the sibling kgqa-agent checkout: `data/eval_buildings/<name>.ttl` (as
published, ontology included) and `data/eval_buildings/without-ontology/<name>.ttl` (cleaned by
bschema-rs/eval/remove_ontology.py). Skipped if that checkout isn't present.

Graphs are compared after RDFC-1.0 canonicalization (via pyoxigraph -- rdflib's own
`isomorphic` takes minutes on bldg11's ~900 blank nodes), which also treats `"x"` and
`"x"^^xsd:string` as the same literal, as RDF 1.1 does; the cleaned files were re-serialized
with the explicit datatype on every string.

One caveat on "identical": for b59, remove_ontology.py also did two things that aren't ontology
removal -- dropping inferred superclass types and renaming nodes after their labels -- so those
two steps (`_remove_less_specific_classes`/`_rename_b59_nodes`, copied from that script) are
applied to the stripped graph before comparing.
"""

from __future__ import annotations

from pathlib import Path

import pyoxigraph
import pytest
from rdflib import RDF, RDFS, Graph, Namespace

from sparql_relax_mcp.server import _strip_ontology

KGQA_AGENT_DATA = Path(__file__).resolve().parents[2] / "kgqa-agent" / "data"
EVAL_BUILDINGS_DIR = KGQA_AGENT_DATA / "eval_buildings"
S223_ONTOLOGY = KGQA_AGENT_DATA / "ontologies" / "223p.ttl"

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


def _remove_less_specific_classes(graph: Graph, ontology: Graph) -> Graph:
    query = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        CONSTRUCT { ?s a ?parent . }
        WHERE { ?s a ?child . ?child rdfs:subClassOf+ ?parent . ?s a ?parent . }
    """
    return graph - (graph + ontology).query(query).graph


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
    removed = _strip_ontology(graph)
    if building == "b59.ttl":
        graph = _remove_less_specific_classes(graph, Graph().parse(S223_ONTOLOGY))
        _rename_b59_nodes(graph)
    expected = Graph().parse(EVAL_BUILDINGS_DIR / "without-ontology" / building)

    # bldg11 bundles Brick and b59 a slice of 223P; TUC and dflexlibs have no ontology at all,
    # so nothing may be removed from them (including their many data blank nodes).
    assert (removed > 0) == (building in ("bldg11.ttl", "b59.ttl"))
    assert len(graph) == len(expected)
    assert _canonical(graph) == _canonical(expected)
