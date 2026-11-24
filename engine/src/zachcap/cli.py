"""Command-line entry point for the ZachCap engine.

zachcap record --title "Weekly sync"     record until Enter, then process
zachcap import FILE --title "..."        import a recording (mp4, m4a, mp3...) and process
zachcap show 3                           transcript and speakers for meeting 3
zachcap name 3 "Speaker 2" "Dana Lee"    name a speaker; their voice is learned
zachcap confirm 3                        accept every automatic match in meeting 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import __version__, apikeys, paths
from .store import Store

# What serve takes as its token: 16 or more printable characters, no spaces.
TOKEN = re.compile(r"[!-~]{16,}")


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return (
        f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        if seconds >= 3600
        else (f"{seconds // 60:02d}:{seconds % 60:02d}")
    )


def _meter(dbfs: float, width: int = 12) -> str:
    filled = max(0, min(width, round((dbfs + 60) / 60 * width)))
    return "#" * filled + "." * (width - filled)


def _show(store: Store, meeting_id: int) -> None:
    meeting = store.meeting(meeting_id)
    if meeting is None:
        raise SystemExit(f"No meeting {meeting_id}")
    minutes = (meeting["duration"] or 0) / 60
    print(f"\nMeeting {meeting_id}: {meeting['title']}  ({minutes:.1f} min, {meeting['status']})")
    speakers = store.speakers(meeting_id)
    if speakers:
        print("\nSpeakers")
        for s in speakers:
            detail = s["status"]
            if s["status"] in ("auto", "suggested") and s["person_name"]:
                detail += f" {s['person_name']} ({s['match_score']:.2f})"
            print(
                f"  #{s['id']:<4} {s['label']:<14} {s['track']:<7} "
                f"{s['speech_seconds'] or 0:6.1f} s  {detail}"
            )
    print("\nTranscript")
    for row in store.transcript(meeting_id):
        print(f"  {_clock(row['start'])}  {row['speaker']:<22} {row['text']}")
    open_count = sum(s["status"] in ("suggested", "unknown") for s in speakers)
    if open_count:
        print(
            f"\n{open_count} speaker(s) need names: "
            f'zachcap name {meeting_id} "Speaker 1" "Their Name"'
        )


def _process(store: Store, meeting_id: int) -> None:
    from . import pipeline

    pipeline.process(store, meeting_id, log=lambda msg: print(f"  {msg}"))
    _show(store, meeting_id)


def cmd_record(args: argparse.Namespace, store: Store) -> None:
    from .capture import Recorder

    folder = paths.meetings_dir() / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    recorder = Recorder(folder)
    recorder.start()
    title = args.title or f"Meeting {datetime.now():%b %d %H:%M}"
    started = (recorder.started_at or datetime.now().astimezone()).isoformat(timespec="seconds")
    # Saved now, so the app can recover the recording if this window is closed mid-call.
    meeting_id = store.create_meeting(title, started, folder, status="recording")
    print("Recording mic + computer audio. Press Enter to stop.")
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
    try:
        while not stop.wait(0.25):
            levels = recorder.levels()
            print(
                f"\r  {_clock(recorder.elapsed)}  You {_meter(levels['mic'])}  "
                f"Meeting {_meter(levels['system'])}",
                end="",
                flush=True,
            )
    except KeyboardInterrupt:
        pass
    info = recorder.stop()
    store.update_meeting(meeting_id, status="recorded", duration=info["duration"])
    print(f"\nSaved meeting {meeting_id} ({info['duration'] / 60:.1f} min) to {folder}")
    if not args.no_process:
        _process(store, meeting_id)


def cmd_import(args: argparse.Namespace, store: Store) -> None:
    source = args.source.resolve()
    if source.is_dir():
        if not any(source.glob("mic*")) and not any(source.glob("system*")):
            raise SystemExit(f"{source} has no mic or system audio")
        folder, title = source, args.title or source.name
        started = datetime.fromtimestamp(source.stat().st_mtime).astimezone()
        meeting_id = store.create_meeting(title, started.isoformat(timespec="seconds"), folder)
    else:
        from .imports import import_recording

        meeting_id, seconds = import_recording(store, source, args.title, args.start, args.end)
        print(f"Extracted {seconds / 60:.1f} min of audio from {source.name}")
    print(f"Imported meeting {meeting_id} from {source}")
    if not args.no_process:
        _process(store, meeting_id)


def cmd_process(args: argparse.Namespace, store: Store) -> None:
    _process(store, args.meeting)


def cmd_show(args: argparse.Namespace, store: Store) -> None:
    _show(store, args.meeting)


def cmd_name(args: argparse.Namespace, store: Store) -> None:
    from . import pipeline

    pipeline.name_speaker(store, args.meeting, args.speaker, args.name)
    _show(store, args.meeting)


def cmd_confirm(args: argparse.Namespace, store: Store) -> None:
    from . import pipeline

    count = pipeline.confirm_matches(store, args.meeting)
    print(f"Confirmed {count} speaker(s).")
    _show(store, args.meeting)


def cmd_list(args: argparse.Namespace, store: Store) -> None:
    for m in store.meetings():
        minutes = (m["duration"] or 0) / 60
        print(
            f"  {m['id']:>4}  {m['started_at'][:16]}  {minutes:5.1f} min  "
            f"{m['status']:<12} {m['title']}"
        )


def cmd_people(args: argparse.Namespace, store: Store) -> None:
    for p in store.people():
        me = " (you)" if p["is_me"] else ""
        print(f"  {p['id']:>4}  {p['name']}{me}  {p['samples']} voice sample(s)")


def cmd_search(args: argparse.Namespace, store: Store) -> None:
    for hit in store.search(args.query):
        print(
            f"  meeting {hit['meeting_id']} {_clock(hit['start'])}  {hit['speaker']}: "
            f"{hit['snippet']}"
        )


def cmd_keys(args: argparse.Namespace, store: Store | None) -> None:
    if args.action == "set":
        apikeys.prompt_and_store(args.name)
        print(f"Saved the {args.name} key to Windows Credential Manager.")
    elif args.action == "delete":
        apikeys.delete(args.name)
        print(f"Deleted the {args.name} key.")
    else:
        for name in apikeys.KEYS:
            print(f"  {name:<12} {apikeys.source(name)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zachcap", description="ZachCap engine")
    parser.add_argument("--version", action="version", version=f"zachcap {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="record mic + computer audio until Enter")
    p.add_argument("--title")
    p.add_argument("--no-process", action="store_true")
    p.set_defaults(handler=cmd_record)

    p = sub.add_parser(
        "import", help="add a recording (mp4, m4a, mp3, wav...) or a mic/system folder"
    )
    p.add_argument("source", type=Path)
    p.add_argument("--title")
    p.add_argument("--start", help="keep audio from here (seconds or mm:ss)")
    p.add_argument("--end", help="keep audio until here (seconds or mm:ss)")
    p.add_argument("--no-process", action="store_true")
    p.set_defaults(handler=cmd_import)

    for name, handler, text in (
        ("process", cmd_process, "(re)process a meeting"),
        ("show", cmd_show, "show a meeting's speakers and transcript"),
        ("confirm", cmd_confirm, "accept every automatic match in a meeting"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("meeting", type=int)
        p.set_defaults(handler=handler)

    p = sub.add_parser("name", help="name a speaker; their voice is learned")
    p.add_argument("meeting", type=int)
    p.add_argument("speaker", help='speaker label ("Speaker 2") or #id')
    p.add_argument("name")
    p.set_defaults(handler=cmd_name)

    sub.add_parser("list", help="list meetings").set_defaults(handler=cmd_list)
    sub.add_parser("people", help="list people and voice samples").set_defaults(handler=cmd_people)
    p = sub.add_parser("search", help="full-text search across meetings")
    p.add_argument("query")
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser("serve", help="run the local API for the desktop app")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--token",
        default=os.environ.get("ZACHCAP_TOKEN"),
        help="shared secret the app sends with each call (default: $ZACHCAP_TOKEN)",
    )
    p.set_defaults(handler=None)

    sub.add_parser(
        "mcp", help="serve your meetings to Claude Desktop or Claude Code (read-only MCP, stdio)"
    ).set_defaults(handler=None)

    p = sub.add_parser("keys", help="store API keys in Windows Credential Manager")
    p.add_argument("action", choices=["set", "status", "delete"])
    p.add_argument("name", nargs="?", choices=list(apikeys.KEYS))
    p.set_defaults(handler=cmd_keys)

    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import run

        run()
        return 0
    if args.command == "serve":
        from .api import serve

        if not args.token:
            parser.error("serve needs --token or ZACHCAP_TOKEN")
        # The shell's are 32 random hex digits; a short or guessable one isn't taken.
        if not TOKEN.fullmatch(args.token):
            parser.error(
                "the token must be at least 16 characters: letters, digits and punctuation, "
                "no spaces"
            )
        serve(args.port, args.token)
        return 0
    if args.command == "keys":
        if args.action != "status" and not args.name:
            parser.error(f"keys {args.action} needs a key name: {', '.join(apikeys.KEYS)}")
        args.handler(args, None)
        return 0
    if args.command == "name":
        args.speaker = args.speaker.lstrip("#")
    store = Store()
    try:
        args.handler(args, store)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
