# Roadmap

What is open. What has shipped is in the [changelog](../CHANGELOG.md).

## Next

| What | Where it stands |
|---|---|
| A signed installer | Needs a code-signing certificate. Until then SmartScreen warns, and Smart App Control has to be off |
| Tests on more hardware | A live Teams call on a PC with an NVIDIA GPU, a headset plugged in mid-call, a laptop without built-in echo cancellation, Windows 10 |

## Maybe

| What | What has to be settled first |
|---|---|
| Automatic snapshots of the call window | Privacy: which windows, how often, how long pictures are kept |
| A "ticket note" dictation that Claude tidies | How it stays out of the way of plain dictation |
| Encrypting the library | Where the key lives without a sign-in |

## Not planned

Meeting bots, calendar access, cloud accounts or sync, virtual audio drivers, mobile apps, macOS or Linux.

## Upkeep

- PyTorch and torchaudio aren't in Dependabot's reach (a separate index) and are bumped by hand.
- Updating Tauri's CLI means bringing `src-tauri/windows/installer.nsi` up to its template.
