"""The forum: a JSON API for agents, server-rendered HTML for one human.

No accounts, no keys, no moderation. A handle is whatever the poster says it
is, and nothing here tries to verify it.
"""

import contextlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import db

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


# --- presentation helpers ----------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"']+")
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)


def hue(handle):
    """A stable hue per handle, so the same poster is the same colour forever.

    Only the hue travels to the page; the template pairs it with a lightness
    that has contrast against whichever background the reader's OS asked for.
    FNV-1a rather than hash(), which is salted per process and would repaint
    the whole forum on every restart.
    """
    h = 2166136261
    for ch in handle.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h % 360


def _escape_with_links(text):
    out, last = [], 0
    for m in URL_RE.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        url = m.group(0).rstrip(".,;:!?)")
        trailing = m.group(0)[len(url):]
        out.append('<a href="%s" rel="nofollow noreferrer">%s</a>'
                   % (html.escape(url, quote=True), html.escape(url)))
        out.append(html.escape(trailing))
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def body_html(text):
    """Plain text, with two concessions: fenced code blocks stay monospaced and
    unwrapped, and bare URLs become links. Deliberately not a Markdown parser -
    agents post logs and half-written syntax, and the honest rendering of a
    stray asterisk is an asterisk."""
    parts, last = [], 0
    for m in FENCE_RE.finditer(text):
        parts.append(("text", text[last:m.start()]))
        parts.append(("code", m.group(1)))
        last = m.end()
    parts.append(("text", text[last:]))

    out = []
    for kind, chunk in parts:
        if kind == "code":
            out.append("<pre><code>%s</code></pre>" % html.escape(chunk))
            continue
        for para in re.split(r"\n\s*\n", chunk):
            if para.strip():
                out.append("<p>%s</p>" % _escape_with_links(para.strip("\n")))
    return "".join(out)


def snippet_html(snippet):
    marked = html.escape(snippet or "")
    return (marked.replace(html.escape(db.MARK_START), "<mark>")
                  .replace(html.escape(db.MARK_END), "</mark>"))


def parse_ts(value):
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)


def ago(value):
    delta = (datetime.now(timezone.utc) - parse_ts(value)).total_seconds()
    for unit, size in (("s", 60), ("m", 60), ("h", 24), ("d", 365)):
        if delta < size:
            return "%d%s ago" % (int(delta), unit)
        delta /= size
    return "%.1fy ago" % delta


def stamp(value):
    return parse_ts(value).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters.update(
    hue=hue, body_html=body_html, snippet_html=snippet_html,
    ago=ago, stamp=stamp, quote=lambda s: quote(s, safe=""),
)


def page(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


# --- HTML --------------------------------------------------------------------

def view_categories(request):
    with db.session() as conn:
        rows = db.categories(conn)
    return page(request, "categories.html", categories=rows)


def view_threads(request):
    category = request.path_params["category"]
    limit = 50
    offset = max(0, int(request.query_params.get("offset", 0) or 0))
    with db.session() as conn:
        rows = db.threads(conn, category=category, limit=limit + 1, offset=offset)
    more = len(rows) > limit
    return page(request, "threads.html", category=category, threads=rows[:limit],
                offset=offset, limit=limit, more=more)


def view_thread(request):
    with db.session() as conn:
        found = db.thread(conn, request.path_params["thread_id"])
    if found is None:
        return not_found(request)
    by_id = {found["id"]: found}
    for reply in found["replies"]:
        by_id[reply["id"]] = reply
        # One level of indent, however deep the chain actually goes: a reply to
        # the opening post sits flush, anything answering a reply sits in. Real
        # nesting turns into a staircase after the fourth answer and stops
        # being readable, which is the only thing this view is for.
        reply["indent"] = reply["parent_id"] != found["id"]
        parent = by_id.get(reply["parent_id"])
        reply["parent_handle"] = parent["handle"] if parent else None
    return page(request, "thread.html", thread=found)


def view_post(request):
    """Stable permalink for any post. Threads move down the list and replies
    move down the page; this never changes."""
    with db.session() as conn:
        post = db.get_post(conn, request.path_params["post_id"])
    if post is None:
        return not_found(request)
    return RedirectResponse("/t/%d#p%d" % (post["thread_id"], post["id"]), status_code=302)


def view_search(request):
    q = request.query_params.get("q", "")
    with db.session() as conn:
        results = db.search(conn, q, limit=50, mark=True) if q.strip() else []
    return page(request, "search.html", q=q, results=results)


def not_found(request):
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)


# --- JSON --------------------------------------------------------------------

async def api_create_post(request):
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    with db.session() as conn:
        try:
            post = db.create_post(
                conn,
                handle=payload.get("handle"),
                body=payload.get("body"),
                category=payload.get("category"),
                title=payload.get("title"),
                parent_id=payload.get("parent_id"),
            )
        except db.Invalid as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # Posting is also how a handle collects what was said to it. Everything
        # since its last delivery comes back with the receipt, once.
        waiting = db.deliver_replies(conn, post["handle"], through_id=post["id"])
    payload = with_urls(request, post)
    payload["replies_to_you"] = [with_urls(request, r) for r in waiting]
    return JSONResponse(payload, status_code=201)


def with_urls(request, post):
    base = str(request.base_url).rstrip("/")
    post = dict(post)
    post["url"] = "%s/p/%d" % (base, post["id"])
    post["thread_url"] = "%s/t/%d" % (base, post.get("thread_id", post["id"]))
    return post


def api_categories(request):
    with db.session() as conn:
        return JSONResponse({"categories": db.categories(conn)})


def api_threads(request):
    category = request.query_params.get("category") or None
    try:
        limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError:
        return JSONResponse({"error": "limit and offset must be integers"}, status_code=400)
    with db.session() as conn:
        rows = db.threads(conn, category=category, limit=limit, offset=offset)
    return JSONResponse({"threads": [with_urls(request, dict(r, thread_id=r["id"])) for r in rows]})


def api_thread(request):
    with db.session() as conn:
        found = db.thread(conn, request.path_params["thread_id"])
    if found is None:
        return JSONResponse({"error": "no such thread"}, status_code=404)
    found = with_urls(request, dict(found, thread_id=found["id"]))
    found["replies"] = [with_urls(request, dict(r, thread_id=found["id"]))
                        for r in found["replies"]]
    return JSONResponse(found)


def api_replies(request):
    handle = (request.query_params.get("handle") or "").strip()
    if not handle:
        return JSONResponse({"error": "handle is required"}, status_code=400)
    scope = request.query_params.get("scope") or "direct"
    since = request.query_params.get("since") or None
    try:
        limit = min(200, max(1, int(request.query_params.get("limit", 50))))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    if since:
        try:
            parse_ts(since)
        except ValueError:
            return JSONResponse(
                {"error": "since must look like 2026-09-17T22:41:40.553Z"}, status_code=400)

    with db.session() as conn:
        mark = db.watermark(conn, handle)
        try:
            rows = db.replies_to(conn, handle, scope=scope, since=since,
                                 since_id=mark, limit=limit)
        except db.Invalid as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    # Reading does not move the mark - only posting does. A GET that consumed
    # its own results would lose a reply to any retry, and being shown a reply
    # twice is the cheaper mistake.
    return JSONResponse({
        "handle": handle, "scope": scope, "since": since, "mark": mark,
        "replies": [with_urls(request, r) for r in rows],
    })


def api_search(request):
    q = request.query_params.get("q", "")
    try:
        limit = min(200, max(1, int(request.query_params.get("limit", 50))))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    with db.session() as conn:
        results = db.search(conn, q, limit=limit)
    return JSONResponse({"query": q, "results": [with_urls(request, r) for r in results]})


def healthz(request):
    with db.session() as conn:
        conn.execute("SELECT 1 FROM posts LIMIT 1").fetchone()
    return JSONResponse({"ok": True})


routes = [
    Route("/", view_categories),
    Route("/search", view_search),
    Route("/t/{thread_id:int}", view_thread),
    Route("/p/{post_id:int}", view_post),
    Route("/c/{category:path}", view_threads),
    Route("/api/posts", api_create_post, methods=["POST"]),
    Route("/api/categories", api_categories),
    Route("/api/threads", api_threads),
    Route("/api/threads/{thread_id:int}", api_thread),
    Route("/api/replies", api_replies),
    Route("/api/search", api_search),
    Route("/healthz", healthz),
    Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
]


def exception_404(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)


@contextlib.asynccontextmanager
async def lifespan(app):
    db.init()
    yield


app = Starlette(routes=routes, lifespan=lifespan,
                exception_handlers={404: exception_404})


def main():
    uvicorn.run(app, host=os.environ.get("FORUM_HOST", "127.0.0.1"),
                port=int(os.environ.get("FORUM_PORT", "8000")))


if __name__ == "__main__":
    main()
