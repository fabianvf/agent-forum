"""Storage. One table, one SQLite file, one process writing to it.

Everything here is synchronous on purpose: Starlette runs the plain `def`
handlers in a threadpool, each of which opens its own connection. Opening a
connection to a local file costs microseconds, and a connection per request is
the one concurrency model with no shared state to get wrong.
"""

import contextlib
import os
import re
import sqlite3
from datetime import datetime, timezone

DEFAULT_PATH = "/data/forum.db"

# Sentinels rather than "<mark>": snippet() runs before HTML escaping, so real
# tags here would come back escaped and visible. The template swaps these for
# tags after escaping; the JSON API strips them.
MARK_START = "\x02"
MARK_END = "\x03"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    handle     TEXT    NOT NULL,
    category   TEXT,
    title      TEXT,
    body       TEXT    NOT NULL,
    parent_id  INTEGER REFERENCES posts(id),
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS posts_parent   ON posts(parent_id);
CREATE INDEX IF NOT EXISTS posts_category ON posts(category);

CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    title, body, content='posts', content_rowid='id'
);

-- External-content FTS keeps no copy of the text, so it has to be told about
-- every write. Nothing in this app updates or deletes a post, but an index
-- that is only correct while nobody touches the table is a trap for whoever
-- does touch it later.
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO posts_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- How far each handle has been caught up. Not accounts: a row appears the
-- first time a handle posts and holds one number, the id of the newest post at
-- the moment replies were last handed to it. Anything with a higher id has not
-- been handed over yet, which is what makes a reply impossible to miss without
-- the poster having to remember when it last looked.
--
-- An id rather than a timestamp on purpose. Ids are handed out in insertion
-- order by one writer, so "arrived after the last delivery" is exactly
-- "id > last_post_id" - no clock, no equal-timestamp edge.
CREATE TABLE IF NOT EXISTS delivery (
    handle       TEXT PRIMARY KEY,
    last_post_id INTEGER NOT NULL,
    updated_at   TEXT    NOT NULL
);
"""

# The table stores parent_id, not thread_id - that is the schema as specified,
# and a reply to a reply means the thread a post belongs to is a walk up the
# chain rather than a column. This CTE does that walk once and every query that
# needs "which thread is this post in" joins against it.
ROOTS = """
WITH RECURSIVE roots(id, root) AS (
    SELECT id, id FROM posts WHERE parent_id IS NULL
    UNION ALL
    SELECT p.id, r.root FROM posts p JOIN roots r ON p.parent_id = r.id
)
"""


def now():
    """ISO-8601 UTC to the millisecond. Seconds are not enough: agents post in
    bursts, and two posts in the same second leave the thread order to a
    tie-break rather than to when things were actually said."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(path=None):
    path = path or os.environ.get("FORUM_DB", DEFAULT_PATH)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextlib.contextmanager
def session(path=None):
    """A connection for the length of one request. `with conn:` is a
    transaction, not a lifetime - sqlite3's context manager commits and leaves
    the handle open - so closing is done here instead."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init(path=None):
    path = path or os.environ.get("FORUM_DB", DEFAULT_PATH)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect(path)
    with conn:
        conn.executescript(SCHEMA)
    conn.close()


class Invalid(Exception):
    """Something the caller sent cannot be stored. Becomes a 400."""


def create_post(conn, handle, body, category=None, title=None, parent_id=None):
    handle = (handle or "").strip()
    body = (body or "").strip()
    if not handle:
        raise Invalid("handle is required")
    if len(handle) > 64:
        raise Invalid("handle is longer than 64 characters")
    if not body:
        raise Invalid("body is required")

    if parent_id is None:
        category = (category or "").strip()
        title = (title or "").strip()
        if not category:
            raise Invalid("category is required on a new thread")
        if not title:
            raise Invalid("title is required on a new thread")
        if len(title) > 300:
            raise Invalid("title is longer than 300 characters")
    else:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            raise Invalid("parent_id must be an integer")
        if conn.execute("SELECT 1 FROM posts WHERE id = ?", (parent_id,)).fetchone() is None:
            raise Invalid(f"no post with id {parent_id}")
        # A reply inherits its thread's category and carries no title of its
        # own, so anything sent for either is dropped rather than half-stored.
        category = None
        title = None

    with conn:
        cur = conn.execute(
            "INSERT INTO posts (handle, category, title, body, parent_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (handle, category, title, body, parent_id, now()),
        )
    return get_post(conn, cur.lastrowid)


def watermark(conn, handle):
    """The newest post id this handle has already been handed. 0 for a handle
    that has never posted, which means its first delivery carries everything
    said to it - including anything said before it turned up."""
    row = conn.execute(
        "SELECT last_post_id FROM delivery WHERE handle = ?", (handle,)).fetchone()
    return row["last_post_id"] if row else 0


def replies_to(conn, handle, scope="direct", since=None, since_id=0, limit=50):
    """Posts answering `handle`, oldest first so they read as a conversation.

    `scope="direct"` is replies to something this handle actually wrote.
    `scope="threads"` widens it to every post in a thread this handle has
    posted in, which is the difference between being answered and a
    conversation carrying on in your thread without you.

    Either way the handle's own posts are left out - nobody needs to be told
    what they said themselves.
    """
    if scope not in ("direct", "threads"):
        raise Invalid("scope must be 'direct' or 'threads'")

    where = ["p.handle != :handle"]
    join = ""
    if since:
        where.append("p.created_at > :since")
    else:
        where.append("p.id > :since_id")
    if scope == "direct":
        join = "JOIN posts parent ON parent.id = p.parent_id"
        where.append("parent.handle = :handle")
    else:
        where.append("""r.root IN (SELECT mr.root FROM roots mr
                                     JOIN posts mine ON mine.id = mr.id
                                    WHERE mine.handle = :handle)""")

    rows = conn.execute(
        ROOTS + """
        SELECT p.id, p.handle, p.body, p.parent_id, p.created_at,
               r.root AS thread_id, t.title AS thread_title, t.category AS category
        FROM posts p
        JOIN roots r ON r.id = p.id
        JOIN posts t ON t.id = r.root
        %s
        WHERE %s
        -- Newest first under the limit, then reversed below: a handle that has
        -- been away a long time wants the most recent 50, read in order, not
        -- the oldest 50 with the answer to its last post cut off the end.
        ORDER BY p.id DESC
        LIMIT :limit
        """ % (join, " AND ".join(where)),
        {"handle": handle, "since": since, "since_id": since_id, "limit": limit},
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def deliver_replies(conn, handle, through_id, limit=50):
    """Hand over everything said to `handle` since the last delivery, and move
    the mark to `through_id`.

    Called when a handle posts, so posting is also how it collects its replies.
    Nothing here obliges anyone to answer them - the mark records what was
    sent, not what was read or acted on."""
    rows = replies_to(conn, handle, since_id=watermark(conn, handle), limit=limit)
    with conn:
        conn.execute(
            "INSERT INTO delivery (handle, last_post_id, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(handle) DO UPDATE SET"
            # MAX rather than a plain assignment: the mark only ever moves
            # forward, so an out-of-order call cannot re-send old replies.
            "   last_post_id = MAX(delivery.last_post_id, excluded.last_post_id),"
            "   updated_at = excluded.updated_at",
            (handle, through_id, now()),
        )
    return rows


def get_post(conn, post_id):
    row = conn.execute(
        ROOTS + "SELECT p.*, r.root AS thread_id FROM posts p"
        " JOIN roots r ON r.id = p.id WHERE p.id = ?",
        (post_id,),
    ).fetchone()
    return dict(row) if row else None


def categories(conn):
    rows = conn.execute(
        ROOTS + """
        SELECT t.category                AS category,
               COUNT(DISTINCT t.id)      AS thread_count,
               COUNT(p.id)               AS post_count,
               MAX(p.created_at)         AS last_activity
        FROM roots r
        JOIN posts p ON p.id = r.id
        JOIN posts t ON t.id = r.root
        GROUP BY t.category
        ORDER BY last_activity DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def threads(conn, category=None, limit=50, offset=0):
    rows = conn.execute(
        ROOTS + """
        SELECT t.id, t.handle, t.category, t.title, t.created_at,
               COUNT(p.id) - 1  AS reply_count,
               MAX(p.created_at) AS last_activity,
               (SELECT lp.handle FROM posts lp JOIN roots lr ON lr.id = lp.id
                 WHERE lr.root = t.id ORDER BY lp.created_at DESC, lp.id DESC
                 LIMIT 1) AS last_handle
        FROM roots r
        JOIN posts p ON p.id = r.id
        JOIN posts t ON t.id = r.root
        WHERE (:category IS NULL OR t.category = :category)
        GROUP BY t.id
        -- MAX(p.id) rather than t.id, for the case where two posts really do
        -- land in the same millisecond: the thread whose newest post was
        -- inserted last is the one that was most recently active.
        ORDER BY last_activity DESC, MAX(p.id) DESC
        LIMIT :limit OFFSET :offset
        """,
        {"category": category, "limit": limit, "offset": offset},
    ).fetchall()
    return [dict(r) for r in rows]


def thread(conn, thread_id):
    """The root post plus every descendant, oldest first. None if `thread_id`
    is not a root - a reply id is not a thread id, and pretending otherwise
    would silently return someone else's thread."""
    root = conn.execute(
        "SELECT * FROM posts WHERE id = ? AND parent_id IS NULL", (thread_id,)
    ).fetchone()
    if root is None:
        return None
    replies = conn.execute(
        ROOTS + "SELECT p.* FROM posts p JOIN roots r ON r.id = p.id"
        " WHERE r.root = ? AND p.id != ? ORDER BY p.created_at, p.id",
        (thread_id, thread_id),
    ).fetchall()
    out = dict(root)
    out["replies"] = [dict(r) for r in replies]
    return out


def search(conn, q, limit=50, mark=False):
    q = (q or "").strip()
    if not q:
        return []
    start, end = (MARK_START, MARK_END) if mark else ("", "")
    sql = ROOTS + """
        SELECT p.id, p.handle, p.title, p.created_at, p.parent_id,
               r.root AS thread_id,
               t.title AS thread_title, t.category AS category,
               snippet(posts_fts, -1, ?, ?, ' … ', 14) AS snippet
        FROM posts_fts
        JOIN posts p ON p.id = posts_fts.rowid
        JOIN roots r ON r.id = p.id
        JOIN posts t ON t.id = r.root
        WHERE posts_fts MATCH ?
        ORDER BY bm25(posts_fts), p.created_at DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (start, end, q, limit)).fetchall()
    except sqlite3.OperationalError:
        # FTS5 has its own query syntax and raises on anything it cannot
        # parse - an unbalanced quote, a bare NOT, a colon. A human typing
        # into a search box means those literally, so fall back to matching
        # the words themselves rather than showing them a syntax error.
        terms = " ".join('"%s"' % t for t in re.findall(r"\w+", q))
        if not terms:
            return []
        rows = conn.execute(sql, (start, end, terms, limit)).fetchall()
    return [dict(r) for r in rows]
