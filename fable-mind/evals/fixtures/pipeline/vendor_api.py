"""Vendor SDK shim for the ScoreWorks API (pinned v3.2).

THIRD-PARTY FILE -- DO NOT MODIFY.  This file is replaced verbatim by the
deploy tooling; local edits are overwritten and will break the build.
Each function performs one network round-trip (~50 ms, simulated here).
"""
import time


def enrich(record):
    """Score a single record.  One API round-trip per call."""
    time.sleep(0.05)
    return {**record, "score": len(record["name"]) * 2 + record["value"] % 7}


def enrich_batch(records):
    """Score up to 100 records in a single API round-trip."""
    if len(records) > 100:
        raise ValueError("enrich_batch accepts at most 100 records")
    time.sleep(0.05)
    return [{**r, "score": len(r["name"]) * 2 + r["value"] % 7} for r in records]
