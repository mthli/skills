#!/usr/bin/env python3
"""Fetch and display Hacker News content for viewing inside Claude Code.

Subcommands:
  list [feed] [-n N]        List stories from a feed (top/new/best/ask/show/job)
  comments <id> [-n N] [-d D]  Show a story's comment thread
  search <query> [-n N]     Search HN via Algolia (relevance or recency)

Output is ready-to-display Markdown by default; pass --json for structured data.
Only the Python standard library is used, so no install step is needed.
"""

import argparse
import concurrent.futures
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

FIREBASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://news.ycombinator.com/item?id="
USER_AGENT = "hackernews-skill/1.0 (Claude Code)"

# Map friendly feed names to the Firebase endpoint and a display label.
FEEDS = {
    "top": ("topstories", "Top Stories"),
    "new": ("newstories", "New Stories"),
    "best": ("beststories", "Best Stories"),
    "ask": ("askstories", "Ask HN"),
    "show": ("showstories", "Show HN"),
    "job": ("jobstories", "Jobs"),
}

# Bound concurrent fetches so we are polite to the API and fast on the wire.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=16)


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_item(item_id):
    """Fetch a single HN item; return None on failure so callers can skip it."""
    try:
        return _get(f"{FIREBASE}/item/{item_id}.json")
    except Exception:
        return None


def fetch_items(ids):
    """Fetch many items concurrently, preserving the input order."""
    return list(_POOL.map(fetch_item, ids))


# --- time + text helpers -----------------------------------------------------

def age(unix_time, now):
    """Human-friendly relative age, e.g. '3h ago'. now is passed in (no clock
    calls here) so the same data renders deterministically."""
    if not unix_time:
        return ""
    secs = max(0, now - int(unix_time))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


_TAG_RE = re.compile(r"<[^>]+>")


def clean_html(text):
    """Turn HN's HTML comment bodies into plain text: paragraphs become blank
    lines, anchor tags collapse to their visible text, entities are unescaped.
    Code blocks (<pre><code>) are preserved as fenced Markdown so snippets keep
    their formatting instead of being flattened into prose."""
    if not text:
        return ""
    # Stash code blocks behind NUL placeholders so the tag stripper below can't
    # eat their contents; reinsert them as fenced blocks after the prose is clean.
    blocks = []

    def _stash(m):
        blocks.append(html.unescape(m.group(1)).strip("\n"))
        return f"\x00CODE{len(blocks) - 1}\x00"

    text = re.sub(r"<pre><code>(.*?)</code></pre>", _stash, text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = text.replace("<p>", "\n\n").replace("</p>", "")
    text = re.sub(r'<a\s+[^>]*?href="([^"]*)"[^>]*>(.*?)</a>',
                  lambda m: m.group(2) or m.group(1), text, flags=re.IGNORECASE | re.DOTALL)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).strip()
    for i, code in enumerate(blocks):
        text = text.replace(f"\x00CODE{i}\x00", f"\n```\n{code}\n```\n")
    return text


# --- list --------------------------------------------------------------------

def cmd_list(args, now):
    if args.feed not in FEEDS:
        print(f"Unknown feed '{args.feed}'. Choose from: {', '.join(FEEDS)}", file=sys.stderr)
        return 2
    endpoint, label = FEEDS[args.feed]
    ids = _get(f"{FIREBASE}/{endpoint}.json")[: args.count]
    items = [it for it in fetch_items(ids) if it]

    if args.json:
        print(json.dumps(items, indent=2))
        return 0

    out = [f"## Hacker News — {label}", ""]
    for rank, it in enumerate(items, 1):
        title = it.get("title", "(untitled)")
        url = it.get("url") or f"{HN_ITEM}{it['id']}"
        meta = []
        if it.get("score") is not None:
            meta.append(f"{it['score']} points")
        n_comments = it.get("descendants")
        if n_comments is not None:
            meta.append(f"{n_comments} comments")
        if it.get("by"):
            meta.append(f"by {it['by']}")
        a = age(it.get("time"), now)
        if a:
            meta.append(a)
        out.append(f"{rank}. **[{title}]({url})**")
        line = "   " + " · ".join(meta)
        line += f" · [discussion]({HN_ITEM}{it['id']}) · `id {it['id']}`"
        out.append(line)
        out.append("")
    out.append("_Ask to open the comments on any of these (by rank or `id`), or for more stories._")
    print("\n".join(out))
    return 0


# --- comments ----------------------------------------------------------------

def collect_comments(top_kids, max_depth, limit):
    """Collect a story's comments breadth-first across the top-level threads,
    bounded by max_depth and an overall node budget. Returns a list of
    (depth, item) in threaded (pre-order) display order.

    Going breadth-first matters: a depth-first walk would let the first loud
    thread's descendants eat the whole budget, so the later top-level comments
    never show. Here every level fetches only as many items as the remaining
    budget allows, which both spreads the budget across threads and avoids
    pulling hundreds of replies from a hot comment just to discard most of them.
    """
    # Each entry is (depth, order_key, item_id). order_key is the path of sibling
    # indices from the root, so sorting by it later reproduces threaded order.
    frontier = [(0, (i,), kid_id) for i, kid_id in enumerate(top_kids)]
    collected = []  # (order_key, depth, item)
    while frontier and len(collected) < limit:
        remaining = limit - len(collected)
        batch, rest = frontier[:remaining], frontier[remaining:]
        items = fetch_items([item_id for _, _, item_id in batch])
        children = []
        for (depth, key, _id), item in zip(batch, items):
            if not item or item.get("deleted") or item.get("dead"):
                continue
            collected.append((key, depth, item))
            if depth + 1 <= max_depth and item.get("kids"):
                for j, child_id in enumerate(item["kids"]):
                    children.append((depth + 1, key + (j,), child_id))
        # Unfetched same-level siblings stay ahead of the deeper children, so a
        # level that overruns the budget never silently loses its remaining
        # siblings to a dive into replies.
        frontier = rest + children
    collected.sort(key=lambda t: t[0])
    return [(depth, item) for _key, depth, item in collected]


def cmd_comments(args, now):
    story = fetch_item(args.id)
    if not story:
        print(f"Could not fetch item {args.id}.", file=sys.stderr)
        return 1

    top_kids = (story.get("kids") or [])[: args.count]
    acc = collect_comments(top_kids, args.depth, args.limit)

    if args.json:
        print(json.dumps({"story": story, "comments":
                          [{"depth": d, **c} for d, c in acc]}, indent=2))
        return 0

    title = story.get("title", "(untitled)")
    url = story.get("url") or f"{HN_ITEM}{story['id']}"
    out = [f"## {title}"]
    meta = []
    if story.get("score") is not None:
        meta.append(f"{story['score']} points")
    if story.get("descendants") is not None:
        meta.append(f"{story['descendants']} comments")
    if story.get("by"):
        meta.append(f"by {story['by']}")
    a = age(story.get("time"), now)
    if a:
        meta.append(a)
    out.append(" · ".join(meta))
    out.append(f"{url}")
    if story.get("text"):
        out.append("")
        out.append(clean_html(story["text"]))
    out.append("")
    out.append("---")
    out.append("")

    if not acc:
        out.append("_No comments yet._")
    for depth, c in acc:
        prefix = "> " * (depth + 1)
        header = f"**{c.get('by', '[unknown]')}** · {age(c.get('time'), now)}"
        out.append(prefix + header)
        body = clean_html(c.get("text", ""))
        for line in (body.split("\n") if body else [""]):
            # Blank continuation lines would otherwise carry the prefix's trailing
            # space; rstrip keeps the blockquote intact without that whitespace.
            out.append(prefix + line if line else prefix.rstrip())
        out.append("")
    print("\n".join(out))
    return 0


# --- search ------------------------------------------------------------------

def cmd_search(args, now):
    endpoint = "search_by_date" if args.sort == "date" else "search"
    params = urllib.parse.urlencode({
        "query": args.query,
        "tags": "story",
        "hitsPerPage": args.count,
    })
    data = _get(f"{ALGOLIA}/{endpoint}?{params}")
    hits = data.get("hits", [])

    if args.json:
        print(json.dumps(hits, indent=2))
        return 0

    sort_label = "by date" if args.sort == "date" else "by relevance"
    out = [f'## Hacker News search: "{args.query}" ({sort_label})', ""]
    if not hits:
        out.append("_No results._")
    for rank, h in enumerate(hits, 1):
        title = h.get("title") or h.get("story_title") or "(untitled)"
        url = h.get("url") or f"{HN_ITEM}{h['objectID']}"
        meta = []
        if h.get("points") is not None:
            meta.append(f"{h['points']} points")
        if h.get("num_comments") is not None:
            meta.append(f"{h['num_comments']} comments")
        if h.get("author"):
            meta.append(f"by {h['author']}")
        ct = h.get("created_at_i")
        if ct:
            meta.append(age(ct, now))
        out.append(f"{rank}. **[{title}]({url})**")
        out.append("   " + " · ".join(meta) +
                   f" · [discussion]({HN_ITEM}{h['objectID']}) · `id {h['objectID']}`")
        out.append("")
    print("\n".join(out))
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="View Hacker News from the terminal.")
    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("list", help="List stories from a feed")
    lp.add_argument("feed", nargs="?", default="top",
                    help="top (default), new, best, ask, show, job")
    lp.add_argument("-n", "--count", type=int, default=10, help="How many stories")
    lp.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")

    cp = sub.add_parser("comments", help="Show a story's comment thread")
    cp.add_argument("id", type=int, help="HN item id")
    cp.add_argument("-n", "--count", type=int, default=8,
                    help="Top-level comments to expand")
    cp.add_argument("-d", "--depth", type=int, default=2, help="Max reply nesting depth")
    cp.add_argument("--limit", type=int, default=30, help="Total comment node budget")
    cp.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")

    sp = sub.add_parser("search", help="Search HN stories via Algolia")
    sp.add_argument("query", help="Search terms")
    sp.add_argument("-n", "--count", type=int, default=10, help="How many results")
    sp.add_argument("--sort", choices=["relevance", "date"], default="relevance",
                    help="Rank by relevance (default) or recency")
    sp.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    return p


def main(argv):
    args = build_parser().parse_args(argv)
    # Single clock read at the entry point; threaded fetches stay deterministic.
    import time
    now = int(time.time())
    try:
        if args.command == "list":
            return cmd_list(args, now)
        if args.command == "comments":
            return cmd_comments(args, now)
        if args.command == "search":
            return cmd_search(args, now)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        # URLError covers connection/HTTP failures, TimeoutError a stalled read,
        # JSONDecodeError a non-JSON error page — all surface as a clean message
        # rather than a traceback from the top-level feed/search fetch.
        print(f"Couldn't reach Hacker News: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
