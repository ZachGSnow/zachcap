"""The speech models ZachCap downloads from Hugging Face, each pinned to one commit.

A repo's main branch can change under the same name, and pyannote's checkpoints are pickles,
which run code when they load. So every download asks for a fixed commit, and a model only
changes when this file does. To move one on, put the "sha" from
https://huggingface.co/api/models/<repo>/revision/main here (it answers without a token, for
the gated pyannote repo too).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Pin:
    repo: str  # Hugging Face repo id
    revision: str  # a full 40-character commit SHA, never a branch or a tag


# faster-whisper models by the names the engine uses. faster-whisper 1.2 still maps
# large-v3-turbo to mobiuslabsgmbh/faster-whisper-large-v3-turbo, which redirects here.
WHISPER = {
    "large-v3-turbo": Pin(
        "dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
    ),
    "small.en": Pin("Systran/faster-whisper-small.en", "d1d751a5f8271d482d14ca55d9e2deeebbae577f"),
    "small": Pin("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"),
}
# The speaker model. Its config.yaml loads the segmentation, embedding and PLDA models from
# folders of this repo, at the same commit. Voiceprints only compare within one embedding
# model: a commit with new embedding weights needs a new diarize.EMBEDDING_MODEL too.
SPEAKER_MODEL = Pin(
    "pyannote/speaker-diarization-community-1", "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
)


def whisper(name: str, **options):
    """A faster-whisper model: a name above from its pinned commit, or a model folder as it is.
    The options go to WhisperModel (device, compute_type, cpu_threads; local_files_only, to
    load only what's on this PC, else FileNotFoundError)."""
    # Names come first: faster-whisper would load a folder called "small" in the working
    # directory instead of the model.
    if name in WHISPER:
        source, revision = WHISPER[name].repo, WHISPER[name].revision
    elif os.path.isdir(name):
        source, revision = name, None
    else:
        raise ValueError(f"No pinned revision for the Whisper model {name!r}: add it to models.py")
    from faster_whisper import WhisperModel

    return WhisperModel(source, revision=revision, **options)


def download_whisper(name: str) -> None:
    """Downloads a model above at its pinned commit, without loading it: the next load of it
    (whisper) finds it on this PC."""
    pin = WHISPER[name]
    from faster_whisper.utils import download_model

    download_model(pin.repo, revision=pin.revision)
