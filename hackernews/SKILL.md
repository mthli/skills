---
name: hackernews
description: >-
  View Hacker News from inside Claude Code — top stories and other feeds
  (new/best/Ask/Show/jobs), the comment thread for any story, and keyword
  search. Use whenever the user wants to read or browse Hacker News, including
  when they just say "HN", name a feed, or follow up with "read the comments",
  "show more", or "open #3" while browsing.
---

# Hacker News

Browse Hacker News in the terminal. A bundled Python script (`scripts/hn.py`,
standard library only — no install) does the fetching and formats results as
Markdown; your job is to map what the user asked for to the right command, run
it, and present the output.

`<SKILL_DIR>` below is the directory containing this `SKILL.md` — substitute its
absolute path when running.

## Commands

```
# List a feed (default: top, 10 stories). Feeds: top, new, best, ask, show, job
python3 <SKILL_DIR>/scripts/hn.py list [feed] -n <count>

# Show a story's comment thread (needs the numeric id from a listing)
python3 <SKILL_DIR>/scripts/hn.py comments <id> -n <top-level> -d <max-depth>

# Search stories via Algolia (--sort date for newest-first instead of relevance)
python3 <SKILL_DIR>/scripts/hn.py search "<query>" -n <count> [--sort date]
```

Every command takes `--json` if you need structured data instead of Markdown
(e.g. the user wants you to filter, rank, or summarize rather than just list).

## Mapping requests to commands

The default — bare `/hackernews`, "what's on HN", "top stories" — is
`list` with the default top feed and 10 stories. Honor an explicit count
("top 25", "show me 30") with `-n`. Map feed words directly: "new" / "newest"
→ `new`, "best" → `best`, "Ask HN" → `ask`, "Show HN" → `show`, "jobs" / "who's
hiring" → `job`.

"Read the comments on #3" / "open that thread" / "what are people saying about
the SpaceX one" → `comments <id>`, using the id shown in the listing you just
produced (each line ends with `` `id <n>` ``). If the user names a story by
topic rather than rank, pick the matching id from the most recent listing.

**Superlative-across-the-front-page requests** — "which story has the most
comments", "the top discussion right now", "the highest-ranked one" — need a
wider pull than the 10-story default, or you'll pick the max of the wrong set.
The HN front page is ~30 stories, so `list <feed> -n 30` first, choose the
extremum from those 30, then act (e.g. `comments <id>`). Keep ordinary browsing
at the 10-story default; only widen when the user's request hinges on a
"most/highest/biggest" comparison.

"Search HN for Rust async" / "has HN discussed <X>" → `search`. Use
`--sort date` when they want what's *recent* ("latest HN posts about …")
rather than the most-upvoted all-time.

## Presenting results

The script already emits clean, link-rich Markdown. Relay it to the user
largely as-is — don't re-fetch each item or rewrite every line; that wastes
time and adds nothing. A short framing line is welcome ("Here's the HN front
page right now —"), and so is a one-line offer of the natural next step ("Want
me to pull the comments on any of these?"). Keep the clickable links and the
`id` markers intact so follow-ups work.

If the user asked a question *about* the stories (e.g. "anything about AI
infra?", "summarize the top discussion") rather than just to see them, go ahead
and read/summarize — fetch comments or use `--json` as needed, then answer.

## Notes

- Stories carry an article link **and** a `discussion` link (the HN comment
  page); keep both — readers often want one or the other.
- Ask HN / Show HN text posts have no external URL, so the title links to the HN
  item and the post body shows at the top of its `comments` view.
- Comment fetching defaults to a focused view (8 top-level threads, depth 2, 30
  nodes total) — enough to read the room without burying the user in a 300-reply
  megathread. When they want more ("go deeper", "show all comments", "the full
  thread"), widen with `-n` (more top-level), `-d` (deeper nesting), and
  `--limit` (raise the node budget, e.g. `--limit 150`).
- Hacker News data is public and read-only here; the script never posts,
  votes, or logs in.
