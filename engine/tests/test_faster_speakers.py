import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import cuda, diarize, pipeline
from zachcap.api import create_app
from zachcap.diarize import Diarization
from zachcap.store import Store
from zachcap.transcript import Turn, Word

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)
AUTH = {"Authorization": "Bearer test-token"}


def test_the_setting_picks_the_step():
    assert diarize.step_for("1") == diarize.FASTER_STEP
    assert diarize.step_for("0") is None
    assert diarize.step_for(None) is None


def test_a_step_is_set_on_both_copies_of_the_window():
    built = SimpleNamespace(
        segmentation_step=0.1, _segmentation=SimpleNamespace(duration=10.0, step=1.0)
    )
    diarize.set_step(built, 0.2)
    assert built.segmentation_step == 0.2
    assert built._segmentation.step == 2.0


def test_the_speaker_model_takes_the_step(monkeypatch: pytest.MonkeyPatch):
    class Pipeline:
        embedding_batch_size = 32
        segmentation_step = 0.1

        def __init__(self) -> None:
            self._embedding = None
            self._segmentation = SimpleNamespace(duration=10.0, step=1.0)

        @classmethod
        def from_pretrained(cls, checkpoint, revision=None, token=None):
            return cls()

        def to(self, device):
            return self

    torch = SimpleNamespace(device=str, cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "pyannote.audio", SimpleNamespace(Pipeline=Pipeline))
    monkeypatch.setattr(cuda, "prepare", lambda: False)
    assert diarize.Diarizer("hf_test").pipeline._segmentation.step == 1.0
    assert diarize.Diarizer("hf_test", step=0.2).pipeline._segmentation.step == 2.0


def test_processing_separates_speakers_with_the_saved_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    asked = []

    def separated(tracks, words, log, step=None):
        asked.append(step)
        return {"system": Diarization([Turn(0.0, 0.5, "system")], {})}, False

    monkeypatch.setattr(pipeline, "_load_tracks", lambda folder, log: {"system": np.zeros(16000)})
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: {"system": [Word(0.0, 0.5, " Hi.")]})
    monkeypatch.setattr(pipeline, "_diarize", separated)
    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path)
    pipeline.process(store, meeting, log=lambda message: None)
    store.set_setting(diarize.SETTING, "1")
    pipeline.process(store, meeting, log=lambda message: None)
    assert asked == [None, diarize.FASTER_STEP]


def test_settings_turns_it_on_and_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    assert client.get("/api/settings", headers=AUTH).json()["faster_speakers"] is False
    for on in (True, False):
        response = client.put("/api/settings", headers=AUTH, json={"faster_speakers": on})
        assert response.json()["faster_speakers"] is on
