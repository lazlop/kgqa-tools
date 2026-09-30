---
name: building-point-classes
description: Map building automation points (BACnet objects, controller point lists, BMS exports, Haystack or Brick tags) to ASHRAE 223P and Brick classes. Covers choosing property classes, quantity kinds and units, enumeration kinds for binary and multi-state values, aspects and media, and making minimal ontology extensions where the standard falls short, then validating the result. Use this whenever someone wants to classify, tag or semantically model points, build a 223P or Brick model from a point list, pick classes or enumeration kinds for BACnet objects, or extend Brick or 223P, including while building a model with BuildingMOTIF, even if they only say "tag these points", "what class is this point" or "make a semantic model of this controller".
---

# Mapping building points to 223P and Brick

A point list looks like a lookup exercise, but most of the mistakes are judgement mistakes:
- inventing a term when the standard already has one under another name;
- choosing a term that some extension's shapes quietly contradict;
- modelling similar points inconsistently;
- losing the original meaning (the point name, the state texts) along the way.

The workflow below is built to catch those. The reference files hold the details:

- `references/s223.md`: 223P property modelling, enumeration kinds, the state-text synonym
  table, BACnet references, and how to write extensions. Read it for any 223P work.
- `references/brick.md`: Brick point functions, naming, tags, deprecations, extensions, and how
  Brick maps onto 223P. Read it for any Brick work.
- `references/queries.md`: tested SPARQL templates for browsing both taxonomies and checking
  constraints. Copy from these rather than writing taxonomy queries from scratch.
- `scripts/validate.py`: SHACL validation summarized by message, with a baseline diff. It
  prefers the pyshifty engine through BuildingMOTIF when that's installed.

## 1. Load the ontologies where you can query them

Don't guess class names from memory. Brick and 223P both change between versions, and a wrong
guess looks plausible. Load the ontology, plus any extensions whose shapes the model has to
satisfy (for example the G36 extension of 223P), into a queryable graph:

- **With kgqa-tools:** `load_dataset(name="s223", path=".../223p.ttl")`, and the same for Brick.
  Load the extensions into the same dataset as their base ontology, so constraint queries see
  them. Load the model you're building as a separate dataset.
- **Without it:** rdflib, and the templates in `references/queries.md`.

Sources: 223P at <https://open223.info/223p.ttl>, Brick at
<https://brickschema.org/schema/1.4.4/Brick.ttl>. Note the version you used (`owl:versionInfo`)
in your report.

## 2. Inventory the points

For each point, collect:
- the name;
- the object type (AI/AO/AV/BI/BO/BV/MI/MO/MV and so on) and whether it's commandable;
- the units;
- **for binary and multi-state objects, the state texts**: the inactive and active text, or the
  `state-text` array.

The state texts decide the enumeration kind, and a point name alone won't tell you them.
"Window Contact" could be Open/Closed or On/Off; "Standby Mode" turned out to be
Vacant/Occupied. If the state texts aren't in the export, ask for them or read them from the
device, for example with a BACnet read of `inactive-text`, `active-text` or `state-text`. If
you can't get them, mark those kinds as provisional in the output. Don't invent members.

Group the points by the kind of thing they are (temperatures, flows, positions, enables, alarm
inhibits, modes). Similar points should get similar treatment, and deciding per group is faster
and more consistent than deciding per point.

## 3. Find the standard term first

For each group, look for an existing term before considering an extension:

1. **Search** by meaning and by the state texts themselves. With kgqa-tools, use
   `search(dataset, "open closed", kind="class", include_predicates=["rdfs:label", "rdfs:subClassOf", "skos:definition"])`.
   Try synonyms before concluding nothing exists. "Running/Stopped" finds nothing, but it is
   `s223:Binary-OnOff`. `references/s223.md` has a synonym table for common state texts.
2. **Inspect the candidate.** With kgqa-tools, one call covers this step and the next:
   `search(dataset, "^s223:Binary-OnOff$", mode="regex", include_cbd=True, include_cbd_incoming=True)`.
   - `cbd` gives the definition and parents.
   - `incoming.direct["rdfs:subClassOf"]` lists the children. For an enumeration kind, these
     are its members.

   Without kgqa-tools, use queries 1–3.

   In 223P, kinds and their members are both classes, and `search(kind="class")` returns both.
   A member has no subclasses.
3. **Check the constraints on it.** Shapes in extensions can require a specific term. G36's
   `g36:Zone`, for example, expects the window switch to use `s223:Binary-OnOff`. In the same
   search call, `incoming.referenced_in` lists every shape that references the term, with its
   owner and path. Without kgqa-tools, use query 5. If a shape conflicts with the exact
   semantics, that's a judgement call to report, not something to resolve silently.
4. **For Brick, check deprecation (query 8).** Search returns deprecated classes too.

Reuse a standard term whenever its meaning matches, even if the wording differs. Nothing is lost
as long as you keep the original text next to it (step 5). For example, "Enabled/Disabled" maps
to `Logical-True/False`, with "Enabled" kept as the state text.

## 4. Extend only for real gaps, and minimally

Extend when no standard term carries the meaning, not when the wording differs. Prefer the
smallest extension that works:

1. A new **member** in an existing extension kind, before a new kind.
2. A new **kind** under the most specific standard parent. For example, a two-state kind goes
   under `s223:EnumerationKind-Binary`, named `Binary-X`, with members named `X-Member`, following
   223P's own pattern.
3. For Brick, a new **subclass** of the most specific fitting class.

Put extensions in their own namespace and file. If there are more than a handful, generate them
from a CSV. Give every new term a label and a one-sentence comment saying what it means and,
where useful, why the standard term didn't fit. Templates are in the reference files.

Don't add to 223P's own namespace. Don't change a standard term's meaning to fit your data.

## 5. Keep the original meaning next to the chosen class

The class is an interpretation, so keep what it was interpreted from:
- `rdfs:label` set to the original point name;
- the external reference (BACnet device, object and object name);
- for enumerated points, which BACnet state value maps to which member, with the raw state
  text. A typo such as "Smoke Evacution" is fixed in the member name and kept as-is in the
  state text;
- the Brick class alongside a 223P property (for example `rdfs:seeAlso`), so Brick-level
  specificity survives where 223P has no equivalent.

Also produce a mapping table with one row per point: reference, name, chosen class(es), kind or
quantity kind and unit, aspects, and the state mapping. It's what a reviewer actually reads.

## 6. Check consistency, then validate against a baseline

- Run query 10 on your model. Every combination of property class, kind and aspects should be
  something you'd defend. A lone enable point on `Binary-OnOff` among twenty on
  `Binary-Logical` is usually a slip. BV11 "Maintenance Mode Enable" was one such slip in the
  VAV model.
- Validate with SHACL:

  ```bash
  python scripts/validate.py model.ttl extension.ttl --ontology 223p.ttl --ontology g36.ttl \
      --ontology qudt-units.ttl --ontology qudt-quantitykinds.ttl --baseline previous_model.ttl
  ```

  Include the QUDT vocabularies when the model uses units. When you're changing an existing
  model, always pass `--baseline`: what matters is which results appeared or disappeared, not
  the raw count, since 223P reports many advisory warnings on any partial model. Results on the
  ontology's own nodes are counted separately and ignored; 223P v1.0 reports a few against
  itself.

  Run the script with a Python that has BuildingMOTIF installed (the `gtf-buildingmotif`
  branch), so it uses the pyshifty engine. On a 4,300-triple VAV model, pyshifty took about 20
  seconds, TopQuadrant about 50, and pyshacl hadn't finished after 15 minutes. Don't use
  pyshacl for 223P.

## Working alongside BuildingMOTIF

If the model is being built with BuildingMOTIF, its own agent skill (in
`.agents/skills/buildingmotif/` on the `gtf-buildingmotif` branch) owns the build, validate and
repair loop: templates, `model.validate`, repair witnesses and evidence. This skill supplies
what that loop can't: which class, enumeration kind, aspects and extension terms each point
should get.

Don't let repair proposals choose a class or enumeration kind for you. Validation checks
*type*, not *meaning*. On the VAV model, with its window-contact property stripped of its kind,
pyshifty's top-ranked "sound, makes progress" repair was
`hasEnumerationKind s223:Aspect-Alarm`, and the other candidates were all aspects. They all
pass validation, because the 223P shape only asks for some `EnumerationKind`. The right answer,
`Binary-Position`, comes from the state texts (Opened/Closed), which validation never sees.
Use repair to find *what* is missing, and this workflow to decide *what it should be*.

## 7. Report the decisions, not just the result

End with:
- **Reused**: which standard terms covered which groups of points.
- **Extended**: each new term and the gap it fills.
- **Judgement calls**, each with the alternative and what switching would take. For example:
  - semantics vs a shape: Window Opened/Closed → `Binary-Position`, although G36 expects
    `Binary-OnOff`;
  - ambiguous state texts: "Standby Mode" with Vacant/Occupied states was modelled as occupancy;
  - lossy but standard mappings: "Calibrating" → `OnOff-On`.
- **Not modelled**: points you skipped or left provisional (missing state texts, objects not in
  the point list), and why.
- **Validation**: what changed against the baseline.

Someone will check these choices against the building or the sequence of operations, and the
report tells them where to look.
