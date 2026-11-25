-- A library made by ZachCap 0.5.0, with its schema (invented people and meetings).
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
    -- auto|suggested|confirmed|unknown|minor|left_out (see brief.py)
    status          TEXT NOT NULL,
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
DELETE FROM meetings;
INSERT INTO meetings (id, title, started_at, duration, folder, status, source_app, notes, template, audio_removed_at, ticket, client, client_source, ticket_source, template_source, private, created_at) VALUES (1, 'Contoso cutover sync', '2026-11-21T09:00:00-05:00', 600.0, 'meeting1', 'ready', 'Microsoft Teams', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, '2026-11-25 22:28:11');
INSERT INTO meetings (id, title, started_at, duration, folder, status, source_app, notes, template, audio_removed_at, ticket, client, client_source, ticket_source, template_source, private, created_at) VALUES (2, 'Northwind review', '2026-11-22T09:00:00-05:00', 600.0, 'meeting2', 'needs_names', 'Microsoft Teams', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, '2026-11-25 22:28:11');
INSERT INTO meetings (id, title, started_at, duration, folder, status, source_app, notes, template, audio_removed_at, ticket, client, client_source, ticket_source, template_source, private, created_at) VALUES (3, 'A private call', '2026-11-23T09:00:00-05:00', 600.0, 'meeting3', 'needs_names', 'Microsoft Teams', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, '2026-11-25 22:28:11');
DELETE FROM people;
INSERT INTO people (id, name, is_me, org, notes, created_at) VALUES (1, 'You', 1, NULL, NULL, '2026-11-25 22:28:11');
INSERT INTO people (id, name, is_me, org, notes, created_at) VALUES (2, 'Dana Lee', 0, 'Contoso', NULL, '2026-11-25 22:28:11');
DELETE FROM meeting_speakers;
INSERT INTO meeting_speakers (id, meeting_id, track, label, person_id, status, match_score, embedding, speech_seconds, sample_start, sample_end) VALUES (1, 1, 'system', 'Speaker 1', 2, 'confirmed', 0.2, X'0000803F0000803F0000803F0000803F', 60.0, 1.0, 4.0);
INSERT INTO meeting_speakers (id, meeting_id, track, label, person_id, status, match_score, embedding, speech_seconds, sample_start, sample_end) VALUES (2, 1, 'mic', 'You', 1, 'confirmed', 1.0, NULL, 30.0, NULL, NULL);
INSERT INTO meeting_speakers (id, meeting_id, track, label, person_id, status, match_score, embedding, speech_seconds, sample_start, sample_end) VALUES (3, 2, 'system', 'Speaker 1', NULL, 'unknown', 0.2, X'00000040000000400000004000000040', 60.0, 1.0, 4.0);
INSERT INTO meeting_speakers (id, meeting_id, track, label, person_id, status, match_score, embedding, speech_seconds, sample_start, sample_end) VALUES (4, 2, 'mic', 'You', 1, 'confirmed', 1.0, NULL, 30.0, NULL, NULL);
INSERT INTO meeting_speakers (id, meeting_id, track, label, person_id, status, match_score, embedding, speech_seconds, sample_start, sample_end) VALUES (5, 3, 'system', 'Speaker 1', NULL, 'unknown', 0.2, X'00004040000040400000404000004040', 60.0, 1.0, 4.0);
INSERT INTO meeting_speakers (id, meeting_id, track, label, person_id, status, match_score, embedding, speech_seconds, sample_start, sample_end) VALUES (6, 3, 'mic', 'You', 1, 'confirmed', 1.0, NULL, 30.0, NULL, NULL);
DELETE FROM segments;
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (1, 1, 1, 'system', 1.0, 4.0, 'The cutover is on Friday night', 0.9, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (2, 1, 2, 'mic', 5.0, 8.0, 'I will send the plan', 0.9, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (3, 1, NULL, 'system', 9.0, 9.5, 'Yes', 0.4, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (4, 2, 3, 'system', 1.0, 4.0, 'The cutover is on Friday night', 0.9, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (5, 2, 4, 'mic', 5.0, 8.0, 'I will send the plan', 0.9, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (6, 2, NULL, 'system', 9.0, 9.5, 'Yes', 0.4, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (7, 3, 5, 'system', 1.0, 4.0, 'The cutover is on Friday night', 0.9, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (8, 3, 6, 'mic', 5.0, 8.0, 'I will send the plan', 0.9, 0);
INSERT INTO segments (id, meeting_id, meeting_speaker_id, track, start, end, text, confidence, edited) VALUES (9, 3, NULL, 'system', 9.0, 9.5, 'Yes', 0.4, 0);
DELETE FROM voice_samples;
INSERT INTO voice_samples (id, person_id, meeting_id, embedding, model, seconds, created_at) VALUES (1, 2, 1, X'0000803F0000803F0000803F0000803F', 'demo', 60.0, '2026-11-25 22:28:11');
DELETE FROM summaries;
INSERT INTO summaries (meeting_id, model, content, input_tokens, output_tokens, cost_usd, created_at) VALUES (1, 'claude-opus-5-5', '{"title": "Contoso cutover sync", "overview": "Short.", "decisions": ["Cut over on Friday"], "action_items": [], "who_said_what": [], "open_questions": [], "topics": []}', 10, 5, 0.01, '2026-11-25 22:28:11');
INSERT INTO summaries (meeting_id, model, content, input_tokens, output_tokens, cost_usd, created_at) VALUES (2, 'claude-opus-5-5', '{"title": "Northwind review", "overview": "Short.", "decisions": ["Cut over on Friday"], "action_items": [], "who_said_what": [], "open_questions": [], "topics": []}', 10, 5, 0.01, '2026-11-25 22:28:11');
INSERT INTO summaries (meeting_id, model, content, input_tokens, output_tokens, cost_usd, created_at) VALUES (3, 'claude-opus-5-5', '{"title": "A private call", "overview": "Short.", "decisions": ["Cut over on Friday"], "action_items": [], "who_said_what": [], "open_questions": [], "topics": []}', 10, 5, 0.01, '2026-11-25 22:28:11');
DELETE FROM settings;
INSERT INTO settings (key, value) VALUES ('last_note_id', '6');
INSERT INTO settings (key, value) VALUES ('last_meeting_id', '4');
DELETE FROM action_items;
INSERT INTO action_items (id, meeting_id, owner, task, due, said_at, kind, status, created_at, done_at) VALUES (1, 1, 'You', 'Send the plan', 'Friday', 5.0, 'commitment', 'done', '2026-11-25 22:28:11', '2026-11-25 22:28:11');
INSERT INTO action_items (id, meeting_id, owner, task, due, said_at, kind, status, created_at, done_at) VALUES (2, 1, 'Dana Lee', 'Book the room', NULL, 2.0, 'action', 'open', '2026-11-25 22:28:11', NULL);
INSERT INTO action_items (id, meeting_id, owner, task, due, said_at, kind, status, created_at, done_at) VALUES (3, 2, 'You', 'Send the plan', 'Friday', 5.0, 'commitment', 'open', '2026-11-25 22:28:11', NULL);
INSERT INTO action_items (id, meeting_id, owner, task, due, said_at, kind, status, created_at, done_at) VALUES (4, 2, 'Dana Lee', 'Book the room', NULL, 2.0, 'action', 'open', '2026-11-25 22:28:11', NULL);
INSERT INTO action_items (id, meeting_id, owner, task, due, said_at, kind, status, created_at, done_at) VALUES (5, 3, 'You', 'Send the plan', 'Friday', 5.0, 'commitment', 'open', '2026-11-25 22:28:11', NULL);
INSERT INTO action_items (id, meeting_id, owner, task, due, said_at, kind, status, created_at, done_at) VALUES (6, 3, 'Dana Lee', 'Book the room', NULL, 2.0, 'action', 'open', '2026-11-25 22:28:11', NULL);
DELETE FROM meeting_notes;
INSERT INTO meeting_notes (id, meeting_id, at, kind, text, created_at) VALUES (1, 1, 2.0, 'note', 'Old firewall', '2026-11-25 22:28:11');
INSERT INTO meeting_notes (id, meeting_id, at, kind, text, created_at) VALUES (2, 1, 3.0, 'snapshot', 'Error 42', '2026-11-25 22:28:11');
INSERT INTO meeting_notes (id, meeting_id, at, kind, text, created_at) VALUES (3, 2, 2.0, 'note', 'Old firewall', '2026-11-25 22:28:11');
INSERT INTO meeting_notes (id, meeting_id, at, kind, text, created_at) VALUES (4, 2, 3.0, 'snapshot', 'Error 42', '2026-11-25 22:28:11');
INSERT INTO meeting_notes (id, meeting_id, at, kind, text, created_at) VALUES (5, 3, 2.0, 'note', 'Old firewall', '2026-11-25 22:28:11');
INSERT INTO meeting_notes (id, meeting_id, at, kind, text, created_at) VALUES (6, 3, 3.0, 'snapshot', 'Error 42', '2026-11-25 22:28:11');
DELETE FROM prompts;
DELETE FROM watched_files;
DELETE FROM changes;
INSERT INTO changes (name, version) VALUES ('lines', 19);
DELETE FROM changes;
INSERT INTO changes VALUES ('lines', 19);
PRAGMA user_version = 1;
