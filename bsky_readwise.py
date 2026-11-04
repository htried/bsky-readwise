#!/usr/bin/env python3
"""Sync Bluesky bookmarks ("Saved posts") into Readwise and/or Readwise Reader.

Stdlib only. Configure with environment variables:
  BSKY_HANDLE           your handle, e.g. alice.bsky.social
  BSKY_APP_PASSWORD     an app password (Settings -> Privacy and security -> App passwords)
  READWISE_TOKEN        from https://readwise.io/access_token
  BSKY_SERVICE          optional, login host (default https://bsky.social)
"""

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APPVIEW_PROXY = "did:web:api.bsky.app#bsky_appview"
READWISE_HIGHLIGHTS = "https://readwise.io/api/v2/highlights/"
READER_SAVE = "https://readwise.io/api/v3/save/"
SOURCE_TYPE = "bluesky_bookmarks"
MAX_HIGHLIGHT_CHARS = 8191


# --------------------------------------------------------------------------- http

def request(method, url, *, headers=None, body=None, params=None, retries=4):
    endpoint = url
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                wait = int(e.headers.get("Retry-After") or 10 * (attempt + 1))
                print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            detail = re.sub(r"at://\S+?(?=[\s\"']|$)", "at://…", e.read().decode(errors="replace"))[:500]
            raise RuntimeError(f"{method} {endpoint} -> {e.code}: {detail}") from None


# ------------------------------------------------------------------------ bluesky

class Bluesky:
    def __init__(self, handle, app_password, service="https://bsky.social"):
        session = request(
            "POST",
            f"{service}/xrpc/com.atproto.server.createSession",
            body={"identifier": handle, "password": app_password},
        )
        self.did = session["did"]
        self.token = session["accessJwt"]
        self.pds = service
        for svc in (session.get("didDoc") or {}).get("service", []):
            if svc.get("id") == "#atproto_pds":
                self.pds = svc["serviceEndpoint"]

    def xrpc(self, nsid, **params):
        return request(
            "GET",
            f"{self.pds}/xrpc/{nsid}",
            headers={"Authorization": f"Bearer {self.token}", "atproto-proxy": APPVIEW_PROXY},
            params={k: v for k, v in params.items() if v is not None},
        )

    def bookmarks(self, is_known, full=False):
        """Yield bookmark entries newest-first. Unless `full`, stop after a
        page containing nothing new (i.e. we've caught up with the last sync)."""
        cursor = None
        while True:
            page = self.xrpc("app.bsky.bookmark.getBookmarks", limit=100, cursor=cursor)
            items = page.get("bookmarks", [])
            yield from items
            fresh = [b for b in items if not is_known(b["subject"]["uri"])]
            cursor = page.get("cursor")
            if not cursor or not items or (not full and not fresh):
                return

    def self_thread(self, post):
        """Return the author's continuation of a thread below `post`."""
        author = post["author"]["did"]
        try:
            res = self.xrpc("app.bsky.feed.getPostThread", uri=post["uri"], depth=100, parentHeight=0)
        except RuntimeError as e:
            print(f"  could not fetch a thread: {e}", file=sys.stderr)
            return []
        chain, node = [], res.get("thread", {})
        while True:
            replies = [
                r for r in node.get("replies") or []
                if r.get("$type", "").endswith("#threadViewPost")
                and r["post"]["author"]["did"] == author
            ]
            if not replies:
                return chain
            node = min(replies, key=lambda r: r["post"]["record"].get("createdAt", ""))
            chain.append(node["post"])


# ---------------------------------------------------------------------- rendering

def post_url(uri):
    # at://did:plc:xyz/app.bsky.feed.post/rkey -> https://bsky.app/profile/did:plc:xyz/post/rkey
    did, _, rkey = uri.removeprefix("at://").split("/")
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def expand_links(record):
    """Replace display-truncated links in post text with their full URIs."""
    text = record.get("text", "")
    raw = text.encode()
    links = []
    for facet in record.get("facets") or []:
        for feat in facet.get("features", []):
            if feat.get("$type") == "app.bsky.richtext.facet#link":
                links.append((facet["index"]["byteStart"], facet["index"]["byteEnd"], feat["uri"]))
    for start, end, uri in sorted(links, reverse=True):
        shown = raw[start:end].decode(errors="replace")
        if shown.rstrip(".…") in uri:
            raw = raw[:start] + uri.encode() + raw[end:]
    return raw.decode(errors="replace")


def render_embed(embed, depth=0):
    """Plain-text/markdown lines for an embed view."""
    if not embed:
        return []
    kind = embed.get("$type", "")
    lines = []
    if kind.startswith("app.bsky.embed.images"):
        for img in embed.get("images", []):
            lines.append(f"![{img.get('alt', '')}]({img['fullsize']})")
    elif kind.startswith("app.bsky.embed.gallery"):
        for img in embed.get("items", []):
            if img.get("fullsize"):
                lines.append(f"![{img.get('alt', '')}]({img['fullsize']})")
    elif kind.startswith("app.bsky.embed.video"):
        if embed.get("thumbnail"):
            lines.append(f"![{embed.get('alt', 'video')}]({embed['thumbnail']})")
        lines.append("[video]")
    elif kind.startswith("app.bsky.embed.external"):
        ext = embed["external"]
        lines.append(f"🔗 {ext.get('title') or ext['uri']}\n{ext['uri']}")
    elif kind.startswith("app.bsky.embed.recordWithMedia"):
        lines += render_embed(embed.get("media"), depth)
        lines += render_embed(embed.get("record"), depth)
    elif kind.startswith("app.bsky.embed.record"):
        rec = embed.get("record", {})
        if rec.get("$type", "").endswith("#viewRecord") and depth == 0:
            who = rec["author"].get("displayName") or rec["author"]["handle"]
            quoted = expand_links(rec["value"]).replace("\n", "\n> ")
            quoted = f"> {quoted}\n" if quoted else ""
            lines.append(f"> {who} (@{rec['author']['handle']}):\n{quoted}> {post_url(rec['uri'])}")
            for e in rec.get("embeds") or []:
                lines += [f"> {l}" for l in render_embed(e, depth + 1)]
        elif rec.get("$type", "").endswith(("#viewNotFound", "#viewBlocked", "#viewDetached")):
            lines.append("> [quoted post unavailable]")
    return lines


def render_post(post):
    parts = [expand_links(post["record"])] + render_embed(post.get("embed"))
    return "\n\n".join(p for p in parts if p)


def author_meta(post):
    a = post["author"]
    return a.get("displayName") or a["handle"], a["handle"], a.get("avatar")


# ------------------------------------------------------------------------ readwise

def to_highlight(bookmark, posts):
    head = posts[0]
    name, handle, avatar = author_meta(head)
    text = "\n\n---\n\n".join(render_post(p) for p in posts)
    if len(text) > MAX_HIGHLIGHT_CHARS:
        text = text[: MAX_HIGHLIGHT_CHARS - 20] + "\n\n[…truncated]"
    url = post_url(head["uri"])
    # Like the Twitter integration: one "book" per author, each post a highlight.
    return {
        "text": text,
        "title": f"{name} on Bluesky",
        "author": f"@{handle}",
        "image_url": avatar,
        "source_url": f"https://bsky.app/profile/{head['author']['did']}",
        "source_type": SOURCE_TYPE,
        "category": "tweets",
        "highlighted_at": bookmark.get("createdAt") or head["record"].get("createdAt"),
        "highlight_url": url,
    }


def to_reader_doc(bookmark, posts):
    head = posts[0]
    name, handle, avatar = author_meta(head)
    body = []
    for p in posts:
        for block in render_post(p).split("\n\n"):
            body.append(f"<p>{html.escape(block).replace(chr(10), '<br>')}</p>")
        body.append(f'<p><a href="{post_url(p["uri"])}">View on Bluesky</a></p><hr>')
    first_line = expand_links(head["record"]).split("\n")[0][:80] or "post"
    return {
        "url": post_url(head["uri"]),
        "html": "\n".join(body),
        "should_clean_html": False,
        "title": f"{name}: {first_line}",
        "author": f"{name} (@{handle})",
        "image_url": avatar,
        "published_date": head["record"].get("createdAt"),
        "category": "tweet",
        "tags": ["bluesky"],
        "saved_using": "bsky-readwise",
    }


def readwise_headers(token):
    return {"Authorization": f"Token {token}"}


def push_highlights(token, highlights, batch=100):
    for i in range(0, len(highlights), batch):
        request("POST", READWISE_HIGHLIGHTS, headers=readwise_headers(token),
                body={"highlights": highlights[i : i + batch]})


def push_reader(token, doc):
    request("POST", READER_SAVE, headers=readwise_headers(token), body=doc)
    time.sleep(1.3)  # Reader save endpoint allows ~50 requests/minute


# --------------------------------------------------------------------------- main

# The state file holds keyed hashes of post URIs rather than the URIs, so it
# doesn't reveal what you bookmarked if it leaks (e.g. from a public repo's
# Actions cache). Changing the app password means re-sending everything once;
# Readwise de-duplicates.
def state_key(uri, secret):
    return hmac.new(secret.encode(), uri.encode(), hashlib.sha256).hexdigest()[:32]


def load_state(path):
    try:
        return set(json.loads(Path(path).read_text())["synced"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return set()


def save_state(path, synced):
    Path(path).write_text(json.dumps({"synced": sorted(synced)}, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", choices=["readwise", "reader", "both"], default="both",
                    help="readwise = highlights (like the Twitter sync); reader = Reader documents")
    ap.add_argument("--state", default="state.json", help="file tracking already-synced posts")
    ap.add_argument("--threads", action="store_true",
                    help="include the author's follow-up posts when a bookmark starts a thread")
    ap.add_argument("--full", action="store_true", help="walk every bookmark page, not just new ones")
    ap.add_argument("--dry-run", action="store_true", help="print payloads instead of sending")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log each post's URL (off by default so CI logs don't reveal your bookmarks)")
    args = ap.parse_args()
    log = print if args.verbose else (lambda *a, **k: None)

    env = os.environ
    missing = [k for k in ("BSKY_HANDLE", "BSKY_APP_PASSWORD") if not env.get(k)]
    if not args.dry_run and not env.get("READWISE_TOKEN"):
        missing.append("READWISE_TOKEN")
    if missing:
        sys.exit(f"missing env vars: {', '.join(missing)}")

    synced = load_state(args.state)
    key = lambda uri: state_key(uri, env["BSKY_APP_PASSWORD"])
    is_known = lambda uri: key(uri) in synced
    bsky = Bluesky(env["BSKY_HANDLE"], env["BSKY_APP_PASSWORD"], env.get("BSKY_SERVICE", "https://bsky.social"))

    new = [b for b in bsky.bookmarks(is_known, args.full) if not is_known(b["subject"]["uri"])]
    new.reverse()  # oldest first
    print(f"{len(new)} new bookmark(s)")

    highlights = []
    for b in new:
        item = b.get("item", {})
        if not item.get("$type", "").endswith("#postView"):
            log(f"  skip unavailable post {b['subject']['uri']}")
            synced.add(key(b["subject"]["uri"]))
            continue
        posts = [item] + (bsky.self_thread(item) if args.threads else [])
        if args.dry_run:
            if args.dest in ("readwise", "both"):
                print(json.dumps({"highlight": to_highlight(b, posts)}, indent=2))
            if args.dest in ("reader", "both"):
                print(json.dumps({"reader": to_reader_doc(b, posts)}, indent=2))
            continue
        if args.dest in ("reader", "both"):
            push_reader(env["READWISE_TOKEN"], to_reader_doc(b, posts))
        if args.dest in ("readwise", "both"):
            highlights.append(to_highlight(b, posts))
        synced.add(key(b["subject"]["uri"]))
        log(f"  {post_url(item['uri'])}")

    if highlights:
        push_highlights(env["READWISE_TOKEN"], highlights)
    if not args.dry_run:
        save_state(args.state, synced)


if __name__ == "__main__":
    main()
