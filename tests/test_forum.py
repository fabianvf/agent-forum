import os
import tempfile

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    os.environ["FORUM_DB"] = os.path.join(tmp.name, "forum.db")
    from agent_forum.app import app
    with TestClient(app) as c:
        yield c
    tmp.cleanup()


def post(client, **payload):
    r = client.post("/api/posts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_thread_and_reply_round_trip(client):
    t = post(client, handle="rake", category="tools", title="on rakes", body="a rake is a tool")
    assert t["parent_id"] is None
    assert t["thread_id"] == t["id"]
    assert t["url"].endswith("/p/%d" % t["id"])

    r = post(client, handle="hoe", body="so is a hoe", parent_id=t["id"])
    assert r["title"] is None
    # A reply inherits the thread rather than carrying its own category.
    assert r["category"] is None
    assert r["thread_id"] == t["id"]

    got = client.get("/api/threads/%d" % t["id"]).json()
    assert got["title"] == "on rakes"
    assert [x["body"] for x in got["replies"]] == ["so is a hoe"]


def test_nested_reply_belongs_to_the_root_thread(client):
    t = post(client, handle="a", category="c", title="t", body="b")
    r1 = post(client, handle="b", body="one", parent_id=t["id"])
    r2 = post(client, handle="c", body="two", parent_id=r1["id"])
    assert r2["thread_id"] == t["id"]

    got = client.get("/api/threads/%d" % t["id"]).json()
    assert len(got["replies"]) == 2

    listed = client.get("/api/threads").json()["threads"]
    assert listed[0]["reply_count"] == 2
    assert listed[0]["last_handle"] == "c"


def test_a_reply_id_is_not_a_thread_id(client):
    t = post(client, handle="a", category="c", title="t", body="b")
    r = post(client, handle="b", body="reply", parent_id=t["id"])
    assert client.get("/api/threads/%d" % r["id"]).status_code == 404
    # Its permalink still resolves, by redirecting into the thread.
    page = client.get("/p/%d" % r["id"])
    assert page.status_code == 200
    assert 'id="p%d"' % r["id"] in page.text


def test_categories_count_threads_and_posts(client):
    t = post(client, handle="a", category="tools", title="one", body="x")
    post(client, handle="b", body="y", parent_id=t["id"])
    post(client, handle="c", category="birds", title="two", body="z")

    cats = {c["category"]: c for c in client.get("/api/categories").json()["categories"]}
    assert cats["tools"]["thread_count"] == 1
    assert cats["tools"]["post_count"] == 2
    assert cats["birds"]["post_count"] == 1


def test_threads_sort_by_last_activity(client):
    first = post(client, handle="a", category="c", title="first", body="x")
    post(client, handle="b", category="c", title="second", body="y")
    post(client, handle="c", body="bumped", parent_id=first["id"])

    titles = [t["title"] for t in client.get("/api/threads?category=c").json()["threads"]]
    assert titles == ["first", "second"]


def test_search_finds_body_and_title(client):
    t = post(client, handle="a", category="c", title="the rake question", body="nothing here")
    post(client, handle="b", body="a mattock is different", parent_id=t["id"])

    hits = client.get("/api/search?q=mattock").json()["results"]
    assert len(hits) == 1
    assert hits[0]["thread_id"] == t["id"]
    assert "mattock" in hits[0]["snippet"]

    assert len(client.get("/api/search?q=rake").json()["results"]) == 1


def test_search_survives_fts_syntax(client):
    post(client, handle="a", category="c", title="quotes", body='he said "no" to me')
    # Unbalanced quote: FTS5 would raise, and a reader typing it means it
    # literally.
    r = client.get('/api/search?q="no')
    assert r.status_code == 200
    assert len(r.json()["results"]) == 1


def test_rejects_incomplete_posts(client):
    assert client.post("/api/posts", json={"handle": "a", "body": "b"}).status_code == 400
    assert client.post("/api/posts", json={"handle": "", "category": "c",
                                           "title": "t", "body": "b"}).status_code == 400
    assert client.post("/api/posts", json={"handle": "a", "category": "c",
                                           "title": "t", "body": " "}).status_code == 400
    assert client.post("/api/posts", json={"handle": "a", "body": "b",
                                           "parent_id": 9999}).status_code == 400
    assert client.post("/api/posts", content=b"not json").status_code == 400


def test_html_views_render(client):
    t = post(client, handle="rake", category="tools & things",
             title="on <rakes>", body="see https://example.com/x\n\n```\ncode & stuff\n```")
    post(client, handle="hoe", body="agreed", parent_id=t["id"])

    index = client.get("/")
    assert index.status_code == 200
    assert "tools &amp; things" in index.text

    listing = client.get("/c/tools%20%26%20things")
    assert "on &lt;rakes&gt;" in listing.text

    page = client.get("/t/%d" % t["id"])
    assert '<a href="https://example.com/x"' in page.text
    assert "<pre><code>code &amp; stuff" in page.text
    assert "rake" in page.text and "hoe" in page.text

    assert client.get("/search?q=agreed").status_code == 200
    assert client.get("/t/999").status_code == 404
    assert client.get("/api/threads/999").status_code == 404
    assert client.get("/healthz").json() == {"ok": True}


def test_handle_colour_is_stable(client):
    from agent_forum.app import hue
    assert hue("rake") == hue("rake")
    assert 0 <= hue("anything") < 360


def test_posting_hands_back_replies_once(client):
    t = post(client, handle="rake", category="tools", title="t", body="b")
    post(client, handle="hoe", body="answering you", parent_id=t["id"])

    # The next thing rake posts carries the reply it has not seen.
    again = post(client, handle="rake", category="tools", title="t2", body="b2")
    assert [r["body"] for r in again["replies_to_you"]] == ["answering you"]
    assert again["replies_to_you"][0]["thread_id"] == t["id"]

    # And not a second time.
    third = post(client, handle="rake", category="tools", title="t3", body="b3")
    assert third["replies_to_you"] == []

    # Something new since then does come back.
    post(client, handle="hoe", body="and again", parent_id=t["id"])
    fourth = post(client, handle="rake", category="tools", title="t4", body="b4")
    assert [r["body"] for r in fourth["replies_to_you"]] == ["and again"]


def test_a_handles_first_post_carries_nothing(client):
    t = post(client, handle="rake", category="tools", title="t", body="b")
    post(client, handle="hoe", body="reply", parent_id=t["id"])
    first = post(client, handle="newcomer", category="tools", title="hello", body="hi")
    assert first["replies_to_you"] == []


def test_replies_endpoint_is_direct_by_default(client):
    t = post(client, handle="rake", category="tools", title="t", body="b")
    theirs = post(client, handle="hoe", body="to rake", parent_id=t["id"])
    post(client, handle="spade", body="to hoe", parent_id=theirs["id"])

    mine = client.get("/api/replies?handle=rake").json()
    # What was said to rake, not what was said to somebody else in rake's
    # thread.
    assert [r["body"] for r in mine["replies"]] == ["to rake"]
    # The mark sits at rake's own post: everything after it is unseen.
    assert mine["mark"] == t["id"]

    # Which is the other question, and the reason scope exists: a conversation
    # carrying on in your own thread without you.
    wider = client.get("/api/replies?handle=rake&scope=threads").json()
    assert [r["body"] for r in wider["replies"]] == ["to rake", "to hoe"]


def test_a_handle_never_gets_its_own_posts_back(client):
    t = post(client, handle="rake", category="tools", title="t", body="b")
    post(client, handle="rake", body="talking to myself", parent_id=t["id"])
    # Looking all the way back, so the mark is not what is doing the filtering.
    got = client.get("/api/replies?handle=rake&scope=threads"
                     "&since=2000-01-01T00:00:00Z").json()
    assert got["replies"] == []


def test_reading_replies_does_not_move_the_mark(client):
    t = post(client, handle="rake", category="tools", title="t", body="b")
    post(client, handle="hoe", body="answering you", parent_id=t["id"])

    assert len(client.get("/api/replies?handle=rake").json()["replies"]) == 1
    assert len(client.get("/api/replies?handle=rake").json()["replies"]) == 1
    # Posting is what consumes them.
    again = post(client, handle="rake", category="tools", title="t2", body="b2")
    assert len(again["replies_to_you"]) == 1
    assert client.get("/api/replies?handle=rake").json()["replies"] == []


def test_replies_endpoint_rejects_bad_input(client):
    assert client.get("/api/replies").status_code == 400
    assert client.get("/api/replies?handle=rake&scope=sideways").status_code == 400
    assert client.get("/api/replies?handle=rake&since=yesterday").status_code == 400
    assert client.get("/api/replies?handle=rake&since=2026-01-01T00:00:00Z").status_code == 200


def test_since_overrides_the_mark(client):
    t = post(client, handle="rake", category="tools", title="t", body="b")
    r = post(client, handle="hoe", body="answering you", parent_id=t["id"])
    post(client, handle="rake", category="tools", title="t2", body="b2")  # consumes it

    back = client.get("/api/replies?handle=rake&since=2000-01-01T00:00:00Z").json()
    assert [x["id"] for x in back["replies"]] == [r["id"]]


def test_recent_shows_threads_and_replies_newest_first(client):
    t = post(client, handle="rake", category="tools", title="a thread", body="opening")
    post(client, handle="hoe", body="a reply, with a  lot   of whitespace", parent_id=t["id"])

    page = client.get("/recent")
    assert page.status_code == 200
    # The reply is newest, so it leads, and it borrows its thread's title.
    assert page.text.index("a reply, with a lot of whitespace") < page.text.index("opening")
    assert "a thread" in page.text
    assert page.text.count(">reply<") == 1


def test_excerpt_collapses_and_truncates():
    from agent_forum.app import excerpt
    assert excerpt("a  b\n\nc") == "a b c"
    long = "word " * 200
    assert len(excerpt(long)) <= 262 and excerpt(long).endswith("…")


def test_mcp_server_wraps_the_api_and_carries_the_skill_text():
    # Skipped where the optional extra is not installed, which is how CI runs
    # (`uv sync` with no extras) and how the container is built.
    pytest.importorskip("mcp")
    import asyncio
    from agent_forum import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"list_categories", "list_threads", "read_thread",
            "start_thread", "reply"} <= names
    # Posting is two tools, not one with five optionals: a model that forgets
    # which fields pair gets a 400 the user reads as "the thing is broken".
    assert "post" not in names
    # The desktop client is told what a Claude Code session is told, from the
    # same file. If this drifts, two clients are being invited differently.
    assert "Pick a handle that reflects what you are" in mcp_server.mcp.instructions
    assert "POST /api/posts" in mcp_server.mcp.instructions


def test_stylesheet_is_linked_relatively(client):
    # Regression: this was url_for(), which builds an absolute URL from the
    # scheme the app thinks it is serving. Behind TLS termination that is
    # http://, the browser drops it as mixed content, and the forum serves
    # unstyled HTML while every status check still says 200.
    body = client.get("/").text
    assert '<link rel="stylesheet" href="/static/style.css">' in body
    assert "http://" not in body
