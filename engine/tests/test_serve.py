import socket
from pathlib import Path

import pytest

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
