"""Paths the app sends: only plain paths on a drive letter are used, this PC's own or a mapped
network drive. A network path naming a server, a device or a stream is refused with a reason
to show, before anything looks at it."""

import builtins
import io
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from zachcap import export, imports, paths, pipeline, watchfolder
from zachcap.api import create_app
from zachcap.store import Store

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}
SHARE = r"\\fileserver\share"
NETWORK = (
    r"Network paths (\\server\share) aren't used; map the share to a drive letter in File "
    "Explorer and choose the {} there"
)
REFUSED = "Choose a {} on this PC or a mapped drive; device paths aren't used"


@pytest.mark.parametrize(
    "path",
    [
        SHARE + r"\call.mp4",
        SHARE,
        "//fileserver/share/call.mp4",  # Windows reads / as \ too
        r"\/fileserver/share\call.mp4",
        r"\\fileserver@SSL@443\share\call.mp4",  # the same over the web (WebDAV)
    ],
)
def test_a_network_path_is_refused_with_what_to_do_instead(path: str):
    with pytest.raises(ValueError) as refused:
        paths.on_a_drive(path, "folder")
    assert str(refused.value) == NETWORK.format("folder")
    with pytest.raises(ValueError) as refused:
        paths.on_a_drive(path)
    assert str(refused.value).endswith("and choose the file there")


@pytest.mark.parametrize(
    "path",
    [
        r"\\?\UNC\fileserver\share\call.mp4",  # Windows' namespaces, even for C:
        r"\\?\GLOBALROOT\Device\Mup\fileserver\share\call.mp4",
        r"\\?\C:\Calls\call.mp4",
        r"\\.\C:\Calls\call.mp4",
        r"\\.\pipe\zachcap",
        r"\??\UNC\fileserver\share\call.mp4",
        "//?/C:/Calls/call.mp4",
        r"\Calls\call.mp4",  # no drive, or no folder on it: wherever the engine happens to be
        r"Calls\call.mp4",
        r"C:Calls\call.mp4",
        "C:",
        "",
        r"C:\Calls\CON",  # devices, in any folder, with or without an extension
        r"C:\Calls\nul.txt",
        r"C:\Calls\Aux .log",
        r"C:\Calls\NUL.",  # Windows drops the dot, and it's NUL
        r"C:\Calls\prn.tar.gz",
        r"C:\LPT1\call.mp4",
        r"Z:\Recordings\COM9.mp4",
        "C:\\Calls\\COM\u00b9.mp4",
        r"C:\Calls\CONIN$",
        r"C:\Calls\Old\..\NUL",
        r"C:\Calls\notes.txt:hidden",  # a hidden stream of a file
        r"C:\Calls\notes.txt::$DATA",
        r"C:\Calls:hidden\call.mp4",
        r"C:\Calls\call?.mp4",  # never part of a file name
        r"C:\Calls\*.mp4",
        r"C:\Calls\a|b.mp4",
        "C:\\Calls\\call\x00.mp4",
    ],
)
def test_device_paths_streams_and_odd_paths_are_refused(path: str):
    with pytest.raises(ValueError) as refused:
        paths.on_a_drive(path)
    assert str(refused.value) == REFUSED.format("file")


@pytest.mark.parametrize(
    ("path", "used"),
    [
        (r"C:\Users\Public\Videos\Weekly sync.mp4", r"C:\Users\Public\Videos\Weekly sync.mp4"),
        ("C:/Users/Public/Videos/call.mp4", r"C:\Users\Public\Videos\call.mp4"),
        (
            r"D:\Recordings\Contoso call 2026-11-23.m4a",
            r"D:\Recordings\Contoso call 2026-11-23.m4a",
        ),
        (r"Z:\Phone system\Exports", r"Z:\Phone system\Exports"),  # a mapped network drive
        (
            "C:\\Users\\Dana\\M\u00fasica\\Llamada con Zo\u00eb \u4f1a\u8b70.m4a",
            "C:\\Users\\Dana\\M\u00fasica\\Llamada con Zo\u00eb \u4f1a\u8b70.m4a",
        ),
        (r"C:\Users\Public\Videos\..\Documents\notes.txt", r"C:\Users\Public\Documents\notes.txt"),
        (r"C:\..\..\Calls\call.mp4", r"C:\Calls\call.mp4"),  # never above the drive
        (r"C:\Calls\.\call.mp4", r"C:\Calls\call.mp4"),
        (r"C:\Calls\call.mp4. ", r"C:\Calls\call.mp4"),  # Windows drops trailing dots and spaces
        ("c:\\calls\\", "c:\\calls"),
        ("C:\\", "C:\\"),
        (r"C:\Calls\CONSOLE.txt", r"C:\Calls\CONSOLE.txt"),  # only look like devices
        (r"C:\Calls\nullable.log", r"C:\Calls\nullable.log"),
        (r"C:\Calls\COM10.mp4", r"C:\Calls\COM10.mp4"),
        (r"C:\Calls\COM1 call.mp4", r"C:\Calls\COM1 call.mp4"),
        (r"C:\Calls\con-call.mp4", r"C:\Calls\con-call.mp4"),
    ],
)
def test_plain_paths_on_a_drive_letter_are_used(path: str, used: str):
    assert str(paths.on_a_drive(path)) == used


def test_a_long_path_is_used_whole():
    folders = "\\".join(["Recordings kept for the quarterly review"] * 10)
    long = f"C:\\{folders}\\Weekly sync.mp4"
    assert len(long) > 400
    assert str(paths.on_a_drive(long)) == long


def test_a_saved_folder_is_used_unless_it_is_refused():
    assert paths.saved_folder(None) is None and paths.saved_folder("") is None
    assert paths.saved_folder(SHARE) is None and paths.saved_folder(r"\\.\pipe\zachcap") is None
    assert str(paths.saved_folder(r"Z:\Recordings")) == r"Z:\Recordings"
    assert str(paths.saved_folder("C:/Calls/")) == r"C:\Calls"


# Through the API ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app("test-token"), base_url=LOCAL)


def far(path) -> bool:
    """A path on another computer: a network path, or Z:, a mapped network drive here."""
    if not isinstance(path, str | os.PathLike):
        return False
    text = os.fsdecode(path).replace("/", "\\")
    return text.startswith("\\\\") or text[:2].upper() == "Z:"


@pytest.fixture
def looked_at(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Paths on another computer the engine looked at or opened. Each look fails the way an
    unreachable share does, so no test ever reaches the network."""
    seen: list[str] = []

    def guarded(real):
        def call(*args, **kwargs):
            if args and far(args[0]):
                seen.append(os.fsdecode(args[0]))
                raise FileNotFoundError(2, "The network path was not found", os.fsdecode(args[0]))
            return real(*args, **kwargs)

        return call

    def decode(store, source, *args, **kwargs):  # FFmpeg opens files itself, past the guards
        seen.append(str(source))
        raise FileNotFoundError(2, "The network path was not found", str(source))

    for module, name in ((os, "stat"), (os, "scandir"), (io, "open"), (builtins, "open")):
        monkeypatch.setattr(module, name, guarded(getattr(module, name)))
    monkeypatch.setattr(imports, "import_recording", decode)
    return seen


@pytest.mark.parametrize(
    ("method", "url", "sent", "what"),
    [
        ("POST", "/api/import", {"json": {"path": SHARE + r"\call.mp4"}}, "file"),
        ("POST", "/api/meetings/{id}/attach", {"json": {"path": SHARE + r"\spooler.log"}}, "file"),
        ("POST", "/api/vocabulary/found", {"json": {"path": SHARE + r"\assets.csv"}}, "file"),
        (
            "POST",
            "/api/meetings/{id}/export",
            {"json": {"format": "md", "path": SHARE + r"\notes.md"}},
            "file",
        ),
        ("PUT", "/api/settings", {"json": {"export_folder": SHARE}}, "folder"),
        (
            "PUT",
            "/api/settings",
            {"json": {"watch_folder": SHARE, "watch_existing": True}},
            "folder",
        ),
        ("GET", "/api/watch-folder/files", {"params": {"path": SHARE}}, "folder"),
    ],
    ids=[
        "import",
        "attach",
        "words found",
        "export",
        "export folder",
        "watch folder",
        "watch folder files",
    ],
)
def test_each_path_the_app_sends_is_refused_on_another_computer_untouched(
    client: TestClient, looked_at: list[str], tmp_path: Path, method, url, sent, what
):
    store = Store()
    meeting_id = store.create_meeting("Printer down", "2026-11-23T09:00:00", tmp_path / "m")
    store.close()

    refused = client.request(method, url.format(id=meeting_id), headers=AUTH, **sent)

    assert refused.status_code == 400
    assert refused.json()["detail"] == NETWORK.format(what)
    assert looked_at == []
    settings = client.get("/api/settings", headers=AUTH).json()
    assert (settings["export_folder"], settings["watch_folder"]) == ("", "")
    assert client.get("/api/stats", headers=AUTH).json()["meetings"] == 1  # nothing imported
    assert client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["notes"] == []


@pytest.mark.parametrize("path", [r"\\.\pipe\zachcap", r"C:\Calls\NUL", r"C:\Calls\a.mp4:hidden"])
def test_device_paths_and_streams_are_refused_through_the_api(client, looked_at, path):
    refused = client.post("/api/import", headers=AUTH, json={"path": path})

    assert refused.status_code == 400 and refused.json()["detail"] == REFUSED.format("file")
    assert looked_at == []


def test_a_mapped_network_drive_is_used_like_any_other_drive(client, looked_at):
    # Z: is offline here, so the folder isn't found; but it was looked for, not refused.
    files = client.get("/api/watch-folder/files", headers=AUTH, params={"path": r"Z:\Exports"})

    assert files.status_code == 400 and files.json()["detail"] == "That folder doesn't exist"
    assert looked_at == [r"Z:\Exports"]


def test_a_network_folder_saved_before_these_checks_is_off_and_says_why(
    client, looked_at, tmp_path
):
    store = Store()
    meeting_id = store.create_meeting("Weekly sync", "2026-11-20T09:00:00", tmp_path / "audio")
    store.set_setting("export_folder", SHARE + r"\vault")
    store.set_setting(watchfolder.SETTING, SHARE + r"\recordings")

    assert export.auto_export(store, meeting_id) is None
    assert watchfolder.FolderWatch(lambda _: None).check(store) == []
    store.close()

    assert looked_at == []
    settings = client.get("/api/settings", headers=AUTH).json()
    assert (settings["export_folder"], settings["watch_folder"]) == ("", "")  # shown as off
    assert settings["export_folder_refused"] == {
        "folder": SHARE + r"\vault",
        "reason": NETWORK.format("folder"),
    }
    assert settings["watch_folder_refused"]["folder"] == SHARE + r"\recordings"
    forgotten = {"export_folder": "", "watch_folder": ""}  # the notice's Forget it
    after = client.put("/api/settings", headers=AUTH, json=forgotten).json()
    assert (after["export_folder_refused"], after["watch_folder_refused"]) == (None, None)


def test_files_and_folders_on_this_pc_work_as_before(client, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    folder = tmp_path / "Notas de reuni\u00f3n" / "Dana's call logs"
    folder.mkdir(parents=True)
    log = folder / "Caf\u00e9 printer \u4f1a\u8b70.log"
    log.write_text("Spooler stopped at 09:12", encoding="utf-8")
    recording = folder / "Llamada con Zo\u00eb.wav"
    sf.write(recording, np.zeros(16000, np.float32), 16000)

    imported = client.post("/api/import", headers=AUTH, json={"path": str(recording)})
    assert imported.status_code == 200, imported.text
    meeting_id = imported.json()["meeting_id"]
    attached = client.post(
        f"/api/meetings/{meeting_id}/attach", headers=AUTH, json={"path": str(log)}
    )
    assert attached.json()["text"] == f"{log.name}\nSpooler stopped at 09:12"
    chosen = folder / "Weekly sync.md"
    saved = client.post(
        f"/api/meetings/{meeting_id}/export",
        headers=AUTH,
        json={"format": "md", "path": chosen.as_posix()},  # / as well as \
    )
    assert saved.json()["path"] == str(chosen) and chosen.exists()
    files = client.get("/api/watch-folder/files", headers=AUTH, params={"path": str(folder)})
    assert files.json() == {"count": 1}
    both = {"export_folder": str(folder), "watch_folder": str(folder)}
    settings = client.put("/api/settings", headers=AUTH, json=both).json()
    assert (settings["export_folder"], settings["watch_folder"]) == (str(folder), str(folder))
    assert (settings["export_folder_refused"], settings["watch_folder_refused"]) == (None, None)
