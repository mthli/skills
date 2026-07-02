"""Record-scoring pipeline: load -> enrich -> aggregate."""
import json

import vendor_api


def load_records(path):
    with open(path) as f:
        return json.loads(f.read())


def process(records):
    enriched = []
    for record in records:
        enriched.append(vendor_api.enrich(record))
    return enriched


def aggregate(enriched):
    return sum(r["score"] for r in enriched)
