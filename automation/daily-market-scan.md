Run these five scan skills one at a time, in order — wait for each to fully
finish before starting the next. Do NOT run them in parallel: they share no
price-data cache, so concurrency saves no Yahoo requests and only trips
Yahoo's per-IP rate limit. Each scan runs fine on its own. Lightest first,
except snapback-scan, which MUST run last: it consumes mean-reversion-scan's
just-written run as its keg list (running it earlier reads yesterday's):

1. /regime-scan
2. /momentum-scan
3. /base-breakout-scan
4. /mean-reversion-scan
5. /snapback-scan

Each scan writes its output files only — no git.

After all five finish, check git status: if there are changes, commit them
on the current branch and push; otherwise do nothing.
