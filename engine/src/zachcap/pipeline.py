"""Turn a recorded meeting folder into a speaker-labeled transcript in the database.

Steps: load both tracks -> Whisper words -> pyannote speaker turns -> utterances
-> drop mic echo -> match speakers to known voices -> store.
Models load one at a time and are freed before the next (the GPU has 4 GB).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import apikeys, audio, transcript, vocabulary, voiceprint
from .store import Store

MIN_SPEAKER_SECONDS = 1.5  # shorter clusters are usually noise or cross-talk
SILENT_TRACK_DBFS = -65.0

Log = Callable[[str], None]
SpeakerKey = tuple[str, str]  # (track, diarization label)


def process(store: Store, meeting_id: int, log: Log = print) -> str:
    """Transcribe and label a meeting. The new results replace the old ones in one step,
    only once everything worked: a failed reprocess keeps the last good transcript."""
    meeting = store.meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"No meeting {meeting_id}")
    previous = meeting["status"]
    had_results = store.has_results(meeting_id)
    store.update_meeting(meeting_id, status="processing")
    started = time.perf_counter()
    try:
        tracks = _load_tracks(Path(meeting["folder"]), log)
        duration = max(len(a) for a in tracks.values()) / audio.RATE
        vocab = vocabulary.parse(store.setting("vocabulary"))
        words = _transcribe(store, tracks, duration, log, vocab)
        diarized, separated = _diarize(tracks, words, log)

        utterances = {
            name: transcript.build_utterances(name, words[name], result.turns)
            for name, result in diarized.items()
        }
        if "mic" in utterances and "system" in utterances:
            utterances["mic"], echo = transcript.drop_echo(utterances["mic"], utterances["system"])
            if echo:
                log(f"dropped {len(echo)} mic lines that were echo of the speakers")

        from .diarize import EMBEDDING_MODEL

        # Words to know are applied before the write below, which locks the database.
        spelled = {u.text: vocab.correct(u.text) for utts in utterances.values() for u in utts}

        def fix(text: str) -> str:
            return spelled.get(text, text)

        with store.transaction():
            if store.meeting(meeting_id) is None:
                log("the meeting was deleted while it was being processed")
                return "deleted"
            store.clear_results(meeting_id)
            if separated:
                _store_results(store, meeting_id, utterances, diarized, EMBEDDING_MODEL, fix)
            else:
                _store_unseparated(store, meeting_id, utterances, fix)
            store.update_meeting(meeting_id, duration=round(duration, 3))
            status = store.refresh_status(meeting_id)
        done = f"done in {time.perf_counter() - started:.0f} s"
        log(done if separated else f"{done}; add a Hugging Face token, then Reprocess for names")
        return status
    except Exception:
        if store.meeting(meeting_id) is not None:
            keep = had_results and previous in ("ready", "needs_names")
            store.update_meeting(meeting_id, status=previous if keep else "failed")
        raise


def _transcribe(
    store: Store, tracks: dict, duration: float, log: Log, vocab: vocabulary.Vocabulary
) -> dict:
    from .asr import Transcriber

    log(f"transcribing {duration / 60:.1f} min of audio")
    started = time.perf_counter()
    # The words to know, then everyone the user has named, so their names come out right.
    names = [p["name"] for p in store.people() if p["name"].lower() not in {"you", "me"}]
    hotwords = vocab.hotwords(names)
    asr = Transcriber()
    try:
        language = store.setting("language", "en")
        language = None if language == "auto" else language
        words = {
            name: asr.words(a, language=language, hotwords=hotwords) for name, a in tracks.items()
        }
    finally:
        asr.close()  # frees the GPU for the speaker model, even after an error
    log(
        f"transcribed {duration / 60:.1f} min on {asr.device} "
        f"in {time.perf_counter() - started:.0f} s"
    )
    return words


def _diarize(tracks: dict, words: dict, log: Log) -> tuple[dict, bool]:
    """Speaker turns per track, and whether speakers were really separated."""
    from .diarize import Diarization, Diarizer, SpeakerModelUnavailable

    log("separating speakers")
    started = time.perf_counter()
    try:
        diarizer = Diarizer(apikeys.get("huggingface"))
    except SpeakerModelUnavailable as exc:
        # Still useful without it: one speaker per track ("You" and "Call audio").
        log(f"speaker separation unavailable: {exc}")
        return {
            name: Diarization([transcript.Turn(0.0, len(a) / audio.RATE, name)], {})
            for name, a in tracks.items()
            if words[name]
        }, False
    try:
        diarized = {name: diarizer.run(a) for name, a in tracks.items() if words[name]}
    finally:
        diarizer.close()
    log(f"separated speakers in {time.perf_counter() - started:.0f} s")
    return diarized, True


def name_speaker(store: Store, meeting_id: int, speaker_ref: str, name: str) -> None:
    """Name a meeting speaker (or confirm a suggestion) and learn their voice."""
    from .diarize import EMBEDDING_MODEL

    speaker = resolve_speaker(store, meeting_id, speaker_ref)
    person_id = store.me()["id"] if name.strip().lower() in {"you", "me"} else store.person_id(name)
    store.set_speaker_person(speaker["id"], person_id, "confirmed")
    if speaker["embedding"] is not None:
        store.add_voice_sample(
            person_id,
            meeting_id,
            voiceprint.from_blob(speaker["embedding"]),
            EMBEDDING_MODEL,
            speaker["speech_seconds"] or 0.0,
        )
    store.refresh_status(meeting_id)


def confirm_matches(store: Store, meeting_id: int) -> int:
    """Accept every automatic match and suggestion in a meeting; returns how many."""
    confirmed = 0
    for speaker in store.speakers(meeting_id):
        if speaker["status"] in ("auto", "suggested") and speaker["person_name"]:
            name_speaker(store, meeting_id, str(speaker["id"]), speaker["person_name"])
            confirmed += 1
    return confirmed


def resolve_speaker(store: Store, meeting_id: int, ref: str):
    """A speaker by its id number or its label ("Speaker 2")."""
    for speaker in store.speakers(meeting_id):
        if ref == str(speaker["id"]) or ref.lower() == speaker["label"].lower():
            return speaker
    raise ValueError(f"Meeting {meeting_id} has no speaker {ref!r}")


def _load_tracks(folder: Path, log: Log) -> dict[str, np.ndarray]:
    tracks = {}
    found = False
    for name in ("mic", "system"):
        data = audio.load_track(folder, name)
        if data is None:
            continue
        found = True
        if audio.dbfs(data) <= SILENT_TRACK_DBFS:
            log(f"{name} track is silent, skipped")
            continue
        tracks[name] = data
    if not found:
        raise RuntimeError(f"This meeting's audio is missing. ZachCap expected it in {folder}.")
    if not tracks:
        raise RuntimeError(
            "Nothing to transcribe: your microphone and the computer audio were both silent. "
            "Check the devices under Settings, Recording."
        )
    return tracks


def _store_unseparated(
    store: Store, meeting_id: int, utterances, fix: Callable[[str], str] = str
) -> None:
    """Without speaker separation: the mic is you, the computer audio is one "Call audio" voice."""
    me = store.me()
    rows = []
    for track, utts in utterances.items():
        if not utts:
            continue
        seconds = round(sum(u.end - u.start for u in utts), 1)
        if track == "mic":
            match = voiceprint.Match(me["id"], 0.0, "auto")
            label = me["name"]
        else:
            match = voiceprint.Match(None, 0.0, "minor")
            label = "Call audio"
        speaker_id = store.add_speaker(meeting_id, track, label, None, seconds, None, match)
        rows += [
            (
                speaker_id,
                track,
                round(u.start, 2),
                round(u.end, 2),
                fix(u.text),
                round(u.confidence, 3),
            )
            for u in utts
        ]
    store.add_segments(meeting_id, sorted(rows, key=lambda r: r[2]))


def _sample_score(seconds: float) -> float:
    """Prefer 3-10 s clips for "Who's this?": long enough to recognize, short to play."""
    return seconds if seconds <= 10 else 10 - (seconds - 10) * 0.1


def _store_results(
    store: Store, meeting_id: int, utterances, diarized, model: str, fix: Callable[[str], str] = str
) -> None:
    stats: dict[SpeakerKey, dict] = {}
    for track, utts in utterances.items():
        for u in utts:
            if u.speaker is None:
                continue
            s = stats.setdefault(
                (track, u.speaker), {"seconds": 0.0, "first": u.start, "sample": None}
            )
            s["seconds"] += u.end - u.start
            if s["sample"] is None or _sample_score(u.end - u.start) > _sample_score(
                s["sample"][1] - s["sample"][0]
            ):
                s["sample"] = (u.start, u.end)

    keys = sorted(stats, key=lambda k: stats[k]["first"])
    embeddings = {k: diarized[k[0]].embeddings.get(k[1]) for k in keys}
    candidates = {
        k: e
        for k, e in embeddings.items()
        if e is not None and stats[k]["seconds"] >= MIN_SPEAKER_SECONDS
    }
    library = store.voice_library(model)
    matches = voiceprint.match_speakers(candidates, library)

    # First meeting ever: learn your voice from the mic track.
    me = store.me()
    mic_speakers = [k for k in candidates if k[0] == "mic"]
    if not library.get(me["id"]) and mic_speakers:
        main = max(mic_speakers, key=lambda k: stats[k]["seconds"])
        if len(mic_speakers) == 1:
            matches[main] = voiceprint.Match(me["id"], 1.0, "confirmed")
            store.add_voice_sample(
                me["id"], meeting_id, candidates[main], model, stats[main]["seconds"]
            )
        else:
            matches[main] = voiceprint.Match(me["id"], 0.0, "suggested")

    ids: dict[SpeakerKey, int] = {}
    who: dict[int, object] = {}  # meeting speaker id -> who it is (a person, or itself)
    unnamed = 0
    names = {p["id"]: p["name"] for p in store.people()}
    for key in keys:
        match = matches.get(key) or voiceprint.Match(None, 0.0, "minor")
        if match.status in ("auto", "confirmed"):
            label = names[match.person_id]
        else:
            unnamed += 1
            label = f"Speaker {unnamed}"
        ids[key] = store.add_speaker(
            meeting_id,
            key[0],
            label,
            embeddings[key],
            round(stats[key]["seconds"], 1),
            stats[key]["sample"],
            match,
        )
        known = match.status in ("auto", "confirmed")
        who[ids[key]] = ("person", match.person_id) if known else ("speaker", ids[key])

    rows = [
        (
            ids.get((track, u.speaker)),
            track,
            round(u.start, 2),
            round(u.end, 2),
            fix(u.text),
            round(u.confidence, 3),
        )
        for track, utts in utterances.items()
        for u in utts
    ]
    store.add_segments(meeting_id, merge_same_person(sorted(rows, key=lambda r: r[2]), who))


def merge_same_person(rows: list[tuple], who: dict, max_gap: float = 1.5) -> list[tuple]:
    """Join back-to-back lines by the same person on the same track.

    Diarization sometimes splits one voice into two clusters, which both match the
    same person; without this a sentence can break into "The" | "printer is fixed".
    rows: (meeting_speaker_id, track, start, end, text, confidence), sorted by start.
    """
    merged: list[tuple] = []
    for row in rows:
        if merged:
            last = merged[-1]
            same = last[0] is not None and who.get(last[0], last[0]) == who.get(row[0], row[0])
            if same and last[1] == row[1] and row[2] - last[3] <= max_gap:
                merged[-1] = (
                    last[0], last[1], last[2], row[3], f"{last[4]} {row[4]}",
                    round((last[5] + row[5]) / 2, 3),
                )  # fmt: skip
                continue
        merged.append(row)
    return merged
