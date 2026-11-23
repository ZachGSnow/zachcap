"""Call detection reads Windows' mic-in-use registry; a fake registry stands in for it."""

import pytest

from zachcap import calls


class FakeKey:
    def __init__(self, children=None, values=None):
        self.children = children or {}
        self.values = values or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def in_use():
    return FakeKey(values={"LastUsedTimeStart": 133_000_000_000_000_000, "LastUsedTimeStop": 0})


def finished():
    return FakeKey(
        values={
            "LastUsedTimeStart": 133_000_000_000_000_000,
            "LastUsedTimeStop": 133_000_000_100_000_000,
        }
    )


@pytest.fixture
def registry(monkeypatch):
    tree = FakeKey(
        {
            "MSTeams_8wekyb3d8bbwe": in_use(),
            "Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe": finished(),
            "NonPackaged": FakeKey(
                {
                    "C:#Users#me#AppData#Local#Programs#zachcap#python.exe": in_use(),
                    "C:#Program Files#Zoom#bin#Zoom.exe": in_use(),
                    "C:#Tools#softphone#Phone.exe": in_use(),
                }
            ),
        }
    )

    def open_key(parent, path):
        node = tree if parent is calls.winreg.HKEY_CURRENT_USER else parent
        parts = path.split("\\")
        if parent is calls.winreg.HKEY_CURRENT_USER:
            parts = path[len(calls.ROOT) :].strip("\\").split("\\") if path != calls.ROOT else []
        for part in filter(None, parts):
            if part not in node.children:
                raise OSError("no such key")
            node = node.children[part]
        return node

    def enum_key(key, index):
        names = list(key.children)
        if index >= len(names):
            raise OSError("no more keys")
        return names[index]

    def query_value(key, name):
        if name not in key.values:
            raise OSError("no such value")
        return key.values[name], 11

    monkeypatch.setattr(calls.winreg, "OpenKey", open_key)
    monkeypatch.setattr(calls.winreg, "EnumKey", enum_key)
    monkeypatch.setattr(calls.winreg, "QueryValueEx", query_value)


def test_apps_holding_the_mic_are_reported_by_friendly_name(registry):
    # Teams and Zoom are known; an unknown softphone falls back to its exe name.
    # The finished Sound Recorder session and ZachCap's own Python are left out.
    assert calls.apps_using_mic() == ["Microsoft Teams", "Zoom", "Phone"]
