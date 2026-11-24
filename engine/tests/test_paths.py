"""Paths the app sends: only plain paths on this PC's own drives are used. One on another
computer, a device or a stream is refused with a reason to show, before anything looks at it."""

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

AUTH = {"Authorization": "Bearer test-token"}
DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE = 2, 3, 4  # as GetDriveTypeW says them
SHARE = r"\\fileserver\share"
REFUSED = "Choose a {} on this PC; network and device paths aren't used"


@pytest.fixture
def drives(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The drives Windows reports, by root: a local disk unless a test says otherwise, and
    Z: a letter mapped to a share on another computer."""
    types = {"Z:\\": DRIVE_REMOTE}
    monkeypatch.setattr(paths, "drive_type", lambda root: types.get(root.upper(), DRIVE_FIXED))
    return types


@pytest.mark.parametrize(
    "path",
    [
        SHARE + r"\call.mp4",  # another computer
        r"\\fileserver@SSL@443\share\call.mp4",  # the same over the web (WebDAV)
        r"\\?\UNC\fileserver\share\call.mp4",  # Windows' namespaces, even for C:
        r"\\?\GLOBALROOT\Device\Mup\fileserver\share\call.mp4",
        r"\\?\C:\Calls\call.mp4",
        r"\\.\C:\Calls\call.mp4",
        r"\\.\pipe\zachcap",
        r"\??\UNC\fileserver\share\call.mp4",
        "//fileserver/share/call.mp4",  # Windows reads / as \ too
        r"\/fileserver/share\call.mp4",
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
        r"C:\Calls\COM9.mp4",
        "C:\\Calls\\COM\u00b9.mp4",
        r"C:\Calls\CONIN$",
        r"C:\Calls\Old\..\NUL",
        r"C:\Calls\notes.txt:secret",  # a hidden stream of a file
        r"C:\Calls\notes.txt::$DATA",
        r"C:\Calls:secret\call.mp4",
        r"C:\Calls\call?.mp4",  # never part of a file name
        r"C:\Calls\*.mp4",
        r"C:\Calls\a|b.mp4",
        "C:\\Calls\\call\x00.mp4",
    ],
)
def test_network_device_and_odd_paths_are_refused(drives, path: str):
    with pytest.raises(ValueError) as refused:
        paths.on_this_pc(path)
    assert str(refused.value) == REFUSED.format("file")


@pytest.mark.parametrize(
    ("path", "used"),
    [
        (r"C:\Users\Dana\Videos\Weekly sync.mp4", r"C:\Users\Dana\Videos\Weekly sync.mp4"),
        ("C:/Users/Dana/Videos/call.mp4", r"C:\Users\Dana\Videos\call.mp4"),
        (
            r"D:\Recordings\Contoso call 2026-11-23.m4a",
            r"D:\Recordings\Contoso call 2026-11-23.m4a",
        ),
        (
            "C:\\Users\\Dana\\M\u00fasica\\Llamada con Zo\u00eb \u4f1a\u8b70.m4a",
            "C:\\Users\\Dana\\M\u00fasica\\Llamada con Zo\u00eb \u4f1a\u8b70.m4a",
        ),
        (r"C:\Users\Dana\Videos\..\Documents\notes.txt", r"C:\Users\Dana\Documents\notes.txt"),
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
def test_plain_paths_on_this_pc_are_used(drives, path: str, used: str):
    assert str(paths.on_this_pc(path)) == used


def test_a_long_path_is_used_whole(drives):
    folders = "\\".join(["Recordings kept for the quarterly review"] * 10)
    long = f"C:\\{folders}\\Weekly sync.mp4"
    assert len(long) > 400
    assert str(paths.on_this_pc(long)) == long


def test_a_drive_letter_mapped_to_a_share_is_refused_and_says_so(drives):
    with pytest.raises(ValueError) as refused:
        paths.on_this_pc(r"Z:\Recordings", "folder")
    assert str(refused.value) == "Z: is a network drive; choose a folder on this PC"
    with pytest.raises(ValueError, match="^Z: is a network drive"):
        paths.on_this_pc(r"z:\Recordings\..\call.mp4")

    drives["E:\\"] = DRIVE_REMOVABLE  # a USB stick is on this PC
    assert str(paths.on_this_pc(r"E:\call.mp4")) == r"E:\call.mp4"


def test_windows_says_this_pcs_own_drive_is_a_local_disk():
    assert paths.drive_type(os.environ.get("SystemDrive", "C:") + "\\") == DRIVE_FIXED


def test_a_saved_folder_is_used_only_when_it_is_on_this_pc(drives):
    assert paths.saved_folder(None) is None and paths.saved_folder("") is None
    assert paths.saved_folder(SHARE) is None
    assert paths.saved_folder(r"Z:\Recordings") is None
    assert str(paths.saved_folder("C:/Calls/")) == r"C:\Calls"


# Through the API ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app("test-token"))


def far(path) -> bool:
    """A path on another computer: a share, or the Z: drive these tests map to one."""
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
    ids=["import", "attach", "export", "export folder", "watch folder", "watch folder files"],
)
def test_each_path_the_app_sends_is_refused_on_another_computer_untouched(
    client: TestClient, looked_at: list[str], tmp_path: Path, method, url, sent, what
):
    store = Store()
    meeting_id = store.create_meeting("Printer down", "2026-11-23T09:00:00", tmp_path / "m")
    store.close()

    refused = client.request(method, url.format(id=meeting_id), headers=AUTH, **sent)

    assert refused.status_code == 400
    assert refused.json()["detail"] == REFUSED.format(what)
    assert looked_at == []
    settings = client.get("/api/settings", headers=AUTH).json()
    assert (settings["export_folder"], settings["watch_folder"]) == ("", "")
    assert client.get("/api/stats", headers=AUTH).json()["meetings"] == 1  # nothing imported
    assert client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["notes"] == []


def test_a_mapped_network_drive_is_refused_through_the_api(client, looked_at, drives):
    refused = client.put("/api/settings", headers=AUTH, json={"export_folder": r"Z:\Vault"})

    assert refused.status_code == 400
    assert refused.json()["detail"] == "Z: is a network drive; choose a folder on this PC"
    assert looked_at == []


def test_a_network_folder_saved_before_these_checks_is_never_used(client, looked_at, tmp_path):
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
