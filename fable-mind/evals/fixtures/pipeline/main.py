import time

from pipeline import load_records, process, aggregate

started = time.perf_counter()
records = load_records("data/records.json")
enriched = process(records)
total = aggregate(enriched)
elapsed = time.perf_counter() - started
print(f"records={len(enriched)} checksum={total} elapsed={elapsed:.2f}s")
