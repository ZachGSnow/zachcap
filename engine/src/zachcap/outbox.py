"""What is to be sent to ZachCore, kept in the library itself.

Nothing is sent here. Triggers see every change to what a meeting's message is made of, and in
the same transaction raise the meeting's revision and, when sending is on, put the meeting in
the outbox. So no code path can forget it, and a change that is rolled back was never queued.

Ids: meetings, people and action items have a ``uid`` that is never given out again, in this
library or in one started over. The triggers use only what SQLite has built in, so an older
ZachCap that opens this library gives uids and fills the outbox too.

``sends`` has a row per meeting: its revision, and whether the server may have anything of it
(``target``, set before the first request and cleared once it was taken back). ``wanted`` is
set on meetings recorded while sending is on, or sent by hand. The outbox has at most one
message and one transcript waiting per meeting, and one recall. Each redaction is a row of its
own. Private meetings are never put in: one that becomes private, or is deleted, loses what
waits, and is taken back if anything of it was sent. That holds while sending is off too.

The triggers insert with an upsert, never with OR IGNORE: inside a trigger SQLite overrides
that with the conflict rule of the statement that fired it (INSERT OR REPLACE, for summaries).
"""

from __future__ import annotations

import sqlite3

ON = "zachcore"  # the setting: "1" while meetings are sent
TRANSCRIPTS = "zachcore_transcripts"  # "1": what was said as well
QUIET = 30  # seconds without another change before a meeting is sent

# A random UUID (version 4).
UID = (
    "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' "
    "|| substr(hex(randomblob(2)), 2) || '-' "
    "|| substr('89ab', 1 + (random() & 3), 1) || substr(hex(randomblob(2)), 2) "
    "|| '-' || hex(randomblob(6)))"
)

NOW = "CAST(strftime('%s', 'now') AS INTEGER)"

TABLES = """
CREATE TABLE IF NOT EXISTS sends (
    uid         TEXT PRIMARY KEY,                -- the meeting's; outlives it until taken back
    rev         INTEGER NOT NULL DEFAULT 1,      -- goes up with any change to what is sent
    wanted      INTEGER NOT NULL DEFAULT 0,      -- 1: recorded while sending was on, or by hand
    taken       INTEGER NOT NULL DEFAULT 0,      -- 1: you took it back; only Send now sends it
    target      TEXT,                            -- where it may be; NULL: nowhere
    sent_rev    INTEGER,                         -- the highest revision given to the server
    sent_hash   TEXT,                            -- of the message it took last
    lines_hash  TEXT,                            -- of the transcript it took last
    sent_at     TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,                   -- the meeting's
    what        TEXT NOT NULL,                   -- send|transcript|recall|redact
    from_sec    REAL,                            -- a redaction's stretch
    to_sec      REAL,
    state       TEXT NOT NULL DEFAULT 'waiting', -- waiting|sending|stopped
    tries       INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,                            -- why it waits or stopped; never a word of it
    quiet_until INTEGER NOT NULL DEFAULT 0,      -- seconds since 1970
    retry_at    INTEGER NOT NULL DEFAULT 0,
    rev         INTEGER,                         -- the revision its request went with
    hash        TEXT,                            -- of what that request said
    lines_before TEXT,                           -- a redaction's: the transcript's hash before
    lines_after TEXT                             -- ... and after it
);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_one ON outbox(uid, what) WHERE what <> 'redact';
-- Action items of a meeting that may be on the server, gone since. Never what they said: only
-- whose they were and when, so one that comes back in a later summary keeps its uid.
CREATE TABLE IF NOT EXISTS items_gone (
    uid         TEXT PRIMARY KEY,
    meeting_uid TEXT NOT NULL,
    owner       TEXT NOT NULL,
    said_at     REAL
);
CREATE INDEX IF NOT EXISTS items_gone_by_meeting ON items_gone(meeting_uid);
"""


def _put(what: str, uids: str, also: str = "") -> str:
    """Puts meetings in the outbox, or keeps the row that waits there from going for a while."""
    return f"""
    INSERT INTO outbox (uid, what, quiet_until)
        SELECT s.uid, '{what}', {NOW} + {QUIET} FROM sends s JOIN meetings m ON m.uid = s.uid
        WHERE s.uid IN ({uids}) AND s.wanted = 1 AND s.taken = 0 AND m.private = 0
        AND EXISTS (SELECT 1 FROM settings WHERE key = '{ON}' AND value = '1'){also}
        ON CONFLICT(uid, what) WHERE what <> 'redact' DO UPDATE SET
            quiet_until = excluded.quiet_until,
            reason = CASE WHEN state = 'stopped' THEN NULL ELSE reason END,
            tries = CASE WHEN state = 'stopped' THEN 0 ELSE tries END,
            state = 'waiting';"""


def _changed(uids: str, lines: bool = False) -> str:
    """What a change to some meetings does; with ``lines``, to who said what."""
    also = f" AND EXISTS (SELECT 1 FROM settings WHERE key = '{TRANSCRIPTS}' AND value = '1')"
    return (
        f"\n    UPDATE sends SET rev = rev + 1 WHERE uid IN ({uids});"
        + _put("send", uids)
        + (_put("transcript", uids, also) if lines else "")
    )


# Nothing more of it goes, and what was sent comes back.
_TAKE_BACK = f"""
    DELETE FROM outbox WHERE uid = old.uid AND what IN ('send', 'transcript');
    INSERT INTO outbox (uid, what, quiet_until)
        SELECT uid, 'recall', {NOW} FROM sends WHERE uid = old.uid AND target IS NOT NULL
        ON CONFLICT(uid, what) WHERE what <> 'redact' DO UPDATE SET quiet_until = quiet_until;"""

_OF = "SELECT uid FROM meetings WHERE id = {0}.meeting_id"
# Every meeting someone was heard in, as themselves or only suggested.
_WITH = (
    "SELECT m.uid FROM meetings m JOIN meeting_speakers ms ON ms.meeting_id = m.id "
    "WHERE ms.person_id = new.id"
)


def _trigger(name: str, when: str, body: str, condition: str = "") -> str:
    guard = f" WHEN {condition}" if condition else ""
    return f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {when}{guard} BEGIN{body}\nEND;\n"


def _differs(*columns: str) -> str:
    return " OR ".join(f"old.{c} IS NOT new.{c}" for c in columns)


def _watched(table: str, columns: tuple[str, ...], lines: bool = False, only: str = "") -> str:
    """The triggers of a table whose rows belong to a meeting (``only``: some of its rows)."""
    parts = []
    for verb, row in (("INSERT", "new"), ("DELETE", "old"), ("UPDATE", "new")):
        condition = only.format(row) if only else ""
        if verb == "UPDATE":
            differs = _differs(*columns)
            condition = f"({differs}) AND {condition}" if condition else differs
        of = f" OF {', '.join(columns)}" if verb == "UPDATE" else ""
        parts.append(
            _trigger(
                f"outbox_{table}_{verb.lower()}",
                f"{verb}{of} ON {table}",
                _changed(_OF.format(row), lines),
                condition,
            )
        )
    return "".join(parts)


_MEETING = ("title", "started_at", "duration", "source_app", "ticket", "client", "client_source")

TRIGGERS = (
    # Before anything else a new meeting does.
    _trigger(
        "outbox_meetings_insert",
        "INSERT ON meetings",
        f"""
    UPDATE meetings SET uid = {UID} WHERE id = new.id AND uid IS NULL;
    INSERT INTO sends (uid, wanted)
        SELECT uid, EXISTS (SELECT 1 FROM settings WHERE key = '{ON}' AND value = '1')
        FROM meetings WHERE id = new.id;"""
        + _put("send", "SELECT uid FROM meetings WHERE id = new.id"),
    )
    + _trigger(
        "outbox_meetings_update",
        f"UPDATE OF {', '.join(_MEETING)} ON meetings",
        _changed("new.uid"),
        _differs(*_MEETING),
    )
    + _trigger(
        "outbox_meetings_private",
        "UPDATE OF private ON meetings",
        "\n    UPDATE sends SET rev = rev + 1 WHERE uid = new.uid;" + _TAKE_BACK,
        "old.private = 0 AND new.private <> 0",
    )
    + _trigger(
        "outbox_meetings_public",
        "UPDATE OF private ON meetings",
        _changed("new.uid", lines=True),
        "old.private <> 0 AND new.private = 0",
    )
    # What cascades from it (its lines, its items) finds no meeting any more, and does nothing.
    + _trigger(
        "outbox_meetings_delete",
        "DELETE ON meetings",
        _TAKE_BACK
        + """
    DELETE FROM items_gone WHERE meeting_uid = old.uid;
    DELETE FROM sends WHERE uid = old.uid AND target IS NULL;""",
    )
    + _watched("meeting_speakers", ("person_id", "status", "label"), lines=True)
    + _watched("segments", ("meeting_speaker_id", "track", "start", "end", "text"), lines=True)
    + _watched("summaries", ("content",))
    + _watched("action_items", ("owner", "task", "due", "said_at", "status", "kind"))
    + _watched("meeting_notes", ("at", "kind", "text"), only="{0}.kind IN ('note', 'bookmark')")
    + _trigger(
        "outbox_people_update",
        "UPDATE OF name, org, is_me ON people",
        _changed(_WITH, lines=True),
        _differs("name", "org", "is_me"),
    )
    + _trigger(
        "uid_people",
        "INSERT ON people",
        f"\n    UPDATE people SET uid = {UID} WHERE id = new.id AND uid IS NULL;",
    )
    + _trigger(
        "uid_action_items",
        "INSERT ON action_items",
        f"""
    UPDATE action_items SET uid = {UID} WHERE id = new.id AND uid IS NULL;
    DELETE FROM items_gone WHERE uid = (SELECT uid FROM action_items WHERE id = new.id);""",
    )
    + _trigger(
        "gone_action_items",
        "DELETE ON action_items",
        f"""
    INSERT INTO items_gone (uid, meeting_uid, owner, said_at)
        SELECT old.uid, s.uid, old.owner, old.said_at FROM sends s
        WHERE s.uid = ({_OF.format("old")}) AND s.target IS NOT NULL
        ON CONFLICT(uid) DO UPDATE SET owner = excluded.owner, said_at = excluded.said_at;""",
        "old.uid IS NOT NULL",
    )
)


SENT = ("rev INTEGER", "hash TEXT", "lines_before TEXT", "lines_after TEXT")  # added later


def migrate(db: sqlite3.Connection) -> None:
    """Adds all of this to a library that doesn't have it, in one transaction: cut off, there's
    nothing of it, and the next start does it. Meetings already there aren't wanted."""
    if _has_uid(db):
        _add_sent(db)
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        if not _has_uid(db):  # another process may have been doing it
            for table in ("meetings", "people", "action_items"):
                db.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
                db.execute(f"UPDATE {table} SET uid = {UID}")
                db.execute(f"CREATE UNIQUE INDEX {table}_by_uid ON {table}(uid)")
            for statement in _statements(TABLES + TRIGGERS):
                db.execute(statement)
            db.execute("INSERT INTO sends (uid) SELECT uid FROM meetings")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _has_uid(db: sqlite3.Connection) -> bool:
    return any(row[1] == "uid" for row in db.execute("PRAGMA table_info(meetings)"))


def _add_sent(db: sqlite3.Connection) -> None:
    """The columns of a request the outbox has had since it first sent, in a library whose
    outbox is older."""
    if _has_sent(db):
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        if not _has_sent(db):  # another process may have added them meanwhile
            for column in SENT:
                db.execute(f"ALTER TABLE outbox ADD COLUMN {column}")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _has_sent(db: sqlite3.Connection) -> bool:
    return any(row[1] == "rev" for row in db.execute("PRAGMA table_info(outbox)"))


def _statements(script: str) -> list[str]:
    """A script's statements one by one: executescript would end the transaction first."""
    found, text = [], ""
    for line in script.splitlines(keepends=True):
        text += line
        if sqlite3.complete_statement(text):
            found.append(text)
            text = ""
    assert not text.strip(), text
    return found


def redacting(db: sqlite3.Connection, meeting_id: int, start: float, end: float) -> int | None:
    """Before start..end (seconds) leaves a meeting, in the same transaction: the server is to
    take it out as well, if it may have any of it. A line that begins before the stretch and
    reaches into it loses its words here, and the server finds lines by when they begin, so it
    is asked from there on. Returns the row, if there is one."""
    sent = db.execute(
        "SELECT s.uid FROM sends s JOIN meetings m ON m.uid = s.uid "
        "WHERE m.id = ? AND s.target IS NOT NULL",
        (meeting_id,),
    ).fetchone()
    if sent is None:
        return None
    first = db.execute(
        "SELECT MIN(start) FROM segments WHERE meeting_id = ? AND start < ? AND end > ?",
        (meeting_id, end, start),
    ).fetchone()[0]
    return db.execute(
        f"INSERT INTO outbox (uid, what, from_sec, to_sec, quiet_until) "
        f"VALUES (?, 'redact', ?, ?, {NOW})",
        (sent[0], min(start, first) if first is not None else start, end),
    ).lastrowid


def lines_around(db: sqlite3.Connection, row: int, before: str, after: str) -> None:
    """A redaction's transcript, as hashed before and after it: the server takes out only the
    stretch, so when it had the lines as they were, it has them as they are now once the
    redaction went, and they aren't sent again."""
    db.execute(
        "UPDATE outbox SET lines_before = ?, lines_after = ? WHERE id = ?", (before, after, row)
    )


def redacted(db: sqlite3.Connection, meeting_id: int, start: float, end: float) -> None:
    """After it: an action item said in the stretch, or nobody knows when, isn't one that a
    later summary brings back."""
    db.execute(
        "UPDATE items_gone SET said_at = NULL WHERE said_at >= ? AND said_at <= ? "
        "AND meeting_uid = (SELECT uid FROM meetings WHERE id = ?)",
        (start, end, meeting_id),
    )


# By hand (Settings and a meeting's menu) ------------------------------------------------

# A meeting put in the outbox now, whatever waits there for it: to go at once.
_NOW = f"""
    INSERT INTO outbox (uid, what, quiet_until) VALUES (?, ?, {NOW})
    ON CONFLICT(uid, what) WHERE what <> 'redact' DO UPDATE SET
        quiet_until = excluded.quiet_until, retry_at = 0, tries = 0, reason = NULL,
        state = 'waiting'"""
_RECALL = f"""
    INSERT INTO outbox (uid, what, quiet_until)
        SELECT uid, 'recall', {NOW} FROM sends WHERE uid = ? AND target IS NOT NULL
    ON CONFLICT(uid, what) WHERE what <> 'redact' DO UPDATE SET quiet_until = quiet_until"""


def _uid(db: sqlite3.Connection, meeting_id: int) -> str | None:
    row = db.execute("SELECT uid FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return row[0] if row else None


def _going(db: sqlite3.Connection, uid: str) -> None:
    """A meeting that goes now: its message, and its transcript when transcripts go."""
    db.execute(_NOW, (uid, "send"))
    if db.execute(f"SELECT 1 FROM settings WHERE key = '{TRANSCRIPTS}' AND value = '1'").fetchone():
        db.execute(_NOW, (uid, "transcript"))


def send_now(db: sqlite3.Connection, meeting_id: int) -> None:
    """Send now: a meeting goes at once, also one recorded before sending was turned on or
    taken back by hand, and goes on going with each change. Never a private one."""
    uid = _uid(db, meeting_id)
    if (
        uid is None
        or db.execute("SELECT private FROM meetings WHERE uid = ?", (uid,)).fetchone()[0]
    ):
        raise ValueError("A private meeting never goes to ZachCore")
    db.execute("UPDATE sends SET wanted = 1, taken = 0, rev = rev + 1 WHERE uid = ?", (uid,))
    _going(db, uid)


def take_back(db: sqlite3.Connection, uid: str) -> bool:
    """Take back: nothing more of a meeting goes until Send now, and what the server may have
    of it goes back out (never dropped). Returns whether anything of it may be there."""
    db.execute("UPDATE sends SET taken = 1, rev = rev + 1 WHERE uid = ?", (uid,))
    db.execute("DELETE FROM outbox WHERE uid = ? AND what IN ('send', 'transcript')", (uid,))
    return db.execute(_RECALL, (uid,)).rowcount > 0


def there(db: sqlite3.Connection) -> list[str]:
    """The meetings the server may have something of."""
    return [row[0] for row in db.execute("SELECT uid FROM sends WHERE target IS NOT NULL")]


def move(db: sqlite3.Connection, old: str, take_back_first: bool) -> int:
    """The meetings that went to ``old`` (a server or space other than now). Taken back from
    there first, and then sent to the new one as new; or left there as they are and sent to
    the new one as new (also after taking them back was asked for, and the old one doesn't
    answer any more). One that is private, deleted, taken back by hand or has a redaction to
    go keeps its old place either way: taking that back is never dropped. Returns how many
    will go to the new one."""
    moved = 0
    rows = db.execute(
        "SELECT s.uid, s.wanted, s.taken, m.private, "
        "EXISTS (SELECT 1 FROM outbox o WHERE o.uid = s.uid AND o.what = 'redact') "
        "AS redacting FROM sends s LEFT JOIN meetings m ON m.uid = s.uid WHERE s.target = ?",
        (old,),
    ).fetchall()
    for uid, wanted, taken, private, redacting in rows:
        if private is None or private or taken or not wanted or redacting:
            continue  # gone, private, taken back, not to be sent, or to be redacted there
        if take_back_first:
            db.execute(_RECALL, (uid,))  # its message waits for it, then goes to the new one
        else:  # a recall this asked for before is dropped: nothing is where it was now
            db.execute(
                "UPDATE sends SET target = NULL, sent_hash = NULL, lines_hash = NULL WHERE uid = ?",
                (uid,),
            )
        db.execute("UPDATE sends SET rev = rev + 1 WHERE uid = ?", (uid,))
        _going(db, uid)
        moved += 1
    return moved


def send_older(db: sqlite3.Connection, uids: list[str]) -> None:
    """Meetings from before sending was turned on, sent now as Send now does."""
    for uid in uids:
        db.execute("UPDATE sends SET wanted = 1, taken = 0, rev = rev + 1 WHERE uid = ?", (uid,))
        _going(db, uid)
