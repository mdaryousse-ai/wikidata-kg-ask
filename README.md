# kgg-sparql

Translates natural language questions into SPARQL queries against Wikidata and returns string answers.

```python
from ask_wikidata import ask

ask("how old is Tom Cruise")              # "63"
ask("what age is Madonna?")               # "67"
ask("what is the population of London")   # "8799728"
ask("what is the population of New York?") # "8804190"
```

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Create a `.env` file with your LLM provider credentials:

```env
LLM_MODEL=gpt-4o                   # any model supported by litellm
OPENAI_API_KEY=sk-...               # or whichever provider your model uses
```

`LLM_MODEL` accepts any [litellm model string](https://docs.litellm.ai/docs/providers) (e.g. `gpt-4o`, `anthropic/claude-sonnet-4-20250514`, `groq/qwen3-32b`).

## Usage

```bash
# Download the Wikidata property schema (one-time, ~1.2 MB)
uv run python download_schema.py

# Run the built-in assertions
uv run python ask_wikidata.py

# Ask any question
uv run python -c "from ask_wikidata import ask; print(ask('what is the capital of France'))"
```

The offline schema (`wikidata_schema.json`) is optional but recommended — without it the code falls back to live property queries per entity, which is slower and cannot support multi-hop questions.

## How it works

The `ask()` function follows a **discover-then-generate** pattern. Rather than hardcoding SPARQL templates for known question types, it resolves entities, looks up their schema, and lets an LLM generate an appropriate query grounded in real Wikidata properties.

### Pipeline

```
Question
  |
  v
[1] Entity extraction           (LLM)
  |
  v
[2] Entity resolution           (Wikidata EntitySearch API via SPARQL)
  |
  v
[2b] Disambiguation             (LLM — only when multiple candidates)
  |
  v
[3] Property lookup             (offline schema via P31 type → property constraints)
  |                              (fallback: live DESCRIBE if no schema)
  v
[4] SPARQL generation           (LLM — grounded by resolved entities + properties)
  |
  v
[5] Validation + execution      (rdflib parse → Wikidata endpoint → retry on error)
  |
  v
Answer
```

### Step 1 — Entity extraction

The LLM extracts named entities from the question (e.g. `"Tom Cruise"` from "how old is Tom Cruise"). This is a simple JSON-array extraction prompt.

### Step 2 — Entity resolution

Each extracted term is searched against Wikidata's `EntitySearch` MediaWiki API (via SPARQL `SERVICE wikibase:mwapi`). This returns up to 5 candidate entities with their URIs and English labels. Search terms are escaped to prevent SPARQL injection.

### Step 2b — Disambiguation

When multiple candidates are returned for a term (e.g. "London" could be a city, a film, or a person), the LLM picks the best match given the question context. If there's only one candidate, this step is skipped.

### Step 3 — Property lookup

**With offline schema (recommended):** The entity's `P31` (instance of) types and their parent classes via `P279` (subclass of) are fetched with a single SPARQL query using the path `wdt:P31/wdt:P279*`. This walks up the class hierarchy — for example, London (`Q84`) is a `Q515` (city) which is a subclass of `Q486972` (human settlement) which is a subclass of `Q56061` (administrative territorial entity). All types along the hierarchy are looked up in the local `wikidata_schema.json` to retrieve properties from Wikidata's own property type constraints (P2302), and the results are unioned together. This is important because most properties are constrained against broad parent classes, not specific leaf types.

For example, for a "human" entity the schema provides ~200 useful properties with labels, descriptions, datatypes, and range types:

```
wd:Q37079:
  wdt:P569 — date of birth (Time) — date on which the subject was born
  wdt:P27 — country of citizenship (WikibaseItem) — the object is a country → [country]
  wdt:P106 — occupation (WikibaseItem) — occupation of a person → [occupation]
```

The range type info (e.g. `→ [country]`) tells the LLM what type of entity a property points to, enabling it to reason about multi-hop queries: "if P27 points to a country, and countries have P36 (capital), I can chain them."

**Without offline schema (fallback):** A live DESCRIBE query fetches the entity's actual direct properties (`wdt:` namespace) with labels. This works but is slower, limited to properties the entity happens to have, and cannot inform multi-hop reasoning.

Properties are formatted with `wdt:` prefixes so the LLM can copy identifiers directly into the generated query.

### Step 4 — SPARQL generation

The LLM receives the original question, resolved entities, and available properties. The prompt includes general rules for four query shapes rather than hardcoded patterns:

| Question type | Strategy | Example |
|---|---|---|
| Simple factual lookup | Direct `wdt:` property access | "what is the capital of France" |
| Computed/derived value | SPARQL functions on a stored value | "how old is Tom Cruise" (age from date of birth) |
| Time-series / latest value | Qualified statement with `pq:P585` | "what is the population of London" |
| Multi-hop traversal | Chained triple patterns | "what country was Tom Cruise born in" |

Four generic few-shot examples demonstrate these shapes. The LLM adapts them to the actual entity and property IDs from the discovery steps.

### Step 5 — Validation and execution

Before hitting the Wikidata endpoint, each generated query is validated locally:

1. **Syntax check** — parsed by `rdflib.plugins.sparql.prepareQuery` with all Wikidata namespaces pre-registered. This catches malformed queries without a network round-trip.
2. **Structural checks** — verifies the query selects `?answer` and references the resolved entity URIs.

If execution fails (HTTP error, empty results, null bindings), the error message is fed back to the LLM in a conversational retry loop (up to 5 attempts). The LLM sees the full history of previous attempts, so it can learn from its mistakes within a single question.

### Offline schema — `download_schema.py`

The `download_schema.py` script fetches the following from Wikidata and saves to `wikidata_schema.json` (~1.2 MB):

- **Properties** (~3k): all properties with useful datatypes (WikibaseItem, Quantity, Time, String, Monolingualtext, GlobeCoordinate, Math), with labels and descriptions. ExternalId, Url, and CommonsMedia properties are excluded as noise.
- **Class-property mappings** (~4k classes): derived from Wikidata's property type constraints (P2302 → Q21503250). Maps each class (e.g. Q5 = human) to the properties expected on entities of that type.
- **Class labels**: human-readable names for all mapped classes.
- **Range constraints**: for WikibaseItem properties, what type of entity the property points to (e.g. P27 "country of citizenship" → Q6256 "country").

This schema is downloaded once and committed to the repo. It should be refreshed periodically as Wikidata adds new properties.

## Design rationale

### Why LLM-assisted instead of rule-based?

A rule-based approach (pattern matching on question templates) would be brittle — it works for a fixed set of question formats but fails on paraphrases, new question types, or different entity types. The LLM approach generalizes: given the entity's actual properties, it can construct the right query for questions it has never seen before.

### Why not just ask the LLM to generate SPARQL directly?

Without grounding, LLMs hallucinate property IDs. Wikidata has thousands of properties with opaque identifiers (`P569`, `P1082`, etc.) — even large models frequently guess wrong. The discovery pipeline ensures the LLM only uses property IDs that actually exist on the entity type, dramatically improving first-attempt accuracy.

### Why an offline schema instead of live DESCRIBE?

Live DESCRIBE queries have three limitations: (1) they only return properties the specific entity happens to have, missing properties that are valid for the type but absent on this instance; (2) they cannot inform multi-hop reasoning since you'd need to DESCRIBE every intermediate entity; (3) each query is a network round-trip to Wikidata. The offline schema solves all three — it provides the full set of type-appropriate properties with range information, enabling multi-hop reasoning with zero additional queries.

### Why not use VoID schema?

Wikidata's SPARQL endpoint does not expose VoID (Vocabulary of Interlinked Datasets) metadata. Libraries like [sparql-llm](https://github.com/sib-swiss/sparql-llm) rely on VoID for schema discovery, making them a poor fit for Wikidata. The offline schema approach uses Wikidata's own property constraint system (P2302) instead, which is richer and actually maintained.

### Why rdflib for validation?

The initial bracket-matching validator produced false positives on valid SPARQL (e.g. nested function calls like `YEAR(NOW())` inside string literals). A real SPARQL parser eliminates this class of bugs entirely. `rdflib` is a well-maintained library that handles all SPARQL 1.1 syntax, and the parse step is fast (no network I/O).

### Why litellm?

`litellm` provides a unified interface to 100+ LLM providers (OpenAI, Anthropic, Groq, Ollama, etc.) with a single `completion()` call. This makes the model choice a configuration option rather than a code change — useful for testing different models or switching providers.

## Testing environment

Development and testing was done with the following setup:

- **Model**: `qwen/qwen3-32b` via [Groq](https://groq.com/) (free tier)
- **Python**: 3.13
- **Wikidata endpoint**: `https://query.wikidata.org/sparql`

Groq provides fast inference for open-weight models at no cost, making it practical to iterate on prompts without API spend. Qwen3-32b was chosen as a deliberately constrained model — if the pipeline works reliably with a 32B parameter model, it will work with larger models too. The prompt design (chain-of-thought, copy-paste-friendly formatting, few-shot examples) was specifically tuned to compensate for the smaller model's limitations.

## Dependencies

| Package | Purpose |
|---|---|
| `litellm` | Unified LLM API (supports OpenAI, Anthropic, Groq, Ollama, etc.) |
| `requests` | HTTP client for SPARQL endpoint queries |
| `rdflib` | SPARQL syntax validation via `prepareQuery` |
| `python-dotenv` | Load `.env` configuration |

## Project structure

```
ask_wikidata.py        — implementation of the ask() function and pipeline
download_schema.py     — one-time script to download Wikidata property schema
wikidata_schema.json   — offline property index (~1.2 MB, generated by download_schema.py)
pyproject.toml         — project metadata and dependencies
.env                   — LLM model and API key configuration (not committed)
```
