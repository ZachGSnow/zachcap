"""The tests never touch the library on this PC: one that doesn't say where its own is
(ZACHCAP_HOME) finds a folder here that goes when they end. It's set for the whole run rather than
by a fixture, so a test that takes back its own environment (monkeypatch.undo) is back at this."""

import atexit
import os
import shutil
import tempfile

home = tempfile.mkdtemp(prefix="zachcap-tests-")
os.environ["ZACHCAP_HOME"] = home
atexit.register(shutil.rmtree, home, ignore_errors=True)
