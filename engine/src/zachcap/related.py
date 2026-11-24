"""Related meetings: the few others about the same thing, each with the reason.

A meeting's page lists them, so the call about the same ticket, or the last one with this
client, is a click away; Ask and the MCP server list them too. Another meeting is related by
what it shares with this one, the strongest first:

- the same ticket number;
- the same client, set on it or from its people (as clients.py has it);
- people in both, named or recognized by voice: never you, and never someone only suggested;
- topics from Settings said in both (as topics.py counts them);
- the same distinctive words: what this meeting says that few lines of the library do (tf-idf,
  from the full-text index search uses), said in the other one too. Everyday words, names
  (people's, clients', the ones you answer to), the topics' words and numbers (times, dates,
  ticket and phone numbers) are left out.

Meetings still being recorded, or waiting to be processed, are left out. Worked out on this PC
from what the library already has; nothing is sent, and nothing is downloaded.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import unicodedata
from collections import Counter
from typing import NamedTuple

from . import clients, mentions, topics
from .store import Store

# Points for each thing another meeting shares with this one. Each kind outweighs everything
# after it together, so the strongest thing shared decides, and the others order the meetings
# that share as much. Equal scores: the newest first.
TICKET = 32
CLIENT = 16
PERSON = 4  # for each person in both, three at most
TOPIC = 1  # for each topic said in both, three at most
WORDS = 1  # times how much of this meeting's distinctive words it says: 0.1 to 0.9, by tenths
MOST_COUNTED = 3  # people, and topics
LIMIT = 5

UNFINISHED = ("recording", "recorded", "processing")  # not listed until they're processed

CANDIDATES = 40  # this meeting's most said words, looked up in the library
TERMS = 8  # of those, the most distinctive ones said elsewhere, looked for in other meetings
MIN_TERMS = 2  # of them another meeting must say for words to count (one is a coincidence)...
ENOUGH = 1  # ...and how many tenths of the distinctive words, by weight, that must be...
MIN_TERMS_ALONE, ENOUGH_ALONE = 3, 2  # ...and when words are all the two meetings share
# A word, or a topic, said on more lines of the library than this is said all over it, so it
# doesn't tell meetings apart, and finding which meetings say it would take too long.
MAX_LINES = 1500  # a word
MAX_TOPIC_LINES = 4000  # any word of a topic
WIDESPREAD = 3  # nor does a word said in more than a third of the other meetings
SHOWN_NAMES = 3  # people or topics named in a reason
SHOWN_TERMS = 3  # words named in one

# Words as the full-text index splits them: runs of letters and digits ("VPN-profile's" is
# "vpn", "profile", "s").
TOKEN = re.compile(r"[^\W_]+")
# Numbers, and words that are mostly one: times, dates, amounts ("10am", "3rd", "5gb").
NUMBER = re.compile(r"\d+[a-z]{0,3}")
SENTENCE_START = re.compile(r"(?:^|[.!?])\W*$")  # what comes before a word that starts one
# Words any call is full of: grammar, talk, time, small talk, and what's said about work of
# every kind.
STOP_WORDS = frozenset(
    """
    able about above across actually add added after afternoon again against ago ahead ahh all
    almost alone along already alright also although always among and another any anybody anyhow
    anyone anything anyway anyways anywhere appreciate are aren around ask asked asking asks
    away awesome back bad barbecue baseball basically basketball beach because been before
    behind being below beside besides better between beyond big billion birthday bit both
    breakfast bring but bye call called calling calls came can cannot cat cats change changed
    changes changing check checked checking clear clearly client clients close coffee com come
    comes coming cool corp could couldn couple customer customers day days december definitely
    did didn different dinner does doesn dog dogs doing don done down drop dropped dropping
    drops during each early easy eight either eleven else email emails end enough even evening
    eventually ever every everybody everyone everything everywhere exactly except fact families
    family far february feel few fifteen fifth fifty figure figured file files fine first five
    folks football for former forty found four fourth friday from front full further game games
    general get gets getting give given gives giving glad goes going golf gone gonna good got
    gotcha gotta great guess guys had hadn half happen happened happening happy hard has hasn
    have haven having hello help helped helpful her here hers herself hey high him himself his
    hmm hockey hold honestly hope hour hours how however https huge huh hundred husband idea
    ideas important inc instead into isn issue issues its itself january july june just keep
    keeps kid kids kind kinda knew know known last late later least leave less let lets like
    likely line lines listen little llc long look looked looking looks lot lots low ltd lunch
    made mainly make makes making many march matter may maybe mean means meant meeting meetings
    mhm might million mind mine minute minutes moment monday month months more morning most
    mostly move movie movies much must myself name names need needed needs neither never
    nevertheless new next nice night nine nobody none nope nor not nothing notice noticed
    november now nowhere number numbers obviously october off often okay old once one only onto
    ooh option options other others otherwise our ours ourselves out over own part parts people
    per perfect perhaps person place plan planned planning plans please plus point points pretty
    previous probably problem problems put putting question questions quick quickly quite rather
    ready real really reason reasons redacted regarding remember request requested requests
    right run running said same saturday saw say saying says second seconds see seeing seem
    seemed seems seen send sending sent september seriously set sets setting seven several shall
    she should shouldn show showed showing shows side simple since single sit six sixty small
    soccer some somebody somehow someone something sometime sometimes somewhere soon sorry sort
    sorta start started starting starts step still stop stuff such sunday super sure take taken
    takes taking talk talked talking team tell telling ten test tested testing tests thank
    thanks that the their theirs them themselves then there these they thing things think
    thinking third thirteen thirty this those though thought thousand three through throughout
    thursday ticket tickets time times today together told tomorrow tonight too took totally
    toward towards tried tries trip trips try trying tuesday turn turned twelve twenty two uhh
    uhm umm under understand understood unless until update updated updates updating upon use
    used user users uses using usually vacation very via wait waiting walk wanna want wanted
    wants was wasn way ways weather wednesday week weekend weekends weeks well went were weren
    what whatever when whenever where whereas wherever whether which while who whoever whole
    whom whose why wife will wish with within without won wonder wondering word words work
    worked working works worry would wouldn wow write wrong www yeah year years yep yes
    yesterday yet you your yours yourself yourselves yup zero
    """.split()
)


def _fold(word: str) -> str:
    """A word as the full-text index keeps it: lowercase, without accents."""
    if word.isascii():
        return word.lower()
    plain = unicodedata.normalize("NFKD", word.casefold())
    return "".join(char for char in plain if not unicodedata.combining(char))


def _split(text: str) -> list[str]:
    """The words of a line, folded, in the order said."""
    if text.isascii():
        return TOKEN.findall(text.lower())
    return [_fold(word) for word in TOKEN.findall(text)]


def _usable(term: str, skip: frozenset[str]) -> bool:
    """Whether a word can make two meetings alike: not an everyday word, a name or a number."""
    return (
        3 <= len(term) <= 40
        and term not in STOP_WORDS
        and term not in skip
        and not NUMBER.fullmatch(term)
    )


def _counted_already(store: Store, saved_topics: list[dict]) -> frozenset[str]:
    """Words counted another way, left out of the distinctive ones: everyone's names (yours
    too, and the ones you answer to), the clients' names, and the topics' names and words."""
    words: set[str] = set()
    for row in store.db.execute("SELECT name FROM people"):
        words.update(_split(row["name"]))
    for name in [*store.client_names(), *mentions.your_names(store)]:
        words.update(_split(name))
    for topic in saved_topics:
        for word in [topic["name"], *topic["words"]]:
            words.update(_split(word))
    return frozenset(words)


class Shared(NamedTuple):
    """What another meeting shares with one from its lines and voices, and the topics."""

    people: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    words: int = 0  # tenths of this meeting's distinctive words it says, by weight
    said: int = 0  # how many of them
    terms: tuple[str, ...] = ()  # the ones to name


NOTHING = Shared()


class _Library:
    """What's worked out of the library as it is at one lines version: about how many lines it
    has, how many of them say each word asked about, who is in each meeting, and what meetings
    share with others."""

    def __init__(self, store: Store) -> None:
        # The highest line id, near enough: deleted lines leave gaps, but only its logarithm
        # counts, and counting the lines reads the whole table.
        (highest,) = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM segments").fetchone()
        self.lines = max(highest, 1)
        self.lines_with: dict[str, int] = {}
        self.signals: dict[tuple, dict[int, Shared]] = {}
        # Who counts, as clients.py counts people: named or recognized by voice, not only
        # suggested, and never you. Each meeting's voices (name, speaker), most heard first, and
        # each person's (meeting, speaker).
        self.voices: dict[int, list[tuple[str, int]]] = {}
        self.voices_of: dict[str, list[tuple[int, int]]] = {}
        for row in store.db.execute(
            "SELECT ms.meeting_id, ms.id, p.name FROM meeting_speakers ms "
            "JOIN people p ON p.id = ms.person_id "
            "WHERE ms.status IN ('auto', 'confirmed') AND p.is_me = 0 "
            "ORDER BY ms.meeting_id, COALESCE(ms.speech_seconds, 0) DESC, p.name"
        ):
            name, meeting_id, speaker = row["name"], row["meeting_id"], row["id"]
            self.voices.setdefault(meeting_id, []).append((name, speaker))
            self.voices_of.setdefault(name, []).append((meeting_id, speaker))

    def lines_saying(self, store: Store, term: str) -> int:
        """How many lines of the library say a word; MAX_LINES + 1 when it's more than that."""
        if term not in self.lines_with:
            (self.lines_with[term],) = store.db.execute(
                "SELECT COUNT(*) FROM "
                "(SELECT 1 FROM segments_fts WHERE segments_fts MATCH ? LIMIT ?)",
                (f'"{term}"', MAX_LINES + 1),
            ).fetchone()
        return self.lines_with[term]


# What's worked out from the lines, voices, people and topics is kept, per meeting, until one of
# them changes (the store's lines version), or the topics, names or clients do. Tickets, a
# client set on a meeting, and statuses are read fresh each time: they change without the lines
# version moving.
_kept: dict[tuple, _Library] = {}
_kept_lock = threading.Lock()
KEPT_MEETINGS = 20  # whose signals are kept (the page asks again on every change it makes)


def _heard(store: Store, meeting_id: int, speaker_id: int) -> bool:
    """Whether one of a meeting's voices says any line in it (one whose lines were all given
    to other people isn't there)."""
    return bool(
        store.db.execute(
            "SELECT EXISTS (SELECT 1 FROM segments WHERE meeting_id = ? "
            "AND meeting_speaker_id = ?)",
            (meeting_id, speaker_id),
        ).fetchone()[0]
    )


def _people_in_common(store: Store, meeting_id: int, library: _Library) -> dict[int, list[str]]:
    """The people heard in both, by other meeting, as _Library counts them."""
    mine: list[str] = []
    for name, speaker in library.voices.get(meeting_id, []):
        if name not in mine and _heard(store, meeting_id, speaker):
            mine.append(name)
    in_common: dict[int, list[str]] = {}
    for name in mine:
        for other, speaker in library.voices_of[name]:
            both = in_common.get(other, [])
            if other != meeting_id and name not in both and _heard(store, other, speaker):
                in_common[other] = [*both, name]
    return in_common


def _library(store: Store, version: int | None) -> _Library:
    """What's kept of the library as it is now (a new one, not kept, when the version can't be
    told)."""
    if version is None:
        return _Library(store)
    key = (store.path, version)
    with _kept_lock:
        kept = _kept.get(key)
    if kept is None:
        kept = _Library(store)
        with _kept_lock:
            _kept.clear()  # only the latest is ever asked for again
            kept = _kept.setdefault(key, kept)
    return kept


def _distinctive(
    store: Store, meeting_id: int, skip: frozenset[str], library: _Library
) -> tuple[dict[str, float], dict[str, int]]:
    """This meeting's most distinctive words, most distinctive first, as {word: weight}: said
    most here, on fewest lines of the library (tf-idf); and how many of its lines say each."""
    said: Counter[str] = Counter()
    lines_with: Counter[str] = Counter()
    for row in store.db.execute("SELECT text FROM segments WHERE meeting_id = ?", (meeting_id,)):
        words = _split(row["text"])
        said.update(words)
        lines_with.update(set(words))
    usable = Counter({term: times for term, times in said.items() if _usable(term, skip)})
    weights: dict[str, float] = {}
    for term, times in usable.most_common(CANDIDATES):
        anywhere = library.lines_saying(store, term)
        if anywhere > MAX_LINES:
            continue
        idf = math.log(max(library.lines, anywhere) / max(anywhere, 1))
        if idf > 0:
            weights[term] = (1 + math.log(times)) * idf
    ranked = dict(sorted(weights.items(), key=lambda item: (-item[1], item[0])))
    return ranked, {term: lines_with[term] for term in ranked}


def _words(
    store: Store, meeting_id: int, skip: frozenset[str], library: _Library
) -> dict[int, dict]:
    """The other meetings that say enough of this one's distinctive words: {meeting id: {"words",
    "said", "terms"}} (see Shared)."""
    weights, own_lines = _distinctive(store, meeting_id, skip, library)
    # The ones that other lines say too: as far as another meeting can share them.
    terms = [term for term in weights if library.lines_saying(store, term) > own_lines[term]]
    terms = terms[:TERMS]
    (meetings,) = store.db.execute("SELECT COUNT(*) FROM meetings").fetchone()
    too_many = max(2, (meetings - 1) // WIDESPREAD)
    said_by: dict[int, dict[str, int]] = {}  # other meeting -> word -> lines that say it
    for term in list(terms):
        rows = store.db.execute(
            "SELECT s.meeting_id, COUNT(*) FROM segments_fts f JOIN segments s ON s.id = f.rowid "
            "WHERE segments_fts MATCH ? AND s.meeting_id != ? GROUP BY s.meeting_id",
            (f'"{term}"', meeting_id),
        ).fetchall()
        if len(rows) > too_many:  # said all over the library: it tells none apart
            terms.remove(term)
            continue
        for other, lines in rows:
            said_by.setdefault(other, {})[term] = lines
    total = sum(weights[term] for term in terms)
    shared: dict[int, tuple[int, list[str]]] = {}
    for other, lines in said_by.items():
        if len(lines) < MIN_TERMS:
            continue
        # A word on one line there counts half, on two lines two thirds, and so on.
        share = sum(weights[term] * count / (count + 1) for term, count in lines.items()) / total
        tenths = math.floor(share * 10 + 1e-9)
        if tenths >= ENOUGH:
            shared[other] = (tenths, sorted(lines, key=lambda term: (-weights[term], term)))
    if not shared:
        return {}
    written, together = _as_written(store, meeting_id, set(terms))
    return {
        other: {"words": tenths, "said": len(order), "terms": _named(order, written, together)}
        for other, (tenths, order) in shared.items()
    }


def _as_written(store: Store, meeting_id: int, terms: set[str]) -> tuple[dict, dict]:
    """How to write some of this meeting's words: {word: as it's most often written here}, and
    the pairs of them usually said one right after the other ("VPN profile"), {pair: times}."""
    spelled: dict[str, Counter[str]] = {term: Counter() for term in terms}
    pairs: Counter[tuple[str, str]] = Counter()
    for row in store.db.execute("SELECT text FROM segments WHERE meeting_id = ?", (meeting_id,)):
        text = row["text"]
        if terms.isdisjoint(_split(text)):
            continue
        before: tuple[str, int] | None = None  # the word just before, and where it ended
        for match in TOKEN.finditer(text):
            raw, term = match.group(), _fold(match.group())
            if term not in terms:
                before = None
                continue
            if raw.istitle() and SENTENCE_START.search(text[: match.start()]):
                raw = raw.lower()  # the capital that starts a sentence says nothing
            spelled[term][raw] += 1
            if before is not None and text[before[1] : match.start()] == " ":
                pairs[(before[0], term)] += 1
            before = (term, match.end())
    written = {
        term: max(forms, key=lambda raw: (forms[raw], raw.islower()))
        for term, forms in spelled.items()
        if forms
    }
    together = {
        (first, second): times
        for (first, second), times in pairs.items()
        # At least twice, and in at least half the times the rarer of them is said.
        if first != second
        and times >= max(2, min(spelled[first].total(), spelled[second].total()) / 2)
    }
    return written, together


def _named(order: list[str], written: dict[str, str], together: dict) -> tuple[str, ...]:
    """The words to name, most distinctive first, up to SHOWN_TERMS; two usually said together
    here are one phrase, in the order they're said."""
    named: list[str] = []
    used: set[str] = set()
    for term in order:
        if term in used:
            continue
        pairs = [
            pair
            for pair in together
            if term in pair and set(pair) <= set(order) and not used & set(pair)
        ]
        if pairs:
            pair = max(pairs, key=lambda pair: (together[pair], pair))
            used.update(pair)
            named.append(f"{written[pair[0]]} {written[pair[1]]}")
        else:
            used.add(term)
            named.append(written[term])
        if len(named) == SHOWN_TERMS:
            break
    return tuple(named)


def _signals(store: Store, meeting_id: int) -> dict[int, Shared]:
    """What other meetings share with this one from their lines and voices, and the topics."""
    saved = topics.saved(store)
    skip = _counted_already(store, saved)
    library = _library(store, store.lines_version())
    key = (meeting_id, repr(saved), skip)
    with _kept_lock:
        kept = library.signals.get(key)
    if kept is not None:
        return kept
    found: dict[int, dict] = {}
    for other, names in _people_in_common(store, meeting_id, library).items():
        found.setdefault(other, {})["people"] = tuple(names)
    for name, saying in topics.in_common(store, meeting_id, MAX_TOPIC_LINES).items():
        for other in saying:
            if other != meeting_id:
                found.setdefault(other, {}).setdefault("topics", []).append(name)
    try:
        words = _words(store, meeting_id, skip, library)
    except sqlite3.OperationalError:  # the index can't be read: no words counted
        words = {}
    for other, got in words.items():
        found.setdefault(other, {}).update(got)
    signals = {
        other: Shared(**{**got, "topics": tuple(got.get("topics", ()))})
        for other, got in found.items()
    }
    with _kept_lock:
        if len(library.signals) >= KEPT_MEETINGS:
            library.signals.pop(next(iter(library.signals)))
        library.signals[key] = signals
    return signals


def _listed(names: tuple[str, ...]) -> str:
    """Names in a reason: the first three, and how many more."""
    if len(names) <= SHOWN_NAMES:
        return ", ".join(names)
    return f"{', '.join(names[:SHOWN_NAMES])} and {len(names) - SHOWN_NAMES} more"


def find(store: Store, meeting_id: int, limit: int = LIMIT) -> list[dict]:
    """The meetings most related to this one, best first, at most ``limit``: {"id", "title",
    "started_at", "score", "reasons"}, the reasons in plain words ("Ticket 1234567", "Contoso",
    "Dana Lee, Mark Ruiz", "Topic: Firewall", "Also about: VPN profile, cutover")."""
    meeting = store.meeting(meeting_id)
    if meeting is None:
        return []
    others = [
        other
        for other in store.meetings()
        if other["id"] != meeting_id and other["status"] not in UNFINISHED
    ]
    client_of = clients.of_meetings(store, [meeting, *others])
    ticket = (store.column(meeting, "ticket") or "").strip().upper()
    client = (client_of[meeting_id]["client"] or "").casefold()
    signals = _signals(store, meeting_id)
    related = []
    for other in others:
        shared = signals.get(other["id"], NOTHING)
        theirs = client_of[other["id"]]["client"] or ""
        reasons: list[str] = []
        score = 0.0
        if ticket and (store.column(other, "ticket") or "").strip().upper() == ticket:
            score += TICKET
            reasons.append(f"Ticket {ticket}")
        if client and theirs.casefold() == client:
            score += CLIENT
            reasons.append(theirs)
        if shared.people:
            score += PERSON * min(len(shared.people), MOST_COUNTED)
            reasons.append(_listed(shared.people))
        if shared.topics:
            score += TOPIC * min(len(shared.topics), MOST_COUNTED)
            label = "Topic" if len(shared.topics) == 1 else "Topics"
            reasons.append(f"{label}: {_listed(shared.topics)}")
        alone = shared.said >= MIN_TERMS_ALONE and shared.words >= ENOUGH_ALONE
        if shared.terms and (reasons or alone):
            score += WORDS * shared.words / 10
            reasons.append(f"Also about: {', '.join(shared.terms)}")
        if reasons:
            related.append(
                {
                    "id": other["id"],
                    "title": other["title"],
                    "started_at": other["started_at"],
                    "score": round(score, 1),
                    "reasons": reasons,
                }
            )
    related.sort(key=lambda found: found["started_at"], reverse=True)  # the newest first, and
    related.sort(key=lambda found: -found["score"])  # that order kept among equal scores
    return related[:limit]
