# Investment Knowledge Loop — CLAUDE.md read-rules template

This is the block `/commit-invest` injects into the project's `CLAUDE.md` during the one-shot
bootstrap (the same invocation that creates `.claude/THEMES.md` for the first time). After
that bootstrap moment, `/commit-invest` never touches `CLAUDE.md` again — the user owns
their project config from there on.

## When to use this file

Read this file during `/commit-invest`'s step 3, **only** in the branch where
`.claude/THEMES.md` was just bootstrapped and the user approved the CLAUDE.md read-rules
install. Copy the content below the `---` separator (no leading whitespace — paste it
flush-left).

## Language handling

If the existing `CLAUDE.md` is in a non-English language, translate the prose to match. But
**keep these tokens verbatim** — they're literal identifiers used by `/distill-ticker` and
`/distill-theme` parsers, and translating silently breaks distillation:

`TICKER`, `THEMES`, `STANCE`, `HORIZON`, `THESIS`, `CATALYSTS`, `INVALIDATION`,
`CONVICTION`, `ENTRY_PRICE`, `EXIT_PRICE`, `SUPERSEDES`, `REGIME`, `DRIVERS`,
`IMPLICATIONS`, `RELATED_THEMES`, `RELATED_TICKERS`, `WHAT_HAPPENED`, `WHAT_WORKED`,
`WHAT_DIDNT`, `GENERAL_LESSON`, `RULE`, `WHY`, `HOW_TO_APPLY`, `WHAT`, `WHY_IT_MATTERS`,
`.claude/THEMES.md`, `.claude/snapshots/`, `/commit-invest`, `/distill-ticker`,
`/distill-theme`, `git log`, and any field name in ALL CAPS.

---

## Investment Knowledge Loop

### Before any discussion

1. Identify the ticker(s) and theme(s) involved in the question.
2. For each ticker, read `.claude/snapshots/positions/<TICKER>.md` (current consensus
   snapshot). If the journal has had blocks added since the snapshot, also read
   `positions/<TICKER>.md` in full — it's the source of truth.
3. For each theme, read `.claude/snapshots/themes/<theme>.md`. If macro context applies,
   read `macro.md` in full.
4. If a snapshot doesn't exist yet, fall back to the raw journal file. If neither exists,
   say so explicitly before forming new views — don't pretend prior context that isn't
   there.

### After forming new views

- Use `/commit-invest` to persist the view as a structured block.
- Every Thesis must fill `INVALIDATION` — a thesis without an exit trigger is a story, not
  a position. Same for `STANCE` and `HORIZON`: vague views don't survive months of
  forgetting.
- If a new view overrides a prior one, add `SUPERSEDES:` referencing the date and commit
  hash of the prior block.

### Periodically

- Run `/distill-ticker <TICKER>` after a ticker has accumulated 3–5 new blocks since the
  last distill, so the snapshot stays current.
- Run `/distill-theme <theme>` after several theses across multiple tickers have referenced
  the theme.
