"""Words to know: names and jargon Whisper should spell right.

The user lists terms in Settings, one per line, optionally followed by what Whisper
mishears them as: "Proxmox: prox mocks, proxmocks". Terms (plus the names of people the
user has identified) go to Whisper as hotwords; afterwards the misheard forms, and terms in
the wrong case, are corrected in the transcript text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Whisper's prompt holds about 224 tokens; a long hotword list also makes it invent words.
HOTWORD_CHARS = 500
PROMPT = "Names and terms in this meeting: {}."


@dataclass
class Vocabulary:
    terms: list[str] = field(default_factory=list)
    corrections: dict[str, str] = field(default_factory=dict)  # lowercase heard -> term

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
        """Misheard forms and wrong-case terms, replaced by the term as written."""
        for heard, term in sorted(self.corrections.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", term, text, flags=re.IGNORECASE)
        return text


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
                vocabulary.corrections[term.lower()] = term
        for word in misheard:
            if word and terms:
                vocabulary.corrections[word.lower()] = terms[0]
    return vocabulary
