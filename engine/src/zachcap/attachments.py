"""Files attached to a meeting: a log, an exported email, a config file.

Their text joins the meeting's notes, so summaries, ticket notes and Ask can use the exact
error codes, or the email that led to the call. Text only: the file is read once, and its
text (up to MAX_TEXT characters) is kept with the meeting; the file itself is never changed,
moved or copied. Sent to Claude only with the rest of the meeting, masked like your notes.
"""

from __future__ import annotations

import email
import email.policy
import html
import re
from pathlib import Path

KIND = "file"
MAX_BYTES = 5_000_000  # a bigger file isn't read
MAX_TEXT = 20_000  # characters kept from one file
MAX_FILES = 10  # per meeting
_LEFT_OUT = " more characters left out]"  # how a text that was cut ends
TEXT_TYPES = {
    ".txt", ".log", ".md", ".csv", ".tsv", ".json", ".xml", ".ini", ".cfg", ".conf", ".config",
    ".yaml", ".yml", ".toml", ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".py", ".reg", ".html",
    ".htm", ".eml", ".err", ".out",
}  # fmt: skip


class NotText(ValueError):
    """The file can't be attached: not a text file, too big, or unreadable."""


def _decoded(data: bytes) -> str:
    """Bytes as text: by their byte-order mark, else UTF-8, else Windows' own code page."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _without_markup(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _email(data: bytes) -> str:
    """An exported email (.eml): who, when, the subject, then the text of its body."""
    message = email.message_from_bytes(data, policy=email.policy.default)
    lines = [
        f"{name}: {message[name]}"
        for name in ("From", "To", "Cc", "Date", "Subject")
        if message[name]
    ]
    body = message.get_body(preferencelist=("plain", "html"))
    content = body.get_content() if body is not None else ""
    if body is not None and body.get_content_type() == "text/html":
        content = _without_markup(content)
    return "\n".join(lines) + "\n\n" + content


def read(path: Path) -> tuple[str, str]:
    """The file's name and its text, tidied and cut to MAX_TEXT characters. Raises NotText
    with a reason to show when it can't be attached."""
    name = path.name
    suffix = path.suffix.lower()
    if suffix not in TEXT_TYPES:
        raise NotText(
            f"{name} isn't a text file ZachCap reads (a log, text, CSV, JSON, XML, an email...)"
        )
    try:
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise NotText(f"{name} is too big to attach (over {MAX_BYTES // 1_000_000} MB)")
        data = path.read_bytes()
    except OSError as exc:
        raise NotText(f"Couldn't read {name}: {exc.strerror or exc}") from exc
    text = _email(data) if suffix == ".eml" else _decoded(data)
    if suffix in (".html", ".htm"):
        text = _without_markup(text)
    if "\x00" in text[:4096]:
        raise NotText(f"{name} doesn't hold text")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise NotText(f"{name} is empty")
    if len(text) > MAX_TEXT:
        left = len(text) - MAX_TEXT
        text = text[:MAX_TEXT].rstrip() + f"\n[... {left:,}{_LEFT_OUT}"
    return name, text


def was_cut(text: str) -> bool:
    """Whether a file's text is only its first MAX_TEXT characters."""
    return text.endswith(_LEFT_OUT)


def as_note(name: str, text: str) -> str:
    """How an attached file is kept as a note: its name on the first line, then its text."""
    return f"{name}\n{text}"


def parts(note_text: str) -> tuple[str, str]:
    """A file note's name and text."""
    name, _, text = note_text.partition("\n")
    return name, text
