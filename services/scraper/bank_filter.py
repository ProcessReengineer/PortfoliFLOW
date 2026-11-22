# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bank-detail filter for the Reading Desk (ADR-0139 §4, RD-D-24).

Bank details are never extracted and never stored. The prompt excludes them;
this module removes them again from every string of the parsed model output,
before anything is rendered or stored. Changed bank details in a capital call
are a known fraud pattern, so an identifier that reached a reading would be a
dangerous convenience.

What is removed, each replaced by :data:`REPLACEMENT`:

* an IBAN, printed without separators or in groups of four, whose check
  digits are valid (ISO 13616, mod 97) — anywhere in a string;
* after a bank term (``IBAN``, ``BIC``, ``SWIFT``, ``BLZ``, ``sort code``,
  ``routing``, ``ABA``, ``account number``, ``Kontonummer`` …): an
  IBAN-shaped identifier whatever its check digits, a BIC (after ``BIC`` or
  ``SWIFT`` only), or a run of at least six digits;
* a BIC that directly follows a removed identifier;
* after the bare words ``account`` or ``Konto``: an IBAN-shaped identifier,
  or a run of at least eight digits that is not written as an amount in
  groups of three. With a qualifier ("account no.", "Konto-Nr.") the two
  words count as bank terms.

What is kept: amounts, dates, percentages, ISINs and any long number that
does not follow a bank term. The term itself stays, so the text still says
that a bank detail stood there.

Pure: standard library only.
"""

from __future__ import annotations

import re
from typing import Any

#: What a removed bank detail is replaced by.
REPLACEMENT = "[bank detail removed]"

# One separator between the characters or groups of an identifier: space,
# tab, no-break space, narrow no-break space or hyphen.
_SEP = r"[ \t  \-]"

# An IBAN as it is printed: two letters, two check digits, then 11 to 30
# characters without separators, or groups of four with a shorter last group.
_IBAN_PRINTED = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}[0-9]{2}"
    rf"(?:[A-Z0-9]{{11,30}}|(?:{_SEP}[A-Z0-9]{{4}}){{2,7}}(?:{_SEP}[A-Z0-9]{{1,4}})?)"
    r"(?![A-Za-z0-9])"
)

_STRONG_TERM = (
    r"IBAN|BIC|SWIFT|BLZ|Bankleitzahl|sort[ \-]?code|routing|ABA|acct\.?|a/c"
    r"|Kontonummer|Kontonr\.?|Kto\.?"
)
_WEAK_TERM = r"(?<!capital\s)account|Konto"
_QUALIFIER = r"[ .\-]*(?:number|nummer|no|nr|num|code|#)"

# A token that starts another bank term ends an identifier that precedes it.
_TERM_TOKEN = r"(?:IBAN|BIC|SWIFT|BLZ|ABA|KTO|KONTO|ACCOUNT|ACCT|SORT|ROUTING)(?![A-Za-z0-9])"

# A bank term with its qualifier and punctuation, then the identifier. The
# term is matched without regard to case; the identifier is case-sensitive,
# so that an ordinary word after "SWIFT" is not taken for a BIC.
_AFTER_TERM = re.compile(
    r"(?<![A-Za-z])"
    rf"(?i:(?:(?P<strong>{_STRONG_TERM})|(?P<weak>{_WEAK_TERM}))(?P<qualifier>{_QUALIFIER})?)"
    r"(?![A-Za-z])"
    rf"(?i:(?:[ \t.]*[/&]\s*(?:{_STRONG_TERM}|account|Konto)(?:{_QUALIFIER})?(?![A-Za-z]))?)"
    r"[ \t.]*[:=]?[ \t ]*"
    r"(?:"
    rf"(?P<iban>[A-Z]{{2}}[0-9]{{2}}(?:[A-Z0-9]|{_SEP}(?!{_TERM_TOKEN})[A-Z0-9]){{8,30}})"
    r"|(?P<bic>[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)"
    rf"|(?P<digits>[0-9](?:{_SEP}?[0-9]){{5,}})"
    r")(?![A-Za-z0-9])(?![.,][0-9])"
)

# A BIC that directly follows a removed identifier: "IBAN …, COBADEFFXXX".
_BIC_AFTER_REMOVED = re.compile(
    r"(?<=\[bank detail removed\])(?P<gap>[ \t\u00a0,;/|]*)"
    r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?(?![A-Za-z0-9])"
)

_BIC_TERMS = frozenset({"bic", "swift"})
_AMOUNT_GROUPED = re.compile(r"[0-9]{1,3}(?:[   ][0-9]{3})+")
_STRONG_MIN_DIGITS = 6
_WEAK_MIN_DIGITS = 8

# Whether a keyword's name asks for a bank detail.
_BANK_TERM_NAME = re.compile(
    r"(?<![a-z])(?:"
    r"iban|bic|swift|blz|bankleitzahl|sort[ \-]?code|routing|aba|acct|a/c"
    r"|kontonummer|kontonr|konto|kto|bankverbindung|bankkonto"
    r"|bank[ \-]+(?:account|details|data|information|instructions)"
    r"|account[ .\-]*(?:number|no|nr|num|#|details)"
    r"|wire|payment[ \-]+instructions|zahlungsanweisung"
    r")(?![a-z])",
    re.IGNORECASE,
)


def asks_for_bank_details(name: str) -> bool:
    """Whether a keyword's name asks for a bank detail.

    Such a keyword is never sent to the model. The bare word "account" does
    not count ("capital account" is a legitimate figure); "account number"
    does.

    Args:
        name: The keyword's display name.

    Returns:
        ``True`` if the name contains a bank term.
    """
    return _BANK_TERM_NAME.search(name) is not None


def remove_bank_details(text: str) -> str:
    """Return ``text`` with every bank detail replaced by :data:`REPLACEMENT`.

    Args:
        text: One string of the model's output.

    Returns:
        The text without account identifiers; unchanged if it holds none.
    """
    filtered = _AFTER_TERM.sub(_replace_after_term, _remove_valid_ibans(text))
    if REPLACEMENT in filtered:
        filtered = _BIC_AFTER_REMOVED.sub(rf"\g<gap>{REPLACEMENT}", filtered)
    return filtered


def filter_parsed_output(value: Any) -> Any:
    """Apply :func:`remove_bank_details` to every string value of parsed JSON.

    Walks dicts and lists. Keys are left alone: the mapping reads the output
    by the keys it asked for and never stores one the model invented.

    Args:
        value: The parsed model output, or any part of it.

    Returns:
        A new structure of the same shape with every string filtered.
    """
    if isinstance(value, str):
        return remove_bank_details(value)
    if isinstance(value, dict):
        return {key: filter_parsed_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [filter_parsed_output(item) for item in value]
    return value


def _remove_valid_ibans(text: str) -> str:
    """Replace every printed IBAN whose check digits are valid."""
    parts: list[str] = []
    position = 0
    while (match := _IBAN_PRINTED.search(text, position)) is not None:
        end = _valid_iban_end(match.group())
        if end is None:
            # Not an IBAN. Look again from the next character: a real one
            # may start inside this candidate.
            parts.append(text[position : match.start() + 1])
            position = match.start() + 1
            continue
        parts.append(text[position : match.start()])
        parts.append(REPLACEMENT)
        position = match.start() + end
    parts.append(text[position:])
    return "".join(parts)


def _valid_iban_end(candidate: str) -> int | None:
    """Return where the longest valid IBAN at the start of ``candidate`` ends.

    A candidate printed in groups may run on into the text that follows it
    ("… 0130 00 BIC"), so every prefix that ends with a group is tried, the
    longest first.

    Args:
        candidate: A match of the printed-IBAN pattern.

    Returns:
        The end offset of the IBAN within ``candidate``, or ``None`` if no
        prefix of 15 to 34 characters has valid check digits.
    """
    offsets = [index for index, char in enumerate(candidate) if char.isalnum()]
    for count in range(min(len(offsets), 34), 14, -1):
        end = offsets[count - 1] + 1
        if end < len(candidate) and candidate[end].isalnum():
            continue  # inside a group
        compact = "".join(candidate[index] for index in offsets[:count])
        if _has_valid_check_digits(compact):
            return end
    return None


def _has_valid_check_digits(compact: str) -> bool:
    """ISO 13616: move the first four characters to the end, read letters as 10 to 35."""
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _replace_after_term(match: re.Match[str]) -> str:
    """Replace the identifier after a bank term, or keep the match unchanged."""
    term = match.group("strong")
    if match.group("iban") is not None:
        group = "iban"
    elif match.group("bic") is not None:
        if term is None or term.lower() not in _BIC_TERMS:
            return match.group()
        group = "bic"
    else:
        group = "digits"
        digits = match.group("digits")
        count = sum(char.isdigit() for char in digits)
        # "account" and "Konto" alone are weak; with a qualifier ("account
        # no.", "Konto-Nr.") they name an identifier as surely as "IBAN" does.
        weak = term is None and match.group("qualifier") is None
        if weak and (count < _WEAK_MIN_DIGITS or _AMOUNT_GROUPED.fullmatch(digits)):
            return match.group()
        if count < _STRONG_MIN_DIGITS:
            return match.group()
    head = match.group()[: match.start(group) - match.start()]
    return head + REPLACEMENT
