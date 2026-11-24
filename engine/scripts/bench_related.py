"""Time how long a meeting's related meetings take on a large library.

Builds a synthetic library of 1,500 meetings in a scratch folder, 200 lines each: everyday talk
mixed with made-up general words (a long tail, as in speech), and for each meeting a few words
of IT work it is about. People of 30 clients (Contoso's are Dana Lee and Mark Ruiz), tickets,
and five topics come with it. Then, for 20 meetings, times GET /api/meetings/{id}/related
through the API (a new database connection per request, as in the app): the first time after a
change, with nothing worked out yet; the first time once the meeting list has worked out its
people and clients (the app asks for that list every few seconds); and again, kept. Nothing
outside the scratch folder is touched.

    uv run python scripts/bench_related.py [meetings] [lines]

The app's own requests for the meeting list come every few seconds, so "meeting list kept" is
what a meeting page usually waits for after a change, and "kept" any time after that.
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import tempfile
import time
from itertools import accumulate
from pathlib import Path

MEETINGS = 1500
LINES = 200
TOPICS = """Firewall: firewall, fortigate
VPN: vpn, tunnel
Email: mailbox, outlook
Backups: backup, restore
Licensing: license, renewal"""
# What any call is full of, whatever it's about.
EVERYDAY = """so yeah I think we should just check the and it is that you can see on if going do
this with for what about like right was be then now okay well we'll there they have one
time get back up here look let me will all some today next week also any how out make sure
know good thanks again working still need try see first other people thing things actually
maybe really after before over into just more only sorry great perfect morning""".split()
# Words of IT work: each meeting is about a few of them, and says others in passing.
WORK = list(
    dict.fromkeys(
        """firewall fortigate vpn tunnel profile cutover switch router printer toner scanner
mailbox outlook calendar license renewal backup restore snapshot server hypervisor cluster
datastore tenant intune autopilot laptop desktop monitor dock headset teams sharepoint
onedrive permissions folder share drive mapping domain controller dns dhcp certificate
expiry wildcard password reset mfa authenticator phishing quarantine spam filter endpoint
antivirus patching reboot maintenance window rollback firmware wireless access point ssid
guest network vlan subnet gateway isp circuit outage latency bandwidth voip phones extension
voicemail ivr queue fax copier lease invoice quote budget purchase order warranty asset
inventory onboarding offboarding migration exchange retention archive compliance audit
policy conditional sso saml connector sync azure entra group assignment storage quota
recycle versioning workflow approval form database sql agent job schedule script powershell
registry gpo battery keyboard webcam projector badge cabling rack ups""".split()
    )
)


def made_up_words(rng: random.Random, count: int) -> list[str]:
    """Distinct made-up words of two to four syllables ("ketalo")."""
    words: dict[str, None] = {}
    while len(words) < count:
        syllables = (rng.choice("bcdfghjklmnprstvwz") + rng.choice("aeiou") for _ in range(4))
        words["".join(syllable for syllable in syllables)[: rng.choice((4, 6, 8))]] = None
    return list(words)


def build(folder: Path, meetings: int = MEETINGS, lines: int = LINES, seed: int = 7):
    from zachcap import topics
    from zachcap.store import Store
    from zachcap.voiceprint import Match

    rng = random.Random(seed)
    general = made_up_words(rng, 4000)  # said across the library, some far more than others
    general_weights = list(accumulate(1 / (rank + 10) for rank in range(len(general))))
    rare = made_up_words(rng, 20000)  # each said a few times in all
    # Some kinds of work come up in many meetings, most in a few.
    work_weights = list(accumulate(1 / (rank + 1) ** 0.5 for rank in range(len(WORK))))
    store = Store(folder / "zachcap.db")
    store.set_setting(topics.SETTING, TOPICS)
    clients = ["Contoso", "Northwind", "Fabrikam"] + [f"Client {n}" for n in range(4, 31)]
    contacts = {
        client: (["Dana Lee", "Mark Ruiz"] if client == "Contoso" else [])
        + [f"Contact {n} of {client}" for n in range(1, 4)]
        for client in clients
    }
    tickets = [str(1_900_000 + n * 37) for n in range(400)]
    with store.transaction():
        me = store.me()["id"]
        people = {name: store.person_id(name) for names in contacts.values() for name in names}
        for client, names in contacts.items():
            for name in names:
                store.set_client(people[name], client)
        for number in range(meetings):
            client = clients[min(int(rng.expovariate(0.15)), len(clients) - 1)]
            day = f"2026-{1 + number * 11 // meetings:02d}-{1 + number % 28:02d}"
            meeting = store.create_meeting(
                f"Call {number}",
                f"{day}T{9 + number % 8:02d}:00:00-05:00",
                folder / f"m{number}",
                duration=lines * 6.0,
            )
            if rng.random() < 0.4:
                store.update_meeting(meeting, ticket=rng.choice(tickets), ticket_source="user")
            subject = sorted(set(rng.choices(WORK, cum_weights=work_weights, k=4)))
            speakers = [
                store.add_speaker(meeting, "mic", "You", None, 60.0, None, Match(me, 1.0, "auto"))
            ]
            for name in rng.sample(contacts[client], rng.randint(1, 3)):
                status = rng.choice(["confirmed", "auto", "auto", "suggested"])
                match = Match(people[name], 0.8, status)
                speakers.append(store.add_speaker(meeting, "system", name, None, 60.0, None, match))
            rows = []
            for line in range(lines):
                words = []
                for _ in range(rng.randint(8, 16)):
                    roll = rng.random()
                    if roll < 0.55:
                        words.append(rng.choice(EVERYDAY))
                    elif roll < 0.85:
                        words.append(rng.choices(general, cum_weights=general_weights)[0])
                    elif roll < 0.91:
                        words.append(rng.choice(subject))
                    elif roll < 0.95:
                        words.append(rng.choices(WORK, cum_weights=work_weights)[0])
                    else:
                        words.append(rng.choice(rare))
                speaker = rng.choice(speakers)
                track = "mic" if speaker == speakers[0] else "system"
                text = " ".join(words).capitalize() + "."
                rows.append((speaker, track, line * 6.0, line * 6.0 + 5, text, 0.9))
            store.add_segments(meeting, rows)
            store.refresh_status(meeting)
    return store


def main() -> None:
    from fastapi.testclient import TestClient

    from zachcap import related, topics
    from zachcap import store as stores
    from zachcap.api import create_app

    meetings = int(sys.argv[1]) if len(sys.argv) > 1 else MEETINGS
    lines = int(sys.argv[2]) if len(sys.argv) > 2 else LINES
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        os.environ["ZACHCAP_HOME"] = scratch
        started = time.perf_counter()
        store = build(Path(scratch), meetings, lines)
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = os.path.getsize(Path(scratch) / "zachcap.db") / 1e6
        print(
            f"built {meetings} meetings of {lines} lines ({size:.0f} MB) in "
            f"{time.perf_counter() - started:.0f} s"
        )
        ids = [row["id"] for row in store.meetings()][:: max(1, meetings // 20)][:20]
        store.close()
        # The engine only answers requests to this PC (gate.py).
        api = TestClient(create_app("bench"), base_url="http://127.0.0.1")
        headers = {"Authorization": "Bearer bench"}

        def forget() -> None:  # as after a change: nothing worked out is kept
            stores._by_lines.clear()
            topics._counted.clear()
            topics._saying.clear()
            related._kept.clear()

        def timed(meeting_id: int) -> tuple[float, float, list]:
            """Wall time, CPU time (what a busy computer doesn't stretch as much), the answer."""
            began, cpu = time.perf_counter(), time.process_time()
            answer = api.get(f"/api/meetings/{meeting_id}/related", headers=headers)
            took, spent = time.perf_counter() - began, time.process_time() - cpu
            assert answer.status_code == 200, answer.text
            return took, spent, answer.json()

        timed(ids[0])  # the first request of a run also loads what it needs
        runs: dict[str, list[tuple[float, float]]] = {
            "nothing kept": [],
            "meeting list kept": [],
            "kept": [],
        }
        shown = None
        for meeting_id in ids:
            forget()
            runs["nothing kept"].append(timed(meeting_id)[:2])
            forget()
            assert api.get("/api/meetings", headers=headers).status_code == 200
            runs["meeting list kept"].append(timed(meeting_id)[:2])
            took, spent, found = timed(meeting_id)
            runs["kept"].append((took, spent))
            shown = shown or (meeting_id, found)
        api.close()
        for name, times in runs.items():
            wall = [took * 1000 for took, _ in times]
            cpu = [spent * 1000 for _, spent in times]
            print(
                f"{name}: median {statistics.median(wall):.0f} ms, best {min(wall):.0f}, "
                f"slowest {max(wall):.0f}; CPU median {statistics.median(cpu):.0f} ms "
                f"({len(times)} meetings)"
            )
        if shown:
            print(f"M{shown[0]}:")
            for found in shown[1]:
                print(f"  M{found['id']} {found['score']}: {'; '.join(found['reasons'])}")


if __name__ == "__main__":
    main()
