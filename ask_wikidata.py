import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import litellm
import requests
from rdflib.plugins.sparql import prepareQuery
from dotenv import load_dotenv

_ = load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
MAX_RETRIES = 5
SCHEMA_PATH = Path(__file__).parent / "wikidata_schema.json"

_HEADERS = {"Accept": "application/sparql-results+json", "User-Agent": "kgg-sparql/0.1"}


# --- Offline schema -----------------------------------------------------------

def _load_schema() -> dict | None:
    if not SCHEMA_PATH.exists():
        logger.warning("Offline schema not found at %s — run download_schema.py first", SCHEMA_PATH)
        return None
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    logger.info("Loaded offline schema: %d properties, %d classes",
                len(schema.get("properties", {})), len(schema.get("classes", {})))
    return schema

_SCHEMA = _load_schema()


# --- SPARQL / LLM helpers ---------------------------------------------------

def _sparql(endpoint: str, query: str) -> list[dict]:
    response = requests.get(endpoint, params={"query": query, "format": "json"}, headers=_HEADERS)
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def _escape_sparql(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")


def _extract_sparql(text: str) -> str:
    """Strip markdown fences and thinking blocks from LLM output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"```(?:sparql)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _llm(messages: list[dict], parse_sparql: bool = False) -> str:
    response = litellm.completion(model=LLM_MODEL, messages=messages)
    content = response.choices[0].message.content.strip()
    if parse_sparql:
        return _extract_sparql(content)
    # Strip <think>...</think> blocks emitted by reasoning models (e.g. Qwen3)
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


# --- Discovery step 1: extract named entities from the question --------------

_ENTITY_EXTRACTION_PROMPT = """\
Extract the named entities from this question that need to be looked up in a knowledge graph.
Return a JSON array of strings, nothing else.
Question: "{question}"
Example output: ["Tom Cruise"]
"""

def _extract_entities(question: str) -> list[str]:
    raw = _llm([{"role": "user", "content": _ENTITY_EXTRACTION_PROMPT.format(question=question)}])
    entities = json.loads(raw)
    logger.debug("Extracted entities: %s", entities)
    return entities


# --- Discovery step 2: resolve entity names to URIs via Wikidata search ------

_WIKIDATA_ENTITY_SEARCH_QUERY = """\
SELECT ?entity ?entityLabel WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:api "EntitySearch" ;
                    wikibase:endpoint "www.wikidata.org" ;
                    mwapi:search "{term}" ;
                    mwapi:language "en" .
    ?entity wikibase:apiOutputItem mwapi:item .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
}} LIMIT 5
"""

def _search_term(endpoint: str, term: str) -> tuple[str, list[dict]]:
    safe_term = _escape_sparql(term)
    try:
        bindings = _sparql(endpoint, _WIKIDATA_ENTITY_SEARCH_QUERY.format(term=safe_term))
        if bindings:
            matches = [
                {"uri": b["entity"]["value"], "label": b["entityLabel"]["value"]}
                for b in bindings if "entityLabel" in b
            ]
            logger.debug("Entity search %r → %s", term, matches)
            return term, matches
    except Exception as e:
        logger.debug("Entity search failed for %r: %s", term, e)
    return term, []

def _search_entities(endpoint: str, terms: list[str]) -> dict[str, list[dict]]:
    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(_search_term, endpoint, term): term for term in terms}
        return {future.result()[0]: future.result()[1] for future in as_completed(futures)}


# --- Discovery step 2b: disambiguate entities using LLM ---------------------

_DISAMBIGUATE_PROMPT = """\
Given the question and the candidate entities from Wikidata, pick the single best \
match for each search term. Return a JSON object mapping each term to the chosen URI.
Return ONLY the JSON, nothing else.

Question: "{question}"

Candidates:
{candidates}

Example output: {{"Tom Cruise": "http://www.wikidata.org/entity/Q37079"}}
"""

def _disambiguate(question: str, entities: dict[str, list[dict]]) -> dict[str, list[dict]]:
    needs_disambiguation = {t: ms for t, ms in entities.items() if len(ms) > 1}
    if not needs_disambiguation:
        return entities

    candidate_lines = []
    for term, matches in needs_disambiguation.items():
        candidate_lines.append(f"  {term!r}:")
        for m in matches:
            candidate_lines.append(f"    - <{m['uri']}> ({m['label']!r})")

    raw = _llm([{"role": "user", "content": _DISAMBIGUATE_PROMPT.format(
        question=question, candidates="\n".join(candidate_lines)
    )}])
    try:
        picks = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Disambiguation LLM returned invalid JSON, keeping all candidates")
        return entities

    result = {}
    for term, matches in entities.items():
        if term in picks:
            chosen_uri = picks[term]
            picked = [m for m in matches if m["uri"] == chosen_uri]
            result[term] = picked if picked else matches[:1]
        else:
            result[term] = matches
    logger.debug("Disambiguated entities: %s", result)
    return result


# --- Discovery step 3: get entity type and look up properties from schema ----

_ENTITY_TYPE_QUERY = """\
SELECT DISTINCT ?type WHERE {{
  <{uri}> wdt:P31/wdt:P279* ?type .
}} LIMIT 30
"""

def _get_entity_types(endpoint: str, uri: str) -> list[str]:
    """Fetch P31 (instance of) types and their parent classes via P279 (subclass of)."""
    try:
        bindings = _sparql(endpoint, _ENTITY_TYPE_QUERY.format(uri=uri))
        types = [b["type"]["value"].rsplit("/", 1)[-1] for b in bindings]
        logger.debug("Entity types for <%s>: %s (%d total)", uri, types[:5], len(types))
        return types
    except Exception as e:
        logger.debug("Type lookup failed for <%s>: %s", uri, e)
        return []


def _schema_properties_for_types(entity_types: list[str]) -> list[dict]:
    """Look up properties from offline schema based on entity types."""
    if not _SCHEMA:
        return []

    properties = _SCHEMA["properties"]
    class_properties = _SCHEMA["class_properties"]
    classes = _SCHEMA.get("classes", {})

    # Collect all property IDs relevant to these types
    prop_ids = set()
    matched_classes = []
    for type_id in entity_types:
        if type_id in class_properties:
            prop_ids.update(class_properties[type_id])
            matched_classes.append(f"{classes.get(type_id, type_id)} ({type_id})")

    logger.info("Schema lookup: types %s → %d properties", matched_classes, len(prop_ids))

    # Build property list with metadata
    result = []
    for pid in sorted(prop_ids):
        if pid in properties:
            p = properties[pid]
            entry = {
                "property": f"http://www.wikidata.org/prop/direct/{pid}",
                "label": p["label"],
                "description": p.get("description", ""),
                "datatype": p.get("datatype", ""),
            }
            # Add range type info for WikibaseItem properties
            if "range_types" in p:
                range_labels = [classes.get(rt, rt) for rt in p["range_types"][:3]]
                entry["range"] = ", ".join(range_labels)
            result.append(entry)

    return result


# --- Fallback: live DESCRIBE (used when offline schema is unavailable) -------

_ENTITY_PROPS_QUERY = """\
SELECT DISTINCT ?prop ?propLabel ?value ?propType WHERE {{
  <{uri}> ?prop ?value .
  FILTER(STRSTARTS(STR(?prop), "http://www.wikidata.org/prop/direct/"))
  ?propEntity wikibase:directClaim ?prop ;
              wikibase:propertyType ?propType .
  FILTER(?propType IN (
    wikibase:WikibaseItem,
    wikibase:Quantity,
    wikibase:Time,
    wikibase:Monolingualtext,
    wikibase:String,
    wikibase:GlobeCoordinate,
    wikibase:Math
  ))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
}}
"""

def _describe_entity(endpoint: str, uri: str) -> list[dict]:
    """Live fallback when offline schema is not available."""
    try:
        bindings = _sparql(endpoint, _ENTITY_PROPS_QUERY.format(uri=uri))
        props = [
            {
                "property": b["prop"]["value"],
                "label": b.get("propLabel", {}).get("value", ""),
                "value": b["value"]["value"],
            }
            for b in bindings
        ]
        logger.debug("DESCRIBE <%s>: %d properties", uri, len(props))
        return props
    except Exception as e:
        logger.warning("DESCRIBE failed for <%s>: %s", uri, e)
        return []


# --- SPARQL generation -------------------------------------------------------

_SPARQL_GENERATION_PROMPT = """\
You are a SPARQL expert for Wikidata. Generate a SPARQL query to answer the following question.
Return ONLY the SPARQL query, no explanation, no markdown fences.

CRITICAL: You MUST use the exact property and entity identifiers from the sections below. \
Do NOT guess or invent IDs. Match the property label to what the question asks for, \
then use the corresponding wdt:PXXXX or wd:QXXXX identifier.

Rules:
- Always alias the final answer as ?answer in the SELECT clause, with no other variables selected
- For simple factual lookups, use wdt: (direct property) predicates
- When the question asks for a derived/computed value (e.g. age from a date, duration between events), \
use SPARQL functions (YEAR, MONTH, DAY, NOW, etc.) to compute it from the raw property value
- When the question asks for the latest/current value of something that changes over time \
(e.g. population, GDP, head of state), use the qualified form: p:PXXX/ps:PXXX with pq:P585 \
(point in time) qualifier, ORDER BY DESC(?date) LIMIT 1
- Use inline aliasing: SELECT (?value AS ?answer) or SELECT (expression AS ?answer)
- Prefer the simplest query that answers the question

Think step by step:
1. What is the question really asking for?
2. Which property from the available list matches what is needed?
3. Is the answer a direct value, or does it need computation/qualification?
4. Write the simplest correct query.

Examples (for reference — adapt entity and property IDs to the actual question):

Direct lookup:
  SELECT ?answer WHERE {{ wd:Q123 wdt:P36 ?answer . }}

Computed value from a date:
  SELECT (YEAR(NOW()) - YEAR(?dob) - IF(MONTH(NOW())<MONTH(?dob)||(MONTH(NOW())=MONTH(?dob)&&DAY(NOW())<DAY(?dob)),1,0) AS ?answer) WHERE {{ wd:Q123 wdt:P569 ?dob . }}

Latest qualified value:
  SELECT (?val AS ?answer) WHERE {{ wd:Q456 p:P1082 ?stmt . ?stmt ps:P1082 ?val ; pq:P585 ?date . }} ORDER BY DESC(?date) LIMIT 1

Multi-hop (traverse relations):
  SELECT ?answer WHERE {{ wd:Q123 wdt:P19 ?birthplace . ?birthplace wdt:P17 ?answer . }}

Question: "{question}"

Resolved entities:
{entities}

Available properties on those entities:
{properties}
"""

_RETRY_PROMPT = """\
The following SPARQL query has an error:

{sparql}

Error: {error}

Fix the query and return ONLY the corrected SPARQL, no explanation, no markdown fences.
"""

_WIKIDATA_NAMESPACES = {
    "wd": "http://www.wikidata.org/entity/",
    "wdt": "http://www.wikidata.org/prop/direct/",
    "wikibase": "http://wikiba.se/ontology#",
    "p": "http://www.wikidata.org/prop/",
    "ps": "http://www.wikidata.org/prop/statement/",
    "pq": "http://www.wikidata.org/prop/qualifier/",
    "bd": "http://www.bigdata.com/rdf#",
    "schema": "http://schema.org/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "mwapi": "https://www.mediawiki.org/ontology#API/",
    "psv": "http://www.wikidata.org/prop/statement/value/",
    "pqv": "http://www.wikidata.org/prop/qualifier/value/",
    "wikibase": "http://wikiba.se/ontology#",
}

def _validate_sparql(sparql: str, entities: dict[str, list[dict]] | None = None) -> str | None:
    """Validate SPARQL syntax via rdflib parser and check structural requirements."""
    # Syntax check
    try:
        prepareQuery(sparql, initNs=_WIKIDATA_NAMESPACES)
    except Exception as e:
        return f"SPARQL syntax error: {e}"

    # Must select ?answer
    if not re.search(r"\bAS\s+\?answer\b", sparql, re.IGNORECASE) and \
       not re.search(r"\bSELECT\b[^{]*\?\banswer\b", sparql, re.IGNORECASE):
        return "Query does not select ?answer — add AS ?answer alias in SELECT"

    # Check that resolved entity URIs appear in the query
    if entities:
        used_uris = {m["uri"] for ms in entities.values() for m in ms}
        if used_uris and not any(uri in sparql or uri.rsplit("/", 1)[-1] in sparql for uri in used_uris):
            return f"Query does not reference any resolved entity URIs: {used_uris}"

    return None

def _fmt_entities(entities: dict[str, list[dict]]) -> str:
    lines = []
    for term, matches in entities.items():
        for m in matches:
            entity_id = m["uri"].rsplit("/", 1)[-1]
            lines.append(f"  {term!r} → wd:{entity_id} ({m['label']!r})")
    return "\n".join(lines) or "  (none found)"

def _fmt_properties(props_by_uri: dict[str, list[dict]]) -> str:
    lines = []
    for uri, props in props_by_uri.items():
        entity_id = uri.rsplit("/", 1)[-1]
        lines.append(f"  wd:{entity_id}:")
        for p in props:
            label = p.get("label", "")
            prop_id = p["property"].rsplit("/", 1)[-1]
            # Schema format: has description/datatype/range, no value
            if "datatype" in p:
                desc = p.get("description", "")
                dtype = p.get("datatype", "")
                range_info = p.get("range", "")
                detail = f"{label} ({dtype})"
                if desc:
                    detail += f" — {desc}"
                if range_info:
                    detail += f" → [{range_info}]"
                lines.append(f"    wdt:{prop_id} — {detail}")
            # Live DESCRIBE format: has value
            else:
                lines.append(f"    wdt:{prop_id} — {label} — {p['value']}")
    return "\n".join(lines) or "  (none found)"


# --- Main entry point --------------------------------------------------------

def ask(question: str, endpoint: str = "https://query.wikidata.org/sparql") -> str:
    logger.info("Question: %r | Model: %s | Endpoint: %s", question, LLM_MODEL, endpoint)

    # Step 1: extract entities
    entity_terms = _extract_entities(question)

    # Step 2: resolve entity names to URIs
    entities = _search_entities(endpoint, entity_terms)

    # Step 2b: disambiguate to single best match per term
    entities = _disambiguate(question, entities)

    # Step 3: get properties — from offline schema if available, else live DESCRIBE
    uris = [m["uri"] for matches in entities.values() for m in matches[:1]]
    props_by_uri = {}

    if _SCHEMA:
        # Fetch entity types and look up schema properties (one lightweight query per entity)
        for uri in uris:
            entity_types = _get_entity_types(endpoint, uri)
            schema_props = _schema_properties_for_types(entity_types)
            if schema_props:
                props_by_uri[uri] = schema_props
            else:
                logger.info("No schema match for <%s> types %s — falling back to live DESCRIBE", uri, entity_types)
                props_by_uri[uri] = _describe_entity(endpoint, uri)
    else:
        # No offline schema — fall back to live DESCRIBE
        with ThreadPoolExecutor() as executor:
            props_by_uri = dict(zip(
                uris,
                executor.map(lambda uri: _describe_entity(endpoint, uri), uris)
            ))

    # SPARQL generation
    prompt = _SPARQL_GENERATION_PROMPT.format(
        question=question,
        entities=_fmt_entities(entities),
        properties=_fmt_properties(props_by_uri),
    )
    messages = [{"role": "user", "content": prompt}]
    sparql = _llm(messages, parse_sparql=True)
    logger.debug("Generated SPARQL:\n%s", sparql)

    # Execute with retry
    for attempt in range(1, MAX_RETRIES + 1):
        error = _validate_sparql(sparql, entities)
        if error:
            logger.warning("Local validation failed: %s", error)
        else:
            try:
                logger.info("Attempt %d/%d — executing SPARQL", attempt, MAX_RETRIES)
                bindings = _sparql(endpoint, sparql)
                logger.info("Got %d result(s)", len(bindings))
                if not bindings:
                    error = "Query returned no results"
                    logger.warning(error)
                else:
                    first = bindings[0]
                    binding = first.get("answer") or next(
                        (v for v in first.values() if v and v.get("value")), None
                    )
                    if not binding or not binding.get("value"):
                        error = "Query returned a row but all values were null/unbound"
                        logger.warning(error)
                    else:
                        result = str(binding["value"])
                        logger.info("Answer: %r", result)
                        return result
            except requests.HTTPError as e:
                error = str(e)
                logger.warning("HTTP %s — asking LLM to fix the query", e.response.status_code)

        if attempt == MAX_RETRIES:
            raise RuntimeError(f"Failed after {MAX_RETRIES} attempts. Last error: {error}")
        messages += [
            {"role": "assistant", "content": sparql},
            {"role": "user", "content": _RETRY_PROMPT.format(sparql=sparql, error=error)},
        ]
        sparql = _llm(messages, parse_sparql=True)
        logger.debug("Revised SPARQL:\n%s", sparql)


if __name__ == "__main__":
    assert "63" == ask("how old is Tom Cruise")
    assert "67" == ask("what age is Madonna?")
    assert "8799728" == ask("what is the population of London")
    assert "8804190" == ask("what is the population of New York?")
    print("All assertions passed")
