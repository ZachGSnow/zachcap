"""Snapshots of the call window during a recording, with their text, as notes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from zachcap import capture, export, pipeline, snapshot
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.summarize import notes_text

AUTH = {"Authorization": "Bearer test-token"}


class FakeRecorder:
    def __init__(self, folder: Path, **devices) -> None:
        self.folder = folder
        self.elapsed = 42.0
        self.tracks: dict = {}
        self.paused = False
        self.started_at = None

    def start(self) -> None:
        self.folder.mkdir(parents=True)

    def levels(self) -> dict:
        return {}

    def errors(self) -> list:
        return []

    def stop(self) -> dict:
        return {"duration": 42.0}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    return TestClient(create_app("test-token"))


def fake_take(text: str):
    def take(folder: Path, note_id: int, keys: str = "Ctrl+Alt+S") -> dict:
        take.keys = keys  # the snapshot keys, for its messages
        path = snapshot.image_path(folder, note_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 20), "white").save(path)
        return {"path": path, "text": text, "window": "Microsoft Teams", "size": (40, 20)}

    return take


def test_a_snapshot_is_a_note_with_its_picture_and_text(client: TestClient, monkeypatch):
    assert client.post("/api/record/snapshot", headers=AUTH).status_code == 409
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    monkeypatch.setattr(snapshot, "take", fake_take("Error 0x80070005\nAccess is denied."))

    shot = client.post("/api/record/snapshot", headers=AUTH).json()

    assert (shot["kind"], shot["at"], shot["lines"], shot["window"]) == (
        "snapshot",
        42.0,
        2,
        "Microsoft Teams",
    )
    picture = client.get(f"/api/notes/{shot['id']}/image", headers=AUTH)
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
    assert client.get("/api/record", headers=AUTH).json()["notes"] == 1

    folder = Path(Store().meeting(meeting_id)["folder"])
    client.delete(f"/api/notes/{shot['id']}", headers=AUTH)
    assert not snapshot.image_path(folder, shot["id"]).exists()  # the picture goes too
    assert client.get(f"/api/notes/{shot['id']}/image", headers=AUTH).status_code == 404


def test_a_snapshot_says_your_keys(client: TestClient, monkeypatch):
    take = fake_take("")
    monkeypatch.setattr(snapshot, "take", take)
    client.put("/api/settings", headers=AUTH, json={"shortcuts": {"snapshot": "Ctrl+Shift+F9"}})
    client.post("/api/record/start", headers=AUTH, json={})

    assert client.post("/api/record/snapshot", headers=AUTH).status_code == 200
    assert take.keys == "Ctrl+Shift+F9"  # what it says to press when there's no right window


def test_a_failed_snapshot_leaves_no_note(client: TestClient, monkeypatch):
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]

    def broken(folder, note_id, keys):
        raise RuntimeError("There's no window to capture")

    monkeypatch.setattr(snapshot, "take", broken)
    failed = client.post("/api/record/snapshot", headers=AUTH)
    assert failed.status_code == 400 and "no window" in failed.json()["detail"]
    client.post("/api/record/stop", headers=AUTH)
    assert client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["notes"] == []


def test_summaries_and_exports_get_the_screen_text(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting_id = store.create_meeting("Support call", "2026-11-23T09:00:00", tmp_path / "m")
    store.add_note(meeting_id, 65.0, "snapshot", "Error 0x80070005\nAccess is denied.")
    store.add_note(meeting_id, 70.0, "snapshot", "")
    store.add_note(meeting_id, 75.0, "snapshot", "x" * 5000)

    store.add_note(meeting_id, 80.0, "note", "Ask about the license")
    prompt = notes_text(store, meeting_id)
    # Snapshots are their own block, read off the screen: not the user's own notes.
    own, screens = prompt.split("Text read from screen snapshots")
    assert "Ask about the license" in own and "0x80070005" not in own
    assert "not by You" in screens
    assert "[01:05] Error 0x80070005 / Access is denied." in screens
    assert "[01:10] (no text on it)" in screens
    assert "x" * 1500 + "..." in screens and "x" * 1501 not in screens

    markdown = export.markdown(store, meeting_id)
    assert "- [01:05] Snapshot: Error 0x80070005 / Access is denied." in markdown
    assert "- [01:10] Snapshot (no text)" in markdown
    store.close()


def test_the_picture_lives_in_the_meeting_folder(tmp_path: Path):
    assert snapshot.image_path(tmp_path, 7) == tmp_path / "snapshots" / "note-7.png"


# ---- which window: stand-ins for the Windows calls, never a real window or the screen ----


class Windows:
    """Top-level windows, front to back, as (hwnd, pid, title, shown, minimized, guarded)."""

    def __init__(self, windows, front, ours=(1,)):
        self.windows = {w[0]: w for w in windows}
        self.order = [w[0] for w in windows]
        self.front = front
        self.ours = set(ours)

    def install(self, monkeypatch, meeting_paths=None, keys=None):
        from zachcap import calls

        user32 = type("User32", (), {})()
        user32.GetForegroundWindow = lambda: self.front
        user32.IsIconic = lambda hwnd: self.windows[int(hwnd.value)][4]
        monkeypatch.setattr(snapshot, "_user32", lambda: user32)
        monkeypatch.setattr(snapshot, "_ours", lambda: self.ours)
        monkeypatch.setattr(snapshot, "_window_pid", lambda u, h: self.windows[h][1])
        monkeypatch.setattr(snapshot, "_title", lambda u, h: self.windows[h][2])
        monkeypatch.setattr(snapshot, "_shown", lambda u, h: self.windows[h][3])
        monkeypatch.setattr(snapshot, "_protected", lambda u, h: self.windows[h][5])
        monkeypatch.setattr(snapshot, "_top_windows", lambda u: list(self.order))
        paths = meeting_paths or {}
        monkeypatch.setattr(snapshot, "_process_path", lambda pid: paths.get(pid, ""))
        in_use = keys if keys is not None else ["MSTeams_8wekyb3d8bbwe"] if paths else []
        monkeypatch.setattr(calls, "meeting_app_keys", lambda: in_use)


TEAMS_EXE = r"C:\Program Files\WindowsApps\MSTeams_1.0_x64__8wekyb3d8bbwe\ms-teams.exe"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_KEY = "C:#Program Files#Google#Chrome#Application#chrome.exe"


def test_the_window_in_front_is_taken_when_it_isnt_zachcap(monkeypatch):
    Windows([(10, 5, "Error dialog", True, False, False)], front=10).install(monkeypatch)
    assert snapshot.call_window() == (10, "Error dialog")


def test_from_zachcaps_camera_the_call_window_is_taken_not_whats_behind(monkeypatch):
    windows = [
        (20, 1, "ZachCap", True, False, False),  # in front: its camera was clicked
        (21, 7, "Inbox - Mail", True, False, False),  # right behind it: never taken
        (22, 9, "Meeting with Contoso | Microsoft Teams", True, False, False),
    ]
    Windows(windows, front=20).install(monkeypatch, meeting_paths={9: TEAMS_EXE})
    assert snapshot.call_window() == (22, "Meeting with Contoso | Microsoft Teams")


def test_a_minimized_call_window_is_said_not_swapped_for_another(monkeypatch):
    windows = [
        (20, 1, "ZachCap", True, False, False),
        (21, 7, "Inbox - Mail", True, False, False),
        (22, 9, "Microsoft Teams", True, True, False),  # minimized
    ]
    Windows(windows, front=20).install(monkeypatch, meeting_paths={9: TEAMS_EXE})
    with pytest.raises(snapshot.SnapshotError, match="minimized"):
        snapshot.call_window()


def test_a_call_in_a_browser_is_taken_only_when_its_window_is_certain(monkeypatch):
    one = [
        (20, 1, "ZachCap", True, False, False),
        (21, 7, "Inbox - Mail", True, False, False),
        (23, 11, "Meet - Google Chrome", True, False, False),
    ]
    Windows(one, front=20).install(monkeypatch, meeting_paths={11: CHROME_EXE}, keys=[CHROME_KEY])
    assert snapshot.call_window() == (23, "Meet - Google Chrome")

    # Two browser windows: the one in front could be anything (a mailbox, a document).
    two = [*one[:2], (24, 11, "Quarterly numbers - Google Chrome", True, False, False), one[2]]
    Windows(two, front=20).install(monkeypatch, meeting_paths={11: CHROME_EXE}, keys=[CHROME_KEY])
    with pytest.raises(snapshot.SnapshotError, match="several windows"):
        snapshot.call_window()


def test_no_call_app_and_zachcap_in_front_captures_nothing(monkeypatch):
    windows = [(20, 1, "ZachCap", True, False, False), (21, 7, "Inbox - Mail", True, False, False)]
    Windows(windows, front=20).install(monkeypatch)
    with pytest.raises(snapshot.SnapshotError, match=r"front, then press Ctrl\+Alt\+P"):
        snapshot.call_window("Ctrl+Alt+P")  # the keys picked in Settings


def test_hidden_and_protected_windows_are_refused(monkeypatch):
    # A window on another virtual desktop ("cloaked") isn't taken even if Windows calls it
    # the foreground; one an app keeps out of screenshots is refused by name.
    cloaked = [
        (30, 5, "Other desktop", False, False, False),
        (31, 1, "ZachCap", True, False, False),
    ]
    Windows(cloaked, front=30).install(monkeypatch)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.call_window()
    guarded = [(40, 5, "Password vault", True, False, True)]
    Windows(guarded, front=40).install(monkeypatch)
    with pytest.raises(snapshot.SnapshotError, match="Password vault is protected"):
        snapshot.call_window()


def test_a_window_that_wont_draw_is_only_read_off_the_screen_when_in_front(monkeypatch):
    from PIL import Image

    black = Image.new("RGB", (100, 80))
    user32 = type("User32", (), {})()
    user32.SetThreadDpiAwarenessContext = lambda context: None
    user32.GetWindowRect = lambda hwnd, rect: 1
    user32.GetForegroundWindow = lambda: 99  # another window is in front
    monkeypatch.setattr(snapshot, "_user32", lambda: user32)
    monkeypatch.setattr(snapshot, "_bounds", lambda hwnd: (0, 0, 100, 80))
    monkeypatch.setattr(snapshot, "_print_window", lambda hwnd, window: black)

    def screen(**kwargs):
        raise AssertionError("read the screen for a window that isn't in front")

    monkeypatch.setattr(snapshot.ImageGrab, "grab", screen)
    with pytest.raises(snapshot.SnapshotError, match="didn't let itself"):
        snapshot.grab(42)


def test_a_snapshot_keeps_a_small_picture_for_the_notes_list(tmp_path: Path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(snapshot, "call_window", lambda keys: (5, "Shared screen"))
    monkeypatch.setattr(snapshot, "grab", lambda hwnd: Image.new("RGB", (3840, 2160), "white"))
    monkeypatch.setattr(snapshot, "read_text", lambda image: "")
    snapshot.take(tmp_path, 3)
    with Image.open(snapshot.thumbnail_path(tmp_path, 3)) as thumb:
        assert thumb.size == (480, 270)
    snapshot.remove_pictures(tmp_path, 3)
    assert not snapshot.image_path(tmp_path, 3).exists()
    assert not snapshot.thumbnail_path(tmp_path, 3).exists()


def test_note_ids_are_never_given_out_again(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting_id = store.create_meeting("Call", "2026-11-23T09:00:00", tmp_path / "m")
    first = store.add_note(meeting_id, 1.0, "snapshot", "")
    store.delete_note(first)
    second = store.add_note(meeting_id, 2.0, "snapshot", "")
    assert second != first  # a new picture never takes a deleted one's address
    store.close()


def test_note_ids_from_before_the_counter_are_never_given_out_again(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting_id = store.create_meeting("Call", "2026-11-23T09:00:00", tmp_path / "m")
    store.add_note(meeting_id, 1.0, "snapshot", "")
    last = store.add_note(meeting_id, 2.0, "snapshot", "")
    store.delete_setting("last_note_id")  # a library from before the counter
    store.close()

    store = Store(tmp_path / "test.db")
    assert store.forget_derived(meeting_id, 1.5, 2.5)["snapshots"] == [last]  # a redaction
    assert store.add_note(meeting_id, 3.0, "snapshot", "") > last
    store.close()


def test_the_small_picture_is_served_for_the_list(client: TestClient, monkeypatch):
    client.post("/api/record/start", headers=AUTH, json={})

    def take(folder: Path, note_id: int, keys: str) -> dict:
        from PIL import Image

        path = snapshot.image_path(folder, note_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 20), "white").save(path)
        Image.new("RGB", (8, 4), "white").save(snapshot.thumbnail_path(folder, note_id))
        return {"path": path, "text": "", "window": "Teams", "size": (40, 20)}

    monkeypatch.setattr(snapshot, "take", take)
    shot = client.post("/api/record/snapshot", headers=AUTH).json()
    small = client.get(f"/api/notes/{shot['id']}/image?size=thumb", headers=AUTH)
    full = client.get(f"/api/notes/{shot['id']}/image", headers=AUTH)
    assert (
        small.headers["content-type"] == "image/jpeg"
        and full.headers["content-type"] == "image/png"
    )
    client.post("/api/record/stop", headers=AUTH)


def test_a_snapshot_racing_a_stop_is_refused_not_an_error(client: TestClient):
    # Stop empties the recording's state; a snapshot reading it half before and half after
    # must answer "not recording" (409), not fail on the missing meeting (500).
    assert client.post("/api/record/start", headers=AUTH, json={}).status_code == 200
    finish = client.app.state.finish_recording
    cell = finish.__closure__[finish.__code__.co_freevars.index("recording")]
    state = cell.cell_contents

    class StopRightAfterTheRecorderIsRead(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == "recorder" and value is not None:
                self.clear()
            return value

    cell.cell_contents = StopRightAfterTheRecorderIsRead(state)
    try:
        assert client.post("/api/record/snapshot", headers=AUTH).status_code == 409
    finally:
        cell.cell_contents = state
    assert client.post("/api/record/stop", headers=AUTH).status_code == 200
