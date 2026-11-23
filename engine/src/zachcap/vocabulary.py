"""Words to know: names and jargon Whisper should spell right.

The user lists terms in Settings, one per line, optionally followed by what Whisper
mishears them as: "Proxmox: prox mocks, proxmocks". Terms (plus the names of people the
user has identified) go to Whisper as hotwords; afterwards the misheard forms, and distinctive
terms in the wrong case, are corrected in the transcript text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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
