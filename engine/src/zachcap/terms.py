"""Names and terms in a file worth having in Words to know.

The user picks a file they have (a client's asset list, an exported email, a glossary, a project
plan) and ticks which of the names and terms found in it to add. What Whisper can't know is
looked for: people (first those in email headers), names of two or three capitalized words, words
capitalized in the middle of a sentence, words with capitals inside (ConnectWise, vCenter) and
product names with digits (M365). Words the file also writes in lower case, everyday words, ids,
addresses, paths, dates and amounts are left out.

A term goes to Claude with every summary, so a password must never be offered: nothing is taken
from a line about passwords, keys, codes or tokens, from the line after it, from what it heads (a
list, a section, what's indented under it) or from a table's column of them, and nothing that
looks like one.

Plain rules, worked out on this PC: nothing is sent or downloaded, and nothing of the file is
kept.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from typing import NamedTuple

from . import everyday, mentions
from .related import STOP_WORDS, _fold
from .vocabulary import SECRET_WORDS, secretish, usable

LIMIT = 40  # terms offered from one file
LONGEST = 40  # characters in a term

# How useful each kind of find is, before how often it's there.
PERSON = 4.0  # in an email header, or listed as a person
PRODUCT = 3.5  # capitals inside (ConnectWise)
NAME = 3.0  # two or three capitalized words, in a sentence
PROPER = 2.5  # one, in the middle of a sentence
MODEL = 2.5  # letters and digits (MX84)
LISTED = 2.0  # two or three, on a line or in a cell of their own
ALONE = 1.0  # one, there
INITIALS = 0.5  # in capitals (RMM)

EVERYDAY = (
    STOP_WORDS | everyday.WORDS | everyday.NAMES | everyday.CALENDAR | everyday.OTHER_LANGUAGES
)
# The small words English is full of, which tell it from other languages.
_SMALL = STOP_WORDS | {word for word in everyday.WORDS if len(word) == 2}

# Text as typed: no curly quotes, dashes or bullets inside words, odd spaces or hidden characters.
_TYPED: dict[int, str | None] = {
    **dict.fromkeys((0x2018, 0x2019, 0x02BC, 0x00B4), "'"),
    **dict.fromkeys((0x201C, 0x201D, 0x201E, 0x00AB, 0x00BB), '"'),
    **dict.fromkeys((0x2010, 0x2011), "-"),
    **dict.fromkeys((0x2012, 0x2013, 0x2014, 0x2022, 0x00B7, 0x25CF, 0x25AA), " - "),
    0x2026: "...",
    **dict.fromkeys((0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF), None),
}


def _tidy(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_TYPED)
    return re.sub(r"[^\S\n\t ]", " ", text)


# Secrets ------------------------------------------------------------------------------

# What files call them besides SECRET_WORDS, the other languages' words, and what's given without
# calling it one ("Guest Wi-Fi: BlueHeron", "admin / BlueHeron").
_SECRETS = re.compile(
    SECRET_WORDS.pattern + r"|\b(?:passwd|pwd?s?|p/w|psk|psksecret|wpa[23]?|totp|credentials?|"
    r"creds?|log-?ins?|logons?|user ?names?|api ?keys?|(?:seed|recovery) phrases?|"
    r"security (?:question|answer)s?|maiden names?|(?:code|safe)[ -]?words?|"
    r"(?:safe|lock|door|gate) (?:combo|combination)s?|community strings?|snmp community|"
    r"passwor[td]e?|passw\w+rter|kennw\w+rte?r?|zugangsdaten|anmeldedaten|benutzername|"
    r"schl\wssel|contrase\was?|claves?|c\wdigos?|mots? de passe|mdp|cl\ws?|identifiants?|senhas?|"
    r"chaves?|chiav[ei]|codic[ei]|wachtwoord(?:en)?|sleutels?|(?:toegangs|pin)codes?|"
    r"inloggegevens|gebruikersnaam)\b|"
    r"\b(?:wi-?fi|wlan|ssid)\b[^.:=]{0,20}(?:[:=]| - | is | ist )|"
    r"\b(?:admin|administrator|root|guest|user|sa) ?/ ?\S|\b(?:admin|root|sa) {0,2}(?:[:=]|- ) ?\S",
    re.IGNORECASE,
)
# "Key" as "main": people and dates, not a secret.
_KEY = re.compile(
    r"\bkey (?=(?:contact|people|person|personnel|date|point|stakeholder|milestone|decision|risk|"
    r"takeaway|deliverable|activit|resource|role|objective|term|feature|benefit|vendor|supplier|"
    r"client|customer|finding|fact|figure|metric|result|message|account|player|area|goal)s?)",
    re.IGNORECASE,
)
# An identifier: adminPassword, DB_PASSWORD, or what's before a colon or an equals sign.
_IDENTIFIER = re.compile(r"\b(?:[a-z]+[A-Z]\w*|\w+_\w+|\w+(?=[\"']?\s*[:=]))")
_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")
_BULLET = re.compile(r"\s*(?:[-*+#|]+|\(?\d{1,3}(?:\.\d{1,3}){0,3}[.)]?|\(?[A-Za-z][.)])\s+")


def _about_secrets(text: str) -> bool:
    """Whether a line or a column's heading talks about passwords, keys, codes or tokens."""
    if _SECRETS.search(_KEY.sub("", text)):
        return True
    return any(_SECRETS.search(_HUMP.sub(" ", name)) for name in _IDENTIFIER.findall(text))


def _blank(line: str) -> bool:
    return not any(map(str.isalnum, line))


def _few(text: str) -> bool:
    return len(re.split(r"[\s,;|]+", text)) <= 5


def _heads(line: str) -> bool:
    """Whether a line about secrets heads what follows: it ends in a colon, it's a few words that
    are no sentence, or the names of a table's columns."""
    mark = _BULLET.match(line)
    bare = (line[mark.end() :] if mark else line).strip().strip("*_ ")
    if bare.endswith(":") or (bare.startswith("[") and bare.endswith("]")):
        return True
    if re.search(r"[!?=]|:\s*\S|\.$", bare):
        return False
    if re.search(r"[\t;|]|  |,.*,", bare):
        return all(len(name.split()) <= 4 for name in re.split(r"[\t;|,]|  +", bare))
    # A configuration's line ("set type password") is no heading.
    return _few(bare) and (bare[:1].isupper() or bare.endswith("s"))


def _brief(bare: str) -> bool:
    """Whether a line after an empty one may go on a list: a value, a name and its value, a row."""
    if re.search(r"\t|  |:\s{0,3}\S{1,40}$", bare):
        return True
    return _few(bare) and not bare.endswith((".", "!", "?", ","))


class _Row(NamedTuple):
    """A line of a table: what's between its cells, how many there are, and where it starts."""

    mark: str
    count: int
    top: int


def _cells(line: str, mark: str) -> list[str]:
    if mark == "|":
        return [cell.strip() for cell in line.strip().strip("|").split("|")]
    try:
        return [cell.strip() for cell in next(csv.reader([line], delimiter=mark), [])]
    except csv.Error:
        return []


def _rows(lines: list[str]) -> list[_Row | None]:
    """The rows among the lines: a table is a run of lines with the same marks."""
    shapes: list[tuple[str, int] | None] = []
    for line in lines:
        mark = next((mark for mark in "\t|;," if line.count(mark) >= 2), None)
        shapes.append((mark, len(_cells(line, mark))) if mark else None)
    rows: list[_Row | None] = [None] * len(lines)
    top = 0
    for index in range(1, len(lines) + 1):
        if index < len(lines) and shapes[index] == shapes[top]:
            continue
        shape = shapes[top]
        # Sentences have commas too: three lines with as many is a table, two can be chance.
        if shape and index - top >= (3 if shape[0] == "," else 2):
            rows[top:index] = [_Row(*shape, top)] * (index - top)
        top = index
    return rows


_NOWHERE = 1 << 30  # further in than any line


def _safe(lines: list[str], rows: list[_Row | None]) -> Iterator[tuple[int, frozenset[int]]]:
    """The numbers of the lines that can't hold a secret, and a row's columns that can."""
    hidden: dict[tuple[str, int], frozenset[int]] = {}  # columns of secrets, by a table's shape
    in_table = False  # with a column of secrets
    # What's skipped after a line about secrets: the next line, what's further in than it, the
    # lines it heads (those of a list's item, up to the next item), its Markdown section.
    due, level, block, section = False, _NOWHERE, _NOWHERE, 0
    gap = False  # an empty line came since the last one skipped
    for index, line in enumerate(lines):
        bare = line.strip()
        if _blank(line):
            if not bare:
                in_table, gap = False, True
            continue
        row, item = rows[index], _BULLET.match(line)
        depth = len(bare) - len(bare.lstrip("#"))  # of a Markdown heading
        indent = len(line) - len(line.lstrip())
        if item:  # 3.1 is further in than 3
            indent += item[0].rstrip(" .)").count(".")
        if indent <= level:
            level = _NOWHERE
        under = level < _NOWHERE or (block < _NOWHERE and not (item and indent <= block))
        covered = (
            (due and not (depth and gap))
            or (section and not 0 < depth <= section)
            or (under and not depth and (not gap or _brief(bare)))
        )
        if not covered:
            level, block, section = _NOWHERE, _NOWHERE, 0
        if _about_secrets(bare):
            due, gap = True, False
            top = row is not None and row.top == index
            if top and not bare.endswith(":"):
                cells = enumerate(_cells(line, row.mark))
                hidden[row[:2]] = frozenset(n for n, cell in cells if _about_secrets(cell))
                in_table = True
            elif top or row is None:
                level = min(level, indent)
                if depth:
                    section = min(section or depth, depth)
                elif _heads(line):
                    block = min(block, indent if item else -1)
        elif due:
            due, gap = False, False
            if bare.endswith(":"):
                block = min(block, indent if item else -1)
        elif covered:
            gap = False
        elif not in_table or (row is not None and row[:2] in hidden):
            # In a row that doesn't fit its table, the secret may be in any cell.
            yield index, hidden.get(row[:2], frozenset()) if row else frozenset()


# Lines and cells ------------------------------------------------------------------------

# What heads a list or a column of people, the two columns of a name, other names, models, and
# ordinary words.
_MAIL = frozenset("from to cc bcc von an de para da van aan".split())
_PEOPLE = frozenset(
    """
    name names owner owners contact contacts employee manager supervisor technician engineer
    requester author attendee attendees participant participants present absent regrets apologies
    optional required organizer presenter facilitator sponsor lead resource resources representative
    approver assignee user teilnehmer anwesend protokoll ansprechpartner verantwortlich asistentes
    presentes aanwezig afwezig presents excuse excuses
    """.split()
) | {
    "assigned to", "full name", "display name", "project manager", "account manager",
    "executive sponsor", "primary contact", "prepared by", "approved by", "reported by",
    "requested by", "submitted by", "written by", "contact name", "employee name", "user name",
}  # fmt: skip
_FIRST = {"first name", "firstname", "given name", "vorname"}
_LAST = {"last name", "lastname", "surname", "family name", "nachname"}
_MODELS = {"model", "product", "make and model", "make model", "device model", "modell"}
_NAMED = _MODELS | frozenset(
    """
    manufacturer make maker brand vendor supplier company client customer site location branch city
    software application hersteller lieferant firma kunde standort ort programm
    """.split()
)
_PLAIN = {"job title", "position", "department", "status", "priority", "severity", "category",
          "type", "abteilung"}  # fmt: skip
_HEADS = _PEOPLE | _FIRST | _LAST | _NAMED | _PLAIN
_LABEL = re.compile(r"[\s*_|-]*([^\W\d_][\w /]{0,30}?)[*_]{0,2} ?:[*_]{0,2}\s*(?=\S)")


class _Part(NamedTuple):
    """A line or a table's cell, to look in."""

    text: str
    cell: bool = False
    people: str = ""  # it lists people: a "mail" header's, a "list" of them, or one in a "cell"
    named: bool = False  # its label or column says it's a name (Vendor, Site)...
    model: bool = False  # ...or a model


def _parts(text: str) -> Iterator[_Part]:
    """The lines and cells to look in: those that can't hold a secret."""
    lines = [re.sub(r"^(?:\s*>)+ ?", "", line) for line in text.split("\n")]
    rows = _rows(lines)
    for index, hidden in _safe(lines, rows):
        line, row = lines[index], rows[index]
        if row is None:
            label = _LABEL.match(line)
            head = _fold(label[1]).strip() if label else ""
            people = "mail" if head in _MAIL else "list" if head in _PEOPLE else ""
            columns = re.split(r"\t+| {2,}", line.strip())
            for column in columns:
                yield _Part(column, len(columns) > 1, people, head in _NAMED)
            continue
        cells = _cells(line, row.mark)
        heads = [
            " ".join(re.findall(r"\w+", _fold(head))) for head in _cells(lines[row.top], row.mark)
        ]
        if index == row.top and not _HEADS.isdisjoint(heads):
            continue
        last = next((cell for cell, head in zip(cells, heads, strict=True) if head in _LAST), "")
        for n, (cell, head) in enumerate(zip(cells, heads, strict=True)):
            if n in hidden or head in _PLAIN or (head in _LAST and not _FIRST.isdisjoint(heads)):
                continue
            if head in _FIRST:
                cell = f"{cell} {last}"
            person = head in _PEOPLE | _FIRST | _LAST
            yield _Part(cell, True, "cell" if person else "", head in _NAMED, head in _MODELS)


# Words --------------------------------------------------------------------------------


class _Word(NamedTuple):
    text: str
    shape: str  # lower, title, upper, inner, model (letters and digits), or other
    start: bool  # it starts a sentence, a line or a cell
    joined: str = ""  # what's between it and the word before, in one phrase (" ", " & ")
    colon: bool = False  # a colon follows


_END = _Word("", "other", True)  # after a line's last word

_CHUNK = re.compile(r"[^\s,;|\"()\[\]{}<>=\x00]+")
_EDGES = "'`*.:!?-"
_AND = "\x01&\x01"  # " & " between two words, while a line is split
# Not a word: part of an address, a path, an identifier, an amount, a version.
_ODD = re.compile(r"[@\\_~^`$%#+]|:|/.*/|^/|/$|\w\.\w")
_CONTRACTED = re.compile(r"'(?:t|ll|re|ve|d|m)$", re.IGNORECASE)
_TITLES = frozenset(
    """
    mr mrs ms miss dr prof sir madam herr herrn frau hr fr monsieur madame mme senor senora sr sra
    signor signora senhor senhora mevrouw meneer dhr mevr
    """.split()
)
_ABBREVIATED = _TITLES | {"m", "st", "vs", "no", "nr", "approx", "ext", "jr", "inc", "ltd", "co"}


def _shape(text: str) -> str:
    if not all(char.isalnum() or char in "'&-" for char in text):
        return "other"
    if not any(map(str.isalpha, text)):
        return "other"
    if any(map(str.isdigit, text)):
        return "model"
    if text.islower():
        return "lower"
    if text.isupper():
        return "upper"
    pieces = re.split("['&-]", text)
    if text[0].isupper() and all(len(p) < 2 or p[1:].islower() for p in pieces):
        return "title"
    return "inner"


def _words(text: str) -> list[_Word]:
    """A line's words in order, each with how it's written and whether it goes on a phrase."""
    if not text.isascii():  # letters without capitals (another alphabet) are no part of a name
        text = "".join(
            "\x00" if char.isalpha() and char.lower() == char.upper() else char for char in text
        )
    line = re.sub(r"`[^`]*`", "\x00", text).replace(" & ", _AND)  # without inline code
    words: list[_Word] = []
    start, open_phrase, end = True, False, 0
    for match in _CHUNK.finditer(line):
        chunk, gap = match[0], line[end : match.start()]
        end = match.end()
        core = chunk.strip(_EDGES)
        if len(core) > 2 and core[0] == core[-1] == "_":
            core = core.strip("_")
        if not core:  # a dash or a bullet
            start, open_phrase = True, False
            continue
        before, after = gap[-1:], line[end : end + 2]
        code = (
            before == "<"
            or after[:1] in ("(", "=")
            or after == '":'
            or (before == "[" and after[:1] == "]" and after != "](")
            or (before == "{" and after[:1] == "}")
            or (chunk[0] == "-" and match.start() > 0)
        )
        odd = code or len(core) > 40 or _ODD.search(core)
        pieces = [] if odd else core.split("/")
        tail = chunk[chunk.rfind(core) + len(core) :]
        colon = ":" in tail or after == '":'
        joined = " " if open_phrase and gap == " " and chunk[0] not in "'`*_" else ""
        open_phrase = len(pieces) == 1
        for piece in pieces:
            for word in piece.split(_AND):
                if word[-2:].lower() == "'s":
                    word, open_phrase = word[:-2], False
                if word:
                    shape = "lower" if _CONTRACTED.search(word) else _shape(word)
                    words.append(_Word(word, shape, start, joined, colon))
                    start = False
                joined = " & "
            joined = ""
        if not pieces:
            words.append(_Word(core, "other", start or after == '":', colon=colon))
            start = False
        abbreviated = tail == "." and (core.lower() in _ABBREVIATED or len(core) == 1)
        open_phrase = open_phrase and (not tail or abbreviated)
        if not abbreviated and (colon or any(char in tail for char in ".!?")):
            start = True
    return words


# Ordinary words -----------------------------------------------------------------------

_ENDINGS = ("tion", "sion", "ment", "ness", "able", "ible", "ity", "ance", "ence", "ical", "ous",
            "ful", "less", "ship", "ism")  # fmt: skip
_LOOSE_ENDINGS = ("ing", "ings", "ed", "ly", "ize", "ive", "ology")
# Nouns in German end like these.
_GERMAN_ENDINGS = ("ung", "ungen", "heit", "keit", "schaft", "ion", "tat", "nis", "ismus", "raum")
_PREFIXES = frozenset("re pre co non sub multi anti inter un mid post pro in on up off".split())


def _bases(word: str) -> Iterator[str]:
    """What an everyday word may be a form of: plan, for plans, planned and planning."""
    if word.endswith("s") and not word.endswith("ss"):
        yield word[:-1]
        if word.endswith("es"):
            yield word[:-2]
        if word.endswith("ies"):
            yield word[:-3] + "y"
    for ending in ("ed", "ing", "ings"):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            stem = word[: -len(ending)]
            yield from (stem, stem + "e")
            if stem[-1] == stem[-2]:
                yield stem[:-1]
            if stem.endswith("i"):
                yield stem[:-1] + "y"
    if word.endswith("ly"):
        yield word[:-2]


def _everyday(word: str, loose: bool = False) -> bool:
    """Whether a word is an everyday one, going by the word alone. ``loose`` where its capital
    says nothing (a line's first word, a heading, a cell)."""
    folded, shape = _fold(word), _shape(word)
    if shape == "inner" and "-" not in word:
        plural = re.fullmatch(r"[A-Z]{2,}s", word)  # of initials (VMs)
        return bool(plural) or any(form in EVERYDAY for form in (folded, *_bases(folded)))
    if folded.replace("-", "") in EVERYDAY or (shape == "upper" and folded in everyday.SHORT):
        return True
    for piece in re.split("['&-]", folded):
        forms = (piece, *_bases(piece))
        if not (
            len(piece) < 2
            or piece in _PREFIXES
            or any(form in EVERYDAY for form in forms)
            or any(len(form) >= 7 and form.endswith(_ENDINGS) for form in forms)
            or (loose and len(piece) >= 6 and piece.endswith(_LOOSE_ENDINGS))
        ):
            return False
    return True


# Capitals inside, letters and digits ------------------------------------------------------

_LOWER_START = frozenset("mac pf open web de van di la le".split())
_VOWEL = re.compile("[aeiouy]")
_MODEL = re.compile(r"[A-Z]{1,4}[1-9]\d{0,2}[A-Za-z]?|\d[A-Z]{2,3}")
# Quarters, fiscal years, priorities, versions, rooms and the like, and what hosts are named by.
_NOT_MODEL = re.compile(
    r"(?:Q|H|FY|P|L|V|W|T|S|R|A|B|C|D|G|K|N|SP|PO|SN|CR|KB|ID|NO|PC|WS|DC|VM|SRV|SV|LT|DT|NB|FS|DB|"
    r"AP|SW|FW|RT|PRT|PTR|TV|RM|EXT|GW|TS|RDS|NAS|SAN|ESX|WAN|LAN|VLAN|PORT|TLS|SHA|AES|UTF|ISO|"
    r"MP|USB|CAT|TCP|UDP|IP|RAID|NVME|SATA|X|IPV)\d{1,2}[A-Za-z]?|"
    r"\d(?:GB|MB|TB|KB|AM|PM|ST|ND|RD|TH|HZ|KW|KG|KM|CM|MM|FT|LB|MP|MS|HR|GBE)"
)
_BEFORE_ID = frozenset(
    """
    room rooms building floor gate suite unit ext extension port vlan rack row bay line ticket case
    order invoice part item page block step phase level zone section table figure box bin door lot
    """.split()
)


def _humps(word: str) -> list[str]:
    """ConnectWise: Connect, Wise; VMware: V, Mware; LogMeIn: Log, Me, In."""
    cuts = [0]
    for index in range(1, len(word)):
        before, char = word[index - 1], word[index]
        if before.islower() and char.isupper():
            cuts.append(index)
        elif before.isupper() and char.islower() and index - cuts[-1] > 1:
            cuts.append(index - 1)
    return [word[a:b] for a, b in zip(cuts, [*cuts[1:], len(word)], strict=True)]


def _product(word: str) -> bool:
    """Whether a word with capitals inside reads like a name: not an identifier from code or a
    log (lastLogonDate, UserName), nor a run of random letters."""
    if not word.isalpha():
        return False
    humps = [_fold(hump) for hump in _humps(word)]
    first, last = humps[0], humps[-1]
    if len(humps) > 3 or first in everyday.IDENTIFIER_STARTS or last in everyday.IDENTIFIER_ENDS:
        return False
    if word[0].islower() and len(first) > 1 and first not in _LOWER_START:
        return False
    return any(len(hump) >= 3 and _VOWEL.search(hump) for hump in humps) or bool(
        re.fullmatch(r"[A-Z]{2,4}[a-z]{1,2}", word)
    )


# People ---------------------------------------------------------------------------------

_PARTICLES = frozenset(
    "de la le du des di da del della do dos van von der den ter ten al bin y".split()
)
_AFTER_NAME = re.compile(r"\s+(?:via|on behalf of)\s.*$|\s[-|/]\s.*$", re.IGNORECASE)


def _recased(name: str) -> str:
    """A name in capitals as it's usually written: JOOST VAN DER MEER is Joost van der Meer."""
    recased = re.sub(r"[^\W\d_]+", lambda m: m[0][0].upper() + m[0][1:].lower(), name)
    words = recased.split()
    return " ".join(w.lower() if n and w.lower() in _PARTICLES else w for n, w in enumerate(words))


def _person(name: str) -> str | None:
    """A name as it's listed, without titles and initials, or None if it isn't a person's."""
    words = [word.strip(".") for word in name.split()]
    words = [word for word in words if len(word) > 1]
    while words and _fold(words[0]) in _TITLES:
        words.pop(0)
    while words and _fold(words[-1]) in ("jr", "sr", "ii", "iii", "iv"):
        words.pop()
    if not 1 <= len(words) <= 4 or any(_fold(word) in everyday.ROLES for word in words):
        return None
    if words[-1].lower() in _PARTICLES or not all(
        _shape(word) in ("title", "inner", "upper") or word in _PARTICLES for word in words
    ):
        return None
    # Mark Banks is a name, as far as a first name can tell; Front Desk isn't.
    if all(map(_everyday, words)) and _fold(words[0]) not in mentions.EVERYDAY:
        return None
    name = " ".join(words)
    if len(words) == 1:
        return None if name.isupper() or _everyday(name) else name
    return _recased(name) if name.isupper() else name


def _people(value: str, kind: str) -> Iterator[str]:
    """The names in a list of people: Dana Lee <dana@example.com>, "Lee, Dana" <...>; Lee, Dana."""
    value = re.sub(r"\([^()]*\)|\[[^\[\]]*\]", " ", value).strip()
    if kind == "cell" or (kind == "mail" and re.fullmatch(r"\w[\w'-]*, \w[\w'-]*", value)):
        entries = [value]
    elif "<" in value or ";" in value:
        entries = re.split(r">\s*[,;]?|;", value)
    else:
        entries = re.split(r",|\s(?:and|und|y|et|e|en)\s|\s[&/]\s", value)
    for entry in entries:
        listed = _AFTER_NAME.sub("", entry.partition("<")[0].strip(" ,\t'\"*_")).strip()
        last, comma, first = (piece.strip(" '\"") for piece in listed.partition(","))
        family = last.split()
        reverse = comma and family and (len(family) == 1 or family[0].lower() in _PARTICLES)
        for name in [f"{first} {last}"] if reverse else listed.split(","):
            person = _person(name)
            if person:
                yield person


# The file -------------------------------------------------------------------------------

# What ends a company's name.
_COMPANY = frozenset(
    "inc llc llp ltd corp co plc gmbh ag kg bv nv sa srl pty limited incorporated".split()
)
_GERMAN = frozenset(
    """
    und der die das nicht ist fur mit wir auf ein eine dem den zu im von sie auch wird werden sind
    bei nach aber oder wenn noch uber zum zur durch kann haben hat einen einer sich als dass
    """.split()
)
# German capitalizes every noun. These come right before a name...
_INTRODUCE = frozenset(
    """
    firma kunde kunden projekt standort raum stadt werk filiale niederlassung handler lieferant
    hersteller partner anbieter dienstleister
    """.split()
)
# ...and right after one of these, without an article, comes a name more often than not.
_PREPOSITIONS = frozenset("uber mit aus auf in fur bei von nach durch ohne an".split())


def _spelled(run: list[_Word]) -> str:
    return run[0].text + "".join((word.joined or " ") + word.text for word in run[1:])


class _Find:
    """A name or term, as it's found through the file."""

    def __init__(self) -> None:
        self.spelled: Counter[str] = Counter()
        self.weight = 0.0

    def add(self, spelled: str, weight: float, times: int = 1) -> None:
        self.spelled[spelled] += times
        self.weight = max(self.weight, weight)

    @property
    def count(self) -> int:
        return self.spelled.total()

    @property
    def score(self) -> float:
        return self.weight + min(math.log2(1 + self.count), 3)

    @property
    def term(self) -> str:
        return max(self.spelled, key=lambda spelled: (self.spelled[spelled], spelled))


class _File:
    """A file's text being looked through."""

    def __init__(self, text: str) -> None:
        self.parts = []
        for part in _parts(_tidy(text)):
            bullet = _BULLET.match(part.text)
            self.parts.append((part, _words(part.text[bullet.end() :] if bullet else part.text)))
        plain = Counter(
            _fold(word.text) for _, words in self.parts for word in words if word.shape == "lower"
        )
        self.lower = frozenset(plain)
        # German capitalizes every noun: a capital in a sentence says nothing there.
        self.german = sum(plain[word] for word in _GERMAN) >= max(5, 0.12 * plain.total())
        # In another language, the everyday words that start a line aren't known.
        english = sum(times for word, times in plain.items() if word in _SMALL)
        self.foreign = self.german or (plain.total() >= 20 and english < 0.25 * plain.total())
        # Labels that come again are a form's (Champion: Dana Lee), not names.
        self.labels = Counter(
            _fold(words[0].text) for _, words in self.parts if words and words[0].colon
        )
        self.finds: dict[str, _Find] = {}
        self.listed: set[str] = set()  # the people this line lists
        self.tailed: list[tuple[str, str]] = []  # Falkner Ridge, Falkner: a name with a plain tail

    def add(self, spelled: str, weight: float) -> None:
        if _fold(spelled) not in self.listed:
            self.finds.setdefault(_fold(spelled), _Find()).add(spelled, weight)

    def ordinary(self, word: str, loose: bool = False) -> bool:
        """Whether a word is an everyday one, or the file also writes it in lower case."""
        folded = _fold(word)
        if folded in self.lower and _shape(word) != "inner":
            return True
        if self.german and loose and folded.endswith(_GERMAN_ENDINGS):
            return True
        return _everyday(word, loose)

    def look(self) -> None:
        for part, words in self.parts:
            self.listed = set()
            if part.people:
                label = None if part.cell else _LABEL.match(part.text)
                value = part.text[label.end() :] if label else part.text
                for name in _people(value, part.people):
                    self.finds.setdefault(_fold(name), _Find()).add(name, PERSON)
                    self.listed.add(_fold(name))
                words = _words(value)  # the label itself (Von, Asistentes) is no name
            capitals = [word for word in words if word.shape in ("title", "upper", "inner")]
            if len(capitals) > 1 and all(w.shape in ("upper", "other", "model") for w in words):
                continue  # a heading in capitals
            lowered = sum(word.shape == "lower" and len(word.text) > 1 for word in words)
            prose = lowered >= 3 and lowered * 2 >= len(capitals)
            for before, word in zip([None, *words], words, strict=False):
                if word.shape == "model" and self.model(word, before, part, prose):
                    self.add(word.text, MODEL)
            self.runs(words, part, prose)

    def model(self, word: _Word, before: _Word | None, part: _Part, prose: bool) -> bool:
        """Whether letters and digits are a product's name rather than an id (a host, a room, a
        version): in a column of models, after a maker's name (Zebra ZD421), or mostly letters
        in a sentence."""
        text = word.text
        if len(text) < 3 or not _MODEL.fullmatch(text):
            return False
        if part.model:
            return True
        if _NOT_MODEL.fullmatch(text) or (before is not None and _fold(before.text) in _BEFORE_ID):
            return False
        if text[0].isdigit() or text[1:] == "365":
            return True
        if before is not None and word.joined and before.shape in ("title", "inner"):
            if not self.ordinary(before.text) or _fold(before.text) in everyday.NAMES:
                return True
        return prose and 2 * sum(map(str.isalpha, text)) >= len(text)

    def runs(self, words: list[_Word], part: _Part, prose: bool) -> None:
        """Looks at capitalized words in a row, with the particles in them (Maria de la Cruz)."""
        first = -1
        for index, word in enumerate([*words, _END]):
            capital = word.shape in ("title", "upper", "inner")
            # German has articles and "von" between its nouns.
            particle = word.shape == "lower" and word.text in _PARTICLES and not self.german
            if first >= 0 and word.joined and (capital or particle):
                continue
            if first >= 0:
                self.run(words, first, index, part, prose)
            first = index if capital else -1

    def run(self, words: list[_Word], first: int, last: int, part: _Part, prose: bool) -> None:
        if first == 0 and last == 1 and self.labels[_fold(words[0].text)] > 1:
            return
        run = [self.kept(word) for word in words[first:last] if len(word.text) > 1]  # no initials
        while run and run[-1].shape == "lower":
            run.pop()
        if not run:
            return
        following = words[last] if last < len(words) else _END
        before = words[first - 1] if first else None
        start, named = run[0].start, part.named and (part.cell or first > 0)
        while len(run) > 1 and _fold(run[0].text) in _TITLES:
            run.pop(0)
            start, named = False, True
        company = _fold(following.text) in _COMPANY
        while len(run) > 1 and _fold(run[-1].text) in _COMPANY:
            run.pop()
            company = True
        # A cell, a label's value or a term being defined, as it's written there.
        value = following.start and (part.cell or (before is not None and before.colon))
        defined = before is None and prose and words[last - 1].colon
        whole = company or (len(run) > 1 and start and (value or defined))
        if self.foreign and start and not whole and len(run) > 1:
            run.pop(0)
            start = False
        while run and self.leading(run[0], start, whole):
            named = named or _fold(run[0].text) in _INTRODUCE
            run.pop(0)
            start = False
        strong = prose and not start
        full = list(run)
        while run and not company and self.trailing(run[-1]):
            run.pop()
        if whole and any(self.ordinary(word.text) for word in run):
            run = full  # Contoso Managed Services, not Contoso Managed
        full = list(run)
        while run and not (strong or whole) and self.ordinary(run[-1].text, loose=True):
            run.pop()
        capitals = [word for word in run if word.shape != "lower"]
        unusual = [word for word in capitals if not self.ordinary(word.text, loose=not strong)]
        inner = [word for word in capitals if word.shape == "inner"]
        if not unusual:
            return
        if inner and len(inner) >= len(unusual):
            for word in inner:
                self.single(word, strong)
        elif company and 2 <= len(capitals) <= 4:
            self.add(_spelled(run), NAME)
        elif len(capitals) == 1 and 1 < len(full) <= 3:
            self.tailed.append((_spelled(full), capitals[0].text))
        elif len(capitals) == 1:
            compound = capitals[0].text not in {word.text for word in words[first:last]}
            joined = before is not None and words[first].joined
            preposed = bool(joined) and _fold(before.text) in _PREPOSITIONS
            self.single(capitals[0], strong, named or company or compound, preposed)
        elif len(capitals) <= 3 and strong and not (self.german and len(unusual) < len(capitals)):
            self.add(_spelled(run), NAME)
        elif len(capitals) <= 3 and (
            (whole and not self.german)
            or all(word in unusual or _fold(word.text) in everyday.NAMES for word in capitals)
        ):
            self.add(_spelled(run), LISTED)
        else:
            for word in unusual:
                self.single(word, False)

    def leading(self, word: _Word, start: bool, whole: bool) -> bool:
        """Whether a run's first word isn't part of the name: an article, what a sentence or a
        heading starts with, a common abbreviation."""
        folded = _fold(word.text)
        small = any(form in STOP_WORDS for form in (folded, *_bases(folded)))
        if word.shape == "lower" or small:
            return True
        if word.shape == "upper":
            return not whole and self.ordinary(word.text)
        if folded in everyday.NAMES or not self.ordinary(word.text, loose=start):
            return False
        return self.german or (start and not whole)

    def trailing(self, word: _Word) -> bool:
        """Whether a run's last word says what the name is about (the Proxmox Cluster)."""
        folded = _fold(word.text)
        if word.shape == "lower" or (word.shape == "upper" and folded in everyday.SHORT):
            return True
        return folded in everyday.TAILS | everyday.NAMES | everyday.CALENDAR

    def kept(self, word: _Word) -> _Word:
        """Proxmox, of Proxmox-based, Proxmox-Server and non-Proxmox."""
        pieces = word.text.split("-")
        if len(pieces) == 2 and word.shape in ("title", "inner"):
            for name, other in (pieces, pieces[::-1]):
                if len(name) > 2 and name[0].isupper() and len(other) > 2:
                    if (name == pieces[0] or other.islower()) and self.ordinary(other):
                        if not self.ordinary(name):
                            return word._replace(text=name, shape=_shape(name))
        return word

    def single(
        self, word: _Word, strong: bool, named: bool = False, preposed: bool = False
    ) -> None:
        """One word: ``strong`` in the middle of a sentence, ``named`` when what's around it
        says it's a name, ``preposed`` right after a German preposition."""
        text = word.text
        if word.shape == "inner":
            if not self.ordinary(text) and _product(text):
                self.add(text, PRODUCT)
        elif word.shape == "upper":
            if strong and 3 <= len(text) <= 6 and not self.ordinary(text):
                self.add(text, INITIALS)
        elif len(text) < 3 or self.ordinary(text, loose=not strong):
            return
        elif named:
            self.add(text, PROPER if strong else LISTED)
        elif not self.german and (strong or not self.foreign):
            self.add(text, PROPER if strong else ALONE)
        elif self.german and preposed and not self.ordinary(text, loose=True):
            self.add(text, ALONE)


def _merged(file: _File) -> dict[str, _Find]:
    """The finds, each name once: Dana Lee instead of Dana, Fishbowl instead of the Fishbowl
    Advanced said once."""
    finds = file.finds
    for phrase, name in file.tailed:
        if _fold(name) in finds:
            finds[_fold(name)].add(name, ALONE)
        elif not file.foreign:
            finds.setdefault(_fold(phrase), _Find()).add(phrase, LISTED)
    for key in [key for key in finds if " " in key]:
        phrase, words = finds[key], re.findall(r"[^\s&]+", key)
        for word in words:
            alone = finds.get(word)
            if alone is None:
                continue
            plain = all(file.ordinary(other) for other in words if other != word)
            if phrase.weight < PERSON and plain:
                alone.add(alone.term, alone.weight, phrase.count)
                del finds[key]
                break
            if phrase.weight >= PERSON or phrase.count * 3 >= alone.count:
                del finds[word]
    return finds


def found_in(text: str, known: Iterable[str], limit: int = LIMIT) -> list[dict]:
    """The names and terms in a file's text worth knowing, the most useful first: [{"term",
    "count"}]. Not those ``known`` already, in any capitals."""
    file = _File(text)
    file.look()
    taken = {_fold(term) for term in known}
    taken |= {word for term in taken if " " in term for word in term.split()}
    ranked = sorted(_merged(file).items(), key=lambda item: -item[1].score)
    offered = [
        {"term": find.term, "count": find.count}
        for key, find in ranked
        if key not in taken
        and usable(find.term)
        and not secretish(find.term)
        and len(find.term) <= LONGEST
    ]
    return offered[:limit]
