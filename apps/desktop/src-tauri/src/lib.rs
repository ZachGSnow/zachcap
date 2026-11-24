//! ZachCap desktop shell: starts the Python engine, hands the UI its address and
//! token, and lives in the tray like Tailscale (closing the window keeps it running).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde::Serialize;
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};

// Start with Windows is release-only (see run()); this keeps debug builds from warning
// that the plugin is unused.
#[cfg(all(desktop, debug_assertions))]
use tauri_plugin_autostart as _;

#[cfg(windows)]
mod dictation_keys;

/// Added to the sign-in launch: ZachCap starts in the tray instead of opening its window.
const HIDDEN_ARG: &str = "--hidden";

#[derive(Clone, Serialize)]
struct EngineInfo {
    port: u16,
    token: String,
}

struct Engine {
    /// The running engine's port and token; None until it has said which port it got. Each
    /// start has a port and a token of its own.
    info: Mutex<Option<EngineInfo>>,
    launch: Launch,
    child: Mutex<Option<Child>>,
    /// Why the engine stopped, once it has; cleared by a restart.
    stopped: Mutex<Option<String>>,
    /// Set from the moment a start begins until the engine runs or fails, so a second
    /// start (Try again pressed twice) doesn't run another.
    starting: AtomicBool,
    /// While an installed copy sets its engine up: the last thing uv said, for the startup
    /// screen.
    setup: Mutex<Option<String>>,
    /// The uv setting the engine up, while it runs, so quitting ZachCap stops its download too.
    uv: Mutex<Option<u32>>,
}

/// Where the engine and uv are. An installed copy runs the engine that came with it, in a
/// venv of its own that it sets up with the bundled uv; a development build runs the
/// repo's engine with the uv on this PC.
#[derive(Clone)]
struct Launch {
    uv: PathBuf,
    engine: PathBuf,
    installed: bool,
}

/// Set while ZachCap exits, so the engine going away then isn't reported as a crash.
static EXITING: AtomicBool = AtomicBool::new(false);

/// The engine's log in the data folder (a debug build's engine prints to the terminal).
const LOG: &str = "engine.log";
/// The engine's log is emptied once it passes this; it's checked once a minute.
const LOG_LIMIT: u64 = 20 * 1024 * 1024;

/// Empties the engine's log once it passes `limit`, leaving a line that says so. The engine
/// appends to it (see start_engine), so it carries on from the top, not past the old end.
fn trim_log(log: &Path, limit: u64) {
    if !std::fs::metadata(log).is_ok_and(|meta| meta.len() > limit) {
        return;
    }
    // Emptied through one handle, the note added through another that appends, so a line the
    // engine wrote in between isn't written over.
    if let Ok(file) = std::fs::OpenOptions::new().write(true).open(log)
        && file.set_len(0).is_ok()
        && let Ok(mut file) = std::fs::OpenOptions::new().append(true).open(log)
    {
        let _ = writeln!(
            file,
            "[ZachCap] {LOG} passed {} MB and was emptied.",
            limit / (1024 * 1024)
        );
    }
}

/// The live panel's window: a small window over the call while a recording runs, with the
/// live transcript (the page shows it when its window has this label). Left out of screen
/// shares and screenshots, never in the taskbar, and it doesn't take the keyboard from the
/// call when it appears.
const LIVE: &str = "live";

/// What the shell follows of the engine, once a second, so the live panel and the
/// notifications about calls never wait on the page (a hidden window's timers run late).
#[derive(Default)]
struct Following {
    /// A recording with a live transcript ran at the last look.
    live: bool,
    /// The meeting being recorded at the last look.
    meeting: Option<i64>,
    /// The meeting whose live panel was closed: it isn't opened again for it.
    dismissed: Option<i64>,
    /// How far the shell has said what the engine did on its own: the engine's run (`boot`)
    /// and the last event said (see /api/record). None until the first answer.
    events: Option<(String, i64)>,
}

struct Follow(Mutex<Following>);

/// Where the page reaches the engine; None while it starts (the page asks again).
#[tauri::command]
fn engine_info(engine: tauri::State<Engine>) -> Option<EngineInfo> {
    engine.info.lock().unwrap().clone()
}

/// What the engine's setup is doing, while it runs (the first launch of an installed copy
/// downloads Python and the speech libraries); None otherwise.
#[tauri::command]
fn engine_setup(engine: tauri::State<Engine>) -> Option<String> {
    engine.setup.lock().unwrap().clone()
}

/// Why the engine stopped, if it has; the startup screen uses it to fail fast.
#[tauri::command]
fn engine_exited(engine: tauri::State<Engine>) -> Option<String> {
    check_engine(&engine);
    engine.stopped.lock().unwrap().clone()
}

/// Notices that the engine process has ended and records why. Returns the message the
/// first time only.
fn check_engine(engine: &Engine) -> Option<String> {
    let mut child = engine.child.lock().unwrap();
    let status = child.as_mut()?.try_wait().ok()??;
    *child = None;
    let log = if cfg!(debug_assertions) {
        "Its output is in the terminal that ran `npm run tauri dev`.".to_string()
    } else {
        format!("Its log is {}.", data_dir().join("engine.log").display())
    };
    let message = format!("The engine stopped ({status}). {log}");
    *engine.stopped.lock().unwrap() = Some(message.clone());
    Some(message)
}

/// Starts the engine again after it stopped, on a new port with a new token; the window
/// picks both up through engine_info without reloading. Does nothing while it is still
/// running or starting.
#[tauri::command]
fn restart_engine(app: AppHandle) {
    begin(app);
}

/// The tray icon shows a red dot and says so in its tooltip while a recording runs, so
/// it's visible even with the window closed.
#[tauri::command]
fn set_recording_indicator(app: AppHandle, recording: bool) {
    if let Some(keys) = app.try_state::<Keys>() {
        keys.0.lock().unwrap().recording = recording;
    }
    #[cfg(desktop)]
    sync_keys(&app, Vec::new(), false);
    let Some(tray) = app.tray_by_id("main") else {
        return;
    };
    let _ = tray.set_tooltip(Some(if recording {
        "ZachCap: recording"
    } else {
        "ZachCap"
    }));
    if let Some(icon) = app.default_window_icon() {
        let _ = tray.set_icon(Some(if recording {
            with_red_dot(icon)
        } else {
            icon.clone().to_owned()
        }));
    }
}

/// The global shortcuts, with their default keys, in the order Settings lists them.
/// Settings can change the keys; the page passes them on (set_shortcuts).
const ACTIONS: [(&str, &str); 4] = [
    ("record", "Ctrl+Alt+R"),
    ("bookmark", "Ctrl+Alt+B"),
    ("snapshot", "Ctrl+Alt+S"),
    ("dictate", "Ctrl+Alt+D"),
];

/// Which keys each shortcut has, and what decides when each is held: start or stop
/// recording always; bookmark and snapshot only while recording (the rest of the time Word,
/// an IDE, or a keyboard whose AltGr+S types a letter keep them); dictate only while
/// dictation is on.
#[derive(Default)]
struct KeyState {
    /// Keys picked in Settings, by shortcut ("Ctrl+Alt+P"); the default where absent.
    keys: std::collections::HashMap<String, String>,
    recording: bool,
    dictation: bool,
    /// While Settings waits for new keys, ZachCap lets go of its own so the page gets them.
    /// Counted: only pause_shortcuts changes it, so nothing else takes the keys back early.
    pauses: u32,
}

impl KeyState {
    fn keys_for(&self, action: &str) -> String {
        self.keys.get(action).cloned().unwrap_or_else(|| {
            let (_, default) = ACTIONS.iter().find(|(name, _)| *name == action).unwrap();
            default.to_string()
        })
    }

    fn wanted(&self, action: &str) -> bool {
        self.pauses == 0
            && match action {
                "record" => true,
                "bookmark" | "snapshot" => self.recording,
                "dictate" => self.dictation,
                _ => false,
            }
    }
}

struct Keys(Mutex<KeyState>);

/// What Settings shows for a shortcut.
#[derive(Clone, Serialize)]
struct KeyStatus {
    action: String,
    keys: String,
    /// "on" (held now), "shared" (dictate only: another app has registered the same keys,
    /// and dictation gets them first while it's on), "free" (not needed now, and no other app
    /// has registered them), "off" (not needed now, not checked), "taken" (another app has
    /// registered them), "failed" (Windows refused the keyboard hook) or "invalid".
    state: &'static str,
}

/// "Ctrl+Alt+R" as the plugin reads it (it calls the Windows key Super).
#[cfg(desktop)]
fn parse_keys(keys: &str) -> Option<tauri_plugin_global_shortcut::Shortcut> {
    keys.split('+')
        .map(|part| match part.trim() {
            win if win.eq_ignore_ascii_case("win") => "Super",
            other => other,
        })
        .collect::<Vec<_>>()
        .join("+")
        .parse()
        .ok()
}

/// Holds the shortcuts wanted now, lets go of the rest and of keys no longer used
/// (`retired`), and says how each one is. With `probe`, a shortcut not needed now is held
/// and let go at once, to learn whether another app has its keys. The plugin is called
/// without our lock held, since its handler takes the lock.
#[cfg(desktop)]
fn sync_keys(app: &AppHandle, retired: Vec<String>, probe: bool) -> Vec<KeyStatus> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;
    let Some(keys) = app.try_state::<Keys>() else {
        return Vec::new();
    };
    let plan: Vec<(String, String, bool)> = {
        let state = keys.0.lock().unwrap();
        ACTIONS
            .iter()
            .map(|(action, _)| {
                (
                    action.to_string(),
                    state.keys_for(action),
                    state.wanted(action),
                )
            })
            .collect()
    };
    let shortcuts = app.global_shortcut();
    for old in retired.iter().filter_map(|old_keys| parse_keys(old_keys)) {
        if shortcuts.is_registered(old) {
            let _ = shortcuts.unregister(old);
        }
    }
    plan.into_iter()
        .map(|(action, keys, wanted)| {
            let state = match parse_keys(&keys) {
                None => "invalid",
                #[cfg(windows)]
                Some(shortcut) if action == "dictate" => {
                    sync_dictation_keys(shortcuts, shortcut, &keys, wanted, probe)
                }
                Some(shortcut) => {
                    let held = shortcuts.is_registered(shortcut);
                    if wanted && !held {
                        match shortcuts.register(shortcut) {
                            Ok(()) => "on",
                            Err(error) => {
                                // Another app owns the combination; the tray menu still works.
                                eprintln!("{keys} is unavailable: {error}");
                                "taken"
                            }
                        }
                    } else if wanted {
                        "on"
                    } else {
                        if held {
                            let _ = shortcuts.unregister(shortcut);
                        }
                        if !probe {
                            "off"
                        } else if shortcuts.register(shortcut).is_ok() {
                            let _ = shortcuts.unregister(shortcut);
                            "free"
                        } else {
                            "taken"
                        }
                    }
                }
            };
            KeyStatus {
                action,
                keys,
                state,
            }
        })
        .collect()
}

/// Dictation's keys are read by a keyboard hook (dictation_keys), not registered: while
/// they're held the main key never reaches the app in front, and letting go of any of them
/// ends the dictation. Registering is only used to learn whether another app has them.
#[cfg(windows)]
fn sync_dictation_keys(
    shortcuts: &tauri_plugin_global_shortcut::GlobalShortcut<tauri::Wry>,
    shortcut: tauri_plugin_global_shortcut::Shortcut,
    keys: &str,
    wanted: bool,
    probe: bool,
) -> &'static str {
    let Some(chord) = dictation_keys::Chord::parse(keys) else {
        dictation_keys::listen(None);
        return "invalid";
    };
    if shortcuts.is_registered(shortcut) {
        let _ = shortcuts.unregister(shortcut);
    }
    // The hook gets the keys before another app's registered hotkey: "shared", not "taken".
    let shared = probe && {
        let taken = shortcuts.register(shortcut).is_err();
        if !taken {
            let _ = shortcuts.unregister(shortcut);
        }
        taken
    };
    if wanted && !dictation_keys::listen(Some(chord)) {
        eprintln!("The dictation keys couldn't be read: Windows refused the keyboard hook");
        return "failed";
    }
    if !wanted {
        dictation_keys::listen(None);
    }
    match (wanted, probe, shared) {
        (_, _, true) => "shared",
        (true, _, false) => "on",
        (false, false, _) => "off",
        (false, true, false) => "free",
    }
}

/// Which shortcut, if any, has these keys now.
#[cfg(desktop)]
fn action_for(
    app: &AppHandle,
    shortcut: &tauri_plugin_global_shortcut::Shortcut,
) -> Option<&'static str> {
    let keys = app.try_state::<Keys>()?;
    let state = keys.0.lock().unwrap();
    ACTIONS
        .iter()
        .map(|(action, _)| *action)
        .find(|action| parse_keys(&state.keys_for(action)).as_ref() == Some(shortcut))
}

/// The keys picked in Settings (the page passes them on at start and on every change).
/// Returns how each shortcut is, for Settings.
#[tauri::command]
fn set_shortcuts(
    app: AppHandle,
    keys: std::collections::HashMap<String, String>,
) -> Vec<KeyStatus> {
    let retired = {
        let Some(held) = app.try_state::<Keys>() else {
            return Vec::new();
        };
        let mut state = held.0.lock().unwrap();
        let before: Vec<String> = ACTIONS
            .iter()
            .map(|(action, _)| state.keys_for(action))
            .collect();
        state.keys = keys;
        save_keys(&state.keys);
        let after: Vec<String> = ACTIONS
            .iter()
            .map(|(action, _)| state.keys_for(action))
            .collect();
        before
            .into_iter()
            .filter(|keys| !after.contains(keys))
            .collect::<Vec<_>>()
    };
    #[cfg(desktop)]
    return sync_keys(&app, retired, true);
    #[cfg(not(desktop))]
    {
        let _ = retired;
        Vec::new()
    }
}

/// How each shortcut is now, for Settings (checking the ones not needed now).
#[tauri::command]
fn shortcut_status(app: AppHandle) -> Vec<KeyStatus> {
    #[cfg(desktop)]
    return sync_keys(&app, Vec::new(), true);
    #[cfg(not(desktop))]
    {
        let _ = app;
        Vec::new()
    }
}

/// Settings is waiting for new keys: let go of ZachCap's own until it's done, so pressing
/// one of them reaches the page instead of starting a recording.
#[tauri::command]
fn pause_shortcuts(app: AppHandle, paused: bool) {
    if let Some(keys) = app.try_state::<Keys>() {
        let mut state = keys.0.lock().unwrap();
        state.pauses = if paused {
            state.pauses + 1
        } else {
            state.pauses.saturating_sub(1)
        };
    }
    #[cfg(desktop)]
    sync_keys(&app, Vec::new(), false);
}

/// Dictation turned on or off in Settings (the page says so at start and on every change).
#[tauri::command]
fn set_dictation(app: AppHandle, enabled: bool) {
    if let Some(keys) = app.try_state::<Keys>() {
        keys.0.lock().unwrap().dictation = enabled;
    }
    #[cfg(desktop)]
    sync_keys(&app, Vec::new(), false);
}

/// The app icon with a red recording dot (and a white ring) in its bottom-right corner.
fn with_red_dot(icon: &Image<'_>) -> Image<'static> {
    let (width, height) = (icon.width(), icon.height());
    let mut rgba = icon.rgba().to_vec();
    let radius = width.min(height) as f32 * 0.24;
    let (cx, cy) = (width as f32 - radius - 1.0, height as f32 - radius - 1.0);
    for y in 0..height {
        for x in 0..width {
            let distance = ((x as f32 + 0.5 - cx).powi(2) + (y as f32 + 0.5 - cy).powi(2)).sqrt();
            if distance <= radius * 1.25 {
                let color = if distance <= radius {
                    [239, 68, 68, 255]
                } else {
                    [255, 255, 255, 255]
                };
                let at = ((y * width + x) * 4) as usize;
                rgba[at..at + 4].copy_from_slice(&color);
            }
        }
    }
    Image::new_owned(rgba, width, height)
}

/// Opens ZachCap's data folder in Explorer (from Settings). It takes no path, so the page
/// can't ask it to open anything else.
#[tauri::command]
fn open_data_folder() -> Result<(), String> {
    let folder = data_dir();
    if !folder.is_dir() {
        return Err(format!("{} doesn't exist yet", folder.display()));
    }
    Command::new("explorer")
        .arg(folder)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// The Python engine: ZACHCAP_ENGINE_DIR when set, else the one installed with the app (the
/// installer puts it and uv next to the exe), else the repo's `engine/` in development.
fn launch_for(app: &AppHandle) -> Launch {
    if let Some(dir) = std::env::var_os("ZACHCAP_ENGINE_DIR") {
        return Launch {
            uv: find_uv(),
            engine: PathBuf::from(dir),
            installed: false,
        };
    }
    if let Ok(resources) = app.path().resource_dir()
        && resources.join("engine").join("pyproject.toml").is_file()
    {
        let bundled = resources.join("uv").join("uv.exe");
        return Launch {
            uv: if bundled.is_file() {
                bundled
            } else {
                find_uv()
            },
            engine: resources.join("engine"),
            installed: true,
        };
    }
    Launch {
        uv: find_uv(),
        engine: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../engine"),
        installed: false,
    }
}

/// The keys last passed on from Settings, kept so the next start holds yours from the first
/// moment, not the defaults until the engine answers (an app starting meanwhile could lose
/// keys you gave it).
fn keys_file() -> PathBuf {
    data_dir().join("shortcuts.json")
}

fn save_keys(keys: &std::collections::HashMap<String, String>) {
    if let Ok(text) = serde_json::to_string(keys) {
        let _ = std::fs::write(keys_file(), text);
    }
}

fn saved_keys() -> std::collections::HashMap<String, String> {
    std::fs::read_to_string(keys_file())
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

/// Where the engine keeps its data; matches engine/src/zachcap/paths.py.
fn data_dir() -> PathBuf {
    std::env::var_os("ZACHCAP_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .unwrap_or_default()
                .join("ZachCap")
        })
}

/// uv from PATH or its usual install folders: a shortcut started by Explorer can have an
/// older PATH than the terminal that installed uv.
fn find_uv() -> PathBuf {
    let exe = "uv.exe";
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(path) = std::env::var_os("PATH") {
        candidates.extend(std::env::split_paths(&path).map(|dir| dir.join(exe)));
    }
    if let Some(home) = std::env::var_os("USERPROFILE").map(PathBuf::from) {
        candidates.push(home.join(".local").join("bin").join(exe));
        candidates.push(home.join(".cargo").join("bin").join(exe));
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA").map(PathBuf::from) {
        let winget = local.join("Microsoft").join("WinGet");
        candidates.push(winget.join("Links").join(exe));
        if let Ok(packages) = std::fs::read_dir(winget.join("Packages")) {
            candidates.extend(
                packages
                    .flatten()
                    .filter(|entry| {
                        entry
                            .file_name()
                            .to_string_lossy()
                            .starts_with("astral-sh.uv_")
                    })
                    .map(|entry| entry.path().join(exe)),
            );
        }
    }
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("uv"))
}

/// How long a started engine has to say which port it got before it counts as failed (the
/// first start after setup compiles the Python files, which takes a while on a slow PC).
const PORT_WAIT: Duration = Duration::from_secs(300);

/// Starts the engine on its own thread, setting an installed copy's engine up first when this
/// build hasn't been yet, with a new token, on a port it picks itself. Does nothing while the
/// engine runs or is already starting. A failure is kept as the reason it stopped, which the
/// startup screen and the restart banner show.
fn begin(app: AppHandle) {
    let Some(engine) = app.try_state::<Engine>() else {
        return;
    };
    if engine.child.lock().unwrap().is_some() || engine.starting.swap(true, Ordering::SeqCst) {
        return;
    }
    *engine.stopped.lock().unwrap() = None;
    *engine.info.lock().unwrap() = None;
    std::thread::spawn(move || {
        let engine = app.state::<Engine>();
        let token = uuid::Uuid::new_v4().simple().to_string();
        let port_file = data_dir().join(format!("engine-{}.port", uuid::Uuid::new_v4().simple()));
        let started = set_up(&engine).and_then(|()| {
            start_engine(&engine.launch, &token, &port_file).map_err(|error| {
                if engine.launch.installed {
                    format!("Couldn't start the engine ({error}).")
                } else {
                    format!(
                        "Couldn't run uv ({error}). Install it from https://docs.astral.sh/uv/ and start ZachCap again."
                    )
                }
            })
        });
        *engine.setup.lock().unwrap() = None;
        match started {
            Ok(child) => {
                *engine.child.lock().unwrap() = Some(child);
                match wait_for_port(&engine, &port_file) {
                    Some(port) => *engine.info.lock().unwrap() = Some(EngineInfo { port, token }),
                    None => {
                        if engine.stopped.lock().unwrap().is_none() {
                            if let Some(mut child) = engine.child.lock().unwrap().take() {
                                let _ = child.kill();
                            }
                            *engine.stopped.lock().unwrap() = Some(
                                "The engine started but never said which port it's on.".into(),
                            );
                        } // else it stopped, and check_engine said why
                        // An installed engine that can't start may be a broken venv: the next
                        // start (Try again) sets it up again instead of failing the same way.
                        if engine.launch.installed {
                            let _ = std::fs::remove_file(
                                engine.launch.engine.join(".venv").join(SET_UP),
                            );
                        }
                    }
                }
            }
            Err(message) => *engine.stopped.lock().unwrap() = Some(message),
        }
        let _ = std::fs::remove_file(&port_file);
        engine.starting.store(false, Ordering::SeqCst);
    });
}

/// Waits for the engine to write the port it got (see report_port in api.py). None when it
/// stops first or takes longer than PORT_WAIT.
fn wait_for_port(engine: &Engine, port_file: &Path) -> Option<u16> {
    let deadline = std::time::Instant::now() + PORT_WAIT;
    while std::time::Instant::now() < deadline {
        if let Some(port) = read_port(port_file) {
            return Some(port);
        }
        if engine.child.lock().unwrap().is_none() || EXITING.load(Ordering::Relaxed) {
            return None;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    None
}

/// The port in a port file, once it's there and whole.
fn read_port(port_file: &Path) -> Option<u16> {
    let text = std::fs::read_to_string(port_file).ok()?;
    text.trim().parse::<u16>().ok().filter(|&port| port != 0)
}

/// The file in an installed copy's venv that records which build it was set up for.
const SET_UP: &str = "zachcap-build.txt";

/// Brings an installed copy's engine venv up to date with the bundled uv: Python and the
/// speech libraries on first launch (a few GB), what changed after an update. Skipped when
/// this build is set up already (the installer's BUILD stamp is in the venv). What uv says
/// is shown on the startup screen and kept in engine-setup.log.
///
/// In two steps, so nothing that isn't pinned is fetched or run: the libraries from uv.lock
/// (each checked against its hash), then ZachCap's own code from the wheel the installer
/// carries, built when the release was (building it here would fetch its build tool).
fn set_up(engine: &Engine) -> Result<(), String> {
    let launch = &engine.launch;
    if !launch.installed {
        return Ok(());
    }
    let venv = launch.engine.join(".venv");
    let marker = venv.join(SET_UP);
    let build = std::fs::read_to_string(launch.engine.join("BUILD")).unwrap_or_default();
    // A venv without its pyvenv.cfg was half removed (an uninstall while it ran): its python.exe
    // would quietly run uv's shared Python instead, so it's set up again from scratch.
    let whole = venv.join("pyvenv.cfg").is_file();
    if whole
        && !build.is_empty()
        && std::fs::read_to_string(&marker).is_ok_and(|done| done == build)
    {
        return Ok(());
    }
    if venv.exists() && !whole {
        std::fs::remove_dir_all(&venv)
            .map_err(|error| format!("Couldn't clear the old speech engine ({error})."))?;
    }
    let wheel = std::fs::read_dir(launch.engine.join("wheel"))
        .ok()
        .and_then(|entries| {
            entries
                .flatten()
                .map(|entry| entry.path())
                .find(|path| path.extension().is_some_and(|ext| ext == "whl"))
        })
        .ok_or(
            "The installer's copy of the engine is incomplete (no wheel). Install ZachCap again.",
        )?;
    *engine.setup.lock().unwrap() = Some("Getting ready...".to_string());
    let mut log = std::fs::create_dir_all(data_dir())
        .and_then(|_| std::fs::File::create(data_dir().join("engine-setup.log")))
        .ok();
    // The lock as shipped, without the test tools or ZachCap itself. Only the project's own
    // settings: no uv.toml from the user's or the machine's config folders (another account
    // can write C:\ProgramData), and a Python uv manages, not whatever 3.12 is installed.
    run_uv(engine, &mut log, |command| {
        command
            .arg("sync")
            .arg("--project")
            .arg(&launch.engine)
            .args(["--locked", "--no-dev", "--no-install-project"])
            .args(["--no-config", "--managed-python"])
            .env("UV_PROJECT_ENVIRONMENT", &venv);
    })?;
    // ZachCap's own code, copied into the venv (so the installed files are only read).
    run_uv(engine, &mut log, |command| {
        command
            .args(["pip", "install", "--python"])
            .arg(&venv)
            .args(["--no-deps", "--reinstall", "--offline", "--no-config"])
            .arg(&wheel);
    })?;
    let _ = std::fs::write(&marker, &build);
    Ok(())
}

/// Runs the bundled uv to set the engine up: what it says goes to the startup screen and
/// the log, and quitting ZachCap meanwhile stops it (see the Exit handler).
fn run_uv(
    engine: &Engine,
    log: &mut Option<std::fs::File>,
    arguments: impl FnOnce(&mut Command),
) -> Result<(), String> {
    let mut command = Command::new(&engine.launch.uv);
    arguments(&mut command);
    command
        .env_remove("VIRTUAL_ENV")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    no_window(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Couldn't run the bundled uv ({error})."))?;
    *engine.uv.lock().unwrap() = Some(child.id());
    let mut last = String::new();
    if let Some(stderr) = child.stderr.take() {
        use std::io::BufRead;
        for line in std::io::BufReader::new(stderr)
            .lines()
            .map_while(Result::ok)
        {
            if let Some(log) = log.as_mut() {
                let _ = writeln!(log, "{line}");
            }
            let line = line.trim();
            if !line.is_empty() {
                last = line.to_string();
                *engine.setup.lock().unwrap() = Some(last.clone());
            }
        }
    }
    let status = child.wait().map_err(|error| error.to_string());
    *engine.uv.lock().unwrap() = None;
    if !status?.success() {
        return Err(format!(
            "Setting up the engine didn't finish ({last}). Check the internet connection and press Try again. \
             What happened is in {}.",
            data_dir().join("engine-setup.log").display()
        ));
    }
    Ok(())
}

fn no_window(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = command;
}

fn start_engine(launch: &Launch, token: &str, port_file: &Path) -> std::io::Result<Child> {
    let mut command = if launch.installed {
        // Set up already (set_up), so its Python runs the engine directly.
        let mut command = Command::new(
            launch
                .engine
                .join(".venv")
                .join("Scripts")
                .join("python.exe"),
        );
        // Isolated (-I): no current folder, PYTHONPATH or user site-packages ahead of the
        // venv's own code; -u because -I also ignores PYTHONUNBUFFERED.
        command.args(["-I", "-u", "-m", "zachcap.cli"]);
        command.current_dir(&launch.engine);
        command
    } else {
        let mut command = Command::new(&launch.uv);
        command
            .arg("run")
            .arg("--project")
            .arg(&launch.engine)
            .arg("zachcap");
        command
    };
    command
        // The engine binds a port Windows picks and writes it to the port file (wait_for_port).
        .args(["serve", "--port", "0"])
        .env("ZACHCAP_PORT_FILE", port_file)
        // Passed through the environment so the token never shows in process listings.
        .env("ZACHCAP_TOKEN", token)
        // The engine exits by itself if this app goes away without stopping it.
        .env("ZACHCAP_PARENT_PID", std::process::id().to_string())
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null());
    // A release build has no console, so the engine's output goes to a log instead. The last
    // run's log is kept as engine.previous.log, so why an engine stopped survives a restart;
    // the new one is opened to append, so trim_log can empty it while the engine writes.
    #[cfg(not(debug_assertions))]
    {
        let folder = data_dir();
        let _ = std::fs::rename(folder.join(LOG), folder.join("engine.previous.log"));
        if let Ok(log) = std::fs::create_dir_all(&folder).and_then(|_| {
            std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(folder.join(LOG))
        }) && let Ok(copy) = log.try_clone()
        {
            command.stdout(copy).stderr(log);
        }
    }
    no_window(&mut command);
    command.spawn()
}

/// Asks the engine to stop and save a recording in progress, waiting a few seconds at
/// most. Runs as ZachCap exits, which includes Quit, sign-out and shutdown.
fn stop_recording(info: &EngineInfo) {
    let address = SocketAddr::from(([127, 0, 0, 1], info.port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_secs(1)) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let request = format!(
        "POST /api/record/stop HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Content-Length: 0\r\nConnection: close\r\n\r\n",
        info.port, info.token
    );
    if stream.write_all(request.as_bytes()).is_ok() {
        // The engine answers once the audio files are closed.
        let _ = stream.read_to_end(&mut Vec::new());
    }
}

/// GET from the engine, as JSON; None if it isn't answering (or not with a 200). Sent with
/// the host the engine's gate wants (gate.py) and the token; read up to a megabyte.
fn engine_get(info: &EngineInfo, path: &str) -> Option<serde_json::Value> {
    let address = SocketAddr::from(([127, 0, 0, 1], info.port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(500)).ok()?;
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\
         Connection: close\r\n\r\n",
        info.port, info.token
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = Vec::new();
    stream.take(1 << 20).read_to_end(&mut response).ok()?;
    let text = String::from_utf8_lossy(&response);
    let (head, body) = text.split_once("\r\n\r\n")?;
    if !head.starts_with("HTTP/1.1 200") {
        return None;
    }
    serde_json::from_str(body).ok()
}

/// GET from the engine running now: its port and token are read on every call, since each
/// start of it has its own (a restart included). None while it starts or isn't answering.
fn ask_engine(engine: &Engine, path: &str) -> Option<serde_json::Value> {
    let info = engine.info.lock().unwrap().clone()?;
    engine_get(&info, path)
}

/// The recording's state, with the call events after the shell's cursor (as the page asks).
fn record_path(cursor: &Option<(String, i64)>) -> String {
    match cursor {
        // The engine's run is hex; anything else is sent as no cursor at all.
        Some((boot, after)) if boot.bytes().all(|byte| byte.is_ascii_alphanumeric()) => {
            format!("/api/record?boot={boot}&after={after}")
        }
        _ => "/api/record".to_string(),
    }
}

/// The call events to tell about after an answer from /api/record, oldest first, and the
/// cursor after them. Unlike the page, the shell takes everything from a run of the engine it
/// hasn't seen as news: it started that run itself, so none of it has been told yet (a
/// restarted engine numbers its events from 1 again).
fn new_events(
    cursor: &Option<(String, i64)>,
    state: &serde_json::Value,
) -> (Option<(String, i64)>, Vec<serde_json::Value>) {
    let Some(boot) = state["boot"].as_str() else {
        return (cursor.clone(), Vec::new());
    };
    let from = match cursor {
        Some((seen, after)) if seen == boot => *after,
        _ => 0,
    };
    let mut fresh: Vec<serde_json::Value> = state["events"]
        .as_array()
        .map(|events| {
            events
                .iter()
                .filter(|event| event["id"].as_i64().is_some_and(|id| id > from))
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    fresh.sort_by_key(|event| event["id"].as_i64());
    let last = fresh
        .last()
        .and_then(|event| event["id"].as_i64())
        .unwrap_or(from);
    (Some((boot.to_string(), last)), fresh)
}

/// What to do with the live panel after a look: open it when a recording with a live
/// transcript starts (unless it was closed for that recording), close it when the recording
/// ends or its live transcript stops (turned off in Settings). None: leave it as it is.
fn panel_after(
    seen: &mut Following,
    recording: bool,
    meeting: Option<i64>,
    live: bool,
) -> Option<bool> {
    let running = recording && live;
    let started = running && !(seen.live && seen.meeting == meeting);
    let action = if started {
        (seen.dismissed != meeting).then_some(true)
    } else if seen.live && !running {
        Some(false)
    } else {
        None
    };
    seen.live = running;
    seen.meeting = if recording { meeting } else { None };
    action
}

/// Opens the live panel (or shows it again) at the top right of the main screen. It doesn't
/// take the keyboard from the call as it opens, and can't be maximized or minimized (it has
/// no taskbar button to come back from).
fn open_live_panel(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(LIVE) {
        let _ = window.unminimize();
        let _ = window.show();
        return;
    }
    let (width, height) = (360.0, 480.0);
    let mut builder =
        tauri::WebviewWindowBuilder::new(app, LIVE, tauri::WebviewUrl::App("index.html".into()))
            .title("ZachCap live")
            .inner_size(width, height)
            .min_inner_size(260.0, 120.0)
            .always_on_top(true)
            .skip_taskbar(true)
            .decorations(false)
            .shadow(true)
            .focused(false)
            .content_protected(true)
            .resizable(true)
            .maximizable(false)
            .minimizable(false);
    if let Ok(Some(monitor)) = app.primary_monitor() {
        let scale = monitor.scale_factor();
        let area = monitor.work_area();
        let right = (f64::from(area.position.x) + f64::from(area.size.width)) / scale;
        let top = f64::from(area.position.y) / scale;
        builder = builder.position(right - width - 16.0, top + 16.0);
    }
    let _ = builder.build();
}

fn close_live_panel(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(LIVE) {
        let _ = window.destroy();
    }
}

/// Shows the live panel again after it was closed (the recording bar's button). Async: on
/// Windows, a window made in a command that isn't can hang.
#[tauri::command]
async fn show_live_panel(app: AppHandle) {
    if let Some(follow) = app.try_state::<Follow>() {
        follow.0.lock().unwrap().dismissed = None;
    }
    open_live_panel(&app);
}

/// Whether the main window is where the user is looking: shown, not minimized, and focused
/// (as the page's windowInFront has it). Not while the live panel has the focus.
fn main_in_front(app: &AppHandle) -> bool {
    app.get_webview_window("main").is_some_and(|window| {
        window.is_visible().unwrap_or(false)
            && !window.is_minimized().unwrap_or(true)
            && window.is_focused().unwrap_or(false)
    })
}

/// A Windows notification for what the engine did on its own about a call: the events the
/// page says with a toast when its window is in front (the others it only shows there).
fn tell_about_call(app: &AppHandle, event: &serde_json::Value) {
    let name = event["app"].as_str().unwrap_or("The call app");
    let (title, body) = match event["kind"].as_str() {
        Some("started") => (
            format!("Recording your {name} call"),
            "It stops when the call ends. Not one to keep? Press Don't record in ZachCap."
                .to_string(),
        ),
        Some("stopped") => (
            "Recording saved".to_string(),
            if event["reason"].as_str() == Some("quiet") {
                "It stopped after ten quiet minutes. Transcribing and finding speakers..."
            } else {
                "It stopped when the call ended. Transcribing and finding speakers..."
            }
            .to_string(),
        ),
        Some("failed") => (
            format!("Couldn't record the {name} call"),
            event["detail"].as_str().unwrap_or_default().to_string(),
        ),
        _ => return,
    };
    use tauri_plugin_notification::NotificationExt;
    let _ = app.notification().builder().title(title).body(body).show();
}

/// Once a second: opens the live panel when a recording with a live transcript starts and
/// closes it when that ends, and tells about calls recorded or stopped on their own with a
/// Windows notification when the main window isn't in front (the page shows a toast when it
/// is). The events come after a cursor of the shell's own, so none is told twice, or lost to
/// a restart of the engine.
fn follow_engine(app: AppHandle) {
    std::thread::spawn(move || {
        while !EXITING.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_secs(1));
            let (Some(engine), Some(follow)) =
                (app.try_state::<Engine>(), app.try_state::<Follow>())
            else {
                continue;
            };
            let path = record_path(&follow.0.lock().unwrap().events);
            let Some(state) = ask_engine(&engine, &path) else {
                continue;
            };
            let recording = state["recording"].as_bool().unwrap_or(false);
            let meeting = state["meeting_id"].as_i64();
            let live = state["live"].as_bool().unwrap_or(false);
            let (panel, fresh) = {
                let mut seen = follow.0.lock().unwrap();
                let panel = panel_after(&mut seen, recording, meeting, live);
                let (cursor, fresh) = new_events(&seen.events, &state);
                seen.events = cursor;
                (panel, fresh)
            };
            match panel {
                Some(true) => open_live_panel(&app),
                Some(false) => close_live_panel(&app),
                None => {}
            }
            if !fresh.is_empty() && !main_in_front(&app) {
                for event in &fresh {
                    tell_about_call(&app, event);
                }
            }
        }
    });
}

fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// The first time the window is closed, says where ZachCap went. A marker file in the
/// app's local data folder keeps it to once.
fn tell_about_tray(app: &AppHandle) {
    static TOLD: AtomicBool = AtomicBool::new(false);
    if TOLD.swap(true, Ordering::Relaxed) {
        return;
    }
    let Ok(folder) = app.path().app_local_data_dir() else {
        return;
    };
    let marker = folder.join("tray-hint-shown");
    if marker.exists()
        || std::fs::create_dir_all(&folder)
            .and_then(|_| std::fs::write(&marker, b""))
            .is_err()
    {
        return;
    }
    use tauri_plugin_notification::NotificationExt;
    let _ = app
        .notification()
        .builder()
        .title("ZachCap is still running")
        .body("It's in the tray, ready to record. Quit from the tray icon's menu.")
        .show();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();
    #[cfg(desktop)]
    {
        // A second launch (Start menu, shortcut) shows the running copy instead of starting
        // another engine and tray icon. The plugin has to be registered first.
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app)
        }));
        // Release builds only: a debug build started at sign-in would load the dev server,
        // which isn't running then.
        #[cfg(not(debug_assertions))]
        {
            builder = builder.plugin(
                tauri_plugin_autostart::Builder::new()
                    .app_name("ZachCap")
                    .arg(HIDDEN_ARG)
                    .build(),
            );
        }
    }
    builder
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .setup(move |app| {
            // Started on its own thread (an installed copy's first launch sets the engine up
            // for minutes); the startup screen shows how that goes, or what went wrong.
            app.manage(Engine {
                info: Mutex::new(None),
                launch: launch_for(app.handle()),
                child: Mutex::new(None),
                stopped: Mutex::new(None),
                starting: AtomicBool::new(false),
                setup: Mutex::new(None),
                uv: Mutex::new(None),
            });
            begin(app.handle().clone());
            app.manage(Follow(Mutex::new(Following::default())));
            follow_engine(app.handle().clone());

            // Notice when the engine stops later (a crash), so the window can offer a restart;
            // and keep its log from growing without end (ZachCap runs for weeks in the tray).
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let mut seconds: u64 = 0;
                while !EXITING.load(Ordering::Relaxed) {
                    std::thread::sleep(Duration::from_secs(1));
                    seconds += 1;
                    if let Some(engine) = handle.try_state::<Engine>()
                        && let Some(message) = check_engine(&engine)
                        && !EXITING.load(Ordering::Relaxed)
                    {
                        let _ = handle.emit("engine-exited", message);
                    }
                    if seconds % 60 == 0 {
                        trim_log(&data_dir().join(LOG), LOG_LIMIT);
                    }
                }
            });

            // The global shortcuts (see KeyState): start or stop recording from anywhere;
            // while recording, bookmark the moment and snapshot the call; hold to dictate.
            // Handed to the page like the tray menu, so the window's state stays in sync.
            app.manage(Keys(Mutex::new(KeyState {
                keys: saved_keys(),
                ..KeyState::default()
            })));
            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::ShortcutState;
                app.handle().plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |app, shortcut, event| {
                            let pressed = event.state() == ShortcutState::Pressed;
                            let event = match action_for(app, shortcut) {
                                // Dictation listens while the keys are held: down starts, up
                                // stops. On Windows its keys come from the hook instead.
                                #[cfg(not(windows))]
                                Some("dictate") if pressed => "dictate-start",
                                #[cfg(not(windows))]
                                Some("dictate") => "dictate-stop",
                                Some("record") if pressed => "toggle-recording",
                                Some("bookmark") if pressed => "bookmark",
                                Some("snapshot") if pressed => "snapshot",
                                _ => return,
                            };
                            let _ = app.emit(event, ());
                        })
                        .build(),
                )?;
                #[cfg(windows)]
                {
                    let handle = app.handle().clone();
                    dictation_keys::on_change(move |down| {
                        let _ = handle.emit(
                            if down {
                                "dictate-start"
                            } else {
                                "dictate-stop"
                            },
                            (),
                        );
                    });
                }
                sync_keys(app.handle(), Vec::new(), false);
            }

            let record =
                MenuItem::with_id(app, "record", "Start or stop recording", true, None::<&str>)?;
            let mark =
                MenuItem::with_id(app, "bookmark", "Bookmark this moment", true, None::<&str>)?;
            let shot = MenuItem::with_id(app, "snapshot", "Snapshot the call", true, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Open ZachCap", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let quit = MenuItem::with_id(app, "quit", "Quit ZachCap", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&record, &mark, &shot, &open, &separator, &quit])?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().cloned().expect("app icon"))
                .tooltip("ZachCap")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    // The page does the work, so the window's state stays in sync.
                    "record" => {
                        let _ = app.emit("toggle-recording", ());
                    }
                    "bookmark" => {
                        let _ = app.emit("bookmark", ());
                    }
                    "snapshot" => {
                        let _ = app.emit("snapshot", ());
                    }
                    "open" => show_main(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;

            // The window starts hidden (tauri.conf.json); a sign-in launch leaves it in the tray.
            if !std::env::args().any(|arg| arg == HIDDEN_ARG) {
                show_main(app.handle());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                if window.label() == LIVE {
                    // Closed for this recording; the recording bar's button brings it back.
                    if let Some(follow) = window.app_handle().try_state::<Follow>() {
                        let mut seen = follow.0.lock().unwrap();
                        seen.dismissed = seen.meeting;
                    }
                } else {
                    // Closing the window keeps ZachCap (and any recording) running in the tray.
                    tell_about_tray(window.app_handle());
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            engine_info,
            engine_setup,
            engine_exited,
            restart_engine,
            open_data_folder,
            set_recording_indicator,
            set_dictation,
            set_shortcuts,
            shortcut_status,
            pause_shortcuts,
            show_live_panel
        ])
        .build(tauri::generate_context!())
        .expect("error while building ZachCap")
        .run(|app, event| {
            if let RunEvent::Exit = event
                && let Some(engine) = app.try_state::<Engine>()
            {
                EXITING.store(true, Ordering::Relaxed);
                if let Some(info) = engine.info.lock().unwrap().clone() {
                    stop_recording(&info);
                }
                if let Some(mut child) = engine.child.lock().unwrap().take() {
                    let _ = child.kill();
                }
                if let Some(pid) = engine.uv.lock().unwrap().take() {
                    end_process(pid);
                }
            }
        });
}

/// Ends a process by its id (uv, which set_up reads from on another thread).
fn end_process(pid: u32) {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_TERMINATE, TerminateProcess,
        };
        // SAFETY: a handle opened here and closed here; a pid that's gone gives a null one.
        unsafe {
            let process = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if !process.is_null() {
                TerminateProcess(process, 1);
                CloseHandle(process);
            }
        }
    }
    #[cfg(not(windows))]
    let _ = pid;
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    /// One request answered by a pretend engine: what it was asked, and the reply.
    fn answer_once(
        token: &str,
        reply: &'static str,
    ) -> (EngineInfo, std::thread::JoinHandle<String>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let asked = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 1024];
            let read = stream.read(&mut request).unwrap();
            stream.write_all(reply.as_bytes()).unwrap();
            String::from_utf8_lossy(&request[..read]).into_owned()
        });
        let info = EngineInfo {
            port,
            token: token.into(),
        };
        (info, asked)
    }

    /// A 200 with that JSON, as the engine sends it.
    fn json_reply(body: &str) -> &'static str {
        Box::leak(
            format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{body}",
                body.len()
            )
            .into_boxed_str(),
        )
    }

    fn engine(info: Option<EngineInfo>) -> Engine {
        Engine {
            info: Mutex::new(info),
            launch: Launch {
                uv: PathBuf::from("uv"),
                engine: PathBuf::from("engine"),
                installed: false,
            },
            child: Mutex::new(None),
            stopped: Mutex::new(None),
            starting: AtomicBool::new(false),
            setup: Mutex::new(None),
            uv: Mutex::new(None),
        }
    }

    #[test]
    fn the_engine_is_asked_with_its_host_and_token_and_its_json_read() {
        let reply = json_reply(r#"{"recording": true, "meeting_id": 7, "live": true}"#);
        let (info, asked) = answer_once("secret", reply);
        let state = engine_get(&info, "/api/record").expect("an answer");
        assert_eq!(state["meeting_id"].as_i64(), Some(7));
        let request = asked.join().unwrap();
        assert!(request.starts_with("GET /api/record HTTP/1.1\r\n"));
        // The engine's gate refuses any other host (a page rebinding a name of its own).
        assert!(request.contains(&format!("\r\nHost: 127.0.0.1:{}\r\n", info.port)));
        assert!(request.contains("\r\nAuthorization: Bearer secret\r\n"));

        let refused = "HTTP/1.1 401 Unauthorized\r\ncontent-length: 2\r\n\r\n{}";
        let (info, asked) = answer_once("secret", refused);
        assert!(engine_get(&info, "/api/record").is_none());
        asked.join().unwrap();
    }

    #[test]
    fn the_engine_running_now_is_asked_even_after_a_restart() {
        // While it starts there's no port yet, and nothing is asked.
        let engine = engine(None);
        assert!(ask_engine(&engine, "/api/record").is_none());

        let (first, asked) = answer_once("first", json_reply(r#"{"run": 1}"#));
        *engine.info.lock().unwrap() = Some(first);
        assert_eq!(ask_engine(&engine, "/api/record").unwrap()["run"], 1);
        assert!(asked.join().unwrap().contains("Bearer first"));

        // Started again: a new port and a new token, read at the next ask.
        let (second, asked) = answer_once("second", json_reply(r#"{"run": 2}"#));
        let port = second.port;
        *engine.info.lock().unwrap() = Some(second);
        assert_eq!(ask_engine(&engine, "/api/record").unwrap()["run"], 2);
        let request = asked.join().unwrap();
        assert!(request.contains("Bearer second"));
        assert!(request.contains(&format!("Host: 127.0.0.1:{port}")));
    }

    fn event(id: i64, kind: &str) -> serde_json::Value {
        serde_json::json!({"id": id, "kind": kind, "app": "Microsoft Teams", "meeting_id": id})
    }

    fn ids(events: &[serde_json::Value]) -> Vec<i64> {
        events
            .iter()
            .map(|event| event["id"].as_i64().unwrap())
            .collect()
    }

    #[test]
    fn call_events_are_told_once_each_in_order_and_all_after_a_restart() {
        // The first answer from a run the shell started: everything in it is news.
        let state =
            serde_json::json!({"boot": "a1", "events": [event(2, "stopped"), event(1, "started")]});
        let (cursor, fresh) = new_events(&None, &state);
        assert_eq!(ids(&fresh), [1, 2]);
        assert_eq!(cursor, Some(("a1".to_string(), 2)));
        assert_eq!(record_path(&cursor), "/api/record?boot=a1&after=2");

        // Asked from the cursor on: an event already told is never told again.
        let state =
            serde_json::json!({"boot": "a1", "events": [event(2, "stopped"), event(3, "failed")]});
        let (cursor, fresh) = new_events(&cursor, &state);
        assert_eq!(ids(&fresh), [3]);
        let state = serde_json::json!({"boot": "a1", "events": []});
        let (cursor, fresh) = new_events(&cursor, &state);
        assert!(fresh.is_empty());
        assert_eq!(cursor, Some(("a1".to_string(), 3)));

        // The engine started again numbers its events from 1: all of them are news.
        let state = serde_json::json!({"boot": "b2", "events": [event(1, "started")]});
        let (cursor, fresh) = new_events(&cursor, &state);
        assert_eq!(ids(&fresh), [1]);
        assert_eq!(cursor, Some(("b2".to_string(), 1)));

        // An answer without a run changes nothing; a run that isn't hex isn't sent back.
        let (same, fresh) = new_events(&cursor, &serde_json::json!({"recording": false}));
        assert!(fresh.is_empty() && same == cursor);
        assert_eq!(record_path(&Some(("a&b".to_string(), 3))), "/api/record");
        assert_eq!(record_path(&None), "/api/record");
    }

    #[test]
    fn the_live_panel_opens_once_a_recording_and_closes_when_it_ends() {
        let mut seen = Following::default();
        assert_eq!(panel_after(&mut seen, false, None, false), None);
        assert_eq!(panel_after(&mut seen, true, Some(7), true), Some(true));
        assert_eq!(panel_after(&mut seen, true, Some(7), true), None);
        // Closed with its X: not opened again for this recording...
        seen.dismissed = seen.meeting;
        assert_eq!(panel_after(&mut seen, true, Some(7), true), None);
        assert_eq!(panel_after(&mut seen, false, None, false), Some(false));
        // ...but for the next one.
        assert_eq!(panel_after(&mut seen, true, Some(8), true), Some(true));
        // A Stop and a Start between two looks: the next recording's, at once.
        assert_eq!(panel_after(&mut seen, true, Some(9), true), Some(true));
        // Turned off in Settings mid-call: it closes; a recording without one never opens it.
        assert_eq!(panel_after(&mut seen, true, Some(9), false), Some(false));
        assert_eq!(panel_after(&mut seen, false, None, false), None);
        assert_eq!(panel_after(&mut seen, true, Some(10), false), None);
    }

    #[test]
    fn a_port_is_read_only_once_the_engine_has_written_it_whole() {
        let dir = std::env::temp_dir().join(format!("zachcap-port-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("engine.port");
        assert_eq!(read_port(&file), None, "not written yet");
        for (text, port) in [
            ("", None),
            ("0", None),
            ("70000", None),
            ("5432x", None),
            (
                "54321
",
                Some(54321),
            ),
        ] {
            std::fs::write(&file, text).unwrap();
            assert_eq!(read_port(&file), port, "{text:?}");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_engine_log_is_emptied_past_its_limit_and_carries_on_from_the_top() {
        let dir = std::env::temp_dir().join(format!("zachcap-log-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join(LOG);
        // The engine's end of it, opened to append the way start_engine opens it.
        let mut engine = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log)
            .unwrap();
        engine.write_all(&[b'x'; 2048]).unwrap();
        trim_log(&log, 4096);
        assert_eq!(
            std::fs::metadata(&log).unwrap().len(),
            2048,
            "under the limit"
        );
        engine.write_all(&[b'x'; 4096]).unwrap();
        trim_log(&log, 4096);
        engine.write_all(b"after\n").unwrap();
        let text = std::fs::read_to_string(&log).unwrap();
        assert!(text.starts_with("[ZachCap] engine.log passed"), "{text}");
        // Appended after the note, not past the old end with a hole of zeros before it.
        assert!(text.ends_with("was emptied.\nafter\n"), "{text:?}");
        drop(engine);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn keys_the_engine_saves_parse_in_both_shell_parsers() {
        // The forms engine/src/zachcap/shortcuts.py writes (tests/test_shortcuts.py keeps the
        // same list): modifiers in the order Ctrl, Alt, Shift, Win, then the key.
        for keys in [
            "Ctrl+Alt+R",
            "Ctrl+Alt+F9",
            "Ctrl+Alt+Shift+9",
            "Shift+Win+D",
            "Ctrl+Win+F24",
            "Alt+Win+0",
            "F13",
        ] {
            assert!(parse_keys(keys).is_some(), "the plugin can't read {keys}");
            assert!(
                dictation_keys::Chord::parse(keys).is_some(),
                "the hook can't read {keys}"
            );
        }
    }
}
