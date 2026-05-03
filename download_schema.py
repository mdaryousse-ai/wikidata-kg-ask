"""Download Wikidata property metadata and class-property mappings for offline schema lookup.

Produces wikidata_schema.json with:
  - properties: {P-id: {label, description, datatype, range_types}}
  - class_properties: {Q-id: [P-id, ...]}
  - classes: {Q-id: label}

Usage:
    uv run python download_schema.py
"""

import json
import logging
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"Accept": "application/sparql-results+json", "User-Agent": "kgg-sparql/0.1"}
OUTPUT = "wikidata_schema.json"

USEFUL_TYPES = {
    "http://wikiba.se/ontology#WikibaseItem",
    "http://wikiba.se/ontology#Quantity",
    "http://wikiba.se/ontology#Time",
    "http://wikiba.se/ontology#Monolingualtext",
    "http://wikiba.se/ontology#String",
    "http://wikiba.se/ontology#GlobeCoordinate",
    "http://wikiba.se/ontology#Math",
}


def _query(sparql: str) -> list[dict]:
    resp = requests.get(ENDPOINT, params={"query": sparql, "format": "json"}, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


def download_properties() -> dict:
    """Fetch all properties with label, description, and datatype."""
    logger.info("Downloading property metadata...")
    sparql = """\
SELECT ?prop ?propLabel ?propDescription ?propType WHERE {
  ?prop a wikibase:Property ;
        wikibase:propertyType ?propType .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
"""
    bindings = _query(sparql)
    properties = {}
    skipped = 0
    for b in bindings:
        prop_uri = b["prop"]["value"]
        prop_id = prop_uri.rsplit("/", 1)[-1]
        datatype = b.get("propType", {}).get("value", "")

        if datatype not in USEFUL_TYPES:
            skipped += 1
            continue

        properties[prop_id] = {
            "label": b.get("propLabel", {}).get("value", prop_id),
            "description": b.get("propDescription", {}).get("value", ""),
            "datatype": datatype.rsplit("#", 1)[-1],
        }

    logger.info("Downloaded %d useful properties (%d skipped)", len(properties), skipped)
    return properties


def download_class_properties() -> dict:
    """Fetch property type constraints: which properties apply to which classes."""
    logger.info("Downloading class-property mappings from type constraints...")
    sparql = """\
SELECT ?prop ?class WHERE {
  ?prop p:P2302 ?stmt .
  ?stmt ps:P2302 wd:Q21503250 .
  ?stmt pq:P2308 ?class .
}
"""
    bindings = _query(sparql)

    class_props: dict[str, list[str]] = {}
    for b in bindings:
        prop_id = b["prop"]["value"].rsplit("/", 1)[-1]
        class_id = b["class"]["value"].rsplit("/", 1)[-1]
        class_props.setdefault(class_id, []).append(prop_id)

    # Deduplicate
    for cls in class_props:
        class_props[cls] = sorted(set(class_props[cls]))

    logger.info("Downloaded mappings for %d classes", len(class_props))
    return class_props


def download_class_labels(class_ids: set[str]) -> dict:
    """Fetch labels for classes that appear in type constraints."""
    logger.info("Downloading class labels...")
    sparql = """\
SELECT DISTINCT ?class ?classLabel WHERE {
  ?prop p:P2302 ?stmt .
  ?stmt ps:P2302 wd:Q21503250 .
  ?stmt pq:P2308 ?class .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" }
}
"""
    bindings = _query(sparql)
    classes = {}
    for b in bindings:
        class_id = b["class"]["value"].rsplit("/", 1)[-1]
        if class_id in class_ids:
            classes[class_id] = b.get("classLabel", {}).get("value", class_id)

    logger.info("Downloaded %d class labels", len(classes))
    return classes


def download_range_constraints(property_ids: set[str]) -> dict[str, list[str]]:
    """Fetch value-type constraints: what type of entity a WikibaseItem property points to."""
    logger.info("Downloading value-type constraints (range types)...")
    sparql = """\
SELECT ?prop ?rangeClass WHERE {
  ?prop p:P2302 ?stmt .
  ?stmt ps:P2302 wd:Q21510865 .
  ?stmt pq:P2308 ?rangeClass .
}
"""
    bindings = _query(sparql)
    ranges: dict[str, list[str]] = {}
    for b in bindings:
        prop_id = b["prop"]["value"].rsplit("/", 1)[-1]
        if prop_id in property_ids:
            range_class = b["rangeClass"]["value"].rsplit("/", 1)[-1]
            ranges.setdefault(prop_id, []).append(range_class)

    logger.info("Downloaded range constraints for %d properties", len(ranges))
    return ranges


def main():
    properties = download_properties()
    time.sleep(2)

    class_properties = download_class_properties()
    time.sleep(2)

    all_class_ids = set(class_properties.keys())
    classes = download_class_labels(all_class_ids)
    time.sleep(2)

    ranges = download_range_constraints(set(properties.keys()))

    # Attach range types to properties
    for prop_id, range_classes in ranges.items():
        if prop_id in properties:
            properties[prop_id]["range_types"] = range_classes

    schema = {
        "properties": properties,
        "class_properties": class_properties,
        "classes": classes,
    }

    with open(OUTPUT, "w") as f:
        json.dump(schema, f, indent=2)

    size_kb = len(json.dumps(schema)) / 1024
    logger.info(
        "Schema written to %s (%.0f KB) — %d properties, %d classes",
        OUTPUT, size_kb, len(properties), len(classes),
    )


if __name__ == "__main__":
    main()
