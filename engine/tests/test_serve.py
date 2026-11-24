import socket
import tempfile
import threading
from pathlib import Path

import pytest
import uvicorn

from zachcap import api
from zachcap.api import listen_on, report_port


def test_the_engine_binds_a_port_windows_picks_and_says_which(tmp_path: Path):
    sock = listen_on(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and port > 0
        report_port(sock, str(tmp_path / "engine.port"))
        assert (tmp_path / "engine.port").read_text(encoding="ascii") == str(port)
        assert list(tmp_path.iterdir()) == [tmp_path / "engine.port"]  # no half-written file left
        report_port(sock, None)  # started by hand, without the app: nothing to write
    finally:
        sock.close()


def test_starting_the_engine_removes_what_a_decode_left_behind(tmp_path: Path, monkeypatch):
    # A meeting's audio, in the temp folder since the PC went off in the middle of an import:
    # it doesn't wait for another import.
    left = tmp_path / "zachcap-decoding" / "tmpaudio"
    left.parent.mkdir()
    left.write_bytes(bytes(64))
    started = []

    class Thread:  # the engine's threads run as long as it does: none is started here
        def __init__(self, *args, target=None, name="", **kwargs) -> None:
            self.target, self.name = target, name

        def start(self) -> None:
            started.append(self)

    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ZACHCAP_PARENT_PID", raising=False)
    monkeypatch.delenv("ZACHCAP_PORT_FILE", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets: sockets[0].close())
    with monkeypatch.context() as patch:
        patch.setattr(threading, "Thread", Thread)
        api.serve(0, "test-token")

    (tidier,) = [thread for thread in started if thread.name == "zachcap-tidy"]
    tidier.target()
    assert list(left.parent.iterdir()) == []


@pytest.mark.skipif(not hasattr(socket, "SO_EXCLUSIVEADDRUSE"), reason="Windows only")
def test_no_other_socket_can_bind_the_engines_port():
    # Without exclusive use, Windows lets a socket with SO_REUSEADDR bind a port in use and
    # take its connections: the page's requests, token and all.
    sock = listen_on(0)
    other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            other.bind(("127.0.0.1", sock.getsockname()[1]))
    finally:
        other.close()
        sock.close()
