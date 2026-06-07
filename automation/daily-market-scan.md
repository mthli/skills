Run these five skills one at a time, sequentially — wait for each to fully
finish before starting the next. Do NOT run them in parallel: they share no
price-data cache, so concurrency saves no Yahoo requests and only compresses
them in time, which trips Yahoo's per-IP "throttled every request" rate limit.
Each scan already runs fine on its own.

Run in this order (lightest first, the request-heavy options scan last):

1. /regime-scan
2. /momentum-scan
3. /base-breakout-scan
4. /mean-reversion-scan
5. /unusual-options-scan # ~2000 option-chain requests — keep it isolated, run last.

Each writing its output files only (no git).

After all of them finish, check git status;
if there are changes, commit them on the current branch and push,
otherwise do nothing.
