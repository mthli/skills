# Performance

_Last measured: 2026-05 (US connection, US daytime). Re-measure if numbers
feel off — yfinance/Yahoo backends drift. Run
`time uv run --with 'yfinance>=1.3,<2' python <SKILL_DIR>/scripts/info.py AAPL`
and subtract ~0.5s of uv startup for the network-only delta._

## Per-mode latency

| Mode | Latency | Why |
|---|---|---|
| `fast_info` | ~0.3–0.5 s | one Yahoo call, small payload |
| `history` (≤1y daily, 1 ticker) | ~0.5–1.5 s | one call, ~252 rows for 1y |
| `history` (≤1y daily, N tickers) | ~0.7 s for 5, ~1.5–2.5 s for 10 | yf.download batches N≥2 in one request, threaded |
| `history` (max / 5y intraday) | ~2–4 s | larger payload |
| `info` | ~1–3 s | multiple internal modules — as of yfinance 1.3.x: `financialData`, `quoteType`, `defaultKeyStatistics`, `assetProfile`, `summaryDetail` |
| `earnings` (equity) | ~1.5–2.5 s | quote_type pre-check (~0.3s) + HTML scrape (~1–2s) |
| `earnings --estimates` (equity) | ~+1.5–3 s on top of baseline (total ~3–5.5 s) | five Yahoo property reads on a shared Ticker (`earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions`, `growth_estimates`); equity-only. **Worst case under sustained 429:** each of the 5 sources independently retries up to 3 attempts. `with_retry` sleeps between attempts only — 3 attempts means 2 backoff windows (~0.5 s after attempt 1, ~1.0 s after attempt 2, plus jitter), so per-source max sleep ≈ 2 s. 5 sources × ~2 s = ~10 s of cumulative sleep, plus the 15 actual call attempts, gives a total worst-case of ~10–15 s before failing. Drop batch size to ~3 and pause between calls if you see this pattern. |
| `earnings` (non-equity) | ~0.3–0.5 s | quote_type pre-check only; scrape skipped via short-circuit (and `--estimates` short-circuits too) |
| `financials` (equity, any `--statement` value) | ~2 s | quote_type pre-check (~0.3s) + `info["financialCurrency"]` (~1.5s) + statement fetches (see `--statement` cost note below) |
| `financials` (equity, ADR / cross-listed) | ~3–5 s | same path but `info` round-trip is slower for less-common tickers (verified: TM ~4.8s) |
| `financials` (non-equity) | ~1 s | quote_type pre-check only; financials fetch skipped, no `info` call |
| `financials` (equity, soft-fallback path triggered) | up to +3.5 s | when `info["financialCurrency"]` is unavailable (transient 429 / network / field missing), `_meta` retries via `_trading_currency` with backoff. Worst case (sustained 429 on both `info` and `fast_info`) adds ~1.5–3.5s of retry sleeps to the equity baseline above. Watch for `"trading currency"` substring in `note` to detect this path. |
| `news` | ~0.3–0.5 s | one Yahoo call, payload of up to ~10 articles. Per-ticker serial loop (no batching). Cold-start adds ~2 s on the first call of a session (yfinance crumb-cookie init — see the `fast_info` retry paragraph below for the same pattern in retry context). |
| `holders` | ~0.3–0.7 s | the three properties (`major_holders` / `institutional_holders` / `mutualfund_holders`) appear to share a single `quoteSummary` HTTP request — observed timing: first read ~120 ms warm, next two ~0 ms (could be module batching at the yfinance layer or just session-cookie HTTP cache; not source-confirmed, but either way cost is single-call). Per-ticker serial loop. Same cold-start pattern as `news`. Empty-result tickers (ETFs / FX / futures / indexes / crypto / bogus — all verified empty) cost the same — Yahoo doesn't short-circuit on its end. |
| `options` | ~0.3–1.0 s | One or two HTTP calls per ticker depending on `--expiry`. **No `--expiry` (1 HTTP):** `option_chain()` no-arg returns the default-expiry chain AND fills the expirations list in a single call (verified empirically AAPL/SPY/NVDA/TM 2026-05). **With `--expiry` (1–2 HTTP):** `t.options` first to pre-validate the date (so a bad date routes to clean `error_kind: not_found` instead of yfinance's hard-to-classify ValueError), then `t.option_chain(date)` for the chain. The second HTTP **only fires for tickers with listed options** — empty-result tickers (`^GSPC`, `BTC-USD`, `0700.HK`, etc.) short-circuit after `t.options=()` whether or not `--expiry` was passed, so they cost 1 HTTP either way. One expiry per call by design — fetching all 24 of AAPL's expirations would be 24 HTTP calls. **Retry replays the full path**, so a 3-attempt retry of the 2-HTTP path can hit Yahoo up to 6 times; the 1-HTTP path's worst case is 3 (half that). Per-ticker serial loop. |

## Serial vs batched

`fast_info`, `info`, `earnings`, `financials`, `news`, `holders`, and
`options` are all serial — total ≈ N × per-ticker cost. A 10-ticker `info` or
`financials` batch is ~15–30 s and is the most likely path to trigger
Yahoo's empty-response / 429 rate-limit (`financials` actually issues an
`info` call internally for reporting-currency lookup, so the cost
profile is similar to `info`). `history` is the exception: N ≥ 2 routes
through `yf.download` (one HTTP request, threaded), so 5–10 tickers cost
~1–2 s total instead of ~5–15 s. When a question is answerable by
multiple modes, pick the cheapest. Don't call `info` if `fast_info`
already has the field you need (e.g., `market_cap` is in both); don't
call `financials` for "what's AAPL's P/E" — that's in `info`.

`financials` cost note: `--statement income` does NOT save latency over
`--statement all` — yfinance shares the underlying fundamentals payload
across the three statement properties, so all three come back from one
call. Use `--statement <one>` to save **context tokens** (smaller JSON
output), not time.

## Retry cost

A retried call adds backoff (~0.5–1.5 s per retry, 3 attempts max).
`fast_info` retry is the worst case: each retry replays all field reads
from scratch (the first read in a session is ~3 s; subsequent ones
~150 ms cached), so a single retried `fast_info` call can total ~5–7 s
instead of the nominal 0.3–0.5 s. Watch the `attempts` field in the
response — it appears whenever a call retried.

## `--summary` is post-fetch projection, not a network optimization

`--summary` modes (`history --summary`, `info --summary`, `earnings --summary`,
`financials --summary`, `holders --summary`, `options --summary`) **don't reduce latency** —
they're post-fetch projections of the same payload, so network cost is
identical to the default mode. Only the output JSON shrinks (~10× for
`info --summary`, `financials --summary`, and `holders --summary`, more
for `history --summary` when the period is long). Use `--summary` to
save context tokens, not to save time.

`news` and `fast_info` deliberately don't have a `--summary` mode.
`fast_info` is already flat (no projection to do); `news` is a list per
ticker (not headline numerics for peer comparison), so use `--limit N`
to tighten its output instead. See references/news.md.
