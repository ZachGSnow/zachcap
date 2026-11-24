"""Speech models download from the commits pinned in models.py, never from whatever is newest.

faster-whisper, torch and pyannote are replaced with stand-ins: nothing is downloaded, and
these run without the GPU stack (CI doesn't install it)."""

import re
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from zachcap import asr, cuda, diarize, dictation, models


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> list:
    """Each Whisper model made, as (repo or folder, revision)."""
    made = []

    class WhisperModel:
        def __init__(self, source, revision=None, **options) -> None:
            made.append((source, revision))

        def transcribe(self, audio, **options):
            return [], None

    whisper = SimpleNamespace(
        WhisperModel=WhisperModel, BatchedInferencePipeline=lambda model: model
    )
    monkeypatch.setitem(sys.modules, "faster_whisper", whisper)
    monkeypatch.setattr(cuda, "prepare", lambda: False)
    return made


@pytest.mark.parametrize(
    "pin", [*models.WHISPER.values(), models.SPEAKER_MODEL], ids=lambda pin: pin.repo
)
def test_every_pin_is_a_repo_and_a_full_commit(pin: models.Pin):
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", pin.repo)
    # A branch, a tag or a short SHA would follow whatever the repo holds next.
    assert re.fullmatch(r"[0-9a-f]{40}", pin.revision)


def test_meeting_transcription_loads_the_pinned_commit(loaded: list):
    asr.Transcriber()
    pin = models.WHISPER[asr.DEFAULT_MODEL]
    assert loaded == [(pin.repo, pin.revision)]


def test_dictation_loads_the_pinned_commits(loaded: list):
    transcriber = dictation.Transcriber()
    audio = np.zeros(dictation.RATE, np.float32)
    transcriber.text(audio, "en", None)
    transcriber.text(audio, None, None)  # detecting the language takes the multilingual model
    english = models.WHISPER[dictation.ENGLISH_MODEL]
    any_language = models.WHISPER[dictation.ANY_LANGUAGE_MODEL]
    assert loaded == [(english.repo, english.revision), (any_language.repo, any_language.revision)]


def test_a_model_folder_still_loads_as_it_is(loaded: list, tmp_path):
    asr.Transcriber(str(tmp_path))
    assert loaded == [(str(tmp_path), None)]


def test_a_folder_named_like_a_model_doesnt_replace_it(
    loaded: list, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "small").mkdir()
    monkeypatch.chdir(tmp_path)
    models.whisper("small")
    pin = models.WHISPER["small"]
    assert loaded == [(pin.repo, pin.revision)]


def test_a_whisper_model_without_a_pin_is_refused(loaded: list):
    with pytest.raises(ValueError, match="No pinned revision"):
        asr.Transcriber("medium")
    assert loaded == []


def test_a_model_downloads_from_its_pinned_commit_without_loading(
    loaded: list, monkeypatch: pytest.MonkeyPatch
):
    fetched = []
    utils = SimpleNamespace(
        download_model=lambda repo, revision=None: fetched.append((repo, revision))
    )
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    models.download_whisper("small.en")
    pin = models.WHISPER["small.en"]
    assert fetched == [(pin.repo, pin.revision)] and loaded == []
    with pytest.raises(KeyError):
        models.download_whisper("medium")  # only a pinned one


def test_the_speaker_model_loads_the_pinned_commit(monkeypatch: pytest.MonkeyPatch):
    asked = []

    class Pipeline:
        embedding_batch_size = 32
        _embedding = None

        @classmethod
        def from_pretrained(cls, checkpoint, revision=None, token=None):
            asked.append((checkpoint, revision, token))
            return cls()

        def to(self, device):
            return self

    torch = SimpleNamespace(device=str, cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "pyannote.audio", SimpleNamespace(Pipeline=Pipeline))
    monkeypatch.setattr(cuda, "prepare", lambda: False)

    diarize.Diarizer("hf_test")

    pin = models.SPEAKER_MODEL
    assert asked == [(pin.repo, pin.revision, "hf_test")]
    # Voiceprints are tagged with the repo, not the commit: ones saved before the pin still match.
    assert diarize.EMBEDDING_MODEL == pin.repo
