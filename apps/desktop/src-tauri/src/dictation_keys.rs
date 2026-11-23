//! Dictation's keys, read with a low-level keyboard hook while dictation is on.
//!
//! A hotkey registered with Windows only says when its keys go down; the shortcut plugin
//! finds the release by watching the main key alone. Let go of Ctrl or Alt first and that
//! key's repeats reach the app in front: Ctrl+D deletes a message in Outlook, and Alt+D
//! jumps to a browser's address bar, where the dictation would then be typed. The hook
//! swallows the main key from the moment the chord goes down until that key comes up, and
//! the dictation ends as soon as any key of the chord is let go. Windows doesn't show the
//! hook keys typed into a window run as administrator, so nothing happens there (dictation
//! can't type into one anyway). Keys typed by programs (dictation's own typing) are let by.

use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering::SeqCst};
use std::sync::mpsc::{Sender, channel};
use std::sync::{Mutex, OnceLock};

use windows_sys::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, SendInput,
    VK_CONTROL, VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_MENU, VK_RCONTROL, VK_RMENU,
    VK_RSHIFT, VK_RWIN, VK_SHIFT,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, GetMessageW, HC_ACTION, KBDLLHOOKSTRUCT, LLKHF_INJECTED, MSG,
    PostThreadMessageW, SetWindowsHookExW, UnhookWindowsHookEx, WH_KEYBOARD_LL, WM_KEYDOWN,
    WM_QUIT, WM_SYSKEYDOWN,
};

const CTRL: u32 = 1;
const ALT: u32 = 2;
const SHIFT: u32 = 4;
const WIN: u32 = 8;
/// A key no keyboard has, typed while Win or Alt is down (as AutoHotkey does): with the main
/// key swallowed, letting go of Win would open the Start menu, and of Alt a window's menus.
const MASK_KEY: u16 = 0xE8;

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
static SWALLOWING: AtomicBool = AtomicBool::new(false); // the main key went down with the chord
static HOLDING: AtomicBool = AtomicBool::new(false); // a dictation is under way
static EVENTS: OnceLock<Sender<bool>> = OnceLock::new(); // true: keys down; false: let go
static HOOK_THREAD: Mutex<Option<u32>> = Mutex::new(None);

/// Where the hold's start (true) and end (false) go. Called once, at start: the hook
/// itself only hands them over, so it never waits on anything.
pub fn on_change(handle: impl Fn(bool) + Send + 'static) {
    let (sender, receiver) = channel::<bool>();
    if EVENTS.set(sender).is_ok() {
        std::thread::spawn(move || {
            for down in receiver {
                if down && needs_mask(MODIFIERS.load(SeqCst)) {
                    mask();
                }
                handle(down);
            }
        });
    }
}

/// Listens for this chord, or (None) stops listening; either way a change ends a
/// dictation under way. Returns whether the hook is in place.
pub fn listen(chord: Option<Chord>) -> bool {
    let mut thread = HOOK_THREAD.lock().unwrap();
    let (key, modifiers) = chord.map_or((0, 0), |chord| (chord.key, chord.modifiers));
    if (KEY.load(SeqCst), MODIFIERS.load(SeqCst)) != (key, modifiers) {
        KEY.store(0, SeqCst);
        SWALLOWING.store(false, SeqCst);
        if HOLDING.swap(false, SeqCst) {
            tell(false);
        }
        MODIFIERS.store(modifiers, SeqCst);
        KEY.store(key, SeqCst);
    }
    match (chord, *thread) {
        (Some(_), None) => {
            *thread = start_hook();
            thread.is_some()
        }
        (Some(_), Some(_)) => true,
        (None, Some(id)) => {
            // SAFETY: posting a message to a thread id; the thread ends its loop on WM_QUIT.
            unsafe { PostThreadMessageW(id, WM_QUIT, 0, 0) };
            *thread = None;
            false
        }
        (None, None) => false,
    }
}

/// A thread that holds the hook and waits for messages (Windows calls the hook from that
/// wait). Returns its id, or None if Windows refused the hook.
fn start_hook() -> Option<u32> {
    let (ready, started) = channel();
    std::thread::Builder::new()
        .name("dictation-keys".into())
        .spawn(move || {
            // SAFETY: plain Win32 calls; `message` is a zeroed MSG the system fills in.
            unsafe {
                let hook =
                    SetWindowsHookExW(WH_KEYBOARD_LL, Some(on_key), GetModuleHandleW(null()), 0);
                let _ = ready.send((!hook.is_null()).then(|| GetCurrentThreadId()));
                if hook.is_null() {
                    return;
                }
                let mut message: MSG = std::mem::zeroed();
                while GetMessageW(&mut message, null_mut(), 0, 0) > 0 {}
                UnhookWindowsHookEx(hook);
            }
        })
        .ok()?;
    started.recv().ok().flatten()
}

unsafe extern "system" fn on_key(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32 {
        // SAFETY: for HC_ACTION, lparam points to the event's KBDLLHOOKSTRUCT.
        let event = unsafe { &*(lparam as *const KBDLLHOOKSTRUCT) };
        let down = wparam == WM_KEYDOWN as usize || wparam == WM_SYSKEYDOWN as usize;
        if event.flags & LLKHF_INJECTED == 0 && swallow(event.vkCode, down, chord_held) {
            return 1; // the app in front never sees it
        }
    }
    // SAFETY: passes the event on to the next hook, as every hook must.
    unsafe { CallNextHookEx(null_mut(), code, wparam, lparam) }
}

/// What the hook does with one key event, going down or up: true swallows it. `held` says
/// whether exactly the chord's modifiers are down now.
fn swallow(vk: u32, down: bool, held: fn(u32) -> bool) -> bool {
    let key = KEY.load(SeqCst);
    if key == 0 {
        return false;
    }
    if vk == key {
        if down {
            if SWALLOWING.load(SeqCst) {
                return true; // its repeats while it's held
            }
            if held(MODIFIERS.load(SeqCst)) {
                SWALLOWING.store(true, SeqCst);
                HOLDING.store(true, SeqCst);
                tell(true);
                return true;
            }
            return false;
        }
        if SWALLOWING.swap(false, SeqCst) {
            if HOLDING.swap(false, SeqCst) {
                tell(false);
            }
            return true; // its release: the app never saw it go down
        }
        return false;
    }
    if !down && HOLDING.load(SeqCst) && modifier(vk) & MODIFIERS.load(SeqCst) != 0 {
        HOLDING.store(false, SeqCst); // Ctrl or Alt let go first: the dictation ends now
        tell(false);
    }
    false
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

/// Whether exactly these modifiers are down now.
fn chord_held(modifiers: u32) -> bool {
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
    now == modifiers
}

/// Whether letting go of the chord's modifiers would open a menu (Ctrl with Alt doesn't:
/// it's AltGr on many keyboards, which never opens one).
fn needs_mask(modifiers: u32) -> bool {
    modifiers & WIN != 0 || (modifiers & ALT != 0 && modifiers & CTRL == 0)
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
                dwExtraInfo: 0,
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
    fn letting_go_never_opens_a_menu() {
        assert!(!needs_mask(CTRL | ALT)); // AltGr on many keyboards: never opens one
        assert!(needs_mask(ALT | SHIFT) && needs_mask(WIN) && needs_mask(CTRL | WIN));
        assert!(!needs_mask(CTRL | SHIFT) && !needs_mask(0));
    }

    /// One test drives the hook's state, which is shared by the whole process.
    #[test]
    fn the_main_key_never_reaches_the_app_and_any_key_let_go_ends_the_hold() {
        let (sender, events) = channel();
        let _ = EVENTS.set(sender);
        let d = 0x44;
        let (ctrl, alt, other) = (u32::from(VK_LCONTROL), u32::from(VK_RMENU), 0x45);
        let chord: fn(u32) -> bool = |_| true;
        let not_chord: fn(u32) -> bool = |_| false;
        KEY.store(d, SeqCst);
        MODIFIERS.store(CTRL | ALT, SeqCst); // Ctrl+Alt+D

        // Typed without the chord: let by, and no dictation.
        assert!(!swallow(d, true, not_chord) && !swallow(d, false, not_chord));
        assert!(events.try_recv().is_err());

        // Held and let go: D is swallowed down, repeating and up; the hold starts and ends.
        assert!(swallow(d, true, chord) && swallow(d, true, chord) && swallow(d, false, chord));
        assert_eq!(events.try_iter().collect::<Vec<_>>(), [true, false]);

        // Alt let go first: the hold ends then, and D's repeats and release still don't leak.
        assert!(swallow(d, true, chord));
        assert!(!swallow(other, true, chord)); // other keys always pass
        assert!(!swallow(alt, false, chord)); // the modifier itself passes
        assert!(swallow(d, true, not_chord) && swallow(d, false, not_chord));
        assert_eq!(events.try_iter().collect::<Vec<_>>(), [true, false]);

        // Ctrl let go with no hold under way changes nothing.
        assert!(!swallow(ctrl, false, chord));
        assert!(events.try_recv().is_err());

        // Dictation off: everything passes.
        KEY.store(0, SeqCst);
        assert!(!swallow(d, true, chord) && !swallow(d, false, chord));
        assert!(events.try_recv().is_err());
    }
}
