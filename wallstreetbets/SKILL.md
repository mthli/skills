---
name: wallstreetbets
description: >-
  Browse r/wallstreetbets from inside Claude Code — read DD (due-diligence)
  posts, filter by any flair (News, Gain, Loss, YOLO, Daily Discussion…),
  keyword-search the sub, and open any post's full body or comment thread. Use
  whenever the user wants to see what WSB is saying — including bare "/wsb",
  "what's the DD on WSB", "any new DD", "wallstreetbets sentiment on <ticker>",
  or follow-ups like "read that one" / "what are the comments" while browsing.
---

# r/wallstreetbets

Browse r/wallstreetbets in the terminal. A bundled Python script
(`scripts/wsb.py`, standard library only — no install) fetches Reddit's public
RSS feeds and formats results as Markdown; your job is to map what the user
asked for to the right command, run it, and present the output.

`<SKILL_DIR>` below is the directory containing this `SKILL.md` — substitute its
absolute path when running.

## Why RSS

Reddit's unauthenticated JSON endpoints now return 403, but the `.rss` feeds
still serve fine with a descriptive User-Agent (the script sets one). So
everything here goes through RSS, and importantly the **search feed carries each
post's full body** — listing and reading DD share one fetch path. This is
read-only: the script never posts, votes, or logs in.

## Commands

```
# List posts. Default: flair DD, sorted top over the past week.
# --flair <name>  any link flair (DD, News, Gain, Loss, YOLO, Discussion,
#                 "Daily Discussion", "Earnings Thread"); pass --flair "" for
#                 the plain front page (no flair filter).
# --query <text>  keyword search within the sub (combinable with --flair)
# --sort          top | new | relevance | hot | comments   (default: top)
# --time          hour | day | week | month | year | all   (only affects top)
# --sub <name>    any subreddit (default: wallstreetbets)
python3 <SKILL_DIR>/scripts/wsb.py list [--flair DD] [--query "<text>"] -n <count> --sort <sort> --time <window>

# Read a post's full body (id comes from a listing)
python3 <SKILL_DIR>/scripts/wsb.py read <id>

# Read a post's comment thread
python3 <SKILL_DIR>/scripts/wsb.py comments <id> -n <count>
```

Every command takes `--json` if you need structured data instead of Markdown
(e.g. the user wants you to rank, filter, or summarize across posts, or extract
tickers).

## Mapping requests to commands

**The default — bare `/wallstreetbets`, "what's the DD", "anything good on WSB"
— is `list` with its defaults: DD flair, `--sort top --time week`.** That window
matters: DD is low-volume and quality is bimodal (plenty of pump-and-dump posts
flaired "DD" that nobody upvotes), so "top this week" is the curated reading
list — the handful the sub actually found worth reading. Reach for `--sort new`
only when the user explicitly wants what's *fresh* ("any new DD", "DD today",
"latest") — that view includes unvetted posts, which is the point.

Map flair words directly: "news" → `--flair News`, "gains"/"gain porn" →
`--flair Gain`, "losses"/"loss porn" → `--flair Loss`, "YOLOs" → `--flair YOLO`,
"daily"/"daily discussion"/"what are your moves" → `--flair "Daily Discussion"`,
"earnings thread" → `--flair "Earnings Thread"`. "The front page" / "everything"
/ "just what's hot" → `--flair "" --sort hot`.

"WSB on NVDA" / "what's WSB saying about Nvidia" / "sentiment on $TSLA" → add
`--query "<ticker>"`. Combine with a flair when they're specific ("DD on NVDA" →
`--flair DD --query "NVDA"`). Note keyword + flair is relevance-weighted, not a
strict AND — for tight keyword matching prefer `--sort relevance` (the default
`top` still works well for "the big posts about X").

"Read that one" / "open #2" / "what does the ASTS DD actually say" → `read <id>`,
using the `` `id <base36>` `` shown at the end of each listing line. If the user
names a post by topic rather than rank, pick the matching id from the most
recent listing.

"What are people saying" / "read the comments" / "is the room bullish" →
`comments <id>`.

**Superlative-across-the-listing requests** — "which DD is the most detailed",
"the biggest post this week" — need a wider pull than the default 10, so
`list … -n 25` first, then pick from those. The `~N words` on each line is a
quick proxy for how substantial a DD is (a 5,000-word writeup vs a 60-word
shill).

Browsing another subreddit ("do this for r/options", "stocks subreddit") → add
`--sub <name>`; everything else works the same.

## Presenting results

The script already emits clean, link-rich Markdown. Relay it largely as-is —
don't re-fetch each post or rewrite every line; that wastes time and adds
nothing. A short framing line is welcome ("Here's the top DD on WSB this week —")
and so is a one-line offer of the next step ("Want me to read any of these in
full?"). Keep the clickable title links and the `id` markers intact so
follow-ups work.

If the user asked a question *about* the posts rather than just to see them
("which of these is worth reading", "summarize the ASTS thesis", "what tickers
are people pushing"), go ahead and read/summarize — pull the body with `read`,
or use `--json` across a listing to extract and rank — then answer.

Treat WSB content with appropriate skepticism when summarizing: it's anonymous,
often promotional, and not investment advice. Surface the thesis and the
sentiment, but flag when a "DD" reads like a pump rather than analysis (a tiny
body, all hype, no numbers — the `~N words` count and a quick read make this
obvious). Don't launder ramp-job hype into confident-sounding investment advice.

## Notes

- IDs are Reddit's base36 post ids (e.g. `1tnimoe`). `read` and `comments` both
  hit the same `/comments/<id>/.rss` feed — its first entry is the post body,
  the rest are comments — so reading a post and then its comments is cheap.
- Comment feeds are a **flat, partial slice** of the thread (Reddit's RSS caps
  it and doesn't nest replies), enough to read the room but not the whole tree.
  Don't present it as the complete discussion.
- Link/image posts (common under Gain/Loss/Meme) have no text body; `read` will
  say so rather than inventing content.
- If a fetch returns HTTP 403/429, Reddit is throttling — wait and retry; the
  feeds only tolerate light, human-paced use.
