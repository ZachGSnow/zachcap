"""The watch folder: recordings saved there import themselves, once each."""

import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from zachcap import imports, watchfolder
from zachcap.store import Store


class Clock:
    def __init__(self) -> None:
        self.now = time.time() + 120  # files written by the test are old enough to settle

    def __call__(self) -> float:
        return self.now


def recording(path: Path, seconds: float = 1.0) -> Path:
    t = np.arange(int(seconds * 16000)) / 16000
    sf.write(path, (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), 16000)
    return path


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path / "home"))
    store = Store()
    folder = tmp_path / "phone-exports"
    folder.mkdir()
    store.set_setting(watchfolder.SETTING, str(folder))
    yield store, folder
    store.close()


def test_a_saved_recording_imports_once_after_it_settles(library):
    store, folder = library
    queued: list[int] = []
    watch = watchfolder.FolderWatch(queued.append, clock=Clock())
    recording(folder / "Call with Dana-20261121_093000-Meeting Recording.wav")
    (folder / "notes.txt").write_text("not a recording", encoding="utf-8")

    assert watch.check(store) == []  # first look: remember it
    (meeting_id,) = watch.check(store)  # unchanged on the second look: import it
    assert watch.check(store) == [] and queued == [meeting_id]

    meeting = store.meeting(meeting_id)
    assert meeting["title"] == "Call with Dana"  # without Teams' date tail
    assert meeting["started_at"].startswith("2026-11-21T09:30:00")
    assert (Path(meeting["folder"]) / "system.flac").exists()
    assert sorted(p.name for p in folder.iterdir()) == [
        "Call with Dana-20261121_093000-Meeting Recording.wav",
        "notes.txt",
    ]  # the user's files are only read


def test_a_file_still_being_written_waits(library):
    store, folder = library
    clock = Clock()
    watch = watchfolder.FolderWatch(lambda _: None, clock=clock)
    growing = recording(folder / "growing.wav", 1.0)
    watch.check(store)
    recording(growing, 2.0)  # bigger on the next look
    assert watch.check(store) == []
    assert len(watch.check(store)) == 1

    fresh = recording(folder / "fresh.wav")
    clock.now = os.stat(fresh).st_mtime + 5  # changed 5 s ago: not settled yet
    watch.check(store)
    assert watch.check(store) == []
    clock.now += 60
    assert len(watch.check(store)) == 1


def test_a_new_version_imports_again_and_a_deleted_meeting_stays_gone(library):
    store, folder = library
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    path = recording(folder / "standup.wav", 1.0)
    watch.check(store)
    (first,) = watch.check(store)

    store.delete_meeting(first)
    watch.check(store)
    assert watch.check(store) == []  # deleted here: not brought back

    recording(path, 2.0)  # exported again with new content
    watch.check(store)
    (second,) = watch.check(store)
    assert store.meeting(second)["duration"] == 2.0  # a new meeting, from the new version


def test_a_file_that_cant_be_read_is_noted_once_with_the_reason(library):
    store, folder = library
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    (folder / "broken.mp3").write_bytes(b"not really an mp3")
    watch.check(store)
    assert watch.check(store) == [] and watch.check(store) == []

    (failure,) = watch.failures
    assert failure["name"] == "broken.mp3" and failure["error"] and not failure["retrying"]
    (folder / "broken.mp3").write_bytes(b"still broken, but a new version")
    watch.check(store)
    assert watch.failures == []  # the failed version is gone
    assert not list((Path(os.environ["ZACHCAP_HOME"]) / "meetings").glob("*import*"))


def test_turning_it_on_can_skip_or_take_whats_there(library):
    store, folder = library
    recording(folder / "old.wav")
    assert watchfolder.skip_existing(store, folder) == 1
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    watch.check(store)
    assert watch.check(store) == []  # only new recordings

    watchfolder.retry_existing(store, folder)  # "import the ones there too"
    assert len(watch.check(store)) == 1


def test_skipping_whats_there_is_saved_in_one_go(library):
    store, folder = library
    for index in range(5):
        recording(folder / f"old-{index}.wav", 0.1)
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    assert watchfolder.skip_existing(store, folder) == 5
    store.db.set_trace_callback(None)
    assert [s for s in statements if s.strip().upper() == "COMMIT"] == ["COMMIT"]


def test_the_folder_is_read_in_one_listing(library, monkeypatch):
    # A network share answers one directory listing, not a question per file.
    store, folder = library
    for index in range(3):
        recording(folder / f"call-{index}.wav", 0.1)

    def stat(*args, **kwargs):
        raise AssertionError("looked at a file on its own")

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", stat)
        found = watchfolder.media_files(folder)
    assert len(found) == 3 and all(size > 0 for size, _ in found.values())


def test_imports_in_the_same_second_get_their_own_folders(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    moment = datetime(2026, 11, 23, 9, 0, 0)
    first, second = imports.new_folder(moment), imports.new_folder(moment)
    assert first != second and first.exists() and second.name.endswith("-import-2")


def test_the_watch_does_nothing_while_off(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    assert watchfolder.FolderWatch(lambda _: None).check(store) == []
    store.set_setting(watchfolder.SETTING, str(tmp_path / "unplugged-drive"))
    assert watchfolder.FolderWatch(lambda _: None).check(store) == []
    store.close()


def test_a_failure_reads_as_a_reason_not_a_path():
    path = r"C:\Calls\voicemail.mp3"
    error = OSError(f"[Errno 1094995529] Invalid data found when processing input: '{path}'")
    assert watchfolder.reason(error, path) == "Invalid data found when processing input"
    escaped = path.replace("\\", "\\\\")
    doubled = ValueError(f"{escaped} has no audio")
    assert watchfolder.reason(doubled, path) == "voicemail.mp3 has no audio"
    assert watchfolder.reason(RuntimeError(""), path) == "RuntimeError"


def test_a_file_that_cant_be_opened_right_now_is_tried_again(library, monkeypatch):
    # Open in another program or offline is not "broken": it imports once it can be read.
    store, folder = library
    recording(folder / "busy.wav")
    real = watchfolder.import_recording
    attempts = {"n": 0}

    def locked_twice(store_, path):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "The process cannot access the file", str(path))
        return real(store_, path)

    monkeypatch.setattr(watchfolder, "import_recording", locked_twice)
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    watch.check(store)
    assert watch.check(store) == [] and watch.check(store) == []  # locked: not "failed"
    assert watch.failures == [] and store.watched_failures(str(folder)) == []
    assert len(watch.check(store)) == 1  # readable again: imported


def test_a_file_that_stays_locked_is_shown_but_still_retried(library, monkeypatch):
    store, folder = library
    recording(folder / "locked.wav")

    def always_locked(store_, path):
        raise PermissionError(13, "The process cannot access the file", str(path))

    monkeypatch.setattr(watchfolder, "import_recording", always_locked)
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    for _ in range(4):
        watch.check(store)
    (shown,) = watch.failures
    assert shown["name"] == "locked.wav" and shown["retrying"]
    assert shown["error"] == "The process cannot access the file"


def test_turning_it_off_stops_a_run_between_files(library, monkeypatch):
    store, folder = library
    for name in ("a.wav", "b.wav", "c.wav"):
        recording(folder / name)
    real = watchfolder.import_recording

    def import_then_turn_off(store_, path):
        result = real(store_, path)
        store_.set_setting(watchfolder.SETTING, "")  # the user turned it off meanwhile
        return result

    monkeypatch.setattr(watchfolder, "import_recording", import_then_turn_off)
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    watch.check(store)
    assert len(watch.check(store)) == 1  # the one under way finished; the rest waited


def test_retry_gives_failed_files_another_go(library):
    store, folder = library
    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    (folder / "broken.mp3").write_bytes(b"not really an mp3")
    watch.check(store)
    watch.check(store)
    assert [f["name"] for f in watch.failures] == ["broken.mp3"]

    assert watch.retry_failed(store) == 1
    assert watch.failures == [] and store.watched_failures(str(folder)) == []


def test_a_look_under_way_when_the_folder_changes_leaves_nothing_of_the_old_one(
    library, monkeypatch, tmp_path: Path
):
    store, folder = library
    (folder / "broken.mp3").write_bytes(b"not really an mp3")
    other = tmp_path / "other-exports"
    other.mkdir()
    real = watchfolder.import_recording

    def fail_then_switch(store_, path):
        store_.set_setting(watchfolder.SETTING, str(other))  # another folder chosen meanwhile
        return real(store_, path)

    watch = watchfolder.FolderWatch(lambda _: None, clock=Clock())
    watch.check(store)
    monkeypatch.setattr(watchfolder, "import_recording", fail_then_switch)
    watch.check(store)
    assert watch.failures == []  # not the old folder's file under the new folder's name
