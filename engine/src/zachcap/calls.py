"""Notice when a call starts, without any Teams or calendar integration.

Windows records which apps are using the microphone right now under
HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone: an app is using it while
its LastUsedTimeStop is 0. That is the same signal behind the mic icon in the tray.
"""

from __future__ import annotations

import os
import winreg

ROOT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

# Known meeting apps: a fragment of the registry key name -> friendly name.
KNOWN = {
    "msteams": "Microsoft Teams",
    "teams.exe": "Microsoft Teams",
    "zoom": "Zoom",
    "webex": "Webex",
    "slack": "Slack",
    "discord": "Discord",
    "3cx": "3CX",
    "ringcentral": "RingCentral",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "vivaldi.exe": "Vivaldi",
    "brave.exe": "Brave",
}
# Web browsers among them: a call in a tab, and the browser's other windows could show anything.
BROWSERS = {"Chrome", "Edge", "Firefox", "Vivaldi", "Brave"}
# Never report ourselves: recording opens the mic from Python.
IGNORED = ("python", "zachcap")


def _in_use(key) -> bool:
    try:
        stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
        start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
    except OSError:
        return False
    return stop == 0 and start > 0


def _friendly(name: str) -> str:
    lowered = name.lower()
    for fragment, label in KNOWN.items():
        if fragment in lowered:
            return label
    # NonPackaged keys are paths with # for \ ; packaged ones are Publisher.App_hash.
    tail = name.replace("#", "\\").split("\\")[-1]
    return tail.split("_")[0].removesuffix(".exe")


def is_meeting_app(name: str) -> bool:
    """A known meeting or call app (as named by apps_using_mic), not a screen recorder or
    Windows' own Sound settings."""
    return name in set(KNOWN.values())


def _keys_in_use() -> list[str]:
    """The registry key names of the apps using the microphone at this moment: a packaged
    app's package family name (MSTeams_8wekyb3d8bbwe), or a program's path with # for \\."""
    found: list[str] = []
    for path in (ROOT, ROOT + r"\NonPackaged"):
        try:
            parent = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            continue
        with parent:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                if name == "NonPackaged" or any(word in name.lower() for word in IGNORED):
                    continue
                with winreg.OpenKey(parent, name) as key:
                    if _in_use(key):
                        found.append(name)
    return found


def apps_using_mic() -> list[str]:
    """Friendly names of the apps using the microphone at this moment."""
    found: list[str] = []
    for name in _keys_in_use():
        label = _friendly(name)
        if label not in found:
            found.append(label)
    return found


def meeting_app_keys() -> list[str]:
    """The registry keys of the meeting apps using the microphone now (see key_matches)."""
    return [name for name in _keys_in_use() if is_meeting_app(_friendly(name))]


def is_browser(key: str) -> bool:
    """Whether a registry key (see meeting_app_keys) is a web browser's."""
    return _friendly(key) in BROWSERS


def key_matches(key: str, exe_path: str) -> bool:
    """Whether a running program (its full path) is the app a registry key names: the same
    path for a program, or a file inside that package's folder for a packaged app
    (C:\\Program Files\\WindowsApps\\MSTeams_25306.804.4102.7193_x64__8wekyb3d8bbwe\\...)."""
    exe = exe_path.lower()
    if "#" in key:
        return exe == key.replace("#", "\\").lower()
    family, _, publisher = key.lower().rpartition("_")
    if not family or not publisher:
        return False
    folders = exe.split("\\")
    return any(
        part.startswith(family + "_") and part.endswith("__" + publisher) for part in folders
    )


def is_supported() -> bool:
    return os.name == "nt"
