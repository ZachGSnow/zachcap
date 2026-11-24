"""SQLite storage: one file for meetings, transcripts, people and voiceprints."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from . import paths
from .voiceprint import Match, from_blob, to_blob

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration    REAL,
    folder      TEXT NOT NULL,
    -- recording|recorded|processing|needs_names|ready|failed
    status      TEXT NOT NULL DEFAULT 'recorded',
    source_app  TEXT,
    notes       TEXT,
    template    TEXT,                               -- the kind of meeting, for summaries
                                                    -- ('auto': one is to be picked)
    audio_removed_at TEXT,                          -- set when old audio was deleted
    ticket      TEXT,                               -- the service ticket it was about
    client      TEXT,                               -- set on it; else from its people
    client_source TEXT,                             -- 'user' (yours), 'auto' (kept as was)
    ticket_source TEXT,                             -- 'auto' (filled in), 'user' (yours)
    template_source TEXT,                           -- 'auto' (picked on its own); else chosen
    private     INTEGER NOT NULL DEFAULT 0,         -- 1: never sent to Claude (privacy.py)
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_me       INTEGER NOT NULL DEFAULT 0,
    org         TEXT,                               -- the client they work for
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meeting_speakers (
    id              INTEGER PRIMARY KEY,
    meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    track           TEXT NOT NULL,                  -- mic|system
    label           TEXT NOT NULL,                  -- "Speaker 1", shown until named
    person_id       INTEGER REFERENCES people(id) ON DELETE SET NULL,
    status          TEXT NOT NULL,                  -- auto|suggested|confirmed|unknown|minor
    match_score     REAL,
    embedding       BLOB,
    speech_seconds  REAL,
    sample_start    REAL,
    sample_end      REAL
);

CREATE TABLE IF NOT EXISTS segments (
    id                  INTEGER PRIMARY KEY,
    meeting_id          INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    meeting_speaker_id  INTEGER REFERENCES meeting_speakers(id) ON DELETE SET NULL,
    track               TEXT NOT NULL,
    start               REAL NOT NULL,
    end                 REAL NOT NULL,
    text                TEXT NOT NULL,
    confidence          REAL,
    edited              INTEGER NOT NULL DEFAULT 0  -- 1 once the user fixed its words
);
CREATE INDEX IF NOT EXISTS segments_by_meeting ON segments(meeting_id, start);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts
    USING fts5(text, content='segments', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS voice_samples (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    meeting_id  INTEGER,
    embedding   BLOB NOT NULL,
    model       TEXT NOT NULL,
    seconds     REAL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS summaries (
    meeting_id     INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    model          TEXT NOT NULL,
    content        TEXT NOT NULL,      -- JSON
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_items (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    owner       TEXT NOT NULL,
    task        TEXT NOT NULL,
    due         TEXT,
    said_at     REAL,                            -- seconds into the meeting
    kind        TEXT NOT NULL DEFAULT 'action',  -- commitment|action
    status      TEXT NOT NULL DEFAULT 'open',    -- open|done
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS action_items_by_status ON action_items(status, meeting_id);

CREATE TABLE IF NOT EXISTS meeting_notes (
    id          INTEGER PRIMARY KEY,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    at          REAL NOT NULL,                   -- seconds into the meeting
    kind        TEXT NOT NULL DEFAULT 'note',    -- note|bookmark|snapshot
    text        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS meeting_notes_by_meeting ON meeting_notes(meeting_id, at);

-- The user's kinds of meeting and one-click documents (see prompts.py). A built-in has a row
-- only once edited, holding just what was changed (NULL: the built-in's own text).
CREATE TABLE IF NOT EXISTS prompts (
    id            TEXT PRIMARY KEY,                -- a built-in's id ("customer") or custom-<hex>
    kind          TEXT NOT NULL,                   -- template|recipe
    label         TEXT,
    instructions  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT
);

-- Files seen in the watch folder (see watchfolder.py), by path, size and modified time, so
-- each version of a file is handled once and a meeting deleted here doesn't come back.
CREATE TABLE IF NOT EXISTS watched_files (
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    status      TEXT NOT NULL,                   -- imported|skipped|failed|waiting
    meeting_id  INTEGER,                         -- what it became (kept after a delete)
    error       TEXT,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (path, size, mtime_ns)
);

-- A counter bumped by any change to who said what (lines, voices, people), so numbers
-- worked out from them, like the meeting list's mention counts, are kept until it moves.
CREATE TABLE IF NOT EXISTS changes (name TEXT PRIMARY KEY, version INTEGER NOT NULL);
INSERT OR IGNORE INTO changes (name, version) VALUES ('lines', 0);
CREATE TRIGGER IF NOT EXISTS lines_changed_ai AFTER INSERT ON segments BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS lines_changed_au AFTER UPDATE ON segments BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS lines_changed_ad AFTER DELETE ON segments BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS voices_changed_ai AFTER INSERT ON meeting_speakers BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS voices_changed_au AFTER UPDATE ON meeting_speakers BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS voices_changed_ad AFTER DELETE ON meeting_speakers BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS people_changed_ai AFTER INSERT ON people BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS people_changed_au AFTER UPDATE ON people BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
CREATE TRIGGER IF NOT EXISTS people_changed_ad AFTER DELETE ON people BEGIN
    UPDATE changes SET version = version + 1 WHERE name = 'lines';
END;
"""

# What a redacted line says instead.
REDACTED = "[redacted]"
_by_lines: dict = {}  # (library, name) -> (lines version, value): see Store.by_lines
_by_lines_lock = threading.Lock()
LAST_MEETING_ID = "last_meeting_id"  # the highest meeting id ever given out
LAST_NOTE_ID = "last_note_id"  # the highest note id ever given out

# Suggestions show as "Speaker 2 (Dana?)" until confirmed.
SPEAKER_NAME = (
    "CASE WHEN ms.status = 'suggested' THEN ms.label || ' (' || p.name || '?)' "
    "ELSE COALESCE(p.name, ms.label, 'Unknown') END"
)


class Store:
    def __init__(self, path: Path | None = None, readonly: bool = False) -> None:
        # FastAPI may open a request's connection on one worker thread and use it on another;
        # each connection still serves one request at a time, so that is safe.
        path = path or paths.database()
        self.path = str(path)
        self._batch = 0
        if readonly:
            # For outside readers (the MCP server): SQLite itself refuses any write.
            self.db = sqlite3.connect(
                f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
            )
            self.db.row_factory = sqlite3.Row
            return
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Deleted rows are overwritten with zeros, not left readable in the file: deleting a
        # meeting, redacting a moment or replacing a summary leaves nothing behind.
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        if not self.db.execute("SELECT 1 FROM people WHERE is_me = 1").fetchone():
            self.db.execute("INSERT INTO people (name, is_me) VALUES ('You', 1)")
            self._commit()

    def _migrate(self) -> None:
        """Columns added after a database was created (CREATE TABLE IF NOT EXISTS skips them)."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(meetings)")}
        for column in (
            "template",
            "audio_removed_at",
            "ticket",
            "client",
            "ticket_source",
            "client_source",
            "template_source",
        ):
            if column not in columns:
                self.db.execute(f"ALTER TABLE meetings ADD COLUMN {column} TEXT")
                self.db.commit()
        if "private" not in columns:
            self.db.execute("ALTER TABLE meetings ADD COLUMN private INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(segments)")}
        if "edited" not in columns:
            self.db.execute("ALTER TABLE segments ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        # Notes from before their ids were counted: none of those ids is given out again,
        # whichever way the note goes (deleted, redacted, or with its meeting).
        if self.setting(LAST_NOTE_ID) is None:
            highest = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM meeting_notes").fetchone()
            self.set_setting(LAST_NOTE_ID, str(highest[0]))

    def close(self) -> None:
        self.db.close()

    def _commit(self) -> None:
        if not self._batch:
            self.db.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Everything inside is saved together, or not at all if it raises."""
        self._batch += 1
        try:
            yield
        except BaseException:
            self._batch -= 1
            if not self._batch:
                self.db.rollback()
            raise
        self._batch -= 1
        if not self._batch:
            self.db.commit()

    # settings ---------------------------------------------------------------

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._commit()

    def delete_setting(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        self._commit()

    # watch folder ---------------------------------------------------------------

    def watched_file(self, path: str, size: int, mtime_ns: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM watched_files WHERE path = ? AND size = ? AND mtime_ns = ?",
            (path, size, mtime_ns),
        ).fetchone()

    def record_watched_file(
        self,
        path: str,
        size: int,
        mtime_ns: int,
        status: str,
        meeting_id: int | None = None,
        error: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO watched_files (path, size, mtime_ns, status, meeting_id, error) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path, size, mtime_ns) DO UPDATE SET "
            "status = excluded.status, meeting_id = excluded.meeting_id, "
            "error = excluded.error, seen_at = datetime('now')",
            (path, size, mtime_ns, status, meeting_id, error),
        )
        self._commit()

    def forget_watched_files(self, paths: list[str], statuses: tuple[str, ...]) -> None:
        """Lets these files be looked at again, if they were in one of ``statuses``."""
        marks = ", ".join("?" * len(statuses))
        self.db.executemany(
            f"DELETE FROM watched_files WHERE path = ? AND status IN ({marks})",
            [(path, *statuses) for path in paths],
        )
        self._commit()

    def watched_failures(self, folder: str) -> list[sqlite3.Row]:
        """Files in this folder that couldn't be imported, newest first."""
        prefix = os.path.join(str(Path(folder)), "")  # with the trailing separator
        return self.db.execute(
            "SELECT path, size, mtime_ns, error, seen_at FROM watched_files "
            "WHERE status IN ('failed', 'waiting') "
            "AND substr(path, 1, length(?)) = ? ORDER BY seen_at DESC, rowid DESC",
            (prefix, prefix),
        ).fetchall()

    # prompts (kinds of meeting, one-click documents) --------------------------

    def prompt_rows(self, kind: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM prompts WHERE kind = ? ORDER BY created_at, rowid", (kind,)
        ).fetchall()

    def prompt_row(self, prompt_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()

    def save_prompt(
        self, prompt_id: str, kind: str, label: str | None, instructions: str | None
    ) -> None:
        self.db.execute(
            "INSERT INTO prompts (id, kind, label, instructions) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET label = excluded.label, "
            "instructions = excluded.instructions, updated_at = datetime('now')",
            (prompt_id, kind, label, instructions),
        )
        self._commit()

    def delete_prompt(self, prompt_id: str) -> None:
        self.db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
        self._commit()

    def forget_template(self, template: str) -> None:
        """Meetings summarized as a deleted kind of meeting go back to the usual kind."""
        self.db.execute(
            "UPDATE meetings SET template = NULL, template_source = NULL WHERE template = ?",
            (template,),
        )
        self._commit()

    # meetings ---------------------------------------------------------------

    def create_meeting(self, title: str, started_at: str, folder: Path, **fields) -> int:
        """A new meeting, with an id never used before: SQLite would give a deleted
        meeting's id to the next one, and old citations ([M12]) and export file names
        ("... (M12).md") would then point at the wrong meeting."""
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")  # the write lock first, so the id stays ours
        highest = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM meetings").fetchone()[0]
        meeting_id = max(highest, int(self.setting(LAST_MEETING_ID, "0") or 0)) + 1
        self.db.execute(
            "INSERT INTO meetings (id, title, started_at, folder, source_app, duration, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                meeting_id,
                title,
                started_at,
                str(folder),
                fields.get("source_app"),
                fields.get("duration"),
                fields.get("status", "recorded"),
            ),
        )
        self.set_setting(LAST_MEETING_ID, str(meeting_id))
        self._commit()
        return meeting_id

    @staticmethod
    def column(row: sqlite3.Row, name: str):
        """A column that newer versions added: None in a library this version hasn't opened
        for writing yet (the read-only MCP server doesn't add columns)."""
        return row[name] if name in row.keys() else None

    def fill_ticket(self, meeting_id: int, ticket: str) -> bool:
        """Fills in a ticket found in the meeting, only while it has none and you haven't set
        or taken one off. Returns whether it did."""
        written = self.db.execute(
            "UPDATE meetings SET ticket = ?, ticket_source = 'auto' "
            "WHERE id = ? AND ticket IS NULL AND ticket_source IS NULL",
            (ticket, meeting_id),
        ).rowcount
        self._commit()
        return bool(written)

    def drop_auto_ticket(self, meeting_id: int) -> None:
        """Takes off a ticket that was filled in on its own (never one you set)."""
        self.db.execute(
            "UPDATE meetings SET ticket = NULL, ticket_source = NULL "
            "WHERE id = ? AND ticket_source = 'auto'",
            (meeting_id,),
        )
        self._commit()

    def update_meeting(self, meeting_id: int, **fields) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(
            f"UPDATE meetings SET {columns} WHERE id = ?", (*fields.values(), meeting_id)
        )
        self._commit()

    def meeting(self, meeting_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()

    def meetings(self) -> list[sqlite3.Row]:
        """Every meeting except the one being recorded right now."""
        return self.db.execute(
            "SELECT * FROM meetings WHERE status != 'recording' ORDER BY started_at DESC"
        ).fetchall()

    def has_private(self) -> bool:
        """Whether meetings can be private here: not yet in a library from before they could
        be, which the read-only MCP server reads as it is."""
        columns = self.db.execute("PRAGMA table_info(meetings)")
        return any(row["name"] == "private" for row in columns)

    def private_meetings(self) -> set[int]:
        """The ids of the meetings that are never sent to Claude."""
        if not self.has_private():
            return set()
        rows = self.db.execute("SELECT id FROM meetings WHERE private = 1")
        return {row["id"] for row in rows}

    def meetings_with_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ", ".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM meetings WHERE status IN ({marks}) ORDER BY started_at", statuses
        ).fetchall()

    def meeting_folders(self) -> list[str]:
        return [row["folder"] for row in self.db.execute("SELECT folder FROM meetings")]

    def has_results(self, meeting_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM segments WHERE meeting_id = ? LIMIT 1", (meeting_id,)
            ).fetchone()
            is not None
        )

    def clear_results(self, meeting_id: int) -> None:
        """Forget a meeting's transcript and speakers before processing it again, and a
        client kept from those speakers: it follows the people found this time. A client
        you set stays."""
        self.db.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        self.db.execute("DELETE FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))
        self.db.execute(
            "UPDATE meetings SET client = NULL, client_source = NULL "
            "WHERE id = ? AND client_source = 'auto'",
            (meeting_id,),
        )
        self._commit()

    def refresh_status(self, meeting_id: int) -> str:
        open_speakers = self.db.execute(
            "SELECT COUNT(*) FROM meeting_speakers "
            "WHERE meeting_id = ? AND status IN ('suggested', 'unknown')",
            (meeting_id,),
        ).fetchone()[0]
        status = "needs_names" if open_speakers else "ready"
        self.update_meeting(meeting_id, status=status)
        return status

    # speakers and transcript ------------------------------------------------

    def add_speaker(
        self,
        meeting_id: int,
        track: str,
        label: str,
        embedding: np.ndarray | None,
        speech_seconds: float,
        sample: tuple[float, float] | None,
        match: Match,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO meeting_speakers (meeting_id, track, label, person_id, status, "
            "match_score, embedding, speech_seconds, sample_start, sample_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meeting_id,
                track,
                label,
                match.person_id,
                match.status,
                match.score,
                to_blob(embedding) if embedding is not None else None,
                speech_seconds,
                *(sample or (None, None)),
            ),
        )
        self._commit()
        return cur.lastrowid

    def speakers(self, meeting_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT ms.*, p.name AS person_name, {SPEAKER_NAME} AS display_name "
            "FROM meeting_speakers ms LEFT JOIN people p ON p.id = ms.person_id "
            "WHERE ms.meeting_id = ? ORDER BY ms.id",
            (meeting_id,),
        ).fetchall()

    def speaker(self, speaker_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM meeting_speakers WHERE id = ?", (speaker_id,)
        ).fetchone()

    def set_speaker_person(self, speaker_id: int, person_id: int | None, status: str) -> None:
        self.db.execute(
            "UPDATE meeting_speakers SET person_id = ?, status = ? WHERE id = ?",
            (person_id, status, speaker_id),
        )
        self._commit()

    def add_segments(self, meeting_id: int, rows: list[tuple]) -> None:
        """rows: (meeting_speaker_id, track, start, end, text, confidence)"""
        self.db.executemany(
            "INSERT INTO segments (meeting_id, meeting_speaker_id, track, start, end, text, "
            "confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(meeting_id, *row) for row in rows],
        )
        self._commit()

    def transcript(self, meeting_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT s.*, {SPEAKER_NAME} AS speaker, ms.status AS speaker_status "
            "FROM segments s "
            "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "LEFT JOIN people p ON p.id = ms.person_id "
            "WHERE s.meeting_id = ? ORDER BY s.start",
            (meeting_id,),
        ).fetchall()

    def search(self, query: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT s.meeting_id, m.title, s.start, {SPEAKER_NAME} AS speaker, "
            "snippet(segments_fts, 0, '[', ']', '...', 12) AS snippet "
            "FROM segments_fts f JOIN segments s ON s.id = f.rowid "
            "JOIN meetings m ON m.id = s.meeting_id "
            "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "LEFT JOIN people p ON p.id = ms.person_id "
            "WHERE segments_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()

    def search_lines(
        self, query: str, limit: int = 25, meeting_id: int | None = None, private: bool = True
    ) -> list[sqlite3.Row]:
        """Whole transcript lines matching a full-text query (in one meeting, or all), best
        matches first; with ``private`` off, only meetings Claude may read."""
        one = "AND s.meeting_id = ? " if meeting_id is not None else ""
        if not private and self.has_private():
            one += "AND m.private = 0 "
        return self.db.execute(
            f"SELECT s.meeting_id, m.title, m.started_at, s.start, {SPEAKER_NAME} AS speaker, "
            "s.text FROM segments_fts f JOIN segments s ON s.id = f.rowid "
            "JOIN meetings m ON m.id = s.meeting_id "
            "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "LEFT JOIN people p ON p.id = ms.person_id "
            f"WHERE segments_fts MATCH ? AND m.status != 'recording' {one}"
            "ORDER BY rank LIMIT ?",
            (query, *([meeting_id] if meeting_id is not None else []), limit),
        ).fetchall()

    # people and voiceprints -------------------------------------------------

    def me(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM people WHERE is_me = 1").fetchone()

    def people(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT p.*, COUNT(v.id) AS samples FROM people p "
            "LEFT JOIN voice_samples v ON v.person_id = p.id "
            "GROUP BY p.id ORDER BY p.is_me DESC, p.name"
        ).fetchall()

    def person(self, person_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()

    def person_id(self, name: str, create: bool = True) -> int | None:
        row = self.db.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        if not create:
            return None
        cur = self.db.execute("INSERT INTO people (name) VALUES (?)", (name,))
        self._commit()
        return cur.lastrowid

    def add_voice_sample(
        self, person_id: int, meeting_id: int, embedding: np.ndarray, model: str, seconds: float
    ) -> None:
        """Learn a voice. A sample of the same meeting speaker replaces any earlier one, so
        correcting a name moves the voiceprint to the right person instead of copying it."""
        if not np.linalg.norm(embedding) > 1e-6:  # empty (or NaN) embeddings match no one
            return
        blob = to_blob(embedding)
        self.db.execute(
            "DELETE FROM voice_samples WHERE meeting_id = ? AND embedding = ?", (meeting_id, blob)
        )
        self.db.execute(
            "INSERT INTO voice_samples (person_id, meeting_id, embedding, model, seconds) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, meeting_id, blob, model, seconds),
        )
        self._commit()

    def delete_orphan_voice_samples(self) -> int:
        """Forget samples learned from meetings that no longer exist."""
        cursor = self.db.execute(
            "DELETE FROM voice_samples WHERE meeting_id IS NOT NULL "
            "AND meeting_id NOT IN (SELECT id FROM meetings)"
        )
        self._commit()
        return cursor.rowcount

    def voice_library(self, model: str) -> dict[int, list[np.ndarray]]:
        library: dict[int, list[np.ndarray]] = {}
        for row in self.db.execute(
            "SELECT person_id, embedding FROM voice_samples WHERE model = ?", (model,)
        ):
            library.setdefault(row["person_id"], []).append(from_blob(row["embedding"]))
        return library

    # summaries --------------------------------------------------------------

    def save_summary(
        self,
        meeting_id: int,
        model: str,
        content: dict,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO summaries "
            "(meeting_id, model, content, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (meeting_id, model, json.dumps(content), input_tokens, output_tokens, cost_usd),
        )
        self._commit()

    def summary(self, meeting_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM summaries WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()
        if row is None:
            return None
        return {**dict(row), "content": json.loads(row["content"])}

    # action items -----------------------------------------------------------

    def replace_action_items(self, meeting_id: int, items: list[dict]) -> None:
        """Store a meeting's action items after a (re)summary without losing what was ticked
        off: a new item that matches a done one (the same task, perhaps reworded or now with
        a real name as owner) stays done, and done items the new summary left out are kept."""
        done = [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM action_items WHERE meeting_id = ? AND status = 'done'",
                (meeting_id,),
            )
        ]
        rows = []
        for item in items:
            match = _same_task(item["task"], done)
            if match is not None:
                done.remove(match)
            rows.append(
                (
                    meeting_id,
                    item["owner"],
                    item["task"],
                    item.get("due"),
                    item.get("said_at"),
                    item.get("kind", "action"),
                    "done" if match else "open",
                    match["done_at"] if match else None,
                )
            )
        rows += [
            (meeting_id, d["owner"], d["task"], d["due"], d["said_at"], d["kind"], "done",
             d["done_at"])
            for d in done
        ]  # fmt: skip
        self.db.execute("DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,))
        self.db.executemany(
            "INSERT INTO action_items (meeting_id, owner, task, due, said_at, kind, status, "
            "done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._commit()

    def action_items(
        self, status: str | None = None, meeting_id: int | None = None
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if status in ("open", "done"):
            clauses.append("a.status = ?")
            params.append(status)
        if meeting_id is not None:
            clauses.append("a.meeting_id = ?")
            params.append(meeting_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.execute(
            "SELECT a.*, m.title AS meeting_title, m.started_at AS meeting_started_at "
            f"FROM action_items a JOIN meetings m ON m.id = a.meeting_id {where} "
            "ORDER BY a.status = 'done', m.started_at DESC, a.id",
            params,
        ).fetchall()

    def set_action_status(self, item_id: int, status: str) -> int | None:
        """Mark an item open or done. Returns its meeting, or None if there's no such item."""
        self.db.execute(
            "UPDATE action_items SET status = ?, "
            "done_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE NULL END WHERE id = ?",
            (status, status, item_id),
        )
        self._commit()
        row = self.db.execute(
            "SELECT meeting_id FROM action_items WHERE id = ?", (item_id,)
        ).fetchone()
        return row["meeting_id"] if row else None

    # notes and bookmarks ----------------------------------------------------

    def add_note(self, meeting_id: int, at: float, kind: str = "note", text: str = "") -> int:
        """A note with an id never used before (as meetings): a snapshot's picture is found
        by its note's id, so a reused id could show a deleted picture."""
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")  # the write lock first, so the id stays ours
        highest = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM meeting_notes").fetchone()[0]
        note_id = max(highest, int(self.setting(LAST_NOTE_ID, "0") or 0)) + 1
        self.db.execute(
            "INSERT INTO meeting_notes (id, meeting_id, at, kind, text) VALUES (?, ?, ?, ?, ?)",
            (note_id, meeting_id, round(at, 2), kind, text),
        )
        self.set_setting(LAST_NOTE_ID, str(note_id))
        self._commit()
        return note_id

    def notes(self, meeting_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM meeting_notes WHERE meeting_id = ? ORDER BY at, id", (meeting_id,)
        ).fetchall()

    def note(self, note_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM meeting_notes WHERE id = ?", (note_id,)).fetchone()

    def update_note(self, note_id: int, text: str) -> None:
        self.db.execute("UPDATE meeting_notes SET text = ? WHERE id = ?", (text, note_id))
        self._commit()

    def delete_note(self, note_id: int) -> None:
        if note_id > int(self.setting(LAST_NOTE_ID, "0") or 0):  # never given out again
            self.set_setting(LAST_NOTE_ID, str(note_id))
        self.db.execute("DELETE FROM meeting_notes WHERE id = ?", (note_id,))
        self._commit()

    def lines_version(self) -> int | None:
        """Moves whenever a line, a voice or a person changes (see the changes table). None
        when it can't be told (a library opened read-only from before the counter)."""
        try:
            row = self.db.execute("SELECT version FROM changes WHERE name = 'lines'").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    # overviews and stats ----------------------------------------------------

    def meetings_between(self, first: str, last: str) -> list[sqlite3.Row]:
        """Meetings recorded from day ``first`` to day ``last`` (YYYY-MM-DD, the local date
        they started on), oldest first; not the one being recorded."""
        return self.db.execute(
            "SELECT * FROM meetings WHERE status != 'recording' "
            "AND substr(started_at, 1, 10) BETWEEN ? AND ? ORDER BY started_at",
            (first, last),
        ).fetchall()

    def participants(self, meeting_ids: list[int] | None = None) -> dict[int, list[str]]:
        """Named people per meeting, most talkative first (for some meetings, or all: worked
        out once per change to the lines, voices or people)."""
        if meeting_ids is None:
            return self.by_lines("participants", lambda: self._participants(None))
        return self._participants(meeting_ids)

    def _participants(self, meeting_ids: list[int] | None) -> dict[int, list[str]]:
        only = ""
        if meeting_ids is not None:
            if not meeting_ids:
                return {}
            only = f"AND s.meeting_id IN ({', '.join('?' * len(meeting_ids))}) "
        result: dict[int, list[str]] = {}
        for row in self.db.execute(
            "SELECT s.meeting_id, p.name, SUM(s.end - s.start) AS seconds FROM segments s "
            "JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "JOIN people p ON p.id = ms.person_id "
            f"WHERE ms.status IN ('auto', 'confirmed') {only}"
            "GROUP BY s.meeting_id, p.id ORDER BY s.meeting_id, seconds DESC",
            meeting_ids or (),
        ):
            result.setdefault(row["meeting_id"], []).append(row["name"])
        return result

    def by_lines(
        self,
        name: str,
        compute: Callable[[], dict[int, list[str]]],
        meeting_ids: Iterable[int] | None = None,
    ) -> dict[int, list[str]]:
        """A value per meeting worked out from the lines, voices and people (the meeting
        list's participants and clients), kept until one of them changes: for every meeting,
        or only the ones asked for. Each caller gets its own copy, so nothing done with it
        can change what's kept."""
        version = self.lines_version()
        key = (str(self.path), name)
        with _by_lines_lock:
            kept = _by_lines.get(key)
        if version is None or kept is None or kept[0] != version:
            kept = (version, compute())
            if version is not None:
                with _by_lines_lock:
                    _by_lines[key] = kept
        value = kept[1]
        wanted = value if meeting_ids is None else [m for m in meeting_ids if m in value]
        return {meeting_id: list(value[meeting_id]) for meeting_id in wanted}

    def meeting_clients(self, meeting_ids: Iterable[int] | None = None) -> dict[int, list[str]]:
        """The clients of the people named in each meeting (or in these), not you, most
        talkative first, one spelling each. Worked out once per change to the lines, voices
        or people, and kept with their case folded: they're written the way they are now,
        which a meeting's own client can change."""
        spelling = {name.casefold(): name for name in self.client_names()}
        return {
            meeting_id: [spelling.get(name, name) for name in names]
            for meeting_id, names in self.by_lines(
                "meeting_clients", self._meeting_clients, meeting_ids
            ).items()
        }

    def _meeting_clients(self) -> dict[int, list[str]]:
        seconds: dict[int, dict[str, float]] = {}
        for row in self.db.execute(
            "SELECT s.meeting_id, p.org, SUM(s.end - s.start) AS seconds FROM segments s "
            "JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "JOIN people p ON p.id = ms.person_id "
            "WHERE ms.status IN ('auto', 'confirmed') AND p.is_me = 0 "
            "AND COALESCE(p.org, '') != '' GROUP BY s.meeting_id, p.id"
        ):
            name = row["org"].casefold()  # compared in Python: SQLite folds only ASCII
            per = seconds.setdefault(row["meeting_id"], {})
            per[name] = per.get(name, 0.0) + (row["seconds"] or 0.0)
        return {
            meeting_id: sorted(per, key=lambda n: (-per[n], n))
            for meeting_id, per in seconds.items()
        }

    def client_names(self) -> list[str]:
        """Every client named on a person (not you) or a meeting, alphabetically: one
        spelling each, the one most used (an all-capitals one only when it's the only one)."""
        try:
            rows = self.db.execute(
                "SELECT org AS name FROM people WHERE is_me = 0 AND COALESCE(org, '') != '' "
                "UNION ALL SELECT client FROM meetings WHERE COALESCE(client, '') != ''"
            ).fetchall()
        except sqlite3.OperationalError:  # a library from before meetings had clients
            rows = self.db.execute(
                "SELECT org AS name FROM people WHERE is_me = 0 AND COALESCE(org, '') != ''"
            ).fetchall()
        used = Counter(row["name"] for row in rows)
        best: dict[str, str] = {}
        for name in sorted(used, key=lambda n: (-used[n], n.isupper(), n)):
            best.setdefault(name.casefold(), name)
        return sorted(best.values(), key=str.casefold)

    def set_client(self, person_id: int, client: str | None) -> None:
        self.db.execute("UPDATE people SET org = ? WHERE id = ?", (client or None, person_id))
        self._commit()

    def meetings_counting(self, person_id: int) -> list[sqlite3.Row]:
        """The meetings someone counts in for their client (named or recognized by voice,
        not only suggested) that follow their people: no client set or kept on them."""
        return self.db.execute(
            "SELECT * FROM meetings WHERE client_source IS NULL AND COALESCE(client, '') = '' "
            "AND id IN (SELECT meeting_id FROM meeting_speakers WHERE person_id = ? "
            "AND status IN ('auto', 'confirmed'))",
            (person_id,),
        ).fetchall()

    def keep_clients(self, kept: dict[int, str]) -> None:
        """Meetings that keep the client they have now (meeting id -> client), whatever their
        people do later; all of them or, if that fails, none. One set meanwhile wins."""
        with self.transaction():
            self.db.executemany(
                "UPDATE meetings SET client = ?, client_source = 'auto' "
                "WHERE id = ? AND client_source IS NULL",
                [(client, meeting_id) for meeting_id, client in kept.items()],
            )

    def rename_client(self, name: str, spelling: str) -> None:
        """A client's name written another way on every person and meeting that has it
        (matched ignoring case, in Python: SQLite folds only ASCII)."""
        wanted = name.casefold()
        people = [
            (spelling, row["id"])
            for row in self.db.execute("SELECT id, org FROM people WHERE org IS NOT NULL")
            if row["org"].casefold() == wanted
        ]
        meetings = [
            (spelling, row["id"])
            for row in self.db.execute("SELECT id, client FROM meetings WHERE client IS NOT NULL")
            if row["client"].casefold() == wanted
        ]
        with self.transaction():
            self.db.executemany("UPDATE people SET org = ? WHERE id = ?", people)
            self.db.executemany("UPDATE meetings SET client = ? WHERE id = ?", meetings)

    def talk_time(self, meeting_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT {SPEAKER_NAME} AS speaker, ms.person_id, "
            "SUM(s.end - s.start) AS seconds, COUNT(*) AS turns FROM segments s "
            "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "LEFT JOIN people p ON p.id = ms.person_id "
            "WHERE s.meeting_id = ? GROUP BY speaker ORDER BY seconds DESC",
            (meeting_id,),
        ).fetchall()

    def people_stats(self, private: bool = True) -> list[sqlite3.Row]:
        """Everyone, with what they said and when; with ``private`` off, as if private
        meetings weren't there (people heard only in those aren't either)."""
        heard, known = "ms.status IN ('auto', 'confirmed')", ""
        if not private and self.has_private():
            hidden = "(SELECT id FROM meetings WHERE private = 1)"
            heard += f" AND ms.meeting_id NOT IN {hidden}"
            in_any = "SELECT 1 FROM meeting_speakers ms WHERE ms.person_id = p.id"
            known = (
                f"WHERE p.is_me = 1 OR NOT EXISTS ({in_any} AND ms.meeting_id IN {hidden}) "
                f"OR EXISTS ({in_any} AND ms.meeting_id NOT IN {hidden}) "
            )
        return self.db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM voice_samples v WHERE v.person_id = p.id) AS samples, "
            "(SELECT COUNT(DISTINCT ms.meeting_id) FROM meeting_speakers ms "
            f" WHERE ms.person_id = p.id AND {heard}) AS meetings, "
            "(SELECT COALESCE(SUM(s.end - s.start), 0) FROM segments s "
            " JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            f" WHERE ms.person_id = p.id AND {heard}) AS talk_seconds, "
            "(SELECT MAX(m.started_at) FROM meetings m JOIN meeting_speakers ms "
            f" ON ms.meeting_id = m.id WHERE ms.person_id = p.id AND {heard}) AS last_seen "
            f"FROM people p {known}ORDER BY p.is_me DESC, talk_seconds DESC, p.name"
        ).fetchall()

    def stats(self) -> dict:
        week = "datetime('now', '-7 days')"
        one = self.db.execute(
            "SELECT COUNT(*) AS meetings, COALESCE(SUM(duration), 0) AS seconds, "
            f"COALESCE(SUM(CASE WHEN datetime(started_at) >= {week} THEN 1 END), 0) AS week, "
            f"COALESCE(SUM(CASE WHEN datetime(started_at) >= {week} THEN duration END), 0) "
            "AS week_seconds FROM meetings WHERE status != 'recording'"
        ).fetchone()
        open_items = self.db.execute(
            "SELECT COUNT(*) FROM action_items WHERE status = 'open'"
        ).fetchone()[0]
        # "Recognized by voice": people with a voiceprint, not every name typed in.
        people = self.db.execute(
            "SELECT COUNT(DISTINCT v.person_id) FROM voice_samples v "
            "JOIN people p ON p.id = v.person_id WHERE p.is_me = 0"
        ).fetchone()[0]
        return {**dict(one), "open_actions": open_items, "people": people, "by_day": self.by_day()}

    def by_day(self, days: int = 14, today: date | None = None) -> list[dict]:
        """Recorded meeting time per day for the last `days` days, oldest first. Days are
        the local dates the meetings were recorded on (the start of started_at)."""
        today = today or date.today()
        first = today - timedelta(days=days - 1)
        totals: dict[str, float] = {}
        for row in self.db.execute(
            "SELECT substr(started_at, 1, 10) AS day, SUM(COALESCE(duration, 0)) AS seconds "
            "FROM meetings WHERE status != 'recording' AND substr(started_at, 1, 10) >= ? "
            "GROUP BY day",
            (first.isoformat(),),
        ):
            totals[row["day"]] = row["seconds"]
        return [
            {"day": (first + timedelta(days=i)).isoformat(),
             "seconds": round(totals.get((first + timedelta(days=i)).isoformat(), 0.0), 1)}
            for i in range(days)
        ]  # fmt: skip

    # editing ----------------------------------------------------------------

    def rename_person(self, person_id: int, name: str) -> int:
        """Rename someone. If the name already belongs to another person, merge into them."""
        existing = self.person_id(name, create=False)
        if existing is None or existing == person_id:
            self.db.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
            self._commit()
            return person_id
        for table in ("meeting_speakers", "voice_samples"):
            self.db.execute(
                f"UPDATE {table} SET person_id = ? WHERE person_id = ?", (existing, person_id)
            )
        is_me = self.db.execute("SELECT is_me FROM people WHERE id = ?", (person_id,)).fetchone()
        if is_me and is_me["is_me"]:
            self.db.execute("UPDATE people SET is_me = 1 WHERE id = ?", (existing,))
        self.db.execute("DELETE FROM people WHERE id = ?", (person_id,))
        self._commit()
        return existing

    def person_meetings(self, person_id: int) -> list[int]:
        """The meetings someone was named in."""
        return [
            row["meeting_id"]
            for row in self.db.execute(
                "SELECT DISTINCT meeting_id FROM meeting_speakers WHERE person_id = ?",
                (person_id,),
            )
        ]

    def forget_voice(self, person_id: int) -> None:
        self.db.execute("DELETE FROM voice_samples WHERE person_id = ?", (person_id,))
        self._commit()

    def delete_person(self, person_id: int) -> None:
        self.db.execute(
            "UPDATE meeting_speakers SET person_id = NULL, status = 'unknown' WHERE person_id = ?",
            (person_id,),
        )
        self.db.execute("DELETE FROM people WHERE id = ? AND is_me = 0", (person_id,))
        self._commit()

    def delete_meeting(self, meeting_id: int) -> None:
        """Remove a meeting and everything learned from it, voice samples included."""
        # Its id is never given out again (a library from before ids were remembered).
        if meeting_id > int(self.setting(LAST_MEETING_ID, "0") or 0):
            self.set_setting(LAST_MEETING_ID, str(meeting_id))
        self.db.execute("DELETE FROM voice_samples WHERE meeting_id = ?", (meeting_id,))
        self.db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        self._commit()

    def reassign_segment(
        self, segment_id: int, name: str | None = None, speaker_id: int | None = None
    ) -> int:
        """Give one transcript line to another voice of the same meeting (speaker_id), even
        an unnamed one, or to a person by name (created if new). Returns the meeting."""
        segment = self.db.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
        if segment is None:
            raise ValueError("No such line")
        if speaker_id is not None:
            target = self.speaker(speaker_id)
            if target is None or target["meeting_id"] != segment["meeting_id"]:
                raise ValueError("No such speaker in this meeting")
        elif name:
            speaker_id = self._speaker_for_person(segment, name)
        else:
            raise ValueError("Say who said it")
        self.db.execute(
            "UPDATE segments SET meeting_speaker_id = ? WHERE id = ?", (speaker_id, segment_id)
        )
        self._commit()
        return segment["meeting_id"]

    def segment(self, segment_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()

    def edit_segment(self, segment_id: int, text: str) -> None:
        """The user's own wording for a line (the search index follows through its trigger).
        Reprocessing the audio replaces edited lines like any other."""
        self.db.execute("UPDATE segments SET text = ?, edited = 1 WHERE id = ?", (text, segment_id))
        self._commit()

    def redact_lines(self, meeting_id: int, start: float, end: float) -> tuple[int, int]:
        """Remove what was said between start and end (seconds): lines entirely inside are
        deleted, lines reaching into it become "[redacted]". Returns (deleted, replaced)."""
        deleted = self.db.execute(
            "DELETE FROM segments WHERE meeting_id = ? AND start >= ? AND end <= ?",
            (meeting_id, start, end),
        ).rowcount
        replaced = self.db.execute(
            "UPDATE segments SET text = ? WHERE meeting_id = ? AND start < ? AND end > ?",
            (REDACTED, meeting_id, end, start),
        ).rowcount
        self._commit()
        return deleted, replaced

    def set_speaker_sample(self, speaker_id: int, sample: tuple[float, float] | None) -> None:
        start, end = sample or (None, None)
        self.db.execute(
            "UPDATE meeting_speakers SET sample_start = ?, sample_end = ? WHERE id = ?",
            (start, end, speaker_id),
        )
        self._commit()

    def forget_derived(self, meeting_id: int, start: float, end: float) -> dict:
        """After a redaction: what was made from the words now gone. The summary and the
        action items (any part may repeat a password said in the stretch) are removed, to be
        summarized again, and so are notes and snapshots taken during the stretch. Returns
        what went: {"summary": bool, "items": n, "notes": [ids], "snapshots": [ids]}."""
        summary = self.db.execute(
            "DELETE FROM summaries WHERE meeting_id = ?", (meeting_id,)
        ).rowcount
        items = self.db.execute(
            "DELETE FROM action_items WHERE meeting_id = ?", (meeting_id,)
        ).rowcount
        notes = self.db.execute(
            "SELECT id, kind FROM meeting_notes WHERE meeting_id = ? AND at >= ? AND at <= ?",
            (meeting_id, start, end),
        ).fetchall()
        self.db.execute(
            "DELETE FROM meeting_notes WHERE meeting_id = ? AND at >= ? AND at <= ?",
            (meeting_id, start, end),
        )
        self._commit()
        return {
            "summary": bool(summary),
            "items": items,
            "notes": [n["id"] for n in notes],
            "snapshots": [n["id"] for n in notes if n["kind"] == "snapshot"],
        }

    def scrub_deleted_text(self, tries: int = 25, wait: float = 0.2) -> bool:
        """After a redaction or a delete: make removed text unrecoverable from the database
        files as far as SQLite can. The search index keeps deleted words until its pieces are
        merged, so merge them (freed space is overwritten), then empty the write-ahead log.
        A reader still in the log keeps it from emptying, so that is retried for a few
        seconds. Returns whether the log was emptied. The disk itself may still hold old
        copies: this is not a secure erase."""
        self.db.execute("INSERT INTO segments_fts(segments_fts) VALUES ('optimize')")
        self.db.commit()
        for attempt in range(tries):
            busy, _, _ = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if not busy:
                return True
            if attempt < tries - 1:
                time.sleep(wait)
        return False

    def _speaker_for_person(self, segment: sqlite3.Row, name: str) -> int:
        me = self.me()
        person = me["id"] if name.strip().lower() in {"you", "me"} else self.person_id(name)
        speaker = self.db.execute(
            "SELECT id FROM meeting_speakers WHERE meeting_id = ? AND person_id = ? "
            "AND status IN ('auto', 'confirmed') ORDER BY track = ? DESC LIMIT 1",
            (segment["meeting_id"], person, segment["track"]),
        ).fetchone()
        if speaker is not None:
            return speaker["id"]
        return self.add_speaker(
            segment["meeting_id"],
            segment["track"],
            name,
            None,
            segment["end"] - segment["start"],
            None,
            Match(person, 1.0, "confirmed"),
        )


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _same_task(task: str, candidates: list[dict], threshold: float = 0.8) -> dict | None:
    """The candidate describing the same task, allowing for rewording by a new summary."""
    wanted = _normalized(task)
    best, best_ratio = None, threshold
    for candidate in candidates:
        ratio = SequenceMatcher(None, wanted, _normalized(candidate["task"])).ratio()
        if ratio >= best_ratio:
            best, best_ratio = candidate, ratio
    return best
