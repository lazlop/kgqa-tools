# Why there's a `core` and an `extended` toolset

Every tool description is sent to the agent on every turn, and every extra tool is one more
thing the agent can decide to explore with. So the server ships two toolsets (see
[Choosing a toolset](../README.md#choosing-a-toolset) for how to pick one):

- **`core`** (`load_dataset`, `list_datasets`, `summarize_schema`, `run_query`) is the minimal
  question-answering loop: summarize the graph's structure once, then write and diagnose queries
  against it. It's the configuration evaluated in the paper (a B-Schema summary plus a query
  validator).
- **`extended`** (the default) adds `search`, which looks up classes, predicates and instances
  by name and, with `include_predicates`/`include_cbd`, returns a term's definition, parents,
  Brick tags and 223P constraints in the same call. That covers work beyond answering a
  single question against a graph whose vocabulary is already clear: exploring an unfamiliar
  ontology, picking the right class or `EnumerationKind`, or mapping a point list to 223P and
  Brick classes (see [`skills/building-point-classes/`](../skills/building-point-classes/)).

## How they compare on KGQA

Gemma 4 (`lbl/gemma-4-thinking`), zero-shot, on the BuildingQA benchmark from
[`kgqa-agent`](https://github.com/lazlop/kgqa-agent): 188 questions over four building graphs,
with a 250k total-token budget per question. Higher is better for the F1 scores; tokens are
the mean total (prompt + completion) per question.

| Toolset | Row-matching F1 | Entity-set F1 | Arity F1 | Perfect match | Total tokens / question | Questions over budget |
|---|---|---|---|---|---|---|
| `core` | 0.594 | 0.696 | **0.654** | 0.495 | **40.7k** | **0** |
| `extended` | **0.630** | **0.737** | 0.625 | **0.537** | 88.2k | 14 |

For KGQA, `core` is the efficient choice: it gets within a few points of `extended` (and ahead
on arity F1, i.e. returning the right number of columns) at less than half the tokens, and never
ran out of budget. `extended` buys a few points of row- and entity-set F1, mostly on the larger,
messier graphs, by exploring more, which roughly doubles the token cost. It stays the default
because `search` is what makes the server useful for the ontology work above, not because it
answers benchmark questions more cheaply.

`core`'s numbers are the paper's zero-shot Gemma run; `extended`'s are a `kgqa-agent`
`kgqa_tools` run against this server (which at the time also exposed the since-deprecated
`traverse`; see the README).
