# SPARQL templates for 223P and Brick taxonomies

Each query was tested against 223P (`v1.0.0-2026`, with the G36 extension) and Brick 1.4.4.
Replace the `VALUES` term with the one you're asking about. They work in kgqa-tools
`run_query` or any SPARQL engine that has the ontology loaded.

Every query declares its prefixes. `run_query` doesn't add missing ones for you, and an
undeclared `rdf:` is the most common slip.

Contents:
1. Children of a class, with kinds and members told apart
2. Ancestors of a class
3. Members of an enumeration kind
4. Overview of all 223P enumeration kinds
5. Shapes that constrain a term (who requires it, and how)
6. Brick classes by quantity and substance
7. Brick classes by tags
8. Deprecated Brick classes and their replacements
9. Size of a subtree before you browse it
10. What a finished model uses (for consistency checks)

## 1. Children of a class

One level at a time. `n_children = 0` means a leaf. In 223P, a leaf under an
`EnumerationKind` is a member (a value), not a kind. Kinds are typed as themselves too, so
`a ?self` doesn't tell them apart. Having no children does.

```sparql
# dataset: s223
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX s223: <http://data.ashrae.org/standard223#>
SELECT ?child ?label (COUNT(DISTINCT ?grandchild) AS ?n_children) WHERE {
  VALUES ?parent { s223:EnumerationKind-Binary }
  ?child rdfs:subClassOf ?parent .
  OPTIONAL { ?child rdfs:label ?label }
  OPTIONAL { ?grandchild rdfs:subClassOf ?child }
} GROUP BY ?child ?label ORDER BY ?child
```

## 2. Ancestors of a class

Returns every edge on every path up, so multiple inheritance (common in Brick) shows up as
several parents for one class.

```sparql
# dataset: brick
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX brick: <https://brickschema.org/schema/Brick#>
SELECT DISTINCT ?class ?parent WHERE {
  VALUES ?start { brick:Fan_On_Off_Status }
  ?start rdfs:subClassOf* ?class .
  ?class rdfs:subClassOf ?parent .
}
```

## 3. Members of an enumeration kind

The leaves below a kind are its allowed values. Use this to check whether a set of BACnet
state texts fits an existing kind.

```sparql
# dataset: s223
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX s223: <http://data.ashrae.org/standard223#>
SELECT ?member ?label WHERE {
  VALUES ?kind { s223:Occupancy-Motion }
  ?member rdfs:subClassOf+ ?kind .
  FILTER NOT EXISTS { ?x rdfs:subClassOf ?member }
  OPTIONAL { ?member rdfs:label ?label }
} ORDER BY ?member
```

## 4. Overview of all 223P enumeration kinds

Every kind that directly holds members, with its member count and labels. Run this once at the
start of a mapping task. Most of the value-set decisions come down to this list. The query
leaves out the `Substance` tree (media, constituents, electricity and signal types). Those are
values for `ofMedium`/`ofConstituent`, not value sets for a point. Browse them with query 1
from `s223:Mix-Fluid` or `s223:Medium-Constituent` when you need one.

```sparql
# dataset: s223
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX s223: <http://data.ashrae.org/standard223#>
SELECT ?kind (COUNT(?member) AS ?n) (GROUP_CONCAT(?mlabel; separator=" | ") AS ?members) WHERE {
  ?kind rdfs:subClassOf+ s223:EnumerationKind .
  ?member rdfs:subClassOf ?kind .
  FILTER NOT EXISTS { ?x rdfs:subClassOf ?member }
  OPTIONAL { ?member rdfs:label ?mlabel }
  FILTER NOT EXISTS { ?kind rdfs:subClassOf* s223:EnumerationKind-Substance }
} GROUP BY ?kind HAVING (COUNT(?member) <= 25) ORDER BY ?kind
```

## 5. Shapes that constrain a term

Finds every SHACL shape that requires the term, through any nesting of property shapes,
`sh:node`, qualified value shapes and `sh:or`/`sh:and` lists, and reports the path it
constrains. Run it for every class and enumeration kind you are about to use. It is how you
find that `g36:Zone` expects its window switch to use `s223:Binary-OnOff`, and it only finds
that if the extension's shapes are loaded in the same dataset.

```sparql
# dataset: s223
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX sh: <http://www.w3.org/ns/shacl#>
PREFIX s223: <http://data.ashrae.org/standard223#>
SELECT DISTINCT ?shape ?how ?path ?message WHERE {
  VALUES ?term { s223:Binary-OnOff }
  ?shape a sh:NodeShape .
  ?shape (sh:property|sh:node|sh:qualifiedValueShape|sh:or|sh:and|sh:xone|rdf:first|rdf:rest)* ?ps .
  ?ps ?how ?term .
  FILTER(?how IN (sh:class, sh:hasValue, sh:in, sh:targetClass, sh:qualifiedValueShape))
  OPTIONAL { ?ps sh:path ?path }
  OPTIONAL { ?shape sh:property ?top . ?top (sh:qualifiedValueShape|sh:node|sh:property)* ?ps . ?top sh:message ?message }
}
```

## 6. Brick classes by quantity and substance

Brick 1.4 classes carry `brick:hasQuantity` (a QUDT quantity kind) and often
`brick:hasSubstance`. That makes this query more reliable than keyword search when a point
name is abbreviated. Swap `brick:Sensor` for `brick:Setpoint` to select the point function.

This only works for sensors and setpoints. In Brick 1.4.4, 82 of the 88 `Status` subclasses
and 64 of the 74 `Command` subclasses have no `hasQuantity`, so use tags (query 7) or search
for those. Even among live sensors a few are missing it (`Discharge_Air_Temperature_Sensor`,
for example), so treat an empty result as "try search", not as "no such class".

```sparql
# dataset: brick
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX brick: <https://brickschema.org/schema/Brick#>
PREFIX qudtqk: <http://qudt.org/vocab/quantitykind/>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?class ?substance WHERE {
  ?class rdfs:subClassOf+ brick:Sensor ;
         brick:hasQuantity qudtqk:Temperature .
  OPTIONAL { ?class brick:hasSubstance ?substance }
  FILTER NOT EXISTS { ?class owl:deprecated true }
} ORDER BY ?substance ?class
```

## 7. Brick classes by tags

Finds classes carrying all the given tags. It's useful when the source data is Haystack-tagged
or when names are cryptic. Add or remove `brick:hasAssociatedTag` lines to match.

```sparql
# dataset: brick
PREFIX brick: <https://brickschema.org/schema/Brick#>
PREFIX tag: <https://brickschema.org/schema/BrickTag#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?class WHERE {
  ?class brick:hasAssociatedTag tag:Enable, tag:Command .
  FILTER NOT EXISTS { ?class owl:deprecated true }
} ORDER BY ?class
```

## 8. Deprecated Brick classes and their replacements

Brick 1.4.4 has about 240 deprecated classes, and search returns them like any other. Many are
the old water `Supply`/`Return` names, now `Leaving`/`Entering` (for example
`Chilled_Water_Supply_Temperature_Sensor` → `Leaving_Chilled_Water_Temperature_Sensor`).
Before you settle on a Brick class, check that it isn't one of them.

```sparql
# dataset: brick
PREFIX brick: <https://brickschema.org/schema/Brick#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?class ?replacement ?why WHERE {
  VALUES ?class { brick:Chilled_Water_Supply_Temperature_Sensor brick:Zone_Air_Temperature_Setpoint brick:Fan_On_Off_Status }
  ?class owl:deprecated true .
  OPTIONAL { ?class brick:isReplacedBy ?replacement }
  OPTIONAL { ?class brick:deprecationMitigationMessage ?why }
}
```

Only deprecated classes come back, so a class missing from the result is current. If none of
them are deprecated, the result is empty. `run_query` may then report the query as broken,
which in this case just means "nothing is deprecated".

## 9. Size of a subtree

Brick's `Point` has more than 900 descendants. Count before you browse, then go down one
level at a time with query 1.

```sparql
# dataset: brick
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX brick: <https://brickschema.org/schema/Brick#>
SELECT ?child (COUNT(DISTINCT ?d) AS ?descendants) WHERE {
  VALUES ?root { brick:Point }
  ?child rdfs:subClassOf ?root .
  OPTIONAL { ?d rdfs:subClassOf+ ?child }
} GROUP BY ?child ORDER BY DESC(?descendants)
```

## 10. What a finished model uses

Run against your own output model, not the ontology. Every combination of property class,
enumeration kind, with a count and the aspects seen among them. Inconsistencies stand out here,
such as enable points split across two kinds, or a kind you meant to retire still in use.

```sparql
# dataset: model
PREFIX s223: <http://data.ashrae.org/standard223#>
SELECT ?cls ?kind (GROUP_CONCAT(DISTINCT REPLACE(STR(?aspect), "^.*#", ""); separator=" ") AS ?aspects) (COUNT(DISTINCT ?p) AS ?n) WHERE {
  ?p a ?cls ; s223:hasEnumerationKind ?kind .
  OPTIONAL { ?p s223:hasAspect ?aspect }
} GROUP BY ?cls ?kind ORDER BY ?kind
```
