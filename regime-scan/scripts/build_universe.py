"""
Regenerate the breadth universe from the live S&P 500 constituents.

WHY a separate generator (and not just folded into scan.py): yfinance cannot
return index membership — `^GSPC` exposes no constituents attribute, and an
ETF's `funds_data.top_holdings` caps at the top 10. So we pull the list from
Wikipedia's "List of S&P 500 companies" table via pandas.read_html, which also
carries the GICS-sector column, with no API key and no extra service.

Writes into ../state/:
  - breadth_universe.txt              the live list scan.py reads each run
  - breadth_universe.<YYYY>-Q<N>.txt  a dated, pinned quarterly snapshot
  - breadth_universe.bak.txt          one-step undo of the previous live file

REFRESH CADENCE — quarterly, NOT every scan. The breadth signal's whole value
is the *slope* of "% above 50DMA" across days; churning the universe injects
compositional noise into that slope. So freeze the universe within a quarter and
only regenerate at quarter boundaries. The dated snapshots are the audit trail.

Usage:
  uv run --with 'pandas>=2' --with lxml --with requests python build_universe.py
  ...                                                   build_universe.py --dry-run
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
LIVE_FILE = STATE_DIR / "breadth_universe.txt"
UA = "Mozilla/5.0 (regime-scan breadth-universe builder)"


def fetch_constituents() -> pd.DataFrame:
    """Return a 2-col frame (symbol, sector) from the Wikipedia S&P 500 table.
    Wikipedia 403s a bare urllib User-Agent, so fetch via requests with a UA
    and hand the HTML to read_html."""
    resp = requests.get(WIKI_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    for df in pd.read_html(io.StringIO(resp.text)):
        cols = {str(c).strip().lower(): c for c in df.columns}
        if "symbol" in cols and any("sector" in c for c in cols):
            sym = cols["symbol"]
            sec = next(cols[c] for c in cols if "sector" in c)
            out = df[[sym, sec]].rename(columns={sym: "symbol", sec: "sector"})
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out["sector"] = out["sector"].astype(str).str.strip()
            return out
    raise SystemExit("No S&P 500 table with Symbol + Sector columns found.")


def yf_symbol(s: str) -> str:
    """Wikipedia uses dotted class shares (BRK.B, BF.B); yfinance wants dashes."""
    return s.replace(".", "-").upper()


def current_quarter(now: datetime) -> str:
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Regenerate the breadth universe from S&P 500 constituents.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + print the sector breakdown, write nothing.")
    args = ap.parse_args()

    df = fetch_constituents()
    df = df.assign(symbol=df["symbol"].map(yf_symbol))
    df = df.drop_duplicates("symbol").sort_values(["sector", "symbol"])
    tickers = df["symbol"].tolist()

    counts = df.groupby("sector").size().sort_values(ascending=False)
    print(f"Fetched {len(tickers)} S&P 500 names across {counts.size} GICS "
          f"sectors:", file=sys.stderr)
    for sec, n in counts.items():
        print(f"  {n:>3}  {sec}", file=sys.stderr)

    # Sanity floor: the live file feeds the daily scan, so refuse to overwrite
    # it with a suspiciously short list (truncated fetch / Wikipedia re-layout).
    # The S&P 500 is ~500 names; anything under 400 is a broken parse.
    if len(tickers) < 400:
        raise SystemExit(f"Only {len(tickers)} constituents parsed (<400) — "
                         "refusing to overwrite the live universe; the source "
                         "layout may have changed.")

    now = datetime.now()
    quarter = current_quarter(now)
    stamp = now.strftime("%Y-%m-%d")
    content = (
        f"# regime-scan breadth universe — S&P 500 constituents\n"
        f"# generated {stamp} ({quarter}) from {WIKI_URL}\n"
        f"# {len(tickers)} names. Refresh QUARTERLY only — see build_universe.py.\n"
        + "\n".join(tickers) + "\n"
    )

    if args.dry_run:
        print(f"\n--dry-run: nothing written. First 10: {', '.join(tickers[:10])}",
              file=sys.stderr)
        return 0

    STATE_DIR.mkdir(exist_ok=True)
    if LIVE_FILE.exists():
        (STATE_DIR / "breadth_universe.bak.txt").write_text(LIVE_FILE.read_text())
        print("Backed up previous list -> breadth_universe.bak.txt", file=sys.stderr)
    LIVE_FILE.write_text(content)
    snapshot = STATE_DIR / f"breadth_universe.{quarter}.txt"
    snapshot.write_text(content)
    print(f"Wrote breadth_universe.txt and {snapshot.name} "
          f"({len(tickers)} names).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
