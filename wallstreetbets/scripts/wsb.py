#!/usr/bin/env python3
"""Browse r/wallstreetbets (or any subreddit) through Reddit's public RSS feeds.

Standard library only — no install. Reddit's unauthenticated JSON endpoints are
now blocked (403), but the Atom/RSS feeds still serve fine with a descriptive
User-Agent, so everything here goes through `.rss`:

  * listing  -> /r/<sub>/search.rss?q=flair:DD&restrict_sr=on   (flair + keyword)
               or /r/<sub>/<sort>/.rss                          (plain feed)
  * a post   -> /comments/<id>/.rss   (first entry = body, rest = comments)

The search feed carries the FULL post body in <content>, so listing and reading
DD share one fetch path and we never touch JSON.
"""

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# Reddit asks for a descriptive UA of the form <platform>:<app>:<version>.
# A default urllib/curl UA gets 403'd; this one is allowed.
USER_AGENT = "python:claude-code.wallstreetbets-skill:v0.1 (read-only RSS reader)"
ATOM = "http://www.w3.org/2005/Atom"
NS = {"a": ATOM}

VALID_SORTS = ("relevance", "new", "top", "hot", "comments")
VALID_TIMES = ("hour", "day", "week", "month", "year", "all")


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (403, 429):
            hint = (
                "  Reddit is rate-limiting or blocking this request. Wait a bit "
                "and retry; the RSS feeds tolerate light, human-paced use only."
            )
        sys.exit(f"HTTP {e.code} fetching {url}\n{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"Network error fetching {url}: {e.reason}")


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def html_to_text(raw):
    """Lightweight HTML -> readable text. Keeps links as [text](url), turns
    block tags into newlines, drops the rest. Good enough for reading DD."""
    if not raw:
        return ""
    s = html.unescape(raw)
    # Reddit wraps the real body between these markers; the part after SC_ON is
    # the "submitted by /u/... [link] [comments]" footer, which we don't want.
    m = re.search(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", s, re.S)
    if m:
        s = m.group(1)
    s = re.sub(r"<a\s[^>]*?href=\"(.*?)\"[^>]*>(.*?)</a>", r"[\2](\1)", s, flags=re.S)
    s = re.sub(r"<li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"</(p|div|h[1-6]|blockquote|ul|ol|table|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def post_id(entry):
    """Atom <id> looks like 't3_1trgnmg' (post) or 't1_xxx' (comment)."""
    raw = entry.findtext("a:id", default="", namespaces=NS)
    return raw.split("_", 1)[-1] if "_" in raw else raw


def id_from_permalink(url):
    m = re.search(r"/comments/([a-z0-9]+)", url or "")
    return m.group(1) if m else ""


def author_of(entry):
    name = entry.findtext("a:author/a:name", default="", namespaces=NS)
    return name[3:] if name.startswith("/u/") else name


def entry_date(entry):
    """Posts carry <published>; comments often carry only <updated>."""
    raw = (entry.findtext("a:published", default="", namespaces=NS)
           or entry.findtext("a:updated", default="", namespaces=NS) or "")
    return raw[:10]


def word_count(text):
    return len(text.split())


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------
def search_url(sub, flair, query, sort, time, limit):
    terms = []
    if flair:
        # quote multi-word flairs, e.g. flair:"Daily Discussion"
        terms.append(f'flair:"{flair}"' if " " in flair else f"flair:{flair}")
    if query:
        terms.append(query)
    q = " ".join(terms)
    params = {
        "q": q,
        "restrict_sr": "on",
        "sort": sort,
        "t": time,
        "limit": str(limit),
        "include_over_18": "on",
    }
    return f"https://www.reddit.com/r/{sub}/search.rss?" + urllib.parse.urlencode(params)


def feed_sort(sort):
    # Only hot/new/top are real listing sorts. "relevance" and "comments" are
    # search-only; on the plain feed they'd build a bogus path (e.g.
    # /r/sub/comments/.rss is the sub's comment stream, not a post sort), so
    # fall back to hot.
    return sort if sort in ("hot", "new", "top") else "hot"


def feed_url(sub, sort, time, limit):
    sort = feed_sort(sort)
    params = {"limit": str(limit)}
    if sort == "top":
        params["t"] = time
    return f"https://www.reddit.com/r/{sub}/{sort}/.rss?" + urllib.parse.urlencode(params)


def comments_url(post):
    pid = urllib.parse.quote(post, safe="")
    return f"https://www.reddit.com/comments/{pid}/.rss?limit=100"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
import xml.etree.ElementTree as ET  # noqa: E402  (after helpers for readability)


def parse_entries(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # A 200 response that isn't a feed is almost always Reddit's block /
        # rate-limit interstitial — the JSON endpoints 403, but the RSS ones
        # sometimes get served this HTML page instead.
        sys.exit(
            "Reddit returned a non-feed response (likely a block or rate-limit "
            "page). Wait a bit and retry — the RSS feeds tolerate only light, "
            "human-paced use."
        )
    return root.findall(".//a:entry", NS)


def split_thread(entries):
    """Split a /comments/<id>/.rss feed into (post, comments). The post is the
    `t3_` entry; Reddit normally lists it first, but identify it by id prefix
    rather than position so a reordered feed can't turn a comment into the body."""
    post = next(
        (e for e in entries if post_id(e) and
         e.findtext("a:id", default="", namespaces=NS).startswith("t3_")),
        entries[0],
    )
    comments = [e for e in entries if e is not post]
    return post, comments


def cmd_list(args):
    using_search = bool(args.flair) or bool(args.query)
    if using_search:
        url = search_url(args.sub, args.flair, args.query, args.sort, args.time, args.limit)
        eff_sort = args.sort
    else:
        url = feed_url(args.sub, args.sort, args.time, args.limit)
        eff_sort = feed_sort(args.sort)  # header must show the sort actually used

    entries = parse_entries(fetch(url))[: args.limit]
    items = []
    for e in entries:
        link = e.find("a:link", NS)
        href = link.get("href") if link is not None else ""
        body = html_to_text(e.findtext("a:content", default="", namespaces=NS))
        pid = post_id(e) or id_from_permalink(href)
        items.append(
            {
                "id": pid,
                "title": html.unescape(e.findtext("a:title", default="", namespaces=NS) or ""),
                "author": author_of(e),
                "published": entry_date(e),
                "permalink": href,
                "words": word_count(body),
            }
        )

    if args.json:
        print(json.dumps({"source": url, "items": items}, indent=2, ensure_ascii=False))
        return

    scope = []
    if args.flair:
        scope.append(args.flair)
    if args.query:
        scope.append(f'"{args.query}"')
    when = f" · {args.time}" if eff_sort == "top" else ""
    sort_part = f"{eff_sort}{when}"
    header = f"{' · '.join(scope)} · {sort_part}" if scope else sort_part
    print(f"**r/{args.sub} — {header}**\n")
    if not items:
        print("_No posts matched._")
        return
    for i, it in enumerate(items, 1):
        meta = f"u/{it['author']} · {it['published']}"
        if it["words"]:
            meta += f" · ~{it['words']} words"
        print(f"{i}. **[{it['title']}]({it['permalink']})**")
        print(f"   {meta} · `id {it['id']}`")
    print("\n_Read one with_ `read <id>` _or its discussion with_ `comments <id>`.")


def cmd_read(args):
    entries = parse_entries(fetch(comments_url(args.id)))
    if not entries:
        sys.exit(f"No post found for id {args.id}")
    post, comments = split_thread(entries)
    title = html.unescape(post.findtext("a:title", default="", namespaces=NS) or "")
    author = author_of(post)
    published = entry_date(post)
    link = post.find("a:link", NS)
    href = link.get("href") if link is not None else ""
    body = html_to_text(post.findtext("a:content", default="", namespaces=NS))
    n_comments = len(comments)

    if args.json:
        print(json.dumps(
            {"id": args.id, "title": title, "author": author, "published": published,
             "permalink": href, "body": body, "comments_in_feed": n_comments},
            indent=2, ensure_ascii=False))
        return

    print(f"# {title}\n")
    print(f"u/{author} · {published} · [link]({href})\n")
    print(body if body else "_(no text body — likely a link/image post)_")
    print(f"\n---\n_{n_comments}+ comments in feed — see them with_ `comments {args.id}`.")


def cmd_comments(args):
    entries = parse_entries(fetch(comments_url(args.id)))
    if not entries:
        sys.exit(f"No post found for id {args.id}")
    post, comments = split_thread(entries)
    comments = comments[: args.limit]
    title = html.unescape(post.findtext("a:title", default="", namespaces=NS) or "")
    link = post.find("a:link", NS)
    href = link.get("href") if link is not None else ""

    rows = []
    for c in comments:
        rows.append(
            {
                "author": author_of(c),
                "published": entry_date(c),
                "text": html_to_text(c.findtext("a:content", default="", namespaces=NS)),
            }
        )

    if args.json:
        print(json.dumps({"id": args.id, "title": title, "permalink": href, "comments": rows},
                         indent=2, ensure_ascii=False))
        return

    print(f"💬 **Comments on [{title}]({href})**")
    print("_(Reddit's RSS returns a flat, partial slice of the thread in its "
          "default 'best' order — not chronological, and not the full tree.)_\n")
    if not rows:
        print("_No comments in the feed yet._")
        return
    for r in rows:
        text = r["text"].replace("\n", " ").strip()
        if len(text) > 600:
            text = text[:600].rstrip() + "…"
        print(f"- **u/{r['author']}** ({r['published']}): {text}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Browse r/wallstreetbets via Reddit RSS.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list posts (default: DD, top this week)")
    pl.add_argument("--flair", default="DD",
                    help='link flair to filter on (default: DD). Pass "" for no flair / front page.')
    pl.add_argument("--query", default="", help="keyword(s) to search within the subreddit")
    pl.add_argument("--sub", default="wallstreetbets", help="subreddit (default: wallstreetbets)")
    pl.add_argument("--sort", default="top", choices=VALID_SORTS,
                    help="sort order (default: top)")
    pl.add_argument("--time", default="week", choices=VALID_TIMES,
                    help="time window for --sort top (default: week)")
    pl.add_argument("-n", "--limit", type=int, default=10, help="number of posts (default: 10)")
    pl.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("read", help="read a post's full body by id")
    pr.add_argument("id", help="base36 post id from a listing (e.g. 1treiao)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_read)

    pc = sub.add_parser("comments", help="read a post's comments by id")
    pc.add_argument("id", help="base36 post id from a listing")
    pc.add_argument("-n", "--limit", type=int, default=15, help="max comments (default: 15)")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_comments)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
