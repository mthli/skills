Run these five scan skills one at a time, in order — wait for each to fully
finish before starting the next. Do NOT run them in parallel: they share no
price-data cache, so concurrency saves no Yahoo requests and only trips
Yahoo's per-IP rate limit. Each scan runs fine on its own. Lightest first,
the request-heavy options scan last:

1. /regime-scan
2. /momentum-scan
3. /base-breakout-scan
4. /mean-reversion-scan
5. /unusual-options-scan — ~2000 option-chain requests; pass `--max-workers 4`
   to its scan.py (the default 16 hammers Yahoo hard enough to trigger HTTP
   401 "Invalid Crumb" throttle errors).

Each scan writes its output files only — no git.

After all five finish, check git status: if there are changes, commit them
on the current branch and push; otherwise do nothing.
