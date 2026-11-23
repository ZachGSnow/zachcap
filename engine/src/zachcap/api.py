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
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import (
    __version__,
    apikeys,
    audio,
    clients,
    dictation,
    digest,
    export,
    imports,
    insights,
    mentions,
    paths,
    pipeline,
    privacy,
    prompts,
    redact,
    shortcuts,
    tickets,
    watchfolder,
)
from . import vocabulary as words
from .store import Store
from .watchdog import Watch

ALLOWED_ORIGINS = ["http://tauri.localhost", "tauri://localhost", "http://localhost:1420"]
RECORDING_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")
LANGUAGES = {"auto": "Detect automatically", "en": "English", "es": "Spanish", "fr": "French",
             "de": "German", "pt": "Portuguese", "it": "Italian", "nl": "Dutch"}  # fmt: skip


class NameBody(BaseModel):
    name: str


class LineTextBody(BaseModel):
    text: str


class CorrectionBody(BaseModel):
    term: str
    misheard: str | None = None


class RedactBody(BaseModel):
    start: float  # seconds into the meeting
    end: float


class LineSpeakerBody(BaseModel):
    name: str | None = None  # a person, by name
    speaker_id: int | None = None  # or a voice of the same meeting, named or not


class TitleBody(BaseModel):
    title: str


class MeetingChange(BaseModel):
    title: str | None = None
    ticket: str | None = None  # "" takes it off
    client: str | None = None  # "" goes back to the client of the people in it


class ClientBody(BaseModel):
    client: str  # "" for none


class QuestionBody(BaseModel):
    question: str


class SummaryBody(BaseModel):
    template: str | None = None


class RecipeBody(BaseModel):
    recipe: str
    previous: str | None = None  # the document as it stands, to rewrite...
    change: str | None = None  # ...this way ("shorter, for the client")


class PromptBody(BaseModel):
    kind: str  # template (a kind of meeting) or recipe (a one-click document)
    label: str
    instructions: str = ""


class PromptChangeBody(BaseModel):
    label: str | None = None
    instructions: str | None = None


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


class PauseBody(BaseModel):
    paused: bool


class NoteBody(BaseModel):
    text: str = ""
    kind: str = "note"  # note|bookmark
    at: float | None = None  # seconds; while recording, now


class NoteTextBody(BaseModel):
    text: str


class ExportBody(BaseModel):
    format: str
    path: str  # chosen in the app's save dialog


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
    summary_template: str | None = None
    export_folder: str | None = None  # "" turns automatic export off
    keep_audio_days: int | None = None  # 0 keeps recordings forever
    mask_before_claude: bool | None = None
    stop_when_call_ends: bool | None = None
    export_work_log: bool | None = None  # keep a daily and weekly work log in the export folder
    dictation: bool | None = None  # hold the dictation keys to dictate into any app
    dictation_snippets: str | None = None  # "cue: text to type", one per line
    shortcuts: dict[str, str] | None = None  # new keys for some shortcuts, "" for the default
    ticket_patterns: str | None = None  # how ticket numbers look ("#######, INC-####"); "" off
    also_answer_to: str | None = None  # other names you answer to, comma-separated
    watch_folder: str | None = None  # "" turns the watch folder off
    watch_existing: bool = False  # with watch_folder: import the files already there too


KEEP_AUDIO_CHOICES = (0, 7, 30, 90, 365)
NO_STORE = {"Cache-Control": "no-store"}


class DictateBody(BaseModel):
    type: bool = False  # type the text into the app in front (the page asks for it)
    id: int | None = None  # which dictation (the number start gave); the newest if not given


class WorkLogBody(BaseModel):
    period: str = "day"
    day: str | None = None  # YYYY-MM-DD, today if not given
    client: str | None = None  # one client's log (as the page shows it)


def work_log_period(period: str, day: str | None) -> tuple[str, date]:
    if period not in ("day", "week"):
        raise HTTPException(status_code=400, detail='The period is "day" or "week"')
    try:
        return period, date.fromisoformat(day) if day else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="Use a date like 2026-11-23") from None


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
                tickets.fill(store, meeting_id)
                export.auto_export(store, meeting_id)
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
    dictator = dictation.Dictation()

    def language_of(store: Store) -> str | None:
        language = store.setting("language", "en")
        return None if language == "auto" else language

    lock = threading.Lock()

    def leave_dictation_out(listening: bool) -> None:
        """While you dictate during a recording, its mic track records silence: a ticket
        note isn't part of the meeting (the call goes on being recorded)."""
        with lock:
            recorder = recording.get("recorder")
        if recorder is not None:
            recorder.mute_mic(listening)

    dictator.on_listening = leave_dictation_out

    startup = Store()
    try:
        recover(startup, worker.submit)
        if startup.setting(dictation.SETTING) == "1":  # ready before the keys are first held
            dictator.transcriber.warm_up(language_of(startup))
    finally:
        startup.close()

    def remove_old_audio() -> None:
        """The "keep recordings" setting, applied at startup and every few hours after."""
        while True:
            store = Store()
            try:
                privacy.remove_old_audio(store)
            except Exception:
                traceback.print_exc()
            finally:
                store.close()
            time.sleep(6 * 3600)

    threading.Thread(target=remove_old_audio, name="zachcap-retention", daemon=True).start()

    def finish_recording() -> int | None:
        """Stop the recorder, if one is running, and save its meeting. Returns the meeting id.
        Used by Stop, and by the engine itself when the app goes away mid-recording."""
        with lock:
            recorder = recording.pop("recorder", None)
            meeting_id = recording.pop("meeting_id", None)
            watch = recording.pop("watch", None)
            if recorder is None:
                if watch is not None:
                    watch.stop()
                return None
            duration = recorder.elapsed
            try:
                duration = recorder.stop()["duration"]
            except Exception:  # the audio saved so far still counts
                traceback.print_exc()
            finally:
                if watch is not None:  # kept awake until the last audio is written
                    watch.stop()
            store = Store()
            try:
                store.update_meeting(meeting_id, status="recorded", duration=round(duration, 3))
            finally:
                store.close()
        return meeting_id

    app.state.finish_recording = finish_recording
    app.state.submit = worker.submit
    # The watch folder's looks (a thread started by serve()); Settings reads what it found.
    app.state.folder_watch = watchfolder.FolderWatch(worker.submit)

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

    def export_soon(store: Store, meeting_ids: list[int]) -> None:
        """Bring the export folder up to date off the request thread: renaming someone can
        touch every meeting they were in."""
        if not meeting_ids or not store.setting("export_folder"):
            return

        def run() -> None:
            background = Store()
            try:
                for meeting_id in dict.fromkeys(meeting_ids):
                    export.auto_export(background, meeting_id)
            except Exception:
                traceback.print_exc()
            finally:
                background.close()

        threading.Thread(target=run, name="zachcap-export", daemon=True).start()

    def meeting_or_404(store: Store, meeting_id: int):
        meeting = store.meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="No such meeting")
        return meeting

    # One change at a time per meeting: a summary written from the transcript while a
    # redaction removes words from it would bring them back, and so would reprocessing.
    jobs: dict[int, str] = {}
    jobs_lock = threading.Lock()

    @contextmanager
    def only_job(meeting_id: int, what: str):
        with jobs_lock:
            running = jobs.get(meeting_id)
            if running is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"This meeting is busy ({running}). Try again when that's done.",
                )
            jobs[meeting_id] = what
        try:
            yield
        finally:
            with jobs_lock:
                jobs.pop(meeting_id, None)

    def refuse_if_busy(meeting) -> None:
        """Changing a transcript while it is being recorded or (re)processed would be lost
        or would mix with the new results."""
        progress = worker.progress.get(meeting["id"])
        if meeting["status"] == "recording":
            raise HTTPException(status_code=409, detail="This meeting is still being recorded.")
        if progress is not None and not progress.startswith("failed"):
            raise HTTPException(
                status_code=409, detail="This meeting is being processed. Try again when it's done."
            )

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
        mentioned = mentions.counts(store)
        meetings = store.meetings()
        client_of = clients.of_meetings(store, meetings)
        return [
            {
                **dict(m),
                "progress": worker.progress.get(m["id"]),
                "participants": participants.get(m["id"], []),
                "mentions": mentioned.get(m["id"], 0),
                # Its client: the one set on it, else the one its people all work for.
                "client": client_of[m["id"]]["client"],
                "client_inferred": client_of[m["id"]]["inferred"],
                "client_set": client_of[m["id"]]["set"],
            }
            for m in meetings
        ]

    @app.get("/api/meetings/{meeting_id}", dependencies=auth)
    def get_meeting(meeting_id: int, store: Store = Depends(db)):
        meeting = meeting_or_404(store, meeting_id)
        speakers = [
            {k: s[k] for k in s.keys() if k != "embedding"} for s in store.speakers(meeting_id)
        ]
        client = clients.of_meeting(store, meeting)
        return {
            "meeting": {
                **dict(meeting),
                "progress": worker.progress.get(meeting_id),
                "client": client["client"],
                "client_inferred": client["inferred"],
                "client_set": client["set"],
            },
            # The clients of the people in it, and every client named so far (to pick from).
            "clients_heard": client["clients"],
            "clients_known": clients.known(store),
            "speakers": speakers,
            "segments": [dict(row) for row in store.transcript(meeting_id)],
            "summary": store.summary(meeting_id),
            "notes": [dict(row) for row in store.notes(meeting_id)],
            "insights": insights.meeting_insights(store, meeting_id),
            "talk_time": [dict(row) for row in store.talk_time(meeting_id)],
            "action_items": [dict(row) for row in store.action_items(meeting_id=meeting_id)],
            "me": store.me()["name"],
            # Other people's lines that say your name.
            "mentions": mentions.find(store, meeting_id),
            # Ticket numbers found in it, best first, other than the one it has.
            "ticket_suggestions": [
                found
                for found in ticket_candidates(store, meeting_id)
                if found["ticket"] != meeting["ticket"]
            ],
        }

    def ticket_candidates(store: Store, meeting_id: int) -> list[dict]:
        try:
            return tickets.candidates(store, meeting_id)
        except ValueError:  # patterns saved before a stricter check: none found
            return []

    @app.patch("/api/meetings/{meeting_id}", dependencies=auth)
    def change_meeting(meeting_id: int, body: MeetingChange, store: Store = Depends(db)):
        """A new title, or the ticket it was about ("" takes the ticket off)."""
        meeting_or_404(store, meeting_id)
        changes: dict = {}
        if body.title is not None:
            if not body.title.strip():
                raise HTTPException(status_code=400, detail="The title can't be empty")
            changes["title"] = body.title.strip()
        if body.ticket is not None:
            try:
                changes["ticket"] = tickets.checked(body.ticket) or None
                changes["ticket_source"] = tickets.YOURS  # set or taken off: yours to keep
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.client is not None:  # yours to keep; "" is "no client"
            try:
                name = clients.checked(body.client)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            changes["client"] = clients.canonical(store, name) if name else None
            changes["client_source"] = clients.YOURS
        if changes:
            store.update_meeting(meeting_id, **changes)
            if "ticket" not in changes:
                tickets.fill(store, meeting_id)  # a ticket number typed in the title
            export.auto_export(store, meeting_id)
        meeting = store.meeting(meeting_id)
        client = clients.of_meeting(store, meeting)
        return {
            "title": meeting["title"],
            "ticket": meeting["ticket"],
            "client": client["client"],
            "client_inferred": client["inferred"],
            "client_set": client["set"],
        }

    @app.delete("/api/meetings/{meeting_id}", dependencies=auth)
    def delete_meeting(meeting_id: int, store: Store = Depends(db)):
        meeting = meeting_or_404(store, meeting_id)
        folder = Path(meeting["folder"]).resolve()
        store.delete_meeting(meeting_id)
        store.scrub_deleted_text()  # its words leave the search index and the log as well
        # Its copy leaves the export folder too, and it leaves that day's work log.
        export.forget(store, meeting_id, date.fromisoformat(meeting["started_at"][:10]))
        # Only remove audio ZachCap recorded or imported itself, never a folder elsewhere.
        if folder.is_relative_to(paths.meetings_dir().resolve()) and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        return {"deleted": meeting_id}

    @app.get("/api/worklog", dependencies=auth)
    def work_log(
        period: str = "day",
        day: str | None = None,
        client: str | None = None,
        store: Store = Depends(db),
    ):
        """The day's or week's meetings, people, action items and draft time entries (for
        one client, if given)."""
        return digest.build(store, *work_log_period(period, day), client=client or None)

    @app.post("/api/worklog/save", dependencies=auth)
    def save_work_log(body: WorkLogBody, store: Store = Depends(db)):
        if not store.setting("export_folder"):
            raise HTTPException(status_code=400, detail="Choose an export folder in Settings first")
        path = export.save_work_log(
            store, *work_log_period(body.period, body.day), client=body.client or None
        )
        if path is None:
            raise HTTPException(status_code=400, detail="Couldn't write to the export folder")
        return {"path": str(path)}

    @app.post("/api/meetings/{meeting_id}/summary", dependencies=auth)
    def create_summary(
        meeting_id: int, body: SummaryBody | None = None, store: Store = Depends(db)
    ):
        from .summarize import summarize

        meeting_or_404(store, meeting_id)
        try:
            with only_job(meeting_id, "writing its summary"):
                summarize(store, meeting_id, body.template if body else None)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # network, auth, rate limits: show the reason in the app
            raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc
        export.auto_export(store, meeting_id)
        return store.summary(meeting_id)

    @app.post("/api/meetings/{meeting_id}/recipe", dependencies=auth)
    def make_recipe(meeting_id: int, body: RecipeBody, store: Store = Depends(db)):
        """A document made from the meeting: follow-up email, status update, ticket notes."""
        from .summarize import recipe

        meeting_or_404(store, meeting_id)
        try:
            return recipe(store, meeting_id, body.recipe, body.previous, body.change)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Claude request failed: {exc}") from exc

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
        with jobs_lock:
            running = jobs.get(meeting_id)
        if running is not None:
            raise HTTPException(
                status_code=409,
                detail=f"This meeting is busy ({running}). Try again when that's done.",
            )
        if meeting_or_404(store, meeting_id)["audio_removed_at"]:
            raise HTTPException(
                status_code=400,
                detail="This meeting's recording was deleted (Settings, keep recordings), "
                "so it can't be processed again. The transcript is kept.",
            )
        worker.submit(meeting_id)
        return {"queued": True}

    @app.post("/api/meetings/{meeting_id}/speakers/{speaker_id}/name", dependencies=auth)
    def name(meeting_id: int, speaker_id: int, body: NameBody, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="The name can't be empty")
        pipeline.name_speaker(store, meeting_id, str(speaker_id), body.name.strip())
        export.auto_export(store, meeting_id)
        return {"status": store.meeting(meeting_id)["status"]}

    @app.post("/api/meetings/{meeting_id}/confirm", dependencies=auth)
    def confirm(meeting_id: int, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        confirmed = pipeline.confirm_matches(store, meeting_id)
        export.auto_export(store, meeting_id)
        return {"confirmed": confirmed}

    @app.get("/api/meetings/{meeting_id}/audio", dependencies=auth)
    def meeting_audio(meeting_id: int, track: str = "mix", store: Store = Depends(db)):
        if track not in ("mix", "mic", "system"):
            raise HTTPException(status_code=400, detail="track is mix, mic or system")
        folder = Path(meeting_or_404(store, meeting_id)["folder"])
        # Not kept in the app's cache: after a redaction an old copy would still play.
        return FileResponse(
            _playback_file(folder, track), media_type="audio/flac", headers=NO_STORE
        )

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
        return Response(buffer.getvalue(), media_type="audio/wav", headers=NO_STORE)

    # exports ------------------------------------------------------------------------

    def check_format(fmt: str) -> None:
        if fmt not in export.FORMATS:
            raise HTTPException(
                status_code=400, detail=f"format is one of {', '.join(export.FORMATS)}"
            )

    @app.get("/api/meetings/{meeting_id}/export", dependencies=auth)
    def export_meeting(meeting_id: int, format: str = "md", store: Store = Depends(db)):
        """The meeting as a file, for a download."""
        from urllib.parse import quote

        meeting_or_404(store, meeting_id)
        check_format(format)
        name = export.file_name(store, meeting_id, format)
        return Response(
            export.render(store, meeting_id, format),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        )

    @app.post("/api/meetings/{meeting_id}/export", dependencies=auth)
    def export_to_file(meeting_id: int, body: ExportBody, store: Store = Depends(db)):
        """Write the meeting to the path the user picked in the save dialog."""
        meeting_or_404(store, meeting_id)
        check_format(body.format)
        target = Path(body.path)
        if target.suffix.lower() != f".{body.format}" or not target.parent.is_dir():
            raise HTTPException(status_code=400, detail="Pick a file name in an existing folder")
        try:
            target.write_text(export.render(store, meeting_id, body.format), encoding="utf-8")
        except OSError as exc:  # read-only, open in another app, a folder by that name
            raise HTTPException(
                status_code=400, detail=f"Couldn't write {target.name}: {exc.strerror or exc}"
            ) from exc
        return {"path": str(target)}

    @app.get("/api/meetings/{meeting_id}/export-name", dependencies=auth)
    def export_name(meeting_id: int, format: str = "md", store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        check_format(format)
        return {"name": export.file_name(store, meeting_id, format)}

    # notes and bookmarks ----------------------------------------------------------

    def check_note(body: NoteBody) -> str:
        if body.kind not in ("note", "bookmark"):
            raise HTTPException(status_code=400, detail="kind is note or bookmark")
        text = body.text.strip()
        if body.kind == "note" and not text:
            raise HTTPException(status_code=400, detail="Write something first")
        if len(text) > 2000:
            raise HTTPException(status_code=400, detail="Keep a note under 2,000 characters")
        return text

    @app.post("/api/meetings/{meeting_id}/notes", dependencies=auth)
    def add_note(meeting_id: int, body: NoteBody, store: Store = Depends(db)):
        meeting_or_404(store, meeting_id)
        text = check_note(body)
        note_id = store.add_note(meeting_id, max(0.0, body.at or 0.0), body.kind, text)
        tickets.fill(store, meeting_id)
        export.auto_export(store, meeting_id)
        return dict(store.note(note_id))

    @app.patch("/api/notes/{note_id}", dependencies=auth)
    def edit_note(note_id: int, body: NoteTextBody, store: Store = Depends(db)):
        if store.note(note_id) is None:
            raise HTTPException(status_code=404, detail="No such note")
        store.update_note(note_id, body.text.strip()[:2000])
        tickets.fill(store, store.note(note_id)["meeting_id"])
        export.auto_export(store, store.note(note_id)["meeting_id"])
        return dict(store.note(note_id))

    @app.delete("/api/notes/{note_id}", dependencies=auth)
    def remove_note(note_id: int, store: Store = Depends(db)):
        from . import snapshot

        note = store.note(note_id)
        store.delete_note(note_id)
        if note is not None:
            if note["kind"] == "snapshot":  # its picture goes with it
                meeting = store.meeting(note["meeting_id"])
                if meeting is not None:
                    snapshot.remove_pictures(Path(meeting["folder"]), note_id)
            tickets.fill(store, note["meeting_id"])  # a ticket that came from it goes too
            export.auto_export(store, note["meeting_id"])
        return {"deleted": note_id}

    @app.get("/api/notes/{note_id}/image", dependencies=auth)
    def note_image(note_id: int, size: str = "full", store: Store = Depends(db)):
        """A snapshot's picture (size "thumb": the small one the notes list shows)."""
        from . import snapshot

        note = store.note(note_id)
        meeting = store.meeting(note["meeting_id"]) if note is not None else None
        if meeting is None:
            raise HTTPException(status_code=404, detail="No picture for that note")
        folder = Path(meeting["folder"])
        thumb = snapshot.thumbnail_path(folder, note_id)
        if size == "thumb" and thumb.exists():
            return FileResponse(thumb, media_type="image/jpeg", headers=NO_STORE)
        path = snapshot.image_path(folder, note_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="No picture for that note")
        return FileResponse(path, media_type="image/png", headers=NO_STORE)

    @app.post("/api/record/snapshot", dependencies=auth)
    def record_snapshot(store: Store = Depends(db)):
        """A snapshot of the call window, with the text Windows reads in it, as a note at
        this moment of the recording."""
        from . import snapshot

        # Read together, under the lock: a Stop at the same moment means "not recording".
        with lock:
            recorder = recording.get("recorder")
            meeting_id = recording.get("meeting_id")
        if recorder is None or meeting_id is None:
            raise HTTPException(status_code=409, detail="Not recording")
        meeting = store.meeting(meeting_id)
        if meeting is None:  # stopped and deleted at that very moment
            raise HTTPException(status_code=409, detail="Not recording")
        note_id = store.add_note(meeting_id, recorder.elapsed, "snapshot", "")
        try:
            keys = shortcuts.current(store)["snapshot"]
            shot = snapshot.take(Path(meeting["folder"]), note_id, keys)
        except snapshot.SnapshotError as exc:  # nothing right to capture: say why
            store.delete_note(note_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            store.delete_note(note_id)
            snapshot.remove_pictures(Path(meeting["folder"]), note_id)
            raise HTTPException(status_code=400, detail=f"Couldn't take a snapshot: {exc}") from exc
        store.update_note(note_id, shot["text"])
        tickets.fill(store, meeting_id)  # the ticket's own window, snapshotted
        with lock:
            if recording.get("meeting_id") == meeting_id:
                recording["notes"] = recording.get("notes", 0) + 1
        return {
            **dict(store.note(note_id)),
            "window": shot["window"],
            "lines": len([line for line in shot["text"].splitlines() if line.strip()]),
        }

    @app.post("/api/segments/{segment_id}/speaker", dependencies=auth)
    def reassign_segment(segment_id: int, body: LineSpeakerBody, store: Store = Depends(db)):
        name = (body.name or "").strip()
        if body.speaker_id is None and not name:
            raise HTTPException(status_code=400, detail="The name can't be empty")
        try:
            meeting_id = store.reassign_segment(
                segment_id, name=name or None, speaker_id=body.speaker_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        export.auto_export(store, meeting_id)
        return {"ok": True}

    @app.patch("/api/segments/{segment_id}", dependencies=auth)
    def edit_line(segment_id: int, body: LineTextBody, store: Store = Depends(db)):
        """Fix a line's words. Offers the Words to know entry that would fix the same
        mishearing in future meetings, when the edit looks like one."""
        segment = store.segment(segment_id)
        if segment is None:
            raise HTTPException(status_code=404, detail="No such line")
        refuse_if_busy(store.meeting(segment["meeting_id"]))
        text = " ".join(body.text.split())
        if not text:
            raise HTTPException(status_code=400, detail="A line can't be empty")
        if len(text) > 4000:
            raise HTTPException(status_code=400, detail="Keep a line under 4,000 characters")
        suggestion = words.suggest(segment["text"], text, store.setting("vocabulary"))
        store.edit_segment(segment_id, text)
        export.auto_export(store, segment["meeting_id"])
        return {"text": text, "suggestion": suggestion}

    @app.post("/api/vocabulary/correction", dependencies=auth)
    def add_to_words_to_know(body: CorrectionBody, store: Store = Depends(db)):
        try:
            merged = words.add_correction(store.setting("vocabulary"), body.term, body.misheard)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.set_setting("vocabulary", merged)
        return {"vocabulary": merged}

    @app.post("/api/meetings/{meeting_id}/redact", dependencies=auth)
    def redact_moment(meeting_id: int, body: RedactBody, store: Store = Depends(db)):
        """Silence start..end in the recording and remove what was said from the transcript
        and search (a password or code read aloud). Can't be undone."""
        meeting = meeting_or_404(store, meeting_id)
        refuse_if_busy(meeting)
        start, end = round(max(0.0, body.start), 2), round(body.end, 2)
        if end <= start:
            raise HTTPException(status_code=400, detail="The end must come after the start")
        if end - start > 15 * 60:
            raise HTTPException(status_code=400, detail="Redact at most 15 minutes at a time")
        if meeting["duration"] and start >= meeting["duration"]:
            raise HTTPException(status_code=400, detail="That's after the end of the meeting")
        folder = Path(meeting["folder"])
        # Hold the player's locks: a mix being built now would bring the words back.
        with _playback_locks_guard:
            locks = [
                _playback_locks.setdefault(folder / name, threading.Lock())
                for name in redact.PLAYBACK
            ]
        for lock in locks:
            lock.acquire()
        try:
            with only_job(meeting_id, "redacting"):
                result = redact.redact(
                    store, meeting_id, start, end, with_audio=not meeting["audio_removed_at"]
                )
        except PermissionError as exc:
            raise HTTPException(
                status_code=409,
                detail="The recording is open in another program. Close it and try again.",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Couldn't rewrite the audio: {exc.strerror or exc}"
            ) from exc
        finally:
            for lock in locks:
                lock.release()
        tickets.fill(store, meeting_id)  # a ticket found in what was redacted goes too
        export.auto_export(store, meeting_id)
        return {
            **result,
            "start": start,
            "end": end,
            "audio_removed": bool(meeting["audio_removed_at"]),
            "has_summary": store.summary(meeting_id) is not None,
        }

    @app.post("/api/import", dependencies=auth)
    def import_recording(body: ImportBody, store: Store = Depends(db)):
        source = Path(body.path)
        if not source.is_file():
            raise HTTPException(status_code=400, detail="That file doesn't exist")
        try:
            meeting_id, _ = imports.import_recording(
                store, source, body.title, body.start, body.end
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't read the audio: {exc}") from exc
        worker.submit(meeting_id)
        return {"meeting_id": meeting_id}

    @app.post("/api/watch-folder/retry", dependencies=auth)
    def watch_folder_retry(store: Store = Depends(db)):
        """Give the files that couldn't be imported another go, on the next look."""
        return {"retrying": app.state.folder_watch.retry_failed(store)}

    @app.get("/api/watch-folder/files", dependencies=auth)
    def watch_folder_files(path: str):
        """How many recordings a folder holds now (to ask whether to import them too)."""
        folder = Path(path)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail="That folder doesn't exist")
        return {"count": len(watchfolder.media_files(folder))}

    # action items ---------------------------------------------------------------

    @app.get("/api/actions", dependencies=auth)
    def actions(status: str = "all", store: Store = Depends(db)):
        return {"me": store.me()["name"], "items": [dict(r) for r in store.action_items(status)]}

    @app.patch("/api/actions/{item_id}", dependencies=auth)
    def set_action(item_id: int, body: StatusBody, store: Store = Depends(db)):
        if body.status not in ("open", "done"):
            raise HTTPException(status_code=400, detail="status is open or done")
        meeting_id = store.set_action_status(item_id, body.status)
        if meeting_id is not None:
            export.auto_export(store, meeting_id)
        return {"id": item_id, "status": body.status}

    # people -------------------------------------------------------------------

    @app.get("/api/people", dependencies=auth)
    def people(store: Store = Depends(db)):
        return [dict(p) for p in store.people_stats()]

    @app.patch("/api/people/{person_id}", dependencies=auth)
    def rename_person(person_id: int, body: NameBody, store: Store = Depends(db)):
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="The name can't be empty")
        into = store.person_id(body.name.strip(), create=False)
        if into is not None and into != person_id:  # a merge: the client goes with them
            merged, keeping = store.person(person_id), store.person(into)
            if merged and keeping and merged["org"]:
                if not keeping["org"]:
                    store.set_client(into, merged["org"])
                elif keeping["org"].casefold() != merged["org"].casefold():
                    clients.keep_history(store, person_id)
        kept = store.rename_person(person_id, body.name.strip())
        export_soon(store, store.person_meetings(kept))
        return {"id": kept}

    @app.put("/api/people/{person_id}/client", dependencies=auth)
    def set_person_client(person_id: int, body: ClientBody, store: Store = Depends(db)):
        """The client a person works for ("" for none): their meetings follow."""
        if store.person(person_id) is None:
            raise HTTPException(status_code=404, detail="No such person")
        try:
            client = clients.checked(body.client)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        client = clients.canonical(store, client) if client else ""
        before = store.person(person_id)["org"]
        if before and before.casefold() != client.casefold():
            clients.keep_history(store, person_id)  # their past meetings stay as they were
        store.set_client(person_id, client)
        export_soon(store, store.person_meetings(person_id))
        return {"client": client or None}

    @app.get("/api/clients", dependencies=auth)
    def list_clients(store: Store = Depends(db)):
        return clients.known(store)

    @app.delete("/api/people/{person_id}/voice", dependencies=auth)
    def forget_voice(person_id: int, store: Store = Depends(db)):
        store.forget_voice(person_id)
        return {"ok": True}

    @app.delete("/api/people/{person_id}", dependencies=auth)
    def delete_person(person_id: int, store: Store = Depends(db)):
        meetings = store.person_meetings(person_id)
        clients.keep_history(store, person_id)  # their meetings keep the client they had
        store.delete_person(person_id)
        export_soon(store, meetings)
        return {"ok": True}

    @app.get("/api/search", dependencies=auth)
    def search(q: str, store: Store = Depends(db)):
        # A ticket number finds the meetings about that ticket first.
        about = [
            {
                "meeting_id": m["id"],
                "title": m["title"],
                "start": 0,
                "speaker": "Ticket",
                "snippet": f"[{m['ticket']}]",
            }
            for m in store.meetings()
            if m["ticket"] and m["ticket"] in tickets.words_of(q)
        ]
        try:
            return about + [dict(hit) for hit in store.search(q)]
        except Exception:  # FTS syntax errors from half-typed queries
            return about

    # recording ----------------------------------------------------------------

    @app.get("/api/record", dependencies=auth)
    def record_status():
        recorder = recording.get("recorder")
        if recorder is None:
            return {"recording": False}
        watch = recording.get("watch")
        return {
            "recording": True,
            "title": recording.get("title"),
            "elapsed": round(recorder.elapsed, 1),
            "levels": recorder.levels(),
            "devices": {name: t.device["name"] for name, t in recorder.tracks.items()},
            "errors": recorder.errors(),
            "paused": recorder.paused,
            "notes": recording.get("notes", 0),
            # Warnings, whether the call app still holds the mic, and how long it's been quiet
            # (for stopping when the call ends).
            **(watch.status() if watch is not None else {}),
        }

    @app.post("/api/record/pause", dependencies=auth)
    def record_pause(body: PauseBody):
        """A private pause: the recording keeps running but stores silence."""
        recorder = recording.get("recorder")
        if recorder is None:
            raise HTTPException(status_code=409, detail="Not recording")
        recorder.pause(body.paused)
        return {"paused": recorder.paused}

    @app.post("/api/record/note", dependencies=auth)
    def record_note(body: NoteBody, store: Store = Depends(db)):
        """A note or bookmark at this moment of the recording."""
        recorder = recording.get("recorder")
        if recorder is None:
            raise HTTPException(status_code=409, detail="Not recording")
        text = check_note(body)
        note_id = store.add_note(recording["meeting_id"], recorder.elapsed, body.kind, text)
        tickets.fill(store, recording["meeting_id"])
        recording["notes"] = recording.get("notes", 0) + 1
        return dict(store.note(note_id))

    @app.post("/api/record/start", dependencies=auth)
    def record_start(body: RecordBody, store: Store = Depends(db)):
        from .capture import Recorder

        with lock:
            if recording.get("recorder") is not None:
                raise HTTPException(status_code=409, detail="Already recording")
            if recording.get("checking"):
                raise HTTPException(status_code=409, detail="A sound check is running")
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
            if dictator.holding:  # started mid-dictation: that's left out too
                recorder.mute_mic(True)
            # The meeting exists from the first second, so a crash can't lose it.
            title = body.title or f"Meeting {datetime.now():%b %d %H:%M}"
            started = recorder.started_at or datetime.now().astimezone()
            try:
                meeting_id = store.create_meeting(
                    title, started.isoformat(timespec="seconds"), folder, status="recording"
                )
            except Exception as exc:  # the database busy or full: don't leave it recording
                try:
                    recorder.stop()
                except Exception:
                    traceback.print_exc()
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(
                    status_code=500, detail=f"Couldn't start recording: {exc}"
                ) from exc
            # Warnings while it runs, call-end detection, and no sleep until it stops.
            watch = Watch(recorder, folder)
            try:
                watch.start()
            except Exception:  # never worth losing the recording over
                traceback.print_exc()
            recording.update(
                recorder=recorder, meeting_id=meeting_id, title=title, notes=0, watch=watch
            )
        return {"recording": True, "meeting_id": meeting_id}

    @app.post("/api/record/stop", dependencies=auth)
    def record_stop():
        meeting_id = finish_recording()
        if meeting_id is None:
            raise HTTPException(status_code=409, detail="Not recording")
        worker.submit(meeting_id)
        return {"meeting_id": meeting_id}

    @app.post("/api/soundcheck", dependencies=auth)
    def sound_check(store: Store = Depends(db)):
        """A few seconds recorded as a meeting would be, with a short Windows sound, to show
        the microphone and the computer's audio are both heard. Nothing is kept."""
        from . import soundcheck

        with lock:
            if recording.get("recorder") is not None:
                raise HTTPException(status_code=409, detail="Not while recording")
            if recording.get("checking"):
                raise HTTPException(status_code=409, detail="A sound check is already running")
            recording["checking"] = True
        try:
            result = soundcheck.run(
                mic_name=store.setting("mic_device") or None,
                output_name=store.setting("output_device") or None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"The sound check failed: {exc}") from exc
        finally:
            recording.pop("checking", None)
        if result["mic"]["heard"] and result["system"]["heard"]:
            store.set_setting(
                soundcheck.SETTING, datetime.now().astimezone().isoformat(timespec="seconds")
            )
        return result

    # Dictation ------------------------------------------------------------------------

    @app.post("/api/dictate/start", dependencies=auth)
    def dictate_start(store: Store = Depends(db)):
        """The dictation keys went down: listen. Returns the dictation's number for stop."""
        if store.setting(dictation.SETTING) != "1":
            raise HTTPException(status_code=400, detail="Dictation is off (Settings)")
        try:
            number = dictator.start(store.setting("mic_device") or None)
        except dictation.NotHere as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't open the mic: {exc}") from exc
        return {"listening": True, "id": number}

    @app.post("/api/dictate/stop", dependencies=auth)
    def dictate_stop(body: DictateBody | None = None, store: Store = Depends(db)):
        """The keys came up: transcribe what was said and type it where the cursor is."""
        body = body or DictateBody()
        vocab = words.parse(store.setting("vocabulary"))
        try:
            snippets = dictation.snippets(store.setting(dictation.SNIPPETS))
        except ValueError:  # saved before a stricter check: none rather than none typed
            snippets = {}
        names = [p["name"] for p in store.people() if p["name"].lower() not in {"you", "me"}]
        try:
            return dictator.finish(
                body.id,
                language_of(store),
                vocab.hotwords(names),
                fix=vocab.correct,
                type_it=body.type,
                snippets=snippets,
            )
        except dictation.NotListening as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # the speech model or the mic: say what went wrong
            raise HTTPException(status_code=500, detail=f"Dictation failed: {exc}") from exc

    @app.post("/api/dictate/cancel", dependencies=auth)
    def dictate_cancel(body: DictateBody | None = None):
        """Stops listening without typing: one dictation, or all of them (a page that loads
        drops any its last copy left listening)."""
        dictator.cancel(body.id if body else None)
        return {"listening": dictator.listening}

    @app.get("/api/dictate/recent", dependencies=auth)
    def dictate_recent():
        """The last few dictations, newest first, in case one went astray: kept in memory
        only, never on disk."""
        return JSONResponse(list(dictator.recent), headers=NO_STORE)

    @app.post("/api/dictate/recent/{entry_id}/copy", dependencies=auth)
    def copy_dictation(entry_id: int):
        """Copies a recent dictation the way dictation itself does: kept out of clipboard
        history and the cloud clipboard."""
        entry = next((e for e in dictator.recent if e["id"] == entry_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="That dictation is gone")
        if not dictation.copy_to_clipboard(entry["text"]):
            raise HTTPException(status_code=409, detail="Another app has the clipboard open")
        return {"copied": True}

    @app.delete("/api/dictate/recent", dependencies=auth)
    def forget_dictations():
        dictator.forget_recent()
        return {"forgotten": True}

    @app.get("/api/call", dependencies=auth)
    def call():
        """Apps using the microphone right now, so the app can offer to record."""
        from .calls import apps_using_mic

        try:
            return {"apps": apps_using_mic()}
        except OSError:
            return {"apps": []}

    @app.get("/api/mcp", dependencies=auth)
    def mcp_config():
        """How to add ZachCap to Claude Desktop or Claude Code: this engine's own Python
        runs `zachcap mcp`, with the same data folder."""
        import os
        import sys

        args = ["-m", "zachcap.cli", "mcp"]
        env = {"ZACHCAP_HOME": os.environ["ZACHCAP_HOME"]} if os.environ.get("ZACHCAP_HOME") else {}
        server = {"command": sys.executable, "args": args, **({"env": env} if env else {})}
        flags = "".join(f' -e {key}="{value}"' for key, value in env.items())
        return {
            "claude_desktop": json.dumps({"mcpServers": {"zachcap": server}}, indent=2),
            "claude_code": f'claude mcp add zachcap{flags} -- "{sys.executable}" {" ".join(args)}',
        }

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

    @app.get("/api/prompts", dependencies=auth)
    def get_prompts(store: Store = Depends(db)):
        """Kinds of meeting and one-click documents, built-in and the user's own, with their
        instructions."""
        return {
            "templates": prompts.library(store, "template"),
            "recipes": prompts.library(store, "recipe"),
            "limits": {"label": prompts.LABEL_MAX, "instructions": prompts.INSTRUCTIONS_MAX},
        }

    def prompt_change(change: Callable[[], dict | None]) -> dict | None:
        try:
            return change()
        except prompts.UnknownPrompt as exc:
            raise HTTPException(
                status_code=404, detail="No such kind of meeting or document"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/prompts", dependencies=auth)
    def add_prompt(body: PromptBody, store: Store = Depends(db)):
        return prompt_change(lambda: prompts.add(store, body.kind, body.label, body.instructions))

    @app.patch("/api/prompts/{prompt_id}", dependencies=auth)
    def change_prompt(prompt_id: str, body: PromptChangeBody, store: Store = Depends(db)):
        return prompt_change(
            lambda: prompts.update(store, prompt_id, body.label, body.instructions)
        )

    @app.post("/api/prompts/{prompt_id}/reset", dependencies=auth)
    def reset_prompt(prompt_id: str, store: Store = Depends(db)):
        """A built-in kind or document back to how ZachCap ships it."""
        return prompt_change(lambda: prompts.reset(store, prompt_id))

    @app.delete("/api/prompts/{prompt_id}", dependencies=auth)
    def delete_prompt(prompt_id: str, store: Store = Depends(db)):
        prompt_change(lambda: prompts.delete(store, prompt_id))
        return {"deleted": prompt_id}

    def watch_failures(store: Store) -> list[dict]:
        """From the watch folder's last look: Settings never reads the folder itself."""
        if not store.setting(watchfolder.SETTING):
            return []
        return list(app.state.folder_watch.failures)

    @app.get("/api/settings", dependencies=auth)
    def get_settings(store: Store = Depends(db)):
        from .summarize import MODELS, PRICES, meeting_template, summary_model

        # Not cached by the page: it holds your snippets and words.
        return JSONResponse(
            headers=NO_STORE,
            content={
                "summary_model": summary_model(store),
                "summary_models": [
                    {
                        "id": model,
                        "label": label,
                        "input_usd": PRICES[model][0],
                        "output_usd": PRICES[model][1],
                    }
                    for model, label in MODELS.items()
                ],
                # The usual kind, as a summary would use it (a deleted one reads as general).
                "summary_template": meeting_template(store, {"template": None}),
                "export_folder": store.setting("export_folder", ""),
                "watch_folder": store.setting(watchfolder.SETTING, ""),
                # Files there that couldn't be imported, newest first, with the reason.
                "watch_folder_failed": watch_failures(store),
                "also_answer_to": store.setting(mentions.ALSO_ANSWER_TO, ""),
                "keep_audio_days": int(store.setting("keep_audio_days", "0") or 0),
                "mask_before_claude": store.setting("mask_before_claude") == "1",
                # Offer to stop (with a countdown) when the call ends or after a long silence.
                "stop_when_call_ends": store.setting("stop_when_call_ends", "1") == "1",
                "export_work_log": store.setting("export_work_log") == "1",
                "dictation": store.setting(dictation.SETTING) == "1",
                "dictation_snippets": store.setting(dictation.SNIPPETS, ""),
                "shortcuts": shortcuts.current(store),
                "ticket_patterns": store.setting(tickets.SETTING, ""),
                "soundcheck_passed": store.setting("soundcheck_passed"),
                "templates": prompts.choices(store, "template"),
                "recipes": prompts.choices(store, "recipe"),
                "language": store.setting("language", "en"),
                "languages": [{"id": k, "label": v} for k, v in LANGUAGES.items()],
                "mic_device": store.setting("mic_device", ""),
                "output_device": store.setting("output_device", ""),
                "vocabulary": store.setting("vocabulary", ""),
                "me": dict(store.me()),
            },
        )

    @app.put("/api/settings", dependencies=auth)
    def put_settings(body: SettingsBody, store: Store = Depends(db)):
        from .summarize import MODELS

        if body.shortcuts:  # first: keys that can't be used change nothing
            try:
                shortcuts.change(store, body.shortcuts)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.ticket_patterns is not None:
            written = ", ".join(p.strip() for p in body.ticket_patterns.split(",") if p.strip())
            try:
                tickets.patterns(written)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            store.set_setting(tickets.SETTING, written)
            changed = []
            for meeting in store.meetings():  # tickets in titles and notes follow the patterns
                tickets.fill(store, meeting["id"])
                if store.meeting(meeting["id"])["ticket"] != meeting["ticket"]:
                    changed.append(meeting["id"])
            export_soon(store, changed)
        if body.summary_template is not None:
            if prompts.get(store, "template", body.summary_template) is None:
                raise HTTPException(status_code=400, detail="Unknown kind of meeting")
            store.set_setting("summary_template", body.summary_template)
        if body.keep_audio_days is not None:
            if body.keep_audio_days not in KEEP_AUDIO_CHOICES:
                raise HTTPException(
                    status_code=400, detail="Keep audio for 0, 7, 30, 90 or 365 days"
                )
            store.set_setting("keep_audio_days", str(body.keep_audio_days))
            privacy.remove_old_audio(store)
        if body.mask_before_claude is not None:
            store.set_setting("mask_before_claude", "1" if body.mask_before_claude else "0")
        if body.stop_when_call_ends is not None:
            store.set_setting("stop_when_call_ends", "1" if body.stop_when_call_ends else "0")
        if body.dictation_snippets is not None:
            try:
                dictation.snippets(body.dictation_snippets)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            store.set_setting(dictation.SNIPPETS, body.dictation_snippets.strip())
        if body.dictation is not None:
            store.set_setting(dictation.SETTING, "1" if body.dictation else "0")
            if body.dictation:  # loaded now, so the first dictation is quick
                dictator.transcriber.warm_up(language_of(store))
            else:  # turned off mid-dictation: the keys' release won't come
                dictator.cancel()
                dictator.forget_recent()
        if body.export_work_log is not None:
            store.set_setting("export_work_log", "1" if body.export_work_log else "0")
            if body.export_work_log:  # today's and this week's, right away
                export.refresh_work_logs(store, date.today())
        if body.export_folder is not None:
            folder = body.export_folder.strip()
            if folder and not Path(folder).is_dir():
                raise HTTPException(status_code=400, detail="That folder doesn't exist")
            store.set_setting("export_folder", folder)
            if folder:  # bring the folder up to date with what's already here
                for meeting in store.meetings():
                    export.auto_export(store, meeting["id"])
        if body.watch_folder is not None:
            folder = body.watch_folder.strip()
            if folder and not Path(folder).is_dir():
                raise HTTPException(status_code=400, detail="That folder doesn't exist")
            if folder:
                # Only new recordings by default; or give the ones there now a go as well.
                if body.watch_existing:
                    watchfolder.retry_existing(store, Path(folder))
                else:
                    watchfolder.skip_existing(store, Path(folder))
            changed = folder != store.setting(watchfolder.SETTING, "")
            store.set_setting(watchfolder.SETTING, folder)
            if changed:  # a look under way stops between files; the next starts over
                app.state.folder_watch.reset()
        if body.also_answer_to is not None:
            if len(body.also_answer_to) > 400:
                raise HTTPException(status_code=400, detail="Keep the names under 400 characters")
            store.set_setting(
                mentions.ALSO_ANSWER_TO, ", ".join(mentions.aliases(body.also_answer_to))
            )
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
                mix[: len(t)] += audio.even_out(t)  # a quiet call as loud as your mic
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
    # Recordings saved in the watch folder import themselves (off until a folder is chosen).
    # Started here rather than in create_app, so tests never leave one running.
    threading.Thread(
        target=app.state.folder_watch.run_forever,
        name="zachcap-watch-folder",
        daemon=True,
    ).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
