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
    if let Ok(mut file) = std::fs::OpenOptions::new().write(true).open(log)
        && file.set_len(0).is_ok()
    {
        let _ = writeln!(
            file,
            "[ZachCap] {LOG} passed {} MB and was emptied.",
            limit / (1024 * 1024)
        );
    }
}

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
                    None if engine.stopped.lock().unwrap().is_none() => {
                        if let Some(mut child) = engine.child.lock().unwrap().take() {
                            let _ = child.kill();
                        }
                        *engine.stopped.lock().unwrap() =
                            Some("The engine started but never said which port it's on.".into());
                    }
                    None => {} // it stopped; check_engine said why
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

/// Brings an installed copy's engine venv up to date with the bundled uv: Python and the
/// speech libraries on first launch (a few GB), what changed after an update. Skipped when
/// this build is set up already (the installer's BUILD stamp is in the venv). What uv says
/// is shown on the startup screen and kept in engine-setup.log.
fn set_up(engine: &Engine) -> Result<(), String> {
    let launch = &engine.launch;
    if !launch.installed {
        return Ok(());
    }
    let venv = launch.engine.join(".venv");
    let marker = venv.join("zachcap-build.txt");
    let build = std::fs::read_to_string(launch.engine.join("BUILD")).unwrap_or_default();
    if !build.is_empty() && std::fs::read_to_string(&marker).is_ok_and(|done| done == build) {
        return Ok(());
    }
    *engine.setup.lock().unwrap() = Some("Getting ready...".to_string());
    let mut command = Command::new(&launch.uv);
    command
        .arg("sync")
        .arg("--project")
        .arg(&launch.engine)
        // The lock as shipped, without the test tools, with ZachCap's own code copied into
        // the venv (so the installed files are only read) and always refreshed from them.
        .args([
            "--locked",
            "--no-dev",
            "--no-editable",
            "--reinstall-package",
            "zachcap",
        ])
        .env("UV_PROJECT_ENVIRONMENT", &venv)
        .env_remove("VIRTUAL_ENV")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    no_window(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Couldn't run the bundled uv ({error})."))?;
    let mut log = std::fs::create_dir_all(data_dir())
        .and_then(|_| std::fs::File::create(data_dir().join("engine-setup.log")))
        .ok();
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
    let status = child.wait().map_err(|error| error.to_string())?;
    if !status.success() {
        return Err(format!(
            "Setting up the engine didn't finish ({last}). Check the internet connection and press Try again. \
             What happened is in {}.",
            data_dir().join("engine-setup.log").display()
        ));
    }
    let _ = std::fs::write(&marker, &build);
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
        command.args(["-m", "zachcap.cli"]);
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
            });
            begin(app.handle().clone());

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
            // Closing the window keeps ZachCap (and any recording) running in the tray.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
                tell_about_tray(window.app_handle());
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
            pause_shortcuts
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
            }
        });
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

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
