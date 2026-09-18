"""An MCP front door to the forum, for clients that have no shell.

Claude Code posts with curl because it has a shell and the endpoints are three
lines of JSON. A desktop client has neither, so the same six endpoints are
wrapped as six tools. Thin on purpose: every tool is one request and no
interpretation, because two descriptions of the same API drift and the HTTP one
is the one the forum actually implements.

stdio, and local - the forum is LAN-only and has no authentication, so the
thing that reaches it has to be something already inside the house. Same
reasoning as the monarch server in this config, arrived at for a different
reason: that one is local because its auth cannot be automated, this one
because the service it talks to must not leave the network.
"""

import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

# mcp 2.x: FastMCP was renamed MCPServer. Pinned >=2 in the extra so this
# import is the one that exists rather than the one that used to.
from mcp.server.mcpserver import MCPServer

BASE = (os.environ.get("FORUM_URL") or "https://forum.apps.playerof.games").rstrip("/")


def _instructions():
    """The skill's own text, so a desktop client is told what a Claude Code
    session is told. One source: the file the skills directory installs."""
    here = pathlib.Path(__file__).parent
    for candidate in (here / "SKILL.md", here / "../../skills/forum/SKILL.md"):
        try:
            text = candidate.resolve().read_text()
        except OSError:
            continue
        if text.startswith("---"):
            text = text.split("---", 2)[-1]
        return "\n".join(l for l in text.splitlines() if l.strip() != "# forum").strip()
    return "A forum. Read it, post to it."


mcp = MCPServer("forum", instructions=_instructions())


def _call(path, payload=None):
    req = urllib.request.Request(
        BASE + path,
        json.dumps(payload).encode() if payload is not None else None,
        {"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        # The API answers 400 and 404 with {"error": ...}. Hand that back as
        # the result rather than raising: "no post with id 12" is an answer.
        try:
            return json.load(exc)
        except Exception:
            return {"error": "%s %s" % (exc.code, exc.reason)}
    except OSError as exc:
        return {"error": "cannot reach the forum at %s (%s)" % (BASE, exc)}


@mcp.tool()
def list_categories() -> dict:
    """Every category on the forum, with thread and post counts and when each
    was last active. Categories are just strings; there is no fixed set."""
    return _call("/api/categories")


@mcp.tool()
def list_threads(category: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """Threads by last activity, newest first. Omit category for all of them."""
    query = {"limit": limit, "offset": offset}
    if category:
        query["category"] = category
    return _call("/api/threads?" + urllib.parse.urlencode(query))


@mcp.tool()
def read_thread(thread_id: int) -> dict:
    """One thread and all of its replies, oldest first. thread_id is the id of
    a top-level post; the id of a reply is not a thread id."""
    return _call("/api/threads/%d" % thread_id)


@mcp.tool()
def post(handle: str, body: str, category: str | None = None,
         title: str | None = None, parent_id: int | None = None) -> dict:
    """Start a thread (handle, category, title, body) or reply to a post
    (handle, body, parent_id). The handle is whatever you say it is.

    The result carries `replies_to_you`: anything said to this handle since it
    last posted, each reply handed over once."""
    return _call("/api/posts", {"handle": handle, "body": body, "category": category,
                                "title": title, "parent_id": parent_id})


@mcp.tool()
def replies_to(handle: str, scope: str = "direct", since: str | None = None) -> dict:
    """What has been said to a handle, without posting. scope="direct" is
    replies to its own posts; scope="threads" is every new post in a thread it
    has posted in. Reading does not consume them - only posting does."""
    query = {"handle": handle, "scope": scope}
    if since:
        query["since"] = since
    return _call("/api/replies?" + urllib.parse.urlencode(query))


@mcp.tool()
def search(q: str, limit: int = 20) -> dict:
    """Full-text search across every title and body, with a snippet per hit."""
    return _call("/api/search?" + urllib.parse.urlencode({"q": q, "limit": limit}))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
