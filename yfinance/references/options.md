[← back to SKILL.md](../SKILL.md)

# `options` reference

_Yahoo behavior verified: 2026-05, yfinance 1.3.x. Re-run a quick
`options.py --summary AAPL '^GSPC' BOGUS123` if you suspect upstream drift._

**Sections:** [Run](#run) · [CLI arguments](#cli-arguments) · [Output schema](#output-schema) · [Empty result recipe](#empty-recipe) · [Presenting options results](#presenting-options-results) · [Mode-specific caveats](#mode-specific-caveats)

Option chain (calls + puts for **one expiry**) for one or more tickers.
Each ticker returns:

- A `spot` snapshot (Yahoo's `regularMarketPrice` from the underlying
  payload that ships with the chain — no extra Yahoo call), `currency`
  (contract / underlying ccy, typically USD), and `quote_type`.
- The full `expirations` array — every expiration Yahoo lists for the
  ticker — and the single `expiry` actually fetched.
- Two arrays `calls` and `puts`, each row a contract with strike /
  bid / ask / last / volume / open interest / IV / ITM-flag.

**One expiry per call.** `t.option_chain(date)` is a separate Yahoo
HTTP request per expiration. Fetching all 24 of AAPL's expiries would
be 24 HTTP calls — we don't do that. The script picks ONE date per
ticker (user-supplied via `--expiry`, else each ticker's nearest
available). For "term structure" comparisons across expiries on one
ticker, call the script multiple times.

**HTTP cost: 1 or 2 calls per ticker, depending on `--expiry`.**

- **No `--expiry` (1 HTTP).** `option_chain()` with no argument
  returns BOTH the default-expiry chain AND the full expirations
  list in one Yahoo call. The default-expiry-equals-`exps[0]`
  alignment was empirically verified for AAPL / SPY / NVDA / TM in
  2026-05. **The labeled `expiry` is derived from the OCC contract
  symbol of the first returned contract**, not hardcoded as
  `exps[0]` — so even if Yahoo drifts the default-expiry ordering
  in the future, `expiry` and `contract_symbol` stay self-consistent.
  When OCC parsing fails (rare; non-OCC contract format on some
  future non-US listing), we fall back to `exps[0]` — that fallback
  is exactly the pre-OCC-derivation behavior, so worst-case label /
  data divergence equals what we'd have shipped without this
  guard. After the chain fetch, `t.options` is a free read.
- **With `--expiry` (2 HTTP).** We fetch `t.options` first to
  pre-validate the user's date — yfinance raises a `ValueError`
  on an unknown expiry, but its message ("cannot be found") doesn't
  match `classify_error`'s "not found" substring (the word "be" is
  in the way), so we'd lose the `error_kind: not_found` signal.
  Validating client-side keeps the classification logic in one place.

A retried call replays the FULL HTTP path — a 3-attempt retry of the
2-HTTP path can hit Yahoo up to 6 times. Treat options batches as
costing **roughly double** what a similar `holders` / `info` /
`fast_info` batch would.

**Equity / ETF / a few ADRs only.** Empirically verified empty
(2026-05) — `t.options` returns `()` for: indexes (`^GSPC`), crypto
(`BTC-USD`), FX (`EURUSD=X`, `JPY=X`), futures (`ES=F`), non-US
equities (`0700.HK`, `BMW.DE`, `7203.T`), mutual funds (`VFIAX`),
bogus / delisted tickers. The empty case is genuinely ambiguous (real
ticker without listed options vs bogus) — see
[All-empty is ambiguous](#empty-ambiguous) below. ADRs split: `TM`
(Toyota) does have options, `BABA` typically does too; check
empirically per ADR.

## Run

```bash
# Default: nearest expiry, both legs
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py AAPL

# Specific expiry (must match a date in `expirations` exactly)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --expiry 2026-06-19 AAPL

# Filter to in-window strikes + cap rows per leg
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --moneyness 5 --limit 10 AAPL

# Calls-only (puts list returned as []; schema shape unchanged)
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --type calls AAPL

# Peer compare ATM IV / volume / put-call ratio across tickers
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --summary --moneyness 5 NVDA AMD AVGO

# CSV — one row per contract, with `leg` discriminator
uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/options.py --format csv --moneyness 5 AAPL MSFT
```

Tickers are positional args.

## CLI arguments

- `--expiry YYYY-MM-DD` — expiration to fetch. Default: each ticker's
  own nearest. **Must match Yahoo's date string exactly** (the format
  is always `YYYY-MM-DD`; no slashes, no timezone). Pre-validated
  against `t.options` before the chain fetch — a date not in the list
  short-circuits to `error_kind: not_found` with `expiry_requested`
  carrying the user's input. With multiple tickers and `--expiry` set,
  any ticker whose options ladder doesn't include that exact date
  fails with `not_found` (Yahoo aligns weekly Friday expirations
  across most names, but specialty / non-standard expirations vary —
  call without `--expiry` first to inspect per-ticker availability).
- `--type calls|puts|all` — leg filter. Default `all`. `calls` /
  `puts` drops the off-leg to an empty list (`[]`) rather than
  removing the key — keeps the per-ticker schema shape stable for
  downstream parsers regardless of which legs the user asked for.
- `--moneyness PCT` — keep only strikes within ±PCT% of `spot`.
  Default: keep all strikes (often 30–200 rows per leg). Applied
  **before** `--limit`, so the row cap counts in-window strikes only.
  In `--summary` mode the same filter is honored — `atm_*` and
  `total_*` metrics describe the in-window slice when set, the full
  ladder when not. (Distinct from `--limit`: `--moneyness` is a
  semantic filter the metrics respect; `--limit` is a display knob
  the metrics ignore.)
- `--limit N` — cap rows per leg post-`--moneyness`. Default: keep
  all. **Silently ignored in `--summary` mode** — the flat metrics
  read full lists so they describe Yahoo's response, not the display
  knob (same invariance principle as holders' `--limit`).
- `--summary` — flat per-ticker projection: spot + ATM strike / IV /
  volume / OI per leg + total volume / OI + put-call ratios. Same
  network cost as default mode (post-fetch projection); use to save
  context tokens or build peer-compare tables.
- `--format json|ndjson|csv` — output format.
  - **default mode** — CSV: one row per contract with a `leg`
    discriminator (`call` / `put`); symbol / spot / currency / expiry
    repeat across a ticker's rows. Empty / errored tickers emit a
    single row carrying `symbol` + `note` + meta fields so they
    aren't silently dropped.
  - **`--summary` mode** — CSV: strict one row per ticker.

CSV column order (default mode, left to right): `symbol`, `spot`,
`currency`, `expiry`, `leg`, the 14 per-contract columns, `note`,
then the 3 meta fields (`error`, `error_kind`, `attempts`).

> **`contract_currency` looks redundant in CSV — that's intentional.**
> The third column `currency` and the 18th column `contract_currency`
> almost always match (both `USD` for US-listed names). The two
> layers exist because Yahoo carries them separately in the JSON
> payload (one per underlying snapshot, one per contract row);
> CSV preserves the JSON shape so the schemas don't drift. If
> `contract_currency` clutters your spreadsheet, drop it on read —
> `currency` carries the same value 99% of the time. The sole
> reason both stay is the rare cross-listed contract whose
> per-contract currency could in principle differ; we haven't seen
> it but the rename + retention guards against it.

## Output schema

### Default mode

Per ticker (illustrative, AAPL nearest expiry):

```json
[
  {
    "symbol": "AAPL",
    "spot": 287.44,
    "currency": "USD",
    "quote_type": "EQUITY",
    "expirations": ["2026-05-08", "2026-05-11", "2026-05-13", "..."],
    "expiry": "2026-05-08",
    "calls": [
      {
        "contract_symbol": "AAPL260508C00285000",
        "strike": 285.0,
        "last_price": 3.35,
        "bid": 0.0,
        "ask": 0.0,
        "change_abs": 0.0,
        "change_pct": 0.0,
        "volume": 6229,
        "open_interest": 0,
        "implied_vol": 0.00001,
        "in_the_money": true,
        "last_trade_date_iso": "2026-05-07T19:59:45+00:00",
        "contract_size": "REGULAR",
        "contract_currency": "USD"
      }
    ],
    "puts": [
      {
        "contract_symbol": "AAPL260508P00285000",
        "strike": 285.0,
        "last_price": 0.74,
        "bid": 0.0,
        "ask": 0.0,
        "change_abs": 0.0,
        "change_pct": 0.0,
        "volume": 24617,
        "open_interest": 0,
        "implied_vol": 0.0312596875,
        "in_the_money": false,
        "last_trade_date_iso": "2026-05-07T19:59:58+00:00",
        "contract_size": "REGULAR",
        "contract_currency": "USD"
      }
    ]
  }
]
```

#### Top-level fields

| Field | Type | Notes |
|---|---|---|
| `symbol` | str | Echo of input. |
| `spot` | float / null | Underlying's `regularMarketPrice` from Yahoo's chain payload. Same source as `fast_info.last_price` but no extra round-trip — it's bundled with the chain response. May be stale by the same ~15 min lag as `fast_info`. |
| `currency` | str / null | **Underlying / trading currency** — Yahoo's `underlying.currency` from the chain payload. Same semantics as `fast_info.currency` (the currency you'd buy the stock in: USD for AAPL, USD for ADRs like TM/BABA, HKD for `0700.HK`). Per-contract currency lives in each row's `contract_currency` field — see schema below. The two layers are deliberately addressable separately to avoid a CSV header collision; in observed payloads they always match. |
| `quote_type` | str / null | `EQUITY` for stocks, `ETF` for ETFs. (Indexes / crypto / FX / futures don't have options, so this is effectively always `EQUITY` or `ETF` on a successful fetch.) |
| `expirations` | list[str] | Every expiration Yahoo lists. Always populated on success — useful when the user wants to know "what are my date options" without a second call. |
| `expiry` | str | The date actually fetched (matches `--expiry` when supplied, else `expirations[0]`). |
| `calls` / `puts` | list[dict] | One row per contract — schema below. May be `[]` when `--type` filtered the leg out, or when `--moneyness` excluded all strikes, or when the leg is genuinely empty. |

#### Per-contract row (14 fields, identical between calls and puts)

| Field | Type | Notes |
|---|---|---|
| `contract_symbol` | str | OCC-style symbol (e.g. `AAPL260508C00285000` = AAPL, 2026-05-08, Call, 285.000 strike). Stable identifier across fetches. |
| `strike` | float | Strike price in `currency`. |
| `last_price` | float | Last traded price. May be stale (per-contract `last_trade_date_iso` shows when). |
| `bid` | float | Current bid. **`0.0` is a sentinel for "no current quote" / off-hours, not a real penny bid.** Same for `ask`. |
| `ask` | float | Current ask. See `bid`. |
| `change_abs` | float | Today's change vs prior close (Yahoo's `change` field) in `currency`. |
| `change_pct` | float | Today's change — **inferred PERCENT** (Yahoo's `percentChange` field passed through verbatim). The sibling field `regularMarketChangePercent` in the **same options API payload** (`v7/finance/options/{ticker}` → `optionChain.result[0].quote.regularMarketChangePercent`) is verified percent-encoded as of 2026-05 (SPY: `-0.30661` for a -0.31% day). Per-contract `percentChange` is uniformly `0.0` off-hours so we couldn't directly verify same-encoding — flagged as inferred until a live-market run confirms. |
| `volume` | int / null | Today's contract volume. NaN in Yahoo's response → `null` here. |
| `open_interest` | int | Total open contracts. **Yahoo zeroes OI overnight** — values populate during US market hours and reset to `0` after close (not a true zero; it's a sentinel). Use closing-snapshot data sources if you need stable OI. |
| `implied_vol` | float / null | Implied volatility as a **FRACTION** (`0.25` = 25% IV, multiply ×100 for display). **`1e-5` (`0.00001`) is Yahoo's "couldn't compute IV" sentinel** — appears for deep-ITM contracts, illiquid strikes, and many off-hours quotes. Treat any `implied_vol < 1e-3` as suspect. |
| `in_the_money` | bool | Pre-computed by Yahoo. For calls: `strike < spot`; for puts: `strike > spot`. |
| `last_trade_date_iso` | str / null | ISO 8601 UTC string (e.g. `"2026-05-07T17:07:53+00:00"`). The actual moment this contract last traded — often days or weeks old for illiquid strikes. NaT in Yahoo's response → `null` here. |
| `contract_size` | str | Always `"REGULAR"` in observed payloads (= 100 shares per contract). Surfaced in case Yahoo ever serves a non-regular size for mini-options or weeklies. |
| `contract_currency` | str | Per-contract currency, distinct from the top-level `currency` (= underlying). Renamed from Yahoo's `currency` field to avoid a CSV header collision (the top-level `currency` column would otherwise repeat). In every observed payload the two match exactly; the rename keeps the layers separately addressable for the rare cross-listed case where they might diverge. |

### `--summary` mode

Flat per-ticker dict (one expiry's chain reduced to peer-compare metrics):

```json
[
  {
    "symbol": "NVDA",
    "spot": 211.5,
    "currency": "USD",
    "quote_type": "EQUITY",
    "expiry": "2026-05-08",
    "expirations_count": 24,
    "moneyness_pct": 5.0,
    "calls_returned": 8,
    "puts_returned": 8,
    "atm_call_strike": 212.5,
    "atm_call_iv": 0.0312596875,
    "atm_call_volume": 250945,
    "atm_call_oi": 0,
    "atm_put_strike": 212.5,
    "atm_put_iv": 0.00001,
    "atm_put_volume": 74527,
    "atm_put_oi": 0,
    "total_call_volume": 1225298,
    "total_put_volume": 434478,
    "pcr_volume": 0.3546,
    "total_call_oi": 0,
    "total_put_oi": 0,
    "pcr_oi": null
  }
]
```

| Field | Notes |
|---|---|
| `expirations_count` | Length of `expirations` (the array isn't carried in summary mode to keep the row narrow). |
| `moneyness_pct` | Echo of the `--moneyness` arg the caller passed, or `null` when not set. Self-describing flag so a peer-compare CSV mixing filtered and unfiltered runs stays unambiguous — the `atm_*` and `total_*` columns mean different things depending on this. |
| `calls_returned` / `puts_returned` | Number of contracts after `--moneyness` filter. Useful sanity check: if you set `--moneyness 5` and got `0`, the filter wiped the leg out. |
| `atm_call_*` / `atm_put_*` | The contract closest to `spot` in each leg (post-`--moneyness`). All four sub-fields (`strike`, `iv`, `volume`, `oi`) come from the same row. |
| `total_call_volume` / `total_put_volume` | Sum of `volume` across all rows in the leg. **`null` when the leg is empty OR when every row's `volume` is None** (Yahoo didn't populate the field) — distinct from `0`, which means "every row had an explicit `volume: 0`". The distinction matters for PCR division (we short-circuit to `null` rather than dividing by 0). |
| `pcr_volume` | Put-call ratio by volume = `total_put_volume / total_call_volume`. >1 = more put activity (typically bearish), <1 = more call activity (typically bullish). `null` when either total is `null` or when calls volume is 0 (avoid divide-by-zero). |
| `total_call_oi` / `total_put_oi` | Same shape and `null` semantics as `total_*_volume`, summing `open_interest`. **Often both 0 off-hours** (Yahoo's nightly OI sentinel — see per-contract caveat) — `pcr_oi` then comes back `null`. |
| `pcr_oi` | Put-call ratio by open interest. Usually a more stable signal than volume (less noisy day-to-day) — but only meaningful when fetched during market hours. |

### Empty / non-applicable result

Two distinct empty paths, distinguished by whether `expirations` is
populated:

**No options listed at all** (`t.options` returns `()`):

```json
{
  "symbol": "^GSPC",
  "spot": null,
  "currency": null,
  "quote_type": null,
  "expirations": [],
  "expiry": null,
  "calls": [],
  "puts": [],
  "note": "no listed options (Yahoo lists options for US-listed equities — US companies plus a subset of ADRs — and ETFs; non-US primary listings, indexes, crypto, FX, futures, mutual funds, and bogus / delisted tickers all return empty — call fast_info to disambiguate)"
}
```

**Empty chain on a valid expiry** (rare; `expirations` populated but
Yahoo returned `{}` for the requested date). If you hit this on
multiple expiries in a row for the same ticker, Yahoo's options
backend is likely degraded — back off for a few minutes and retry,
rather than walking the full `expirations` array (which would just
burn HTTP calls during the outage):

```json
{
  "symbol": "AAPL",
  "spot": 287.44,
  "currency": "USD",
  "quote_type": "EQUITY",
  "expirations": ["2026-05-08", "2026-05-11", "..."],
  "expiry": "2026-05-08",
  "calls": [],
  "puts": [],
  "note": "expirations listed but Yahoo returned empty chain for this date (transient gap; try another expiry from `expirations`)"
}
```

The two notes have different action implications: the first means "no
options ever for this symbol" (don't retry); the second means "this
specific date came back empty, try a different one from the array".
Both follow the cross-mode convention that `note` is reserved for
**ambiguous-but-successful** state, never for hard failures.

<a id="empty-recipe"></a>

#### After `_NO_OPTIONS_NOTE`: `quote_type` → next step

Chain `fast_info` to recover the underlying instrument's `quote_type`,
then act on the table below. This decision tree turns the ambiguous
empty into a clean per-class verdict:

| `quote_type` (from `fast_info`) | Almost-certain meaning | Next step |
|---|---|---|
| `EQUITY` | Real stock without OPRA-listed options. Typically lower-volume names — not necessarily small-cap; mid-caps without active options interest land here too. | Tell the user this name has no listed options; suggest a peer / sector ETF if they want to hedge |
| `ETF` | Most ETFs have options (SPY/QQQ/IWM/etc.); this one is small enough that OPRA didn't list it | Tell user this specific ETF has no options; suggest a peer ETF |
| `INDEX` | Yahoo doesn't expose options on indexes — even when CBOE does (SPX / VIX / NDX cash-settled) | Suggest the tracking ETF: `^GSPC` → `SPY`, `^NDX` → `QQQ`, `^RUT` → `IWM`. (`^IXIC` Nasdaq Composite has no direct ETF; `QQQ` tracks the narrower Nasdaq 100 / `^NDX` and is the closest commonly-traded proxy.) State explicitly that yfinance can't reach index options. |
| `CRYPTOCURRENCY` | No traditional listed options on crypto via Yahoo | State explicitly; crypto options live on Deribit / CME, not Yahoo |
| `FUTURE` | Yahoo's options endpoint doesn't cover futures options | State explicitly; futures options data needs a futures-specific feed |
| `CURRENCY` | No FX options on Yahoo | State explicitly |
| `MUTUALFUND` | Mutual funds have no listed options at all | State explicitly |
| `not_found` from `fast_info` | Bogus / delisted ticker | Ask user to double-check the symbol (suffix? case? recent listing change?) |

This is the recipe `_NO_OPTIONS_NOTE` is referencing — keep it next
to the JSON example so a model reading the response can route in one
hop instead of guessing.

### Failed result

Bad expiry (pre-validated, no Yahoo round-trip wasted):

```json
{
  "symbol": "AAPL",
  "error": "expiry '1999-01-01' not in available list (call without --expiry to see expirations)",
  "error_kind": "not_found",
  "attempts": 1,
  "expiry_requested": "1999-01-01"
}
```

Other failures (rate limit, network) carry the standard
`error / error_kind / attempts` triple — see SKILL.md "Cross-cutting
caveats".

## Presenting options results

**Spot and ATM first.** Always lead with the spot price + selected
expiry so the reader has the frame: a strike of 285 means nothing
without knowing the stock is at 287.44. Then ATM call / put metrics,
then any tail observations.

**Multiply IV ×100 when displaying.** `implied_vol: 0.32` →
**32% IV**. Same convention as `info`'s margin / growth fields.
Suppress / flag values < 1e-3 (Yahoo's "couldn't compute" sentinel)
rather than rendering them as `0.001%`.

**`change_pct` is already percent.** Render as `+5.20%`, not `0.052`.
Distinct from IV (fraction). Easy to confuse — the unit landmines
table in SKILL.md tracks both.

**Don't render bid/ask of 0.0 as a real quote.** Off-hours all
contracts come back with `bid: 0.0, ask: 0.0`. If you see uniform 0s
across the chain, the right answer is "off-hours snapshot — bid/ask
not available; last_price reflects last trade at $X on $Y", not
"bid is zero". Same for `open_interest: 0` across all rows — that's
Yahoo's overnight reset, not "no one is holding any contracts."

**Single-ticker chain — format template:**

> **AAPL — options chain (expiry 2026-05-08, spot \$287.44)**
> - Calls (in-window ATM ±5%):
>   - \<strike\> — last \$\<X.XX\>, IV \<XX.X\>%, vol \<N\>, OI \<N\>
> - Puts (in-window ATM ±5%):
>   - \<strike\> — last \$\<X.XX\>, IV \<XX.X\>%, vol \<N\>, OI \<N\>
> - Other expirations available: \<count\> through \<latest\>.

If volume is 0 or last_trade is days old, mention that the contract
is illiquid — last_price is a stale print, not a fair quote.

**Multi-ticker peer compare** — use `--summary`; render columns:
ticker | spot | expiry | ATM call IV | ATM put IV | PCR (vol). The
PCR column is the most useful "sentiment" signal; ATM IV is the
"how much does the market think this will move" signal.

**Escape `$` as `\$` in prose** — same rationale as the other modes
(markdown $-math eats the digits between two unescaped dollar signs).

## Mode-specific caveats

- <a id="empty-ambiguous"></a>**Empty `expirations` is ambiguous.**
  yfinance returns `()` from `t.options` in all of these cases.
  Verified empirically (2026-05):
  - Index (`^GSPC`)
  - Crypto (`BTC-USD`)
  - FX (`EURUSD=X`, `JPY=X`)
  - Future (`ES=F`)
  - Non-US equity (`0700.HK`, `BMW.DE`, `7203.T`)
  - Mutual fund (`VFIAX`)
  - Bogus / delisted ticker (`BOGUS123XYZ`)

  Not in the list above but expected to land here: real US equities
  too small / illiquid for option listing. We deliberately don't
  promote empty to `error_kind: not_found` (a real low-coverage
  equity isn't an error) and instead emit success with a `note`. To
  resolve which case you're in, **call `fast_info`** on the same
  symbol — it returns `quote_type` for valid tickers (EQUITY / ETF /
  MUTUALFUND / INDEX / CRYPTOCURRENCY / FUTURE / CURRENCY) and a
  `not_found` classification for bogus.
- **`change_pct` is INFERRED PERCENT; `implied_vol` is verified
  FRACTION.** The two most visible numeric fields per row use
  **different units** — classic Yahoo. Unit landmines table in
  SKILL.md cross-cutting caveats. Cheat sheet: `change_pct: 5.2` ≈
  5.2% daily move; `implied_vol: 0.25` ≈ 25% annualized IV. If both
  look like "around 0.3", you're probably reading IV as percent —
  divide by 100, then ×100 for display.

  **Why "inferred" for change_pct.** The same Yahoo options API
  payload (`v7/finance/options/{ticker}` →
  `optionChain.result[0].quote`) carries
  `regularMarketChangePercent`, which we **directly verified**
  percent-encoded as of 2026-05 (SPY: `-0.30661` corresponds to
  `regularMarketChange / regularMarketPreviousClose × 100 = -0.307%`).
  Per-contract `percentChange` is a different field on the same
  payload — by API-locality convention almost certainly the same
  encoding, but every off-hours sample we tested came back `0.0` so
  we couldn't confirm directly. If a future live-market run shows
  values like `0.0052` (= 0.52% as a fraction) where percent
  encoding would predict `0.52`, flip this caveat. Until then,
  treat as PERCENT but be ready to re-verify if the numbers look
  three-orders-of-magnitude wrong.
- **`open_interest` resets to 0 overnight.** Yahoo populates OI
  during US market hours and zeroes it after close — verified across
  AAPL / MSFT / NVDA in 2026-05 off-hours: all 0 across all strikes.
  Same applies to `bid` / `ask` (sentinel 0.0). If you need stable
  OI / quotes, fetch during US market hours (09:30–16:00 ET); for
  long-dated trends, an EOD options data feed is the right tool —
  yfinance is a real-time-during-hours API.
- **`implied_vol: 1e-5` is "Yahoo couldn't compute".** Common for
  deep-ITM contracts (extrinsic value too small for IV root-finding
  to converge), illiquid strikes (no recent trade to anchor the
  Black-Scholes solve), and most off-hours quotes. Treat any
  `implied_vol < 1e-3` as missing rather than meaningful — render as
  "—" or skip the row.
- **One expiry per call by design.** `option_chain(date)` is one
  HTTP per expiration. The script fetches exactly one date per
  ticker per invocation (user-supplied or each ticker's nearest).
  Term-structure questions ("how does AAPL's IV change across
  expirations") need multiple calls — script the loop yourself or
  bundle into a follow-on script if this comes up often. We
  deliberately did NOT add a `--all-expiries` flag because a single
  `options.py AAPL --all-expiries` would be 24 HTTP calls and trigger
  Yahoo's rate limit immediately.
- **`--moneyness` is computed off `spot` returned in the same
  payload.** Self-consistent within one fetch — no separate
  `fast_info` round-trip needed, no risk of `spot` and the strike
  ladder being from different snapshots. If `spot` is `null`
  (extremely rare, observed only when Yahoo strips the underlying
  block), `--moneyness` silently no-ops (returns the full ladder)
  rather than error — better UX than a hard fail for an edge case
  that's already Yahoo flakiness.
- **`--expiry` is exact-match against `t.options`.** No fuzzy
  matching, no nearest-date fallback. A user's `2026-06-19` against
  a Friday-only ladder containing `2026-06-19` works; against
  `2026-06-20` (Saturday) returns `not_found`. We pre-validate on
  the client side (see `_fetch_chain`) so the bad-expiry path is
  one round-trip (`t.options` only), not two.
- **Strike ladder size varies wildly.** AAPL near-month: ~30 calls /
  ~25 puts. SPY near-month: ~190 / ~210 (penny strikes around ATM).
  Long-dated (LEAPS): ~30–60 per leg. Default emits the full ladder
  — for context-token control, lead with `--moneyness 5` (typically
  10–25 rows per leg) or follow with `--limit`.
- **`spot` may be ~15 min delayed.** Same lag as `fast_info` — Yahoo
  delays the underlying snapshot for free-tier consumers. The
  options chain itself is on the same delay; bid/ask quotes within
  the chain are subject to Yahoo's options-data lag (often longer
  than the underlying lag for less-active strikes). Don't treat
  `spot` as live-tape; it's "last value Yahoo cached".
- **`underlying` block can be partially or fully stripped (rare).**
  Yahoo's options API normally bundles the underlying snapshot
  (`spot`, `currency`, `quote_type`, plus 80+ other fields we don't
  surface). On rare flaky responses the block comes back empty or
  `null` — `spot` / `currency` / `quote_type` then return `null` in
  our schema while `calls` / `puts` populate normally. **Side
  effect on `--moneyness`:** when `spot is None`, the moneyness
  filter silently no-ops (returns the full ladder) — there's
  nothing to compute distance from. Same for `_atm_row`: `atm_*`
  fields in `--summary` mode become `null`, but `total_*` /
  `pcr_*` still compute (they don't need spot). Detect this by
  reading `spot is None` on a successful (no-error) result.

  In the `_EMPTY_CHAIN_NOTE` path specifically, `spot` is also
  `null` — yfinance's `Options(calls=None, puts=None,
  underlying=None)` namedtuple is the source of all three None
  fields when the chain comes back empty for a valid date.
- **`--summary` echoes `moneyness_pct` even on empty-chain results.**
  When the chain is genuinely empty (`_EMPTY_CHAIN_NOTE` path) and
  the user passed `--moneyness 5`, the summary row carries
  `moneyness_pct: 5.0` alongside `calls_returned: 0` /
  `puts_returned: 0` and `note: <_EMPTY_CHAIN_NOTE>`. Read this as
  "the chain itself is empty, not that the ±5% window filtered
  every strike out." The `note` carries the disambiguation; read
  it before interpreting the zero counts.
- **ADRs work, foreign-listed equities don't.** Verified 2026-05:
  `TM` (Toyota ADR) returns 8 expirations; the Tokyo-listed `7203.T`
  returns `()`. Also verified empty: `0700.HK` (HKEX), `BMW.DE`
  (Xetra). Pattern: anywhere Yahoo's options endpoint covers is
  the US-listed venue (NMS / NYQ / PCX / BATS / ASE) plus a subset
  of US-listed ADRs (`TM` has 8 expirations as of 2026-05; broader
  ADR coverage not surveyed). Foreign primary listings — even of
  household names — return `()`. When a user asks about options
  on a non-US name, check whether a US ADR exists (Tokyo `7203.T`
  empty → ADR `TM` has options) and route the user there;
  per-ADR coverage varies and is best confirmed by a quick
  `options.py` call before quoting prices.
