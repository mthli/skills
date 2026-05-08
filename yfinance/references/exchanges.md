# Exchange codes

`fast_info` and `info` return a short Yahoo `exchange` code, not a human-
readable name. Decode before showing to the user — render
"AAPL (Nasdaq)" not "AAPL (NMS)":

| Code | Exchange | Code | Exchange |
|---|---|---|---|
| `NMS` | Nasdaq | `HKG` | HKEX (Hong Kong) |
| `NYQ` | NYSE | `SHH` | Shanghai |
| `ASE` | NYSE American | `SHZ` | Shenzhen |
| `PCX` | NYSE Arca (most ETFs) | `TYO` / `JPX` | Tokyo |
| `BATS` | Cboe BZX | `KSC` | KOSPI / KRX |
| `TOR` | TSX (Toronto) | `LSE` | London |
| `ASX` | ASX (Australia) | `GER` | Xetra (Frankfurt) |
| `NSI` | NSE (India) | `EBS` | SIX Swiss |
| `BOM` | BSE (India) | `MIL` | Borsa Italiana |

Don't infer exchange from the ticker suffix — both `fast_info` and `info`
return it explicitly. `0700.HK` → `HKG`, `BMW.DE` → `GER`, `7203.T` → `JPX`.
