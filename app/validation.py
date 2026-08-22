"""Checking what people type before it is stored.

Worth being clear about what this is and isn't for.

It is NOT what stops SQL injection. Every query in this app passes its values
as parameters, so a name like  '); DROP TABLE items;--  is stored as those
literal characters and can never execute. Blocklisting words like "drop table"
would add nothing and would reject legitimate text.

What this module actually does is keep stored text sane and printable:

  * caps how long each field can be, so nobody pastes a novel into a name
  * removes characters that are invisible or that lie about the text they are
    in -- zero-width spaces, and the bidirectional overrides that can make a
    name display in an order different from how it is stored
  * refuses emoji and pictographs in names, which keeps labels, exports and
    sorting predictable
  * normalises accents to one canonical form, so two names that look identical
    really are identical and match each other in a search
"""

from __future__ import annotations

import re
import unicodedata

# Generous enough that nobody meets them by accident, small enough that a
# stray paste can't fill the database.
LIMITS = {
    "name": 120,
    "category": 60,
    "asset_tag": 60,
    "serial_number": 80,
    "location": 80,
    "notes": 2000,
    "note": 500,
    "email": 200,
    "department": 60,
    "username": 40,
    "display_name": 80,
}

# Ranges holding emoji and pictographs. Deliberately targeted rather than
# "reject every symbol", so that (c), (r) and the degree sign still work.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),   # emoji, pictographs, symbols, game pieces
    (0x1F004, 0x1F0CF),   # mahjong and playing cards
    (0x2600, 0x27BF),     # misc symbols and dingbats
    (0x2B00, 0x2BFF),     # arrows and geometric shapes used as emoji
    (0xFE00, 0xFE0F),     # variation selectors that turn glyphs into emoji
    (0x1F1E6, 0x1F1FF),   # regional indicators, which make flags
    (0x20E3, 0x20E3),     # combining enclosing keycap
)


class ValidationError(Exception):
    """Text that can't be stored, with a message for the person who typed it."""


def _is_emoji(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in _EMOJI_RANGES)


def clean_text(value: str | None, field: str, *, required: bool = False,
               multiline: bool = False, allow_emoji: bool = False,
               label: str | None = None) -> str:
    """Return `value` tidied and safe to store, or raise ValidationError.

    `field` picks the length limit and names the field in any error message.
    """
    shown = label or field.replace("_", " ")
    text = value or ""

    # One canonical form for accented characters, so "José" typed two
    # different ways becomes one string that matches itself.
    text = unicodedata.normalize("NFC", text)

    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)

        if char in ("\n", "\r", "\t"):
            # Newlines belong in a notes box and nowhere else.
            cleaned.append("\n" if multiline and char != "\t" else " ")
            continue

        if category in ("Cc", "Cf", "Cs", "Co", "Cn"):
            # Control, format, surrogate, private-use and unassigned. This is
            # where zero-width spaces and the bidirectional overrides live --
            # characters that are invisible or that reorder what you see, so
            # stored text can read differently from how it displays.
            continue

        if not allow_emoji and _is_emoji(char):
            raise ValidationError(
                f"The {shown} can't contain emoji or picture characters.")

        cleaned.append(char)

    text = "".join(cleaned)

    # Collapse runs of spaces; keep paragraph breaks in multiline fields.
    if multiline:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join(line.strip() for line in text.split("\n")).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()

    if required and not text:
        raise ValidationError(f"A {shown} is needed.")

    limit = LIMITS.get(field, 200)
    if len(text) > limit:
        raise ValidationError(
            f"The {shown} is too long — {len(text)} characters, and the most"
            f" allowed is {limit}.")

    return text


def clean_email(value: str | None) -> str:
    """An email address, or empty. Kept loose on purpose.

    Real addresses are stranger than most patterns allow, and the only thing
    that truly proves one works is sending to it. This catches the typos worth
    catching and lets everything else through.
    """
    text = clean_text(value, "email")
    if not text:
        return ""
    if " " in text or text.count("@") != 1:
        raise ValidationError(f"'{text}' doesn't look like an email address.")
    local, _, domain = text.partition("@")
    if not local or "." not in domain or domain.startswith(".") \
            or domain.endswith("."):
        raise ValidationError(f"'{text}' doesn't look like an email address.")
    return text


def clean_username(value: str | None) -> str:
    """Usernames are the one field with a strict shape.

    They are typed at a sign-in prompt, sometimes read off a note, so keeping
    them to plain characters avoids the class of problem where two accounts
    look identical on screen.
    """
    text = clean_text(value, "username", required=True)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        raise ValidationError(
            "A username can only contain letters, numbers, dots, dashes and"
            " underscores.")
    return text.lower()
