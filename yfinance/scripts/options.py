#!/usr/bin/env python3
"""Fetch yfinance option chain (calls + puts for one expiry) for one or
more tickers and print as JSON / NDJSON / CSV.

See `options.py --help` for usage. Output is a JSON array on stdout, one
entry per ticker; failed / non-applicable tickers carry an "error" or a
"note" field instead of chain data so a single bad symbol does not
poison the batch. Field schema lives in the *_KEYS / *_CSV_COLS
constants below.
"""
from __future__ import annotations
import yfinance as yf
from helpers import (
    RESULT_META, emit_json_or_ndjson,
    safe_bool, safe_float, safe_int, safe_str, with_retry,
)

import argparse
import math
import re
import sys
from pathlib import Path

# Allow this script to be run directly OR imported as a module: ensure
# sibling `helpers.py` is importable regardless of how Python was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Yahoo lists options for most US equities and ETFs (and a handful of
# ADRs). Empirically verified empty (2026-05) — `t.options` returns `()`
# for: indexes (^GSPC), crypto (BTC-USD), FX (EURUSD=X, JPY=X), futures
# (ES=F), non-US equities (0700.HK, BMW.DE, 7203.T), mutual funds
# (VFIAX), bogus tickers (BOGUS123XYZ). yfinance does not raise — it
# just returns an empty tuple — so the empty case is genuinely
# ambiguous (non-applicable instrument vs bogus ticker vs real equity
# with no listed options). We surface this as success-with-note rather
# than promoting to error_kind: not_found.
_NO_OPTIONS_NOTE = (
    "no listed options (Yahoo lists options for US-listed equities — "
    "US companies plus a subset of ADRs — and ETFs; non-US primary "
    "listings, indexes, crypto, FX, futures, mutual funds, and bogus "
    "/ delisted tickers all return empty — call fast_info to "
    "disambiguate)"
)

# Edge case: expirations list populated but Yahoo returns an empty
# chain for the requested expiry. yfinance/scrapers/options.py emits
# this as `Options(calls=None, puts=None, underlying=None)` when the
# server-side `_download_options(date)` payload comes back as `{}`.
# Rare (typically transient Yahoo gap), but distinct from "ticker has
# no options at all" — the expirations array still lets the caller
# retry a different date.
_EMPTY_CHAIN_NOTE = (
    "expirations listed but Yahoo returned empty chain for this date "
    "(transient gap; try another expiry from `expirations`)"
)

# Per-contract row schema. Aligns 1:1 with yfinance's option_chain
# DataFrame columns (snake_cased), with three adjustments:
#   - lastTradeDate → last_trade_date_iso (ISO 8601 UTC string for
#     JSON-friendliness; pandas Timestamp doesn't round-trip cleanly)
#   - contractSize is kept as a string (typically "REGULAR"); surfaced
#     because mini-options / non-standard sizes do exist in principle
#   - Yahoo's `currency` (per-contract) → `contract_currency` here, to
#     avoid a CSV header collision with the top-level `currency` column
#     (= underlying / trading currency). In observed payloads the two
#     match exactly, but the rename keeps both layers addressable.
#
# UNITS (re-stated in references/options.md):
#   strike, last_price, bid, ask, change_abs   — contract currency
#   change_pct                                  — INFERRED PERCENT
#                                                 (Yahoo's `percentChange`
#                                                 field; sibling field
#                                                 `regularMarketChangePercent`
#                                                 in the SAME options
#                                                 API payload is verified
#                                                 percent-encoded
#                                                 [SPY 2026-05: -0.30661
#                                                 = -0.31%]; per-contract
#                                                 percentChange not
#                                                 directly verified —
#                                                 every off-hours sample
#                                                 reads 0.0)
#   implied_vol                                 — FRACTION (0.25 = 25%)
#   volume, open_interest                       — integer counts
#   in_the_money                                — bool
_CONTRACT_KEYS = (
    "contract_symbol",
    "strike",
    "last_price",
    "bid",
    "ask",
    "change_abs",
    "change_pct",
    "volume",
    "open_interest",
    "implied_vol",
    "in_the_money",
    "last_trade_date_iso",
    "contract_size",
    "contract_currency",
)

# CSV default-mode columns. Each row is one CONTRACT, with `leg` ∈
# {call, put} as the discriminator so calls + puts of one expiry coexist
# in one CSV. Symbol / spot / expiry repeat across a ticker's rows.
# Empty / errored tickers emit a single row carrying symbol + note +
# meta so they aren't silently dropped (same pattern as holders).
_DEFAULT_CSV_COLS = (
    "symbol", "spot", "currency", "expiry", "leg",
    *_CONTRACT_KEYS,
    "note", *RESULT_META,
)

# Summary-mode flat projection. Per-ticker peer-comparison signals for
# ONE expiry (whichever was selected). All atm_* fields refer to the
# strike closest to spot in the corresponding leg.
#
# `moneyness_pct` carries the user's --moneyness arg through unchanged
# (None when not set). Without this the same JSON / CSV row shape
# couldn't disambiguate "ATM IV across the full ladder" from "ATM IV
# within ±5% of spot" — both produce the same field set with very
# different semantics. Self-describing output > guess-by-context.
_SUMMARY_FLAT_KEYS = (
    "spot",
    "currency",
    "quote_type",
    "expiry",
    "expirations_count",
    "moneyness_pct",
    "calls_returned",
    "puts_returned",
    "atm_call_strike", "atm_call_iv", "atm_call_volume", "atm_call_oi",
    "atm_put_strike",  "atm_put_iv",  "atm_put_volume",  "atm_put_oi",
    "total_call_volume", "total_put_volume", "pcr_volume",
    "total_call_oi",     "total_put_oi",     "pcr_oi",
)
_SUMMARY_CSV_COLS = ("symbol", *_SUMMARY_FLAT_KEYS, "note", *RESULT_META)


# OCC option contract symbol layout: <ROOT><YY><MM><DD><C|P><STRIKE×1000>
# Examples:
#   AAPL260508C00282500  → AAPL, 2026-05-08, Call, 282.500
#   BRKB260619C00500000  → BRK.B (period stripped), 2026-06-19, Call, 500.00
#   SPY260618P00650000   → SPY, 2026-06-18, Put, 650.00
# Anchor the regex on the YY MM DD block + leg flag — root can be any
# uppercase letters (length varies, may or may not include digits in
# theory, but in practice OPRA roots are letters-only).
_OCC_RE = re.compile(r"^[A-Z]+(\d{6})[CP]\d+$")


def _expiry_from_contract_symbol(contract_symbol: str | None) -> str | None:
    """Extract the YYYY-MM-DD expiry encoded in an OCC option symbol.

    Returns None when contract_symbol is None / empty / not in OCC
    format (e.g. some non-US listings could carry a different
    convention, though we don't currently support those — the empty
    check protects us anyway).

    Year encoding: OCC carries 2-digit year, we hardcode `20YY` —
    correct from 2000 to 2099. After 2099 (or if some long-dated
    contract uses a 19xx-style 2-digit year, which OPRA does not
    use), this would be off; the call site falls back to
    `exps[0]` when the parse looks malformed, but parses with
    valid YY (e.g. `99` → `2099`) succeed unconditionally. This
    is fine for everything within OPRA's actual listing horizon
    (longest LEAPS extend ~3 years out).
    """
    if not contract_symbol:
        return None
    m = _OCC_RE.match(contract_symbol)
    if not m:
        return None
    yymmdd = m.group(1)
    return f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def _expiry_from_chain(chain) -> str | None:
    """Derive YYYY-MM-DD expiry from the first non-empty contract in
    `chain` (a yfinance Options namedtuple).

    Used in the no-`--expiry` path to label `expiry` from authoritative
    Yahoo data rather than guessing `exps[0]`. The two were verified
    identical across AAPL / SPY / NVDA / TM in 2026-05, but the chain
    data is ground truth — if Yahoo ever drifts the ordering of
    `result[0].options[]`, this guard keeps the labeled-expiry honest.

    Returns None when both legs are empty or unparseable; caller falls
    back to `exps[0]` in that case.
    """
    for leg in (chain.calls, chain.puts):
        if leg is None or leg.empty:
            continue
        try:
            sym = leg.iloc[0].get("contractSymbol")
        except (KeyError, IndexError, AttributeError):
            continue
        parsed = _expiry_from_contract_symbol(sym)
        if parsed is not None:
            return parsed
    return None


def _ts_to_iso(ts) -> str | None:
    """pandas Timestamp / NaT / None → ISO 8601 string or None.

    Options chain `lastTradeDate` arrives as `datetime64[ns, UTC]` —
    we serialize as ISO with the offset preserved (e.g.
    "2026-05-07T17:07:53+00:00") so callers get unambiguous UTC.
    """
    if ts is None:
        return None
    try:
        if ts != ts:  # NaT
            return None
    except TypeError:
        pass
    try:
        return ts.isoformat()
    except AttributeError:
        return None


def _project_contract(row: dict) -> dict:
    """One option chain DataFrame row → flat dict keyed by _CONTRACT_KEYS.

    yfinance emits camelCase column names; we snake_case them and
    NaN/Inf-scrub via the safe_* helpers. `lastTradeDate` is a tz-aware
    pandas Timestamp — passed through `_ts_to_iso` so JSON round-trips.
    """
    return {
        "contract_symbol":      safe_str(row.get("contractSymbol")),
        "strike":               safe_float(row.get("strike")),
        "last_price":           safe_float(row.get("lastPrice")),
        "bid":                  safe_float(row.get("bid")),
        "ask":                  safe_float(row.get("ask")),
        "change_abs":           safe_float(row.get("change")),
        "change_pct":           safe_float(row.get("percentChange")),
        "volume":               safe_int(row.get("volume")),
        "open_interest":        safe_int(row.get("openInterest")),
        "implied_vol":          safe_float(row.get("impliedVolatility")),
        "in_the_money":         safe_bool(row.get("inTheMoney")),
        "last_trade_date_iso":  _ts_to_iso(row.get("lastTradeDate")),
        "contract_size":        safe_str(row.get("contractSize")),
        "contract_currency":    safe_str(row.get("currency")),
    }


def _project_leg(df) -> list[dict]:
    if df is None or df.empty:
        return []
    return [_project_contract(r) for r in df.to_dict(orient="records")]


def _apply_moneyness(rows: list[dict], spot: float | None,
                     pct: float | None) -> list[dict]:
    """Keep rows with strike within ±pct% of spot. No-op when either
    arg is None. Pre-filter (applied before --limit) so peer-comparison
    summary metrics see only the in-window strikes when --moneyness is
    set."""
    if pct is None or spot is None or spot <= 0:
        return rows
    band = spot * (pct / 100.0)
    lo, hi = spot - band, spot + band
    return [r for r in rows if r.get("strike") is not None
            and lo <= r["strike"] <= hi]


def _apply_limit(rows: list[dict], limit: int | None) -> list[dict]:
    if limit is None:
        return rows
    return rows[:limit]


def _atm_row(rows: list[dict], spot: float | None) -> dict | None:
    """Return the row whose strike is closest to spot. None when rows
    is empty or spot is missing — the summary projection treats both
    as "no ATM signal" rather than guessing."""
    if not rows or spot is None:
        return None
    best, best_dist = None, math.inf
    for r in rows:
        s = r.get("strike")
        if s is None:
            continue
        d = abs(s - spot)
        if d < best_dist:
            best, best_dist = r, d
    return best


def _sum_int(rows: list[dict], key: str) -> int | None:
    """Sum integer field across rows; skip None entries.

    Returns None when `rows` is empty OR when every row's value is
    None — both are "no data" cases that should NOT collapse to 0
    (would falsely advertise "0 volume" when the truth is "Yahoo
    didn't populate the field"). Returns 0 only when at least one
    row contributed an explicit 0 value (legitimate zero activity).
    PCR callers see None and short-circuit the division accordingly.
    """
    if not rows:
        return None
    total = 0
    seen = False
    for r in rows:
        v = r.get(key)
        if v is not None:
            total += v
            seen = True
    return total if seen else None


def _fetch_chain(symbol: str, expiry: str | None):
    """Fetch (expirations_tuple, chain_or_none, underlying_dict_or_none).

    Two HTTP-cost paths, distinguished by whether the user pinned an
    expiry:

    - **`expiry is None` (default — 1 HTTP).** `option_chain()` with no
      argument fetches the default-expiry chain AND populates the full
      `_expirations` dict in a single Yahoo call (verified by reading
      yfinance/ticker.py: `_download_options(date=None)` returns both
      `expirationDates` and the `options[0]` chain). After that
      `t.options` is a free dict-keys read. The default-expiry
      identity-with-`exps[0]` was empirically verified across AAPL /
      SPY / NVDA / TM in 2026-05 (Yahoo source: yfinance reads
      `result[0].options[0]`, where `options[0]` aligns with the
      first entry of `expirationDates`). Yahoo could in principle
      change the ordering convention; we treat them as identical for
      the schema (`expiry: exps[0]` in the response), but the runtime
      data comes from `option_chain()` directly so a divergence
      would only affect the labeled-expiry field.
    - **`expiry is not None` (2 HTTP).** We need to validate the
      user-supplied date before issuing the chain fetch — otherwise
      yfinance raises `ValueError("Expiration `X` cannot be found.")`
      which defeats classify_error's "not found" substring match
      (the word "be" comes between them). Reading `t.options` first
      (1 HTTP, fills `_expirations`) lets us pre-validate and raise
      our own message that DOES contain "not found", keeping the
      classification logic in one place. Then `option_chain(expiry)`
      fetches the actual chain (1 HTTP).

    Wrapped in a single `with_retry` so a 429 on either HTTP falls
    into the same backoff schedule. **Retry replays the full path** —
    a 3-attempt retry of the 2-HTTP path can hit Yahoo up to 6 times
    (3× outer × 2 HTTP), and the 1-HTTP path's worst case is half
    that (3 × 1 HTTP = 3).

    Returns a 3-tuple `(exps, chain, underlying)`:
      - On the empty-exps short-circuit: `((), None, None)`. Caller
        emits `_NO_OPTIONS_NOTE`.
      - On every other success path: `(non-empty exps, Options
        namedtuple, dict)`. The Options namedtuple may have
        `.calls=None` / `.puts=None` (yfinance's empty-chain
        encoding); caller routes that to `_EMPTY_CHAIN_NOTE`.
        `chain` itself is never None on this branch — only its
        leg fields can be.

    Downstream, fetch() uses `_expiry_from_chain(chain)` to derive
    the labeled `expiry` from the OCC contract symbols actually
    returned (rather than guessing `exps[0]`). This guards against
    Yahoo ever drifting `option_chain()` default ordering away
    from `exps[0]` — the chain data stays self-consistent.
    """
    def _f():
        t = yf.Ticker(symbol)
        if expiry is None:
            # 1-HTTP path. `option_chain()` returns
            # `Options(calls=None, puts=None, underlying=None)` whenever
            # `_download_options()` got an empty payload from Yahoo.
            # That happens for two distinct reasons that we MUST
            # disambiguate downstream:
            #   (a) ticker has no options at all — yfinance source:
            #       `result[]` empty, `_expirations` stays empty,
            #       `t.options` returns `()`. ^GSPC / BTC-USD / ES=F /
            #       0700.HK / ZZZZNOTREAL all land here.
            #   (b) expirations exist but Yahoo returned empty for the
            #       default expiry — `result[0]` populated (so
            #       `_expirations` filled, `t.options` non-empty) but
            #       `result[0].options` empty. Rare transient gap.
            # `t.options` is the discriminator. After `option_chain()`
            # has run it's a free dict-keys read (0 HTTP). Use it to
            # route case (a) to _NO_OPTIONS_NOTE and case (b) to
            # _EMPTY_CHAIN_NOTE in fetch().
            chain = t.option_chain()
            exps = t.options  # populated by option_chain() above; 0 HTTP
            if not exps:
                return (), None, None  # case (a) — no options at all
            # case (b) chain.calls/puts may still be None — let fetch()
            # detect that and route to _EMPTY_CHAIN_NOTE.
            return exps, chain, getattr(chain, "underlying", None) or {}
        # User-supplied expiry: 2-HTTP path with pre-validation.
        exps = t.options or ()
        if not exps:
            return exps, None, None
        if expiry not in exps:
            raise ValueError(
                f"expiry {expiry!r} not found in available list"
            )
        chain = t.option_chain(expiry)
        return exps, chain, getattr(chain, "underlying", None) or {}
    return with_retry(_f)


def fetch(symbol: str, expiry: str | None = None) -> dict:
    """Fetch one expiry's chain for `symbol`.

    Args:
        symbol: ticker symbol (case-insensitive; non-US needs suffix).
        expiry: optional expiration date string in `YYYY-MM-DD` format.
            When None (default), returns the ticker's nearest expiry
            via the 1-HTTP `option_chain()` path. When set, the date
            is pre-validated against `t.options`; a date not in the
            available list short-circuits to `error_kind: not_found`
            with `expiry_requested` carrying the user's input — no
            chain fetch is attempted.

    Returns a dict per the references/options.md schema:
        - happy path: symbol, spot, currency, quote_type, expirations,
          expiry, calls, puts (+ optional `attempts` when > 1)
        - no-options ticker: same shape with empty arrays + `note`
        - empty chain on a valid expiry: same shape with empty
          arrays + `note` (different note string)
        - failure: symbol, error, error_kind, attempts
          (+ `expiry_requested` for the not_found-via-bad-expiry case)

    No filtering — callers apply `--moneyness` and `--limit` (default
    mode) or compute summary metrics from full lists.
    """
    result, err_kind, attempts = _fetch_chain(symbol, expiry)
    # Bad-expiry path: pre-validation in _fetch_chain raises a message
    # classify_error pins as 'not_found'. Surface user-friendlier text
    # so the caller (model) can suggest --list/expirations next time.
    if err_kind == "not_found" and expiry is not None:
        return {
            "symbol": symbol,
            "error": f"expiry {expiry!r} not in available list "
            f"(call without --expiry to see expirations)",
            "error_kind": "not_found",
            "attempts": attempts,
            "expiry_requested": expiry,
        }
    if err_kind:
        return {
            "symbol": symbol,
            "error": f"fetch failed ({err_kind}, after {attempts} attempt(s))",
            "error_kind": err_kind,
            "attempts": attempts,
        }
    exps, chain, underlying = result
    if not exps:
        # Ambiguous empty — see _NO_OPTIONS_NOTE for the case list.
        out = {
            "symbol": symbol,
            "spot": None,
            "currency": None,
            "quote_type": None,
            "expirations": [],
            "expiry": None,
            "calls": [],
            "puts": [],
            "note": _NO_OPTIONS_NOTE,
        }
        if attempts > 1:
            out["attempts"] = attempts
        return out
    # underlying dict carries the spot snapshot (regularMarketPrice) and
    # contract currency. Both are None-safe — Yahoo occasionally serves a
    # stripped underlying (rare); we still return the chain in that case.
    spot = safe_float(underlying.get("regularMarketPrice")
                      ) if underlying else None
    currency = safe_str(underlying.get("currency")) if underlying else None
    quote_type = safe_str(underlying.get("quoteType")) if underlying else None
    # Empty-chain-but-valid-expiry edge case (verified reachable via
    # yfinance source: `_download_options(date)` → `{}` ⇒ Options(None,
    # None, None)). Distinct from "no options listed" because
    # `expirations` is still populated — caller can retry a different
    # date. Surface as success-with-note rather than error: not_found,
    # to mirror the holders/news convention for "valid request,
    # ambiguous-empty Yahoo response".
    #
    # NB: `_fetch_chain` always returns a yfinance Options namedtuple
    # for `chain` on this code path (never None) — we test
    # `chain.calls is None` directly. The `chain is None` defensive
    # check that lived here previously was unreachable in current
    # `_fetch_chain` and removed.
    if chain.calls is None and chain.puts is None:
        out = {
            "symbol": symbol,
            "spot": spot,
            "currency": currency,
            "quote_type": quote_type,
            "expirations": list(exps),
            # No chain data to parse — fall back to label from `exps[0]`
            # (or user-supplied date). Only path where we don't have
            # authoritative chain-derived ground truth.
            "expiry": expiry if expiry is not None else exps[0],
            "calls": [],
            "puts": [],
            "note": _EMPTY_CHAIN_NOTE,
        }
        if attempts > 1:
            out["attempts"] = attempts
        return out
    # Authoritative expiry: when the user pinned `--expiry`, that's
    # ground truth (we 2-HTTP-validated it). When not, we parse the
    # OCC symbol of the first returned contract — Yahoo's `option_chain()`
    # default is *expected* to align with `exps[0]`, but the chain itself
    # is what the user actually got, so we label from there. If parsing
    # fails (non-OCC contract format on some future non-US listing),
    # fall back to `exps[0]`.
    if expiry is not None:
        actual_expiry = expiry
    else:
        actual_expiry = _expiry_from_chain(chain) or exps[0]
    out = {
        "symbol": symbol,
        "spot": spot,
        "currency": currency,
        "quote_type": quote_type,
        "expirations": list(exps),
        "expiry": actual_expiry,
        "calls": _project_leg(chain.calls),
        "puts": _project_leg(chain.puts),
    }
    if attempts > 1:
        out["attempts"] = attempts
    return out


def _filter_legs(result: dict, leg_filter: str,
                 moneyness: float | None, limit: int | None) -> dict:
    """Apply --type / --moneyness / --limit IN-PLACE.

    **Mutates `result`.** Same convention as holders._apply_limit. Order
    matters: --moneyness is applied before --limit so a --limit cap
    counts in-window strikes only. --type drops the off-leg list to
    `[]` rather than removing the key — keeps the schema shape stable
    for downstream parsers.

    On empty-leg results (the `_NO_OPTIONS_NOTE` and `_EMPTY_CHAIN_NOTE`
    paths, where `calls` and `puts` are already `[]`), all three
    operations are silently no-op — `_apply_moneyness([], ...)` and
    `_apply_limit([], ...)` both return `[]`. Error results (no
    `calls` key) early-return unchanged via the `if "calls" not in
    result` guard.
    """
    if "calls" not in result:
        return result
    spot = result.get("spot")
    if leg_filter == "puts":
        result["calls"] = []
    else:
        result["calls"] = _apply_limit(
            _apply_moneyness(result["calls"], spot, moneyness), limit)
    if leg_filter == "calls":
        result["puts"] = []
    else:
        result["puts"] = _apply_limit(
            _apply_moneyness(result["puts"], spot, moneyness), limit)
    return result


def _summarize(full: dict, moneyness: float | None) -> dict:
    """Flat per-ticker projection for peer comparison.

    Reads from the FULL leg lists (post-moneyness, pre-limit) so atm_*
    and total_* aren't sensitive to the user's --limit knob — same
    invariance principle as holders._summarize. --moneyness IS applied
    here because it's a semantic filter ("only in-window strikes
    count"), not a display knob.
    """
    out = {"symbol": full["symbol"], **{k: None for k in _SUMMARY_FLAT_KEYS}}
    spot = full.get("spot")
    out["spot"] = spot
    out["currency"] = full.get("currency")
    out["quote_type"] = full.get("quote_type")
    out["expiry"] = full.get("expiry")
    expirations = full.get("expirations") or []
    out["expirations_count"] = len(expirations)
    # Carry the user's --moneyness arg through unchanged so the row is
    # self-describing: a peer-comparison CSV mixing --moneyness 5 runs
    # with no-filter runs would otherwise look identical at the column
    # level but mean very different things.
    out["moneyness_pct"] = moneyness

    calls = _apply_moneyness(full.get("calls") or [], spot, moneyness)
    puts = _apply_moneyness(full.get("puts") or [], spot, moneyness)
    out["calls_returned"] = len(calls)
    out["puts_returned"] = len(puts)

    atm_call = _atm_row(calls, spot)
    if atm_call:
        out["atm_call_strike"] = atm_call.get("strike")
        out["atm_call_iv"] = atm_call.get("implied_vol")
        out["atm_call_volume"] = atm_call.get("volume")
        out["atm_call_oi"] = atm_call.get("open_interest")
    atm_put = _atm_row(puts, spot)
    if atm_put:
        out["atm_put_strike"] = atm_put.get("strike")
        out["atm_put_iv"] = atm_put.get("implied_vol")
        out["atm_put_volume"] = atm_put.get("volume")
        out["atm_put_oi"] = atm_put.get("open_interest")

    cv = _sum_int(calls, "volume")
    pv = _sum_int(puts, "volume")
    out["total_call_volume"] = cv
    out["total_put_volume"] = pv
    if cv is not None and pv is not None and cv > 0:
        out["pcr_volume"] = pv / cv
    coi = _sum_int(calls, "open_interest")
    poi = _sum_int(puts, "open_interest")
    out["total_call_oi"] = coi
    out["total_put_oi"] = poi
    if coi is not None and poi is not None and coi > 0:
        out["pcr_oi"] = poi / coi

    for k in ("note", *RESULT_META):
        if k in full:
            out[k] = full[k]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description=(
            "Fetch the Yahoo Finance option chain (calls + puts for ONE\n"
            "expiry) for one or more tickers. Equity / ETF only — indexes,\n"
            "crypto, FX, futures, mutual funds, and non-US primary listings\n"
            "all return success-with-note (empty expirations). With multiple\n"
            "tickers, default expiry is each ticker's nearest; pass --expiry\n"
            "to fix one date across the batch."
        ),
        epilog=(
            "Examples:\n"
            "  options.py AAPL                                     # nearest expiry, both legs\n"
            "  options.py --expiry 2026-06-19 AAPL                 # specific expiry\n"
            "  options.py --type calls AAPL                        # calls only\n"
            "  options.py --moneyness 5 AAPL                       # strikes within ±5% of spot\n"
            "  options.py --moneyness 10 --limit 10 AAPL           # ATM ±10%, top 10 per leg\n"
            "  options.py --summary NVDA AMD AVGO                  # peer ATM IV / PCR table\n"
            "  options.py --format csv --moneyness 5 AAPL MSFT     # CSV: one row per contract\n"
            "\n"
            "Units (verbatim Yahoo unless noted; full table in references/options.md):\n"
            "  strike / last_price / bid / ask / change_abs   contract currency\n"
            "  change_pct                                     INFERRED percent (sibling\n"
            "                                                 regularMarketChangePercent in\n"
            "                                                 same payload verified percent-\n"
            "                                                 encoded; per-contract values\n"
            "                                                 are 0.0 off-hours so direct\n"
            "                                                 confirmation is pending)\n"
            "  implied_vol                                    fraction (0.25 = 25%)\n"
            "  volume / open_interest                         int counts\n"
            "  in_the_money                                   bool\n"
            "Off-hours, Yahoo often serves bid=0 / ask=0 / openInterest=0 / IV≈1e-5 as\n"
            "sentinels; the structure is correct but the values are stale — re-fetch\n"
            "during market hours.\n"
            "\n"
            "See references/options.md for the field schema, presentation\n"
            "guidance, and SKILL.md for cross-cutting caveats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--expiry", default=None, metavar="YYYY-MM-DD",
                    help="Expiration date to fetch. Default: each ticker's "
                         "nearest available expiry. Format must match Yahoo's "
                         "(YYYY-MM-DD). When set, any ticker that does not "
                         "list this exact date returns error_kind: not_found "
                         "with the requested date carried in `expiry_requested`.")
    ap.add_argument("--type", dest="leg", default="all",
                    choices=("calls", "puts", "all"),
                    help="Leg to include. Default: all (both calls and puts). "
                         "calls / puts drops the off-leg to an empty list "
                         "rather than removing the key, so downstream parsers "
                         "see a stable schema shape.")
    ap.add_argument("--moneyness", type=float, default=None,
                    metavar="PERCENT",
                    help="Keep strikes within ±PERCENT%% of spot — `5` means "
                         "±5%%, NOT $5 (which would never make sense across "
                         "different price scales). Default: keep all strikes. "
                         "Applied BEFORE --limit so the row cap counts "
                         "in-window strikes only. In --summary mode, ATM and "
                         "total_* metrics are computed AFTER this filter "
                         "(in-window strikes only); the value is also echoed "
                         "as `moneyness_pct` in the output for self-described "
                         "rows. Minimum 0.1; below typical strike spacing for "
                         "most tickers and would yield empty legs.")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap rows per leg (calls and puts each capped to N). "
                         "Default: keep all (Yahoo returns the full strike "
                         "ladder, often 30-200 rows per leg). Silently ignored "
                         "in --summary mode — atm_* / total_* read full lists "
                         "so summary metrics describe the data, not the "
                         "display knob.")
    ap.add_argument("--summary", action="store_true",
                    help="Flat per-ticker projection: spot + ATM strike / IV "
                         "/ volume / OI for each leg + total volume / OI + "
                         "put-call ratio (PCR). Useful for peer-comparison "
                         "tables. Same network cost as default mode.")
    ap.add_argument("--format", default="json", choices=("json", "ndjson", "csv"),
                    help="Output format. json (default) = pretty JSON array, "
                         "one record per ticker; "
                         "ndjson = one JSON record per ticker per line; "
                         "csv = default mode emits one row per CONTRACT (with "
                         "a `leg` discriminator: call / put), with symbol / "
                         "spot / expiry repeating; --summary csv emits strict "
                         "one-row-per-ticker.")
    ap.add_argument("symbols", nargs="+", metavar="SYMBOL",
                    help="Ticker symbol(s); case-insensitive; non-US needs suffix "
                         "(but most non-US tickers have no listed options on Yahoo).")
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")
    if args.moneyness is not None and args.moneyness < 0.1:
        # 0.1% × spot is below the strike-grid spacing for almost every
        # ticker (AAPL at $290 → 0.29 dollar window, but strikes are on
        # a $2.50 grid). Anything tighter would silently wipe the leg
        # — fail loudly so the user can pick a sensible value.
        ap.error("--moneyness must be >= 0.1 (percent of spot; "
                 "values below that wipe out almost every chain)")

    results = [fetch(s.strip().upper(), expiry=args.expiry)
               for s in args.symbols if s.strip()]

    if args.summary:
        # _summarize reads full leg lists, so --limit is a no-op here by
        # design; --moneyness IS honored (semantic filter, not display).
        results = [_summarize(r, args.moneyness) for r in results]
        _emit_summary(results, args.format)
    else:
        results = [_filter_legs(r, args.leg, args.moneyness, args.limit)
                   for r in results]
        _emit_default(results, args.format)


def _emit_default(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    # CSV: one row per contract, with `leg` discriminator. Empty / errored
    # tickers emit a single carry row so they aren't silently dropped.
    import csv as _csv
    cols = list(_DEFAULT_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        symbol = r.get("symbol")
        spot = r.get("spot", "")
        currency = r.get("currency", "")
        expiry = r.get("expiry", "")
        carry = {k: r.get(k, "") for k in ("note", *RESULT_META) if k in r}
        if "error" in r or (not r.get("calls") and not r.get("puts") and "note" in r):
            row = {"symbol": symbol, "spot": spot, "currency": currency,
                   "expiry": expiry, **carry}
            writer.writerow([row.get(c, "") for c in cols])
            continue
        for leg_name, leg_key in (("call", "calls"), ("put", "puts")):
            for c in r.get(leg_key, []):
                row = {"symbol": symbol, "spot": spot, "currency": currency,
                       "expiry": expiry, "leg": leg_name, **c, **carry}
                writer.writerow([row.get(col, "") for col in cols])


def _emit_summary(results: list, fmt: str) -> None:
    if emit_json_or_ndjson(results, fmt):
        return
    import csv as _csv
    cols = list(_SUMMARY_CSV_COLS)
    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(cols)
    for r in results:
        writer.writerow([r.get(c, "") for c in cols])


if __name__ == "__main__":
    main()
