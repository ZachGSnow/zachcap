# ZachCap desktop app

Tauri 2 shell with a React + TypeScript + Tailwind v4 + shadcn/ui interface.

The Rust side starts the Python engine on a free 127.0.0.1 port with a random per-launch token,
and hands both to the UI through the `engine_info` command. In development that's the repo's
engine (`../../engine`) with `uv run zachcap serve`; an installed copy runs the engine that came
with the installer, from a venv it sets up on first launch with the uv bundled next to it (the
startup screen shows the setup; `engine_setup` reports it). An update changes what's different in
that venv; one that may not be whole (a setup cut short, an engine that couldn't start from it) is
built again from scratch. It also puts ZachCap in the tray: closing the window keeps it running.

## Installer

`scripts/build-installer.ps1` stages the engine's tracked files and a pinned, checksummed uv in
`src-tauri/installer/`, then runs `tauri build --config src-tauri/tauri.installer.conf.json`
(NSIS, per-user, into `%LOCALAPPDATA%\Programs\ZachCap`; see `src-tauri/windows/installer-hooks.nsh`).
It stops when `uv.lock` or the version of the engine doesn't go with the app's, which would make
every first start fail. The tools that build the engine's wheel are pinned with hashes
(`scripts/build-constraints.txt`; `build-constraints.in` says how to refresh them). The plain
`tauri.conf.json` keeps bundling off, so `cargo check` and `npm run tauri dev` need none of it.

## Develop

```powershell
npm install
npm run tauri dev
```

Needs Node, Rust with the MSVC build tools, and `uv` on PATH. Set `ZACHCAP_HOME` to use a
scratch data folder instead of `%LOCALAPPDATA%\ZachCap`, and `ZACHCAP_ENGINE_DIR` to point at
a different engine checkout.

## Icon

```powershell
uv run --project ..\..\engine python scripts\make-icon.py app-icon.png
npx tauri icon app-icon.png
```
