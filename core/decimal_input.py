# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Read a number out of what an operator typed, in either notation (R10).

``ui-standards.md`` §2.5 R10: a number field accepts both the German and the
English notation and is parsed **server-side**, which is what lets the control
be a plain ``type="text" inputmode="decimal"`` input instead of a
locale-bound ``type="number"``. This module is that parser. It sits in
``core/`` and imports nothing of the project's, because every Area's forms
delegate to it as their strand sweeps them.

Four rules decide the notation, and the surface echoes back what they read::

    1.234,56   -> 1234.56      both present: the *last* one is decimal
    1,234.56   -> 1234.56      both present: the *last* one is decimal
    1,234      -> 1.234        one, once: it is the decimal separator
    1.234.567  -> 1234567      one, repeated: it is grouping
    1 234'567  -> 1234567      space, NBSP, thin space and ``'`` group
    -12,5 / +7 -> -12.5 / 7    a leading ``-`` or ``+`` is the sign
    12,        -> 12           a trailing separator is no fraction
    12,34,56   -> None         grouping runs 1-3 digits, then 3s
    1e3 / abc  -> None         no exponent notation, no stray glyph

A lone separator is never read as grouping: guessing by digit count would be a
coin toss the operator cannot see, and :func:`was_interpreted` is what makes
the stated rule safe — the surface says which number it read.
"""

from __future__ import annotations

from decimal import Decimal

#: Grouping glyphs that carry no value of their own and are simply dropped.
_NOISE: dict[int, None] = str.maketrans({c: None for c in "    '"})

#: Every glyph a number may consist of once sign and noise are off.
_BODY: frozenset[str] = frozenset("0123456789.,")

#: What makes a raw string *interpreted* rather than taken at face value.
_INTERPRETED: frozenset[str] = frozenset(".,'+-    ")


def was_interpreted(raw: str | None) -> bool:
    """Did reading this text apply a rule the operator should be shown?

    Args:
        raw: The posted text, exactly as typed.

    Returns:
        ``True`` where a separator, a sign or a grouping glyph is present —
        the cases where the two notations could disagree about the value.
    """
    return raw is not None and any(ch in _INTERPRETED for ch in raw.strip())


def parse_decimal(raw: str | None) -> Decimal | None:
    """Read a :class:`~decimal.Decimal` out of typed text, or ``None``.

    Never raises and never refuses loudly: a half-typed value is the ordinary
    state of a field being typed into, so anything unreadable reads as ``None``
    and the surface derives less (operator decision D-2).

    Args:
        raw: The posted text, exactly as typed.

    Returns:
        The value this module's table states, or ``None``.
    """
    if raw is None:
        return None
    text = raw.strip().translate(_NOISE)
    sign = "-" if text[:1] == "-" else ""
    text = text[1:] if text[:1] in {"-", "+"} else text
    if not text.strip(".,") or not all(ch in _BODY for ch in text):
        return None
    dots, commas = text.count("."), text.count(",")
    whole, fraction, grouping = text, "", ""
    if dots and commas:
        # Both notations are on screen at once, so the operator's own ordering
        # decides: whichever separator comes last is the decimal one, it may
        # come only once, and nothing groups behind it.
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        grouping = "," if decimal_sep == "." else "."
        if text.count(decimal_sep) != 1 or grouping in text[text.index(decimal_sep) :]:
            return None
        whole, _, fraction = text.partition(decimal_sep)
    elif dots or commas:
        separator = "." if dots else ","
        if text.count(separator) == 1:
            whole, _, fraction = text.partition(separator)
        else:
            grouping = separator
    if grouping:
        head, *groups = whole.split(grouping)
        if not (1 <= len(head) <= 3 and all(len(group) == 3 for group in groups)):
            return None
        whole = whole.replace(grouping, "")
    return Decimal(f"{sign}{whole or '0'}{'.' + fraction if fraction else ''}")
