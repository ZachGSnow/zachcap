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


def apps_using_mic() -> list[str]:
    """Friendly names of the apps using the microphone at this moment."""
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
                        label = _friendly(name)
                        if label not in found:
                            found.append(label)
    return found


def is_supported() -> bool:
    return os.name == "nt"
