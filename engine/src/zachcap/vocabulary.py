"""Words to know: names and jargon Whisper should spell right.

The user lists terms in Settings, one per line, optionally followed by what Whisper
mishears them as: "Proxmox: prox mocks, proxmocks". Terms (plus the names of people the
user has identified) go to Whisper as hotwords; afterwards the misheard forms, and distinctive
terms in the wrong case, are corrected in the transcript text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Whisper's prompt holds about 224 tokens; a long hotword list also makes it invent words.
HOTWORD_CHARS = 500
PROMPT = "Names and terms in this meeting: {}."


def distinctive(term: str) -> bool:
    """Whether a term's spelling can replace any casing of it in the text.

    Several words, inner capitals or digits (Microsoft Teams, ConnectWise, M365) don't turn
    up by accident. "Will", "Mark" or "IT" would recase every "will", "mark" and "it".
    """
    has_lower = any(c.islower() for c in term)
    inner_capital = any(c.isupper() for c in term[1:]) and has_lower
    return " " in term.strip() or inner_capital or any(c.isdigit() for c in term)


@dataclass
class Vocabulary:
    terms: list[str] = field(default_factory=list)
    corrections: dict[str, str] = field(default_factory=dict)  # lowercase heard -> term
    _pattern: re.Pattern[str] | None = field(default=None, init=False, repr=False, compare=False)

    def hotwords(self, names: list[str] = ()) -> str | None:
        """The terms and people's names for Whisper, most specific first, within the limit.

        Whisper copies the style of its prompt: a bare comma list made it drop every full
        stop in testing (6 sentence ends to 0), so the words go in as a sentence.
        """
        words: list[str] = []
        for word in [*self.terms, *names]:
            if word and word.lower() not in {w.lower() for w in words}:
                if len(PROMPT.format(", ".join([*words, word]))) > HOTWORD_CHARS:
                    break
                words.append(word)
        return PROMPT.format(", ".join(words)) if words else None

    def correct(self, text: str) -> str:
        """Misheard forms and wrong-case terms, replaced by the term as written.

        One pattern for the whole list, matched once per line: a long list stays fast on a
        long meeting, and terms are plain text (CORP\\admin is a name, not a regex).
        """
        if not self.corrections:
            return text
        if self._pattern is None:
            heard = sorted(self.corrections, key=len, reverse=True)  # the longest form wins
            self._pattern = re.compile(
                r"(?<!\w)(?:" + "|".join(map(re.escape, heard)) + r")(?!\w)", re.IGNORECASE
            )
        return self._pattern.sub(lambda m: self.corrections.get(m[0].lower(), m[0]), text)


def parse(text: str | None) -> Vocabulary:
    """'Term' or 'Term: misheard, other misheard' per line; commas separate several terms."""
    vocabulary = Vocabulary()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            term, heard = (part.strip() for part in line.split(":", 1))
            terms, misheard = [term] if term else [], [h.strip() for h in heard.split(",")]
        else:
            terms, misheard = [t.strip() for t in line.split(",")], []
        for term in terms:
            if term and term not in vocabulary.terms:
                vocabulary.terms.append(term)
                if distinctive(term):
                    vocabulary.corrections[term.lower()] = term
        for word in misheard:
            if word and terms:
                vocabulary.corrections[word.lower()] = terms[0]
    return vocabulary


# Fixing a line ------------------------------------------------------------------------

LIST_LIMIT = 4000  # characters, as in Settings
_WORD_EDGES = "\"'()[]{}.,;:!?"


def _bare(word: str) -> str:
    return word.strip(_WORD_EDGES)


def suggest(before: str, after: str, text: str | None) -> dict | None:
    """When a fixed line swaps a short phrase for one that looks or sounds alike ("connect
    wise" for "ConnectWise"), the Words to know entry that would fix it in future meetings:
    {"term", "misheard"}, with misheard None when only the capitalization changed. None for
    other edits (rewording, a different fact) and for what the list already has."""
    old, new = before.split(), after.split()
    matcher = SequenceMatcher(
        None, [_bare(w).lower() for w in old], [_bare(w).lower() for w in new], autojunk=False
    )
    changed = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    vocabulary = parse(text)
    if not changed and before != after:
        # Only capitals or punctuation changed: a name or distinctive term, not the
        # capital that starts a sentence.
        pairs = [(i, _bare(a), _bare(b)) for i, (a, b) in enumerate(zip(old, new, strict=True))]
        pairs = [(i, a, b) for i, a, b in pairs if a != b and a.lower() == b.lower()]
        if len(pairs) != 1:
            return None
        index, _, term = pairs[0]
        proper = index > 0 and term[:1].isupper()
        if not usable(term) or term in vocabulary.terms or not (distinctive(term) or proper):
            return None
        return {"term": term, "misheard": None}
    if len(changed) != 1 or changed[0][0] != "replace":
        return None
    _, i1, i2, j1, j2 = changed[0]
    if i2 - i1 > 3 or j2 - j1 > 4:
        return None
    heard = " ".join(filter(None, (_bare(w) for w in old[i1:i2])))
    term = " ".join(filter(None, (_bare(w) for w in new[j1:j2])))
    if not usable(term) or not usable(heard) or vocabulary.corrections.get(heard.lower()) == term:
        return None
    # Misheard words look alike once spaces and case go: "prox mocks" and "Proxmox".
    # "tomorrow" and "on Friday" don't: that edit changed what was said.
    squeeze = lambda words: re.sub(r"[^0-9a-z]", "", words.lower())  # noqa: E731
    if SequenceMatcher(None, squeeze(heard), squeeze(term)).ratio() < 0.4:
        return None
    return {"term": term, "misheard": heard}


def usable(term: str) -> bool:
    """A term that fits on a Words to know line (no list separators) and has letters."""
    return bool(term) and not any(c in term for c in ":,\n") and any(c.isalpha() for c in term)


def add_correction(text: str | None, term: str, misheard: str | None) -> str:
    """The Words to know list with ``term`` (and what it was misheard as) added: merged into
    the term's own line when it has one, else a new line at the end."""
    term, misheard = term.strip(), (misheard or "").strip()
    if not usable(term) or any(c in misheard for c in ":,\n"):
        raise ValueError("A term can't contain a colon, a comma or a line break")
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        head, colon, tail = line.partition(":")
        if head.strip().lower() != term.lower():
            continue
        heard = [h.strip() for h in tail.split(",") if h.strip()] if colon else []
        if misheard and misheard.lower() not in {h.lower() for h in heard}:
            heard.append(misheard)
        lines[index] = f"{head.strip()}: {', '.join(heard)}" if heard else head.strip()
        break
    else:
        lines.append(f"{term}: {misheard}" if misheard else term)
    merged = "\n".join(line for line in lines if line.strip() or line == "").strip()
    if len(merged) > LIST_LIMIT:
        raise ValueError("Words to know is full (4,000 characters); make room in Settings")
    return merged
