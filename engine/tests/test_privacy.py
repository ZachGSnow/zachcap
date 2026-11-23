import json
from datetime import datetime, timedelta
from pathlib import Path

from zachcap import privacy
from zachcap.store import Store


def test_contact_details_are_masked_and_other_numbers_kept():
    text = (
        "Mail dana.lee+it@contoso.example or call 555-123-4567, (555) 123-4567, "
        "+1 555 123 4567 or ext 555-0123. Card 4111 1111 1111 1111 on file. "
        "Ticket 1924086, server 10.0.0.12, at 12:30 on 2026-11-20, order 1234567890123456."
    )
    masked = privacy.mask(text)
    assert masked == (
        "Mail [email] or call [phone], [phone], "
        "[phone] or ext [phone]. Card [card number] on file. "
        "Ticket 1924086, server 10.0.0.12, at 12:30 on 2026-11-20, order 1234567890123456."
    )


def test_masking_follows_the_setting(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    assert privacy.outgoing(store, "a@b.example") == "a@b.example"
    store.set_setting("mask_before_claude", "1")
    assert privacy.outgoing(store, "a@b.example") == "[email]"
    store.close()


def test_old_recordings_are_deleted_and_transcripts_kept(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    now = datetime(2026, 11, 23, 9, 0).astimezone()

    def meeting(name: str, days_ago: int, status: str = "ready", inside: bool = True) -> int:
        folder = (tmp_path / "meetings" if inside else tmp_path / "elsewhere") / name
        folder.mkdir(parents=True)
        for file in ("mic-000.flac", "system-000.flac", "playback.flac"):
            (folder / file).write_bytes(b"audio")
        (folder / "recording.json").write_text(json.dumps({"complete": True}), encoding="utf-8")
        started = (now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        meeting_id = store.create_meeting(name, started, folder)
        store.update_meeting(meeting_id, status=status)
        return meeting_id

    old = meeting("old", 40)
    recent = meeting("recent", 5)
    busy = meeting("busy", 40, status="processing")
    outside = meeting("imported-elsewhere", 40, inside=False)

    assert privacy.remove_old_audio(store, now) == []  # keeps everything by default
    store.set_setting("keep_audio_days", "30")
    assert privacy.remove_old_audio(store, now) == [old]

    old_folder = Path(store.meeting(old)["folder"])
    assert [p.name for p in old_folder.iterdir()] == ["recording.json"]
    assert store.meeting(old)["audio_removed_at"]
    for kept in (recent, busy, outside):
        assert len(list(Path(store.meeting(kept)["folder"]).glob("*.flac"))) == 3
    assert privacy.remove_old_audio(store, now) == []  # nothing left to do
    store.close()


def test_keeping_recordings_also_removes_snapshot_pictures_but_not_their_text(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    now = datetime(2026, 11, 23, 9, 0).astimezone()
    folder = tmp_path / "meetings" / "call"
    (folder / "snapshots").mkdir(parents=True)
    (folder / "mic-000.flac").write_bytes(b"audio")
    for picture in ("note-1.png", "note-1.thumb.jpg"):
        (folder / "snapshots" / picture).write_bytes(b"picture")
    started = (now - timedelta(days=40)).isoformat(timespec="seconds")
    meeting = store.create_meeting("Call", started, folder)
    store.update_meeting(meeting, status="ready")
    note = store.add_note(meeting, 5.0, "snapshot", "Error 0x80070005")
    store.set_setting("keep_audio_days", "30")

    assert privacy.remove_old_audio(store, now) == [meeting]
    assert not (folder / "snapshots").exists()
    assert store.note(note)["text"] == "Error 0x80070005"
    store.close()
