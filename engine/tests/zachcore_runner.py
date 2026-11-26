"""The sender on its own, as the engine runs it, for test_zachcore_kills.py to kill: it sends
from the library ZACHCAP_TEST_LIBRARY to the tests' server, with the key the tests made for it
(a key for nothing else), looking at the outbox often rather than every few seconds."""

import os
from pathlib import Path

from zachcap import apikeys, sender

address, key = os.environ["ZACHCAP_TEST_ADDRESS"], os.environ["ZACHCAP_TEST_KEY"]
apikeys.zachcore = lambda wanted: key if wanted == address else None
sender.IDLE = 0.05
sender.Sender(Path(os.environ["ZACHCAP_TEST_LIBRARY"])).run_forever()
