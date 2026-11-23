"""SQLite storage: one file for meetings, transcripts, people and voiceprints."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_me       INTEGER NOT NULL DEFAULT 0,
    org         TEXT,
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
    confidence          REAL
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
"""

# Suggestions show as "Speaker 2 (Dana?)" until confirmed.
SPEAKER_NAME = (
    "CASE WHEN ms.status = 'suggested' THEN ms.label || ' (' || p.name || '?)' "
    "ELSE COALESCE(p.name, ms.label, 'Unknown') END"
)


class Store:
    def __init__(self, path: Path | None = None) -> None:
        # FastAPI may open a request's connection on one worker thread and use it on another;
        # each connection still serves one request at a time, so that is safe.
        self.db = sqlite3.connect(path or paths.database(), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._batch = 0
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM people WHERE is_me = 1").fetchone():
            self.db.execute("INSERT INTO people (name, is_me) VALUES ('You', 1)")
            self._commit()

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

    # meetings ---------------------------------------------------------------

    def create_meeting(self, title: str, started_at: str, folder: Path, **fields) -> int:
        cur = self.db.execute(
            "INSERT INTO meetings (title, started_at, folder, source_app, duration, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                title,
                started_at,
                str(folder),
                fields.get("source_app"),
                fields.get("duration"),
                fields.get("status", "recorded"),
            ),
        )
        self._commit()
        return cur.lastrowid

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
        """Forget a meeting's transcript and speakers before processing it again."""
        self.db.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
        self.db.execute("DELETE FROM meeting_speakers WHERE meeting_id = ?", (meeting_id,))
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

    # people and voiceprints -------------------------------------------------

    def me(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM people WHERE is_me = 1").fetchone()

    def people(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT p.*, COUNT(v.id) AS samples FROM people p "
            "LEFT JOIN voice_samples v ON v.person_id = p.id "
            "GROUP BY p.id ORDER BY p.is_me DESC, p.name"
        ).fetchall()

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

    def set_action_status(self, item_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE action_items SET status = ?, "
            "done_at = CASE WHEN ? = 'done' THEN datetime('now') ELSE NULL END WHERE id = ?",
            (status, status, item_id),
        )
        self._commit()

    # overviews and stats ----------------------------------------------------

    def participants(self) -> dict[int, list[str]]:
        """Named people per meeting, most talkative first."""
        result: dict[int, list[str]] = {}
        for row in self.db.execute(
            "SELECT s.meeting_id, p.name, SUM(s.end - s.start) AS seconds FROM segments s "
            "JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "JOIN people p ON p.id = ms.person_id "
            "WHERE ms.status IN ('auto', 'confirmed') "
            "GROUP BY s.meeting_id, p.id ORDER BY s.meeting_id, seconds DESC"
        ):
            result.setdefault(row["meeting_id"], []).append(row["name"])
        return result

    def talk_time(self, meeting_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            f"SELECT {SPEAKER_NAME} AS speaker, ms.person_id, "
            "SUM(s.end - s.start) AS seconds, COUNT(*) AS turns FROM segments s "
            "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "LEFT JOIN people p ON p.id = ms.person_id "
            "WHERE s.meeting_id = ? GROUP BY speaker ORDER BY seconds DESC",
            (meeting_id,),
        ).fetchall()

    def people_stats(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM voice_samples v WHERE v.person_id = p.id) AS samples, "
            "(SELECT COUNT(DISTINCT ms.meeting_id) FROM meeting_speakers ms "
            " WHERE ms.person_id = p.id AND ms.status IN ('auto', 'confirmed')) AS meetings, "
            "(SELECT COALESCE(SUM(s.end - s.start), 0) FROM segments s "
            " JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            " WHERE ms.person_id = p.id AND ms.status IN ('auto', 'confirmed')) AS talk_seconds, "
            "(SELECT MAX(m.started_at) FROM meetings m JOIN meeting_speakers ms "
            " ON ms.meeting_id = m.id WHERE ms.person_id = p.id "
            " AND ms.status IN ('auto', 'confirmed')) AS last_seen "
            "FROM people p ORDER BY p.is_me DESC, talk_seconds DESC, p.name"
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
        people = self.db.execute("SELECT COUNT(*) FROM people WHERE is_me = 0").fetchone()[0]
        return {**dict(one), "open_actions": open_items, "people": people}

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
        self.db.execute("DELETE FROM voice_samples WHERE meeting_id = ?", (meeting_id,))
        self.db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        self._commit()

    def reassign_segment(
        self, segment_id: int, name: str | None = None, speaker_id: int | None = None
    ) -> None:
        """Give one transcript line to another voice of the same meeting (speaker_id), even
        an unnamed one, or to a person by name (created if new)."""
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
