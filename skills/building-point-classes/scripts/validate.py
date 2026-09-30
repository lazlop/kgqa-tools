"""Validate a 223P/Brick model with SHACL and summarize the results by message.

Usage:
  python validate.py MODEL.ttl [MODEL2.ttl ...] --ontology 223p.ttl [--ontology g36.ttl ...]
                     [--baseline OLD_MODEL.ttl ...] [--engine auto|pyshifty|topquadrant|pyshacl]

Every MODEL file is merged into one data graph (put your extension .ttl files here too).
Every --ontology file is used both as shapes and as the ontology (223P ships its shapes inside
the ontology). With --baseline, the old model is validated the same way and only the
differences are printed. That's the useful view after a change: new results are what the change
broke, resolved results are what it fixed.

Engines, and how long each took on a 4,600-triple 223P VAV model (with 223P, the G36 extension
and QUDT loaded):
- `pyshifty`, run through BuildingMOTIF (the `gtf-buildingmotif` branch, which pins pyshifty
  0.4.x): about 20 s.
- `topquadrant`, from the brick-tq-shacl package (needs Java): about 50 s.
- `pyshacl`: hadn't finished after 15 minutes. Avoid it for 223P.
`auto` tries them in that order. pyshifty and topquadrant reported the same results on the
model's own nodes. topquadrant also reports three violations inside 223P's own definitions,
which this script ignores either way.

Results are grouped by (severity, message) with instance IRIs masked, so 120 copies of the same
warning are one line with a count. Pass --focus to also list the focus nodes of each group.
"""
import argparse
import collections
import re
import sys
import time

from rdflib import Graph, Namespace
from rdflib.namespace import RDF

SH = Namespace("http://www.w3.org/ns/shacl#")


def load(paths):
    g = Graph()
    for p in paths:
        g.parse(p)
    return g


def validate_pyshifty(model, onto):
    """Validate through BuildingMOTIF's pyshifty engine, with an in-memory database. BuildingMOTIF
    wants exactly one owl:Ontology header per library and per model, so the ontology files'
    headers are folded into one here."""
    from rdflib import URIRef
    from rdflib.namespace import OWL
    from buildingmotif import BuildingMOTIF
    from buildingmotif.dataclasses import Library, Model

    shapes, data = Graph(), Graph()
    shapes += onto
    data += model
    for o in list(shapes.subjects(RDF.type, OWL.Ontology)):
        shapes.remove((o, None, None))
    shapes.add((URIRef("urn:validate-py/shapes"), RDF.type, OWL.Ontology))
    headers = list(data.subjects(RDF.type, OWL.Ontology))
    for o in headers[1:]:
        data.remove((o, RDF.type, OWL.Ontology))
    if not headers:
        data.add((URIRef("urn:validate-py/model"), RDF.type, OWL.Ontology))
    with BuildingMOTIF("sqlite://", shacl_engine="pyshifty"):
        lib = Library.from_ontology(shapes, infer_templates=False, run_shacl_inference=False,
                                    fetch_imports=False)
        ctx = Model.from_graph(data).validate([lib.get_shape_collection()], error_on_missing_imports=False)
        report = Graph()
        report += ctx.report
        return ctx.valid, report


def validate(model, onto, engine):
    if engine in ("auto", "pyshifty"):
        try:
            import buildingmotif  # noqa: F401
        except ImportError:
            if engine == "pyshifty":
                sys.exit("buildingmotif is not installed (the gtf-buildingmotif branch bundles pyshifty)")
        else:
            conforms, report = validate_pyshifty(model, onto)
            return conforms, report, "pyshifty"
    if engine in ("auto", "topquadrant"):
        try:
            from brick_tq_shacl.topquadrant_shacl import infer, validate as tq_validate
        except ImportError:
            if engine == "topquadrant":
                sys.exit("brick-tq-shacl is not installed (pip install brick-tq-shacl; needs Java)")
            engine = "pyshacl"
        else:
            inferred = infer(model, onto)
            conforms, report, _ = tq_validate(inferred, onto)
            return conforms, report, "topquadrant"
    import pyshacl
    data = model + onto
    conforms, report, _ = pyshacl.validate(data, shacl_graph=onto, ont_graph=onto, advanced=True,
                                           inference="none", iterate_rules=True, allow_warnings=True)
    return conforms, report, "pyshacl"


def namespace(uri):
    return re.match(r"(.*[#/])", uri).group(1) if re.match(r"(.*[#/])", uri) else uri


def local(uri):
    return uri[len(namespace(uri)):]


def summarize(report, model):
    """(severity, masked message) -> sorted focus nodes. Only results whose focus node is in the
    model's own namespaces count, so problems inside the ontology itself don't swamp the view.
    Those are listed separately as a count."""
    own = {namespace(str(s)) for s in model.subjects()}
    groups, foreign = collections.defaultdict(set), collections.Counter()
    for r in report.subjects(RDF.type, SH.ValidationResult):
        sev = str(report.value(r, SH.resultSeverity)).split("#")[-1]
        focus = str(report.value(r, SH.focusNode))
        msg = str(report.value(r, SH.resultMessage) or report.value(r, SH.sourceConstraintComponent))
        if namespace(focus) not in own:
            foreign[sev] += 1
            continue
        # shorten IRIs to their local names, and mask the focus node so identical results group
        msg = re.sub(r"<?https?://[^\s>]+[#/]([\w.-]+)>?", r"<\1>", msg)
        msg = re.sub(rf"(<|\b[\w-]+:){re.escape(local(focus))}>?", "<x>", msg)[:220]
        groups[(sev, msg)].add(local(focus))
    return groups, foreign


ORDER = {"Violation": 0, "Warning": 1, "Info": 2}


def show(groups, focus, prefix=""):
    for (sev, msg), nodes in sorted(groups.items(), key=lambda kv: (ORDER.get(kv[0][0], 9), kv[0][1])):
        print(f"{prefix}{len(nodes):4d} {sev:9s} {msg}")
        if focus:
            print(f"{prefix}       {', '.join(sorted(nodes)[:15])}{' ...' if len(nodes) > 15 else ''}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", nargs="+")
    ap.add_argument("--ontology", action="append", required=True)
    ap.add_argument("--baseline", action="append")
    ap.add_argument("--engine", default="auto", choices=["auto", "pyshifty", "topquadrant", "pyshacl"])
    ap.add_argument("--focus", action="store_true", help="list the focus nodes of each group")
    args = ap.parse_args()

    onto = load(args.ontology)
    runs = [("model", args.model)] + ([("baseline", args.baseline)] if args.baseline else [])
    results = {}
    for name, paths in runs:
        model = load(paths)
        n = len(model)  # count first: topquadrant's infer() adds inferred triples to `model` in place
        t = time.time()
        conforms, report, engine = validate(model, onto, args.engine)
        groups, foreign = summarize(report, model)
        results[name] = groups
        print(f"== {name}: {n} triples, conforms={conforms}, engine={engine}, {time.time() - t:.0f}s"
              + (f"; ignored {dict(foreign)} results on ontology nodes" if foreign else ""))

    if "baseline" not in results:
        show(results["model"], args.focus)
        return
    new, old = results["model"], results["baseline"]
    changed = False
    for label, a, b in [("NEW (only in model)", new, old), ("RESOLVED (only in baseline)", old, new)]:
        diff = {k: v for k, v in a.items() if k not in b}
        if diff:
            changed = True
            print(f"-- {label}")
            show(diff, args.focus, "  ")
    moved = {k: (len(old[k]), len(new[k])) for k in new.keys() & old.keys() if len(new[k]) != len(old[k])}
    if moved:
        changed = True
        print("-- COUNT CHANGED (baseline -> model)")
        for (sev, msg), (a, b) in sorted(moved.items()):
            print(f"  {a:4d} -> {b:<4d} {sev:9s} {msg}")
    if not changed:
        print("-- no differences: the change neither introduced nor resolved any result")
        show(new, args.focus, "  ")


if __name__ == "__main__":
    main()
