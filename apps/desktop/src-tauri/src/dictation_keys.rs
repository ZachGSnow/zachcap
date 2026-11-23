//! Dictation's keys, read with a low-level keyboard hook while dictation is on.
//!
//! A hotkey registered with Windows only says when its keys go down; the shortcut plugin
//! finds the release by watching the main key alone. Let go of Ctrl or Alt first and that
//! key's repeats reach the app in front: Ctrl+D deletes a message in Outlook, and Alt+D
//! jumps to a browser's address bar, where the dictation would then be typed. The hook
//! swallows the main key from the moment the chord goes down until that key comes up (even
//! if the chord changes or dictation is turned off meanwhile), and the dictation ends as soon
//! as any key of the chord is let go.
//!
//! Windows doesn't show the hook keys typed into a window run as administrator. A hold whose
//! release is never seen (focus moved to such a window mid-hold) still ends: a watch checks
//! the chord's modifiers while a hold is under way, and a press of the main key long after
//! its last event is taken as a new press. Keys typed by programs (remappers, macro keys)
//! count like any other; only the mask key ZachCap types itself is let by.

use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering::SeqCst};
use std::sync::mpsc::{Sender, channel};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use windows_sys::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::{
    GetCurrentThread, GetCurrentThreadId, SetThreadPriority, THREAD_PRIORITY_HIGHEST,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, SendInput,
    VK_CONTROL, VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_MENU, VK_RCONTROL, VK_RMENU,
    VK_RSHIFT, VK_RWIN, VK_SHIFT,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, GetMessageW, HC_ACTION, KBDLLHOOKSTRUCT, MSG, PostThreadMessageW, SetTimer,
    SetWindowsHookExW, UnhookWindowsHookEx, WH_KEYBOARD_LL, WM_KEYDOWN, WM_QUIT, WM_SYSKEYDOWN,
    WM_TIMER,
};

const CTRL: u32 = 1;
const ALT: u32 = 2;
const SHIFT: u32 = 4;
const WIN: u32 = 8;
/// A key no keyboard has, typed while the chord's modifiers are down (as AutoHotkey does):
/// with the main key swallowed, Windows would otherwise see them pressed on their own, and
/// letting go of Win opens the Start menu, of Alt a window's menus, and Ctrl+Shift can switch
/// the keyboard layout.
const MASK_KEY: u16 = 0xE8;
/// Marks the keys ZachCap types itself (the mask key), so the hook lets them by.
const OURS: usize = 0x5A43_4150;
/// A press of the main key this long after its last event is a new press: its release was
/// missed, so it's no longer swallowed (repeats come every 30-500 ms).
const REPEAT_GAP_MS: u32 = 1000;
/// How often a hold checks that the chord's modifiers are still down.
const CHECK_EVERY: Duration = Duration::from_millis(50);
/// How often the hook is put back while no key is held: Windows removes a hook that once took
/// too long to answer, and doesn't say so.
const REFRESH_MS: u32 = 60_000;

/// A chord: one main key (its virtual-key code) and exactly the modifiers held with it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Chord {
    key: u32,
    modifiers: u32,
}

impl Chord {
    /// Keys as Settings saves them ("Ctrl+Alt+D"); None for keys that can't be a chord.
    pub fn parse(keys: &str) -> Option<Chord> {
        let mut parts: Vec<&str> = keys.split('+').map(str::trim).collect();
        let key = parts.pop()?.to_ascii_uppercase();
        let mut modifiers = 0;
        for part in parts {
            modifiers |= match part.to_ascii_lowercase().as_str() {
                "ctrl" | "control" => CTRL,
                "alt" => ALT,
                "shift" => SHIFT,
                "win" | "super" => WIN,
                _ => return None,
            };
        }
        let key = match key.as_bytes() {
            [c] if c.is_ascii_uppercase() || c.is_ascii_digit() => u32::from(*c),
            [b'F', number @ ..] => match std::str::from_utf8(number).ok()?.parse::<u32>().ok()? {
                n @ 1..=24 => 0x70 + n - 1, // VK_F1 is 0x70
                _ => return None,
            },
            _ => return None,
        };
        Some(Chord { key, modifiers })
    }
}

static KEY: AtomicU32 = AtomicU32::new(0); // the chord's main key; 0 while dictation is off
static MODIFIERS: AtomicU32 = AtomicU32::new(0);
static SWALLOWED: AtomicU32 = AtomicU32::new(0); // the key swallowed until it comes up (0: none)
static PASSED: AtomicBool = AtomicBool::new(false); // the main key reached the app: no hold yet
static LAST_MAIN: AtomicU32 = AtomicU32::new(0); // when its last event was (ms, from Windows)
static HOLDING: AtomicBool = AtomicBool::new(false); // a dictation is under way
static STOPPING: AtomicBool = AtomicBool::new(false); // off: the hook goes once that key is up
static EVENTS: OnceLock<Sender<bool>> = OnceLock::new(); // true: keys down; false: let go
static HOOK_THREAD: Mutex<Option<u32>> = Mutex::new(None);

/// Where the hold's start (true) and end (false) go. Called once, at start: the hook itself
/// only hands them over, so it never waits on anything. While a hold is under way, this
/// thread also checks that the chord's modifiers are still down: if their release was never
/// seen, the hold ends here.
pub fn on_change(handle: impl Fn(bool) + Send + 'static) {
    let (sender, receiver) = channel::<bool>();
    if EVENTS.set(sender).is_ok() {
        std::thread::spawn(move || {
            for down in receiver {
                if !down {
                    handle(false);
                    continue;
                }
                let modifiers = MODIFIERS.load(SeqCst);
                if modifiers != 0 {
                    mask();
                }
                handle(true);
                while HOLDING.load(SeqCst) {
                    std::thread::sleep(CHECK_EVERY);
                    if HOLDING.load(SeqCst) && !modifiers_down(modifiers) {
                        end_hold();
                    }
                }
            }
        });
    }
}

/// Listens for this chord, or (None) stops listening; either way a change ends a dictation
/// under way. Returns whether the hook is in place.
pub fn listen(chord: Option<Chord>) -> bool {
    let mut thread = HOOK_THREAD.lock().unwrap();
    let (key, modifiers) = chord.map_or((0, 0), |chord| (chord.key, chord.modifiers));
    if (KEY.load(SeqCst), MODIFIERS.load(SeqCst)) != (key, modifiers) {
        KEY.store(0, SeqCst);
        PASSED.store(false, SeqCst);
        end_hold();
        MODIFIERS.store(modifiers, SeqCst);
        KEY.store(key, SeqCst);
    }
    match (chord, *thread) {
        (Some(_), None) => {
            STOPPING.store(false, SeqCst);
            *thread = start_hook();
            thread.is_some()
        }
        (Some(_), Some(_)) => {
            STOPPING.store(false, SeqCst);
            true
        }
        (None, Some(id)) => {
            if SWALLOWED.load(SeqCst) != 0 {
                STOPPING.store(true, SeqCst); // the key held now is swallowed until it's up
            } else {
                // SAFETY: posting a message to a thread id; its loop ends on WM_QUIT.
                unsafe { PostThreadMessageW(id, WM_QUIT, 0, 0) };
                *thread = None;
            }
            false
        }
        (None, None) => false,
    }
}

/// A thread that holds the hook and waits for messages (Windows calls the hook from that
/// wait), at high priority so the hook answers in time. Returns its id, or None if Windows
/// refused the hook.
fn start_hook() -> Option<u32> {
    let (ready, started) = channel();
    std::thread::Builder::new()
        .name("dictation-keys".into())
        .spawn(move || {
            // SAFETY: plain Win32 calls; `message` is a zeroed MSG the system fills in.
            unsafe {
                SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);
                let module = GetModuleHandleW(null());
                let mut hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(on_key), module, 0);
                let _ = ready.send((!hook.is_null()).then(|| GetCurrentThreadId()));
                if hook.is_null() {
                    return;
                }
                SetTimer(null_mut(), 0, REFRESH_MS, None);
                let mut message: MSG = std::mem::zeroed();
                while GetMessageW(&mut message, null_mut(), 0, 0) > 0 {
                    if message.message != WM_TIMER || !quiet() {
                        continue;
                    }
                    if STOPPING.swap(false, SeqCst) {
                        break; // turned off while a key was held, which is up now
                    }
                    UnhookWindowsHookEx(hook);
                    hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(on_key), module, 0);
                }
                if !hook.is_null() {
                    UnhookWindowsHookEx(hook);
                }
                let mut thread = HOOK_THREAD.lock().unwrap();
                if *thread == Some(GetCurrentThreadId()) {
                    *thread = None; // stopped by itself, once the held key came up
                }
            }
        })
        .ok()?;
    started.recv().ok().flatten()
}

/// No key held for a dictation: the hook can be put back without missing anything.
fn quiet() -> bool {
    !HOLDING.load(SeqCst) && SWALLOWED.load(SeqCst) == 0
}

unsafe extern "system" fn on_key(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32 {
        // SAFETY: for HC_ACTION, lparam points to the event's KBDLLHOOKSTRUCT.
        let event = unsafe { &*(lparam as *const KBDLLHOOKSTRUCT) };
        let down = wparam == WM_KEYDOWN as usize || wparam == WM_SYSKEYDOWN as usize;
        if event.dwExtraInfo != OURS && swallow(event.vkCode, down, event.time, chord_held) {
            if STOPPING.load(SeqCst) && SWALLOWED.load(SeqCst) == 0 {
                STOPPING.store(false, SeqCst);
                // SAFETY: ends this thread's message loop once this event is answered.
                unsafe { PostThreadMessageW(GetCurrentThreadId(), WM_QUIT, 0, 0) };
            }
            return 1; // the app in front never sees it
        }
    }
    // SAFETY: passes the event on to the next hook, as every hook must.
    unsafe { CallNextHookEx(null_mut(), code, wparam, lparam) }
}

/// What the hook does with one key event, going down or up at `time` (ms): true swallows it.
/// `held` says whether exactly the chord's modifiers are down now.
fn swallow(vk: u32, down: bool, time: u32, held: fn(u32) -> bool) -> bool {
    let swallowed = SWALLOWED.load(SeqCst);
    if swallowed != 0 && vk == swallowed {
        let gap = time.wrapping_sub(LAST_MAIN.swap(time, SeqCst));
        if down && gap <= REPEAT_GAP_MS {
            return true; // its repeats while it's held
        }
        SWALLOWED.store(0, SeqCst);
        end_hold();
        if !down {
            return true; // its release: the app never saw it go down
        }
        // Long after its last event: its release was missed, and this is a new press.
    }
    let key = KEY.load(SeqCst);
    if key == 0 {
        return false;
    }
    if vk == key {
        let gap = time.wrapping_sub(LAST_MAIN.swap(time, SeqCst));
        if !down {
            PASSED.store(false, SeqCst);
            return false;
        }
        if PASSED.load(SeqCst) && gap <= REPEAT_GAP_MS {
            return false; // the app has it down already: its repeats are its own
        }
        if held(MODIFIERS.load(SeqCst)) {
            PASSED.store(false, SeqCst);
            SWALLOWED.store(vk, SeqCst);
            HOLDING.store(true, SeqCst);
            tell(true);
            return true;
        }
        PASSED.store(true, SeqCst);
        return false;
    }
    if !down && HOLDING.load(SeqCst) && modifier(vk) & MODIFIERS.load(SeqCst) != 0 {
        end_hold(); // Ctrl or Alt let go first: the dictation ends now
    }
    false
}

fn end_hold() {
    if HOLDING.swap(false, SeqCst) {
        tell(false);
    }
}

fn modifier(vk: u32) -> u32 {
    match u16::try_from(vk).unwrap_or(0) {
        VK_CONTROL | VK_LCONTROL | VK_RCONTROL => CTRL,
        VK_MENU | VK_LMENU | VK_RMENU => ALT,
        VK_SHIFT | VK_LSHIFT | VK_RSHIFT => SHIFT,
        VK_LWIN | VK_RWIN => WIN,
        _ => 0,
    }
}

/// Which modifiers are down now. Windows says none while a window run as administrator is in
/// front, which ends a hold there too.
fn modifiers_now() -> u32 {
    // SAFETY: GetAsyncKeyState only reads the key's state.
    let down = |vk: u16| (unsafe { GetAsyncKeyState(i32::from(vk)) } as u16) & 0x8000 != 0;
    let mut now = 0;
    for (vks, flag) in [
        (&[VK_CONTROL][..], CTRL),
        (&[VK_MENU][..], ALT),
        (&[VK_SHIFT][..], SHIFT),
        (&[VK_LWIN, VK_RWIN][..], WIN),
    ] {
        if vks.iter().any(|&vk| down(vk)) {
            now |= flag;
        }
    }
    now
}

/// Whether exactly these modifiers are down now.
fn chord_held(modifiers: u32) -> bool {
    modifiers_now() == modifiers
}

/// Whether these modifiers are all still down (others may be too).
fn modifiers_down(modifiers: u32) -> bool {
    modifiers_now() & modifiers == modifiers
}

fn mask() {
    let key = |flags| INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: MASK_KEY,
                wScan: 0,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: OURS,
            },
        },
    };
    let keys = [key(0), key(KEYEVENTF_KEYUP)];
    // SAFETY: two keyboard INPUTs, with their size.
    unsafe { SendInput(2, keys.as_ptr(), std::mem::size_of::<INPUT>() as i32) };
}

fn tell(down: bool) {
    if let Some(events) = EVENTS.get() {
        let _ = events.send(down);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::Receiver;

    /// The hook's state is shared by the whole process: the tests that use it take turns.
    static TURN: Mutex<()> = Mutex::new(());

    fn events() -> &'static Mutex<Receiver<bool>> {
        static RECEIVER: OnceLock<Mutex<Receiver<bool>>> = OnceLock::new();
        RECEIVER.get_or_init(|| {
            let (sender, receiver) = channel();
            let _ = EVENTS.set(sender);
            Mutex::new(receiver)
        })
    }

    fn told() -> Vec<bool> {
        events().lock().unwrap().try_iter().collect()
    }

    fn fresh(chord: &str) {
        let chord = Chord::parse(chord).unwrap();
        for flag in [&PASSED, &HOLDING, &STOPPING] {
            flag.store(false, SeqCst);
        }
        SWALLOWED.store(0, SeqCst);
        KEY.store(chord.key, SeqCst);
        MODIFIERS.store(chord.modifiers, SeqCst);
        told();
    }

    const D: u32 = 0x44;
    const CHORD: fn(u32) -> bool = |_| true;
    const NOT_CHORD: fn(u32) -> bool = |_| false;

    #[test]
    fn keys_as_settings_saves_them() {
        assert_eq!(
            Chord::parse("Ctrl+Alt+D"),
            Some(Chord {
                key: 0x44,
                modifiers: CTRL | ALT
            })
        );
        assert_eq!(
            Chord::parse("Shift+Win+9"),
            Some(Chord {
                key: 0x39,
                modifiers: SHIFT | WIN
            })
        );
        assert_eq!(
            Chord::parse("Ctrl+Alt+F9"),
            Some(Chord {
                key: 0x78,
                modifiers: CTRL | ALT
            })
        );
        assert_eq!(
            Chord::parse("F13"),
            Some(Chord {
                key: 0x7C,
                modifiers: 0
            })
        );
        assert_eq!(Chord::parse("Ctrl+Alt+F25"), None);
        assert_eq!(Chord::parse("Ctrl+Hyper+D"), None);
        assert_eq!(Chord::parse("Ctrl+Alt+Tab"), None);
    }

    #[test]
    fn every_chord_with_a_modifier_is_masked() {
        // The mask goes with any modifier: Ctrl+Shift alone can switch the keyboard layout.
        let start = |keys: &str| Chord::parse(keys).unwrap().modifiers != 0;
        assert!(start("Ctrl+Alt+D") && start("Ctrl+Shift+D") && start("Win+D"));
        assert!(!start("F13"));
    }

    #[test]
    fn the_main_key_never_reaches_the_app_and_any_key_let_go_ends_the_hold() {
        let _turn = TURN.lock().unwrap();
        fresh("Ctrl+Alt+D");
        let (ctrl, alt, other) = (u32::from(VK_LCONTROL), u32::from(VK_RMENU), 0x45);

        // Typed without the chord: let by, and no dictation.
        assert!(!swallow(D, true, 0, NOT_CHORD) && !swallow(D, false, 10, NOT_CHORD));
        assert!(told().is_empty());

        // Held and let go: D is swallowed down, repeating and up; the hold starts and ends.
        assert!(swallow(D, true, 1000, CHORD) && swallow(D, true, 1500, CHORD));
        assert!(swallow(D, false, 1530, CHORD));
        assert_eq!(told(), [true, false]);

        // Alt let go first: the hold ends then, and D's repeats and release still don't leak.
        assert!(swallow(D, true, 5000, CHORD));
        assert!(!swallow(other, true, 5010, CHORD)); // other keys always pass
        assert!(!swallow(alt, false, 5020, CHORD)); // the modifier itself passes
        assert!(swallow(D, true, 5040, NOT_CHORD) && swallow(D, false, 5070, NOT_CHORD));
        assert_eq!(told(), [true, false]);

        // Ctrl let go with no hold under way changes nothing.
        assert!(!swallow(ctrl, false, 6000, CHORD));
        assert!(told().is_empty());

        // Dictation off: everything passes.
        KEY.store(0, SeqCst);
        assert!(!swallow(D, true, 7000, CHORD) && !swallow(D, false, 7010, CHORD));
        assert!(told().is_empty());
    }

    #[test]
    fn a_release_never_seen_doesnt_keep_the_key_or_the_hold() {
        let _turn = TURN.lock().unwrap();
        fresh("Ctrl+Alt+D");
        assert!(swallow(D, true, 1000, CHORD));
        // Focus went to a window the hook can't see; D's release was never seen. Much later D
        // is pressed again without the chord: typed, and the old hold is over.
        assert!(!swallow(D, true, 9000, NOT_CHORD));
        assert_eq!(told(), [true, false]);
        assert!(!swallow(D, false, 9050, NOT_CHORD));
        // And held with the chord, it's a new dictation.
        assert!(swallow(D, true, 12_000, CHORD));
        assert_eq!(told(), [true]);
    }

    #[test]
    fn a_key_the_app_already_has_down_never_starts_a_hold() {
        let _turn = TURN.lock().unwrap();
        fresh("Ctrl+Alt+D");
        // Ctrl+Alt+Shift+D: not the chord, so D reaches the app and repeats there. Letting go of
        // Shift mid-repeat doesn't turn those repeats into a dictation with a swallowed release.
        assert!(!swallow(D, true, 1000, NOT_CHORD));
        assert!(!swallow(D, true, 1500, CHORD) && !swallow(D, true, 1530, CHORD));
        assert!(!swallow(D, false, 1560, CHORD));
        assert!(told().is_empty());
        assert!(swallow(D, true, 3000, CHORD)); // a fresh press with the chord is one
        assert_eq!(told(), [true]);
    }

    #[test]
    fn the_held_key_stays_swallowed_when_the_chord_changes_mid_hold() {
        let _turn = TURN.lock().unwrap();
        fresh("Ctrl+Alt+D");
        assert!(swallow(D, true, 1000, CHORD));
        // Settings changes the chord (or turns dictation off) while D is still held.
        KEY.store(u32::from(b'E'), SeqCst);
        end_hold();
        assert!(swallow(D, true, 1030, CHORD) && swallow(D, false, 1060, CHORD));
        assert_eq!(told(), [true, false]);
        assert!(!swallow(D, true, 2000, CHORD)); // D is an ordinary key now
    }
}
