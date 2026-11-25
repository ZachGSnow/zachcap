# ZachCap engine

Python 3.12 package that does the audio work for ZachCap: capture, transcription, diarization, voiceprint matching and storage.

## Setup

```powershell
uv sync                 # everything, including the GPU speech stack (torch CUDA 12.8,
                        # faster-whisper, pyannote)
uv sync --no-group ml   # light install without the ML stack (CI)

# API keys go to Windows Credential Manager (input is hidden)
uv run zachcap keys set huggingface
uv run zachcap keys set anthropic
uv run zachcap keys status
```

The speech models (Whisper and pyannote) download from Hugging Face on first use, each at the commit pinned in `src/zachcap/models.py`; to move one on, put the `sha` from `https://huggingface.co/api/models/<repo>/revision/main` there.

## Probes

```powershell
# Generate a synthetic two-track meeting with Windows TTS voices
uv run python scripts/make_meeting.py tests/fixtures/meeting1.json tests/fixtures/generated/meeting1

# Record mic + speakers while playing a clip, and check loopback, gaps and echo
uv run python scripts/capture_probe.py --seconds 14 --play tests/fixtures/generated/meeting1/system.wav --out ../.scratch/probe

# Time Whisper on the GPU
uv run python scripts/asr_bench.py tests/fixtures/generated/meeting1/system.wav --reference tests/fixtures/generated/meeting1/reference.json --track system
```
