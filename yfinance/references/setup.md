# Setup

**Python 3.9+ required** (the helpers use PEP 604 unions and lowercase
`tuple[]` subscripts). `uv run` picks a recent Python automatically; if
you bypass `uv` and run scripts directly under an older interpreter,
helpers.py will refuse to import with a clear message.

## Install `uv`

```bash
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
```

`uv` is Astral's Rust-based Python package manager. The official installer
drops the binary in `~/.local/bin`, which may not yet be on `PATH` in the
current shell — the `export` line covers that case.

## Why `uv run --with`

`uv run --with 'yfinance>=1.3,<2'` resolves and caches yfinance in an
ephemeral env:
- First call on a fresh machine takes ~5–15 s while uv downloads wheels
- Subsequent calls are nearly instant (uv reuses the cache)
- No `pip install` side-effects on the user's global Python
- The version pin guards against yfinance's not-infrequent breaking changes;
  bump the upper bound deliberately, not by accident

## `earnings.py` needs `lxml`

**`earnings.py` needs an extra `--with 'lxml'`** — yfinance scrapes earnings
from Yahoo's HTML calendar via `pandas.read_html`, which requires `lxml`.
yfinance documents the requirement in a comment but doesn't pin it as a hard
dependency, so without `--with 'lxml'` every earnings fetch fails with
`error_kind: unknown` and a misleading "Missing optional dependency 'lxml'"
log line. The other seven modes (`fast_info`, `history`, `info`, `news`,
`financials`, `holders`, `options`) hit Yahoo's JSON API and don't need it.
`smoke.py` also needs lxml because it imports `earnings`. Use
`--with 'yfinance>=1.3,<2' --with 'lxml'` for those two.
