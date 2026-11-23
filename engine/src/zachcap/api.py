"""Local HTTP API for the desktop app.

Listens on 127.0.0.1 only. The Tauri shell picks the port and a random token
at launch; every request must carry the token (as a Bearer header, or a
``token`` query parameter for <audio> elements, which can't send headers).
GPU work runs on a single background worker so jobs never compete for memory.
"""

from __future__ import annotations

import io
import json
import queue
import re
import secrets as pysecrets
import shutil
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import __version__, apikeys, audio, paths, pipeline
from .store import Store

ALLOWED_ORIGINS = ["http://tauri.localhost", "tauri://localhost", "http://localhost:1420"]
RECORDING_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")
LANGUAGES = {"auto": "Detect automatically", "en": "English", "es": "Spanish", "fr": "French",
             "de": "German", "pt": "Portuguese", "it": "Italian", "nl": "Dutch"}  # fmt: skip


class NameBody(BaseModel):
    name: str


class LineSpeakerBody(BaseModel):
    name: str | None = None  # a person, by name
    speaker_id: int | None = None  # or a voice of the same meeting, named or not


class TitleBody(BaseModel):
    title: str


class QuestionBody(BaseModel):
    question: str


class Turn(BaseModel):
    question: str
    answer: str


class LibraryQuestionBody(BaseModel):
    question: str
    history: list[Turn] = []


class StatusBody(BaseModel):
    status: str


class RecordBody(BaseModel):
    title: str | None = None


class KeyBody(BaseModel):
    value: str


class ImportBody(BaseModel):
    path: str
    title: str | None = None
    start: str | None = None
    end: str | None = None


class SettingsBody(BaseModel):
    summary_model: str | None = None
    language: str | None = None
    mic_device: str | None = None
    output_device: str | None = None
    vocabulary: str | None = None


class Worker:
    """Runs processing jobs one at a time and remembers the latest message per meeting."""

    def __init__(self) -> None:
        self.jobs: queue.Queue[int] = queue.Queue()
        self.progress: dict[int, str] = {}
        threading.Thread(target=self._run, name="zachcap-worker", daemon=True).start()

    def submit(self, meeting_id: int) -> None:
        if self.progress.get(meeting_id) == "queued":
            return
        self.progress[meeting_id] = "queued"
        self.jobs.put(meeting_id)

    def _run(self) -> None:
        while True:
            meeting_id = self.jobs.get()
            store = None
            try:
                store = Store()
                pipeline.process(
                    store,
                    meeting_id,
                    log=lambda msg, m=meeting_id: self.progress.__setitem__(m, msg),
                )
                self.progress.pop(meeting_id, None)
            except Exception as exc:  # surfaced to the UI, full trace to the console
                traceback.print_exc()
                self.progress[meeting_id] = f"failed: {exc}"
            finally:
                if store is not None:
                    store.close()


def recover(store: Store, submit: Callable[[int], None]) -> None:
    """Finish what an earlier run left behind when ZachCap quit, crashed or lost power:
    recordings that never stopped cleanly, and meetings that were waiting for or in the
    middle of processing."""
    for meeting in store.meetings_with_status("recording"):
        store.update_meeting(meeting["id"], status="recorded")
    store.delete_orphan_voice_samples()
    known = {Path(folder).resolve() for folder in store.meeting_folders()}
    for folder in sorted(paths.meetings_dir().iterdir()):
        if not folder.is_dir() or not RECORDING_FOLDER.fullmatch(folder.name):
            continue
        if folder.resolve() in known or not any(folder.glob("*-000.flac")):
            continue
        info: dict = {}
        info_path = folder / "recording.json"
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except ValueError:  # cut off while being written
                info = {"complete": False}
            # Only recordings that were cut off: a finished one without a meeting was deleted.
            if info.get("complete") is not False:
                continue
        started = datetime.strptime(folder.name, "%Y-%m-%d_%H%M%S").astimezone()
        store.create_meeting(
            f"Recovered recording {started:%b %d %H:%M}",
            info.get("started_at") or started.isoformat(timespec="seconds"),
            folder,
        )
    for meeting in store.meetings_with_status("recorded", "processing"):
        submit(meeting["id"])


def create_app(token: str) -> FastAPI:
    # No interactive docs: they would answer without the token.
    app = FastAPI(
        title="ZachCap engine", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    worker = Worker()
    recording: dict = {}
    lock = threading.Lock()

    startup = Store()
    try:
        recover(startup, worker.submit)
    finally:
        startup.close()

    def finish_recording() -> int | None:
        """Stop the recorder, if one is running, and save its meeting. Returns the meeting id.
        Used by Stop, and by the engine itself when the app goes away mid-recording."""
        with lock:
            recorder = recording.pop("recorder", None)
            meeting_id = recording.pop("meeting_id", None)
            if recorder is None:
                return None
            duration = recorder.elapsed
            try:
                duration = recorder.stop()["duration"]
            except Exception:  # the audio saved so far still counts
                traceback.print_exc()
            store = Store()
            try:
                store.update_meeting(meeting_id, status="recorded", duration=round(duration, 3))
            finally:
                store.close()
        return meeting_id

    app.state.finish_recording = finish_recording

    def authorized(request: Request) -> None:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        supplied = supplied or request.query_params.get("token", "")
        if not pysecrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Missing or wrong token")

    def db():
        store = Store()
        try:
            yield store
        finally:
            store.close()

    def meeting_or_404(store: Store, meeting_id: int):
        meeting = store.meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="No such meeting")
        return meeting

    auth = [Depends(authorized)]

    # app status ---------------------------------------------------------------

    @app.get("/api/health", dependencies=auth)
    def health():
        return {"ok": True, "version": __version__, "data": str(paths.home())}

    @app.get("/api/stats", dependencies=auth)
    def stats(store: Store = Depends(db)):
        return store.stats()

    # meetings -----------------------------------------------------------------

    @app.get("/api/meetings", dependencies=auth)
    def list_meetings(store: Store = Depends(db)):
        participants = store.participants()
        return [
            {
                **dict(m),
                "progress": worker.progress.get(m["id"]),
                "participants": participants.get(m["id"], []),
            }
            for m in store.meetings()
        ]

    @app.get("/api/meetings/{meeting_id}", dependencies=auth)
    def get_meeting(meeting_id: int, store: Store = Depends(db)):
        meeting = meeting_or_404(store, meeting_id)
        speakers = [
            {k: s[k] for k in s.keys() if k != "embedding"} for s in store.speakers(meeting_id)
        ]
        return {
            "meeting": {**dict(meeting), "progress": worker.progress.get(meeting_id)},
            "speakers": speakers,
            "segments": [dict(row) for row in store.transcript(meeting_id)],
            "summary": store.summary(meeting_id),
            "talk_time": [dict(row) for row in store.talk_time(meeting_id)],
            "action_items": [dict(row) for row in store.action_items(meeting_id=meeting_id)],
            "me": store.me()["name"],
        }

    @app.patch("/api/meetings/{meeting_id}", dependencies=auth)
    def rename_meeting(meeting_id: int, body: TitleBody, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="The title can't be empty")
        store.update_meeting(meeting_id, title=body.title.strip())
        return {"title": body.title.strip()}

    @app.delete("/api/meetings/{meeting_id}", dependencies=auth)
    def delete_meeting(meeting_id: int, store: Store = Depends(db)):
        folder = Path(meeting_or_404(store, meeting_id)["folder"]).resolve()
        store.delete_meeting(meeting_id)
        # Only remove audio ZachCap recorded or imported itself, never a folder elsewhere.
        if folder.is_relative_to(paths.meetings_dir().resolve()) and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        return {"deleted": meeting_id}

    @app.post("/api/meetings/{meeting_id}/summary", dependencies=auth)
    def create_summary(meeting_id: int, store: Store = Depends(db)):
        from .summarize import summarize

        meeting_or_404(store, meeting_id)
        try:
            summarize(store, meeting_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # network, auth, rate limits: show the reason in the app
            raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc
        return store.summary(meeting_id)

    @app.post("/api/meetings/{meeting_id}/ask", dependencies=auth)
    def ask(meeting_id: int, body: QuestionBody, store: Store = Depends(db)):
        from .summarize import ask as ask_claude

        meeting_or_404(store, meeting_id)
        if not body.question.strip():
            raise HTTPException(status_code=400, detail="Ask a question first")
        try:
            return ask_claude(store, meeting_id, body.question.strip())
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc

    @app.post("/api/ask", dependencies=auth)
    def ask_library(body: LibraryQuestionBody, store: Store = Depends(db)):
        """A question about every meeting; Claude looks things up with read-only tools."""
        from .assistant import ask_everything

        if not body.question.strip():
            raise HTTPException(status_code=400, detail="Ask a question first")
        try:
            return ask_everything(
                store, body.question.strip(), [turn.model_dump() for turn in body.history]
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc

    @app.post("/api/meetings/{meeting_id}/process", dependencies=auth)
    def process(meeting_id: int, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        worker.submit(meeting_id)
        return {"queued": True}

    @app.post("/api/meetings/{meeting_id}/speakers/{speaker_id}/name", dependencies=auth)
    def name(meeting_id: int, speaker_id: int, body: NameBody, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="The name can't be empty")
        pipeline.name_speaker(store, meeting_id, str(speaker_id), body.name.strip())
        return {"status": store.meeting(meeting_id)["status"]}

    @app.post("/api/meetings/{meeting_id}/confirm", dependencies=auth)
    def confirm(meeting_id: int, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        return {"confirmed": pipeline.confirm_matches(store, meeting_id)}

    @app.get("/api/meetings/{meeting_id}/audio", dependencies=auth)
    def meeting_audio(meeting_id: int, track: str = "mix", store: Store = Depends(db)):
        if track not in ("mix", "mic", "system"):
            raise HTTPException(status_code=400, detail="track is mix, mic or system")
        folder = Path(meeting_or_404(store, meeting_id)["folder"])
        return FileResponse(_playback_file(folder, track), media_type="audio/flac")

    @app.get("/api/meetings/{meeting_id}/speakers/{speaker_id}/sample", dependencies=auth)
    def speaker_sample(meeting_id: int, speaker_id: int, store: Store = Depends(db)):
        folder = Path(meeting_or_404(store, meeting_id)["folder"])
        speaker = store.speaker(speaker_id)
        if (
            speaker is None
            or speaker["meeting_id"] != meeting_id
            or speaker["sample_start"] is None
        ):
            raise HTTPException(status_code=404, detail="No sample for that speaker")
        track = audio.load_track(folder, speaker["track"])
        if track is None:
            raise HTTPException(status_code=404, detail="Audio missing")
        clip = audio.clip(track, speaker["sample_start"], speaker["sample_end"])
        buffer = io.BytesIO()
        sf.write(buffer, clip, audio.RATE, format="WAV", subtype="PCM_16")
        return Response(buffer.getvalue(), media_type="audio/wav")

    @app.post("/api/segments/{segment_id}/speaker", dependencies=auth)
    def reassign_segment(segment_id: int, body: LineSpeakerBody, store: Store = Depends(db)):
        name = (body.name or "").strip()
        if body.speaker_id is None and not name:
            raise HTTPException(status_code=400, detail="The name can't be empty")
        try:
            store.reassign_segment(segment_id, name=name or None, speaker_id=body.speaker_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/import", dependencies=auth)
    def import_recording(body: ImportBody, store: Store = Depends(db)):
        source = Path(body.path)
        if not source.is_file():
            raise HTTPException(status_code=400, detail="That file doesn't exist")
        folder = paths.meetings_dir() / f"{datetime.now():%Y-%m-%d_%H%M%S}-import"
        folder.mkdir(parents=True)
        try:
            seconds = audio.import_media(source, folder / "system.flac", body.start, body.end)
        except Exception as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Couldn't read the audio: {exc}") from exc
        meeting_id = store.create_meeting(
            body.title or source.stem,
            audio.recorded_at(source).isoformat(timespec="seconds"),
            folder,
            duration=round(seconds, 3),
        )
        worker.submit(meeting_id)
        return {"meeting_id": meeting_id}

    # action items ---------------------------------------------------------------

    @app.get("/api/actions", dependencies=auth)
    def actions(status: str = "all", store: Store = Depends(db)):
        return {"me": store.me()["name"], "items": [dict(r) for r in store.action_items(status)]}

    @app.patch("/api/actions/{item_id}", dependencies=auth)
    def set_action(item_id: int, body: StatusBody, store: Store = Depends(db)):
        if body.status not in ("open", "done"):
            raise HTTPException(status_code=400, detail="status is open or done")
        store.set_action_status(item_id, body.status)
        return {"id": item_id, "status": body.status}

    # people -------------------------------------------------------------------

    @app.get("/api/people", dependencies=auth)
    def people(store: Store = Depends(db)):
        return [dict(p) for p in store.people_stats()]

    @app.patch("/api/people/{person_id}", dependencies=auth)
    def rename_person(person_id: int, body: NameBody, store: Store = Depends(db)):
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="The name can't be empty")
        return {"id": store.rename_person(person_id, body.name.strip())}

    @app.delete("/api/people/{person_id}/voice", dependencies=auth)
    def forget_voice(person_id: int, store: Store = Depends(db)):
        store.forget_voice(person_id)
        return {"ok": True}

    @app.delete("/api/people/{person_id}", dependencies=auth)
    def delete_person(person_id: int, store: Store = Depends(db)):
        store.delete_person(person_id)
        return {"ok": True}

    @app.get("/api/search", dependencies=auth)
    def search(q: str, store: Store = Depends(db)):
        try:
            return [dict(hit) for hit in store.search(q)]
        except Exception:  # FTS syntax errors from half-typed queries
            return []

    # recording ----------------------------------------------------------------

    @app.get("/api/record", dependencies=auth)
    def record_status():
        recorder = recording.get("recorder")
        if recorder is None:
            return {"recording": False}
        return {
            "recording": True,
            "title": recording.get("title"),
            "elapsed": round(recorder.elapsed, 1),
            "levels": recorder.levels(),
            "devices": {name: t.device["name"] for name, t in recorder.tracks.items()},
            "errors": recorder.errors(),
        }

    @app.post("/api/record/start", dependencies=auth)
    def record_start(body: RecordBody, store: Store = Depends(db)):
        from .capture import Recorder

        with lock:
            if recording.get("recorder") is not None:
                raise HTTPException(status_code=409, detail="Already recording")
            folder = paths.meetings_dir() / datetime.now().strftime("%Y-%m-%d_%H%M%S")
            recorder = Recorder(
                folder,
                mic_name=store.setting("mic_device") or None,
                output_name=store.setting("output_device") or None,
            )
            try:
                recorder.start()
            except Exception as exc:
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(
                    status_code=500, detail=f"Couldn't start recording: {exc}"
                ) from exc
            # The meeting exists from the first second, so a crash can't lose it.
            title = body.title or f"Meeting {datetime.now():%b %d %H:%M}"
            started = recorder.started_at or datetime.now().astimezone()
            meeting_id = store.create_meeting(
                title, started.isoformat(timespec="seconds"), folder, status="recording"
            )
            recording.update(recorder=recorder, meeting_id=meeting_id, title=title)
        return {"recording": True, "meeting_id": meeting_id}

    @app.post("/api/record/stop", dependencies=auth)
    def record_stop():
        meeting_id = finish_recording()
        if meeting_id is None:
            raise HTTPException(status_code=409, detail="Not recording")
        worker.submit(meeting_id)
        return {"meeting_id": meeting_id}

    @app.get("/api/call", dependencies=auth)
    def call():
        """Apps using the microphone right now, so the app can offer to record."""
        from .calls import apps_using_mic

        try:
            return {"apps": apps_using_mic()}
        except OSError:
            return {"apps": []}

    @app.get("/api/devices", dependencies=auth)
    def devices():
        from .capture import list_devices

        return list_devices()

    # keys and settings ------------------------------------------------------------

    @app.get("/api/keys", dependencies=auth)
    def key_status():
        return {name: apikeys.source(name) for name in apikeys.KEYS}

    @app.put("/api/keys/{name}", dependencies=auth)
    def key_set(name: str, body: KeyBody):
        if name not in apikeys.KEYS or not body.value.strip():
            raise HTTPException(status_code=400, detail="Unknown key or empty value")
        apikeys.store(name, body.value.strip())
        return {name: apikeys.source(name)}

    @app.get("/api/settings", dependencies=auth)
    def get_settings(store: Store = Depends(db)):
        from .summarize import MODELS, summary_model

        return {
            "summary_model": summary_model(store),
            "summary_models": [
                {"id": model, "label": label, "input_usd": per_in, "output_usd": per_out}
                for model, (label, per_in, per_out) in MODELS.items()
            ],
            "language": store.setting("language", "en"),
            "languages": [{"id": k, "label": v} for k, v in LANGUAGES.items()],
            "mic_device": store.setting("mic_device", ""),
            "output_device": store.setting("output_device", ""),
            "vocabulary": store.setting("vocabulary", ""),
            "me": dict(store.me()),
        }

    @app.put("/api/settings", dependencies=auth)
    def put_settings(body: SettingsBody, store: Store = Depends(db)):
        from .summarize import MODELS

        if body.summary_model is not None:
            if body.summary_model not in MODELS:
                raise HTTPException(status_code=400, detail="Unknown summary model")
            store.set_setting("summary_model", body.summary_model)
        if body.language is not None:
            if body.language not in LANGUAGES:
                raise HTTPException(status_code=400, detail="Unknown language")
            store.set_setting("language", body.language)
        for key in ("mic_device", "output_device"):
            value = getattr(body, key)
            if value is not None:
                store.set_setting(key, value)
        if body.vocabulary is not None:
            if len(body.vocabulary) > 4000:
                raise HTTPException(
                    status_code=400, detail="Keep the word list under 4,000 characters"
                )
            store.set_setting("vocabulary", body.vocabulary.strip())
        return get_settings(store)

    return app


_playback_locks: dict[Path, threading.Lock] = {}
_playback_locks_guard = threading.Lock()


def _playback_file(folder: Path, track: str = "mix") -> Path:
    """16 kHz mono audio for the player, built once and cached next to the recording.

    "mix" is everything; "mic" and "system" let the app isolate one side of the call.
    Built under a lock and moved into place when complete, so a second request or a
    crash mid-build can never serve a half-written file.
    """
    target = folder / ("playback.flac" if track == "mix" else f"playback-{track}.flac")
    with _playback_locks_guard:
        lock = _playback_locks.setdefault(target, threading.Lock())
    with lock:
        if not target.exists():
            names = ("mic", "system") if track == "mix" else (track,)
            tracks = [t for t in (audio.load_track(folder, n) for n in names) if t is not None]
            if not tracks:
                raise HTTPException(status_code=404, detail="Audio missing")
            mix = np.zeros(max(len(t) for t in tracks), np.float32)
            for t in tracks:
                mix[: len(t)] += t
            peak = float(np.abs(mix).max()) or 1.0
            partial = target.with_name(f"{target.stem}.partial.flac")
            sf.write(partial, mix / max(peak, 1.0), audio.RATE, subtype="PCM_16")
            partial.replace(target)
    return target


def _exit_with_parent(pid: int, before_exit: Callable[[], object]) -> None:
    """Exit when the desktop app's process ends, even if it couldn't stop us. A recording
    in progress is saved first, so quitting or a crash of the app never loses it."""
    import ctypes
    import os

    synchronize, infinite, no_such_process = 0x00100000, 0xFFFFFFFF, 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if handle:
        kernel32.WaitForSingleObject(handle, infinite)
    elif ctypes.get_last_error() != no_such_process:
        return  # can't watch it (access denied?): keep running rather than guess
    try:
        before_exit()
    finally:
        os._exit(0)


def serve(port: int, token: str) -> None:
    import logging
    import os

    import uvicorn

    # The player cancels audio requests whenever it seeks or switches tracks; on Windows
    # asyncio logs each one as a ConnectionResetError traceback. They are harmless.
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    app = create_app(token)
    parent = os.environ.get("ZACHCAP_PARENT_PID")
    if parent and parent.isdigit():
        threading.Thread(
            target=_exit_with_parent,
            args=(int(parent), app.state.finish_recording),
            name="zachcap-watchdog",
            daemon=True,
        ).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
