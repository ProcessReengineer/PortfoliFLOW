# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bank-detail filter for the Reading Desk (ADR-0139 §4, RD-D-24).

Bank details are never extracted and never stored. The prompt excludes them;
this module removes them again from every string of the parsed model output,
before anything is rendered or stored. Changed bank details in a capital call
are a known fraud pattern, so an identifier that reached a reading would be a
dangerous convenience.

What the filter is for: the ordinary case that the model, against the prompt,
returns payment details as the document prints them. It is the second of
three layers, after the prompt and the refusal of keywords that ask for bank
details. It is not a proof against a text built to evade it.

What is removed, each replaced by :data:`REPLACEMENT`:

* an IBAN with valid check digits (ISO 13616: a registered country, that
  country's length, mod 97) — anywhere, in upper or lower case, printed
  without separators, in groups, with dots or hyphens, or wrapped over lines;
* after ``IBAN``: an IBAN-shaped identifier whatever its check digits, within
  :data:`_WINDOW` characters; or a run of at least six digits right after it;
* after ``BIC`` or ``SWIFT``: a BIC in upper case right after the term;
* after a number term (``account number``, ``Kontonummer``, ``BLZ``,
  ``sort code``, ``routing``, ``ABA``, ``acct``, ``Kto`` …): every run of at
  least six digits that starts within :data:`_WINDOW` characters, also on the
  next line or after words ("account number is …");
* after the bare words ``account``, ``Konto`` or ``Depot``: a run of at least
  eight digits right after the word, unless it is written as an amount in groups
  of three or follows "capital account" and its like; a run with a leading
  zero is always removed;
* a BIC that directly follows a removed identifier.

What is kept: amounts, dates, percentages, ISINs and any number that does not
follow a bank term. The term itself stays, so the text still says that a bank
detail stood there.

Known limits, accepted (closing report of strand RD-1): a bare number without
any term in the same string; a BIC in lower case; an IBAN with invalid check
digits and no term beside it; an account number printed in groups of three
after the bare word "account".

Every pattern runs in linear time: quantifiers that meet are possessive, and
the text after a term is examined in a bounded window.

Pure: standard library only.
"""

from __future__ import annotations

import re
from typing import Any

#: What a removed bank detail is replaced by.
REPLACEMENT = "[bank detail removed]"

# IBAN length by country code (ISO 13616 registry).
_IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27,
    "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15,
    "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24,
    "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23,
    "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}  # fmt: skip
_IBAN_MAX_LENGTH = 34
_IBAN_MIN_TAKEN = 11

# Characters that may stand between the characters of a printed identifier:
# blanks of any kind, line breaks, zero-width space, dot, hyphen.
_SEPARATORS = frozenset(" \t\r\n\u00a0\u2007\u2009\u202f\u200b.-")
_MAX_SEPARATORS = 3

# The head of an IBAN: country code and check digits. It starts a token, or
# is glued to the label ("IBANDE89…").
_IBAN_HEAD = re.compile(r"(?:(?<![A-Za-z0-9])|(?<=[Ii][Bb][Aa][Nn]))[A-Za-z]{2}[0-9]{2}")

# How far behind a term an identifier is looked for.
_WINDOW = 48

_JOINER = r"[\s._\-\u00a0]{0,3}+"
_NUMBER_WORD = r"(?:numbers?|nummern?|nos?|nrn?|num|#|\u2116)"
_TERM = re.compile(
    r"(?<![A-Za-z])(?:"
    r"(?P<iban>IBANs?)"
    rf"|(?P<bic>(?:BICs?|SWIFT)(?:{_JOINER}codes?)?)"
    r"|(?P<number>BLZ|Bankleitzahl|sort[ \-_]?codes?|routing|ABA|acct|a/c|Kto"
    r"|Kontonummern?|Kontonr|Bankkonto|Depotnummern?|Depotnr"
    rf"|(?:account|Konto|Depot){_JOINER}{_NUMBER_WORD})"
    r"|(?P<weak>account|Konto|Depot)"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)

# Right after a term: blanks, line breaks and punctuation only.
_PUNCTUATION_GAP = re.compile(r"[\s.:;=#()\[\],/\-\u2013\u2014\u00a0]{0,12}+")
_DIGIT_RUN = re.compile(r"[0-9](?:[ \u00a0\u2009\u202f\-]?+[0-9])*+")
_BIC = re.compile(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?+(?![A-Za-z0-9])")
_BIC_AFTER_REMOVED = re.compile(
    r"(?<=\[bank detail removed\])(?P<gap>[ \t\u00a0,;/|]{0,6}+)"
    r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?+(?![A-Za-z0-9])"
)

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_GROUPED = re.compile(r"[1-9][0-9]{0,2}(?:[ \u00a0\u2009\u202f][0-9]{3})+")
# "capital account 12500000" is a figure, not a bank account.
_LEDGER_ACCOUNT = re.compile(
    r"(?:capital|current|kapital|LP|partners?|partner['\u2019]s|investors?|investor['\u2019]s"
    r"|members?)[\s\-]{1,3}$",
    re.IGNORECASE,
)
_LEDGER_LOOKBACK = 16
_NUMBER_MIN_DIGITS = 6
_WEAK_MIN_DIGITS = 8

# A token that ends an identifier of unknown length: a word, or another term.
_STOP_TOKEN = re.compile(
    r"[A-Za-z]{5,}|(?:IBAN|BIC|SWIFT|BLZ|ABA|KTO)(?![A-Za-z0-9])", re.IGNORECASE
)

# Whether a keyword's name asks for a bank detail.
_BANK_TERM_NAME = re.compile(
    r"(?<![a-z])(?:"
    r"ibans?|bics?|swift|blz|bankleitzahl(?:en)?|sort[ \-_]?codes?|routing|aba|acct|a/c"
    r"|kontonummern?|kontonr|konto|konten|kto|kontoverbindung(?:en)?|kontodaten|depotnummern?"
    r"|bankverbindung(?:en)?|bankkonto|bankkonten|bankdaten"
    r"|zahlungsverbindung(?:en)?|zahlungsanweisung(?:en)?|zahlungsinstruktion(?:en)?"
    r"|bank(?:ing)?[ \-_]+(?:accounts?|details|data|information|instructions|coordinates)"
    r"|account[ ._\-]*(?:numbers?|nos?|nrs?|num|#|details)"
    r"|wire|remittance|(?:payment|settlement)[ \-_]+(?:instructions|details)"
    r")(?![a-z])",
    re.IGNORECASE,
)


def asks_for_bank_details(name: str) -> bool:
    """Whether a keyword's name asks for a bank detail.

    Such a keyword is never sent to the model. The bare word "account" does
    not count ("capital account" is a legitimate figure); "account number"
    does. A name that itself holds a bank detail counts as well.

    Args:
        name: The keyword's display name.

    Returns:
        ``True`` if the name contains a bank term or a bank detail.
    """
    return _BANK_TERM_NAME.search(name) is not None or remove_bank_details(name) != name


def remove_bank_details(text: str) -> str:
    """Return ``text`` with every bank detail replaced by :data:`REPLACEMENT`.

    Args:
        text: One string of the model's output.

    Returns:
        The text without account identifiers; unchanged if it holds none.
    """
    spans = _valid_iban_spans(text)
    spans.extend(_spans_after_terms(text))
    if not spans:
        return text
    parts: list[str] = []
    position = 0
    for start, end in sorted(spans):
        if start < position:
            # Overlaps the span before it: extend that one.
            position = max(position, end)
            continue
        parts.append(text[position:start])
        parts.append(REPLACEMENT)
        position = end
    parts.append(text[position:])
    return _BIC_AFTER_REMOVED.sub(rf"\g<gap>{REPLACEMENT}", "".join(parts))


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


def _valid_iban_spans(text: str) -> list[tuple[int, int]]:
    """Find every IBAN of a registered country and length with valid check digits."""
    spans: list[tuple[int, int]] = []
    position = 0
    while (head := _IBAN_HEAD.search(text, position)) is not None:
        position = head.start() + 1
        length = _IBAN_LENGTHS.get(head.group()[:2].upper())
        if length is None:
            continue
        compact, end = _take_identifier(text, head.start(), length, stop_at_words=False)
        if len(compact) == length and _has_valid_check_digits(compact):
            spans.append((head.start(), end))
            position = end
    return spans


def _take_identifier(text: str, start: int, limit: int, *, stop_at_words: bool) -> tuple[str, int]:
    """Read up to ``limit`` letters and digits from ``start``, across separators.

    Args:
        text: The text.
        start: Where the identifier starts.
        limit: The most characters to take.
        stop_at_words: Whether a separated word or bank term ends the
            identifier — for one whose length is not known.

    Returns:
        The characters taken, without separators, and the offset behind the
        last of them.
    """
    taken: list[str] = []
    end = index = start
    separators = 0
    while index < len(text) and len(taken) < limit:
        char = text[index]
        if char in _SEPARATORS:
            separators += 1
            if separators > _MAX_SEPARATORS:
                break
        elif char.isascii() and char.isalnum():
            if stop_at_words and separators and _STOP_TOKEN.match(text, index):
                break
            taken.append(char)
            end = index + 1
            separators = 0
        else:
            break
        index += 1
    return "".join(taken), end


def _has_valid_check_digits(compact: str) -> bool:
    """ISO 13616: move the first four characters to the end, read letters as 10 to 35."""
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _spans_after_terms(text: str) -> list[tuple[int, int]]:
    """Find the identifiers that follow a bank term."""
    spans: list[tuple[int, int]] = []
    for term in _TERM.finditer(text):
        adjacent = _after_punctuation(text, term.end())
        if term.group("iban") is not None:
            spans.extend(_iban_after_term(text, term.end(), adjacent))
        elif term.group("bic") is not None:
            if (bic := _BIC.match(text, adjacent)) is not None:
                spans.append(bic.span())
        elif term.group("number") is not None:
            if _is_ledger_account(text, term) and term.group().lower().startswith("account"):
                continue
            spans.extend(_digit_runs(text, term.end(), term.end() + _WINDOW))
        else:
            spans.extend(_weak_number(text, term, adjacent))
    return spans


def _iban_after_term(text: str, term_end: int, adjacent: int) -> list[tuple[int, int]]:
    """An IBAN-shaped identifier near the term, or digits right after it."""
    head = _IBAN_HEAD.search(text, term_end, term_end + _WINDOW)
    if head is not None:
        length = _IBAN_LENGTHS.get(head.group()[:2].upper(), _IBAN_MAX_LENGTH)
        compact, end = _take_identifier(text, head.start(), length, stop_at_words=True)
        if len(compact) >= _IBAN_MIN_TAKEN:
            return [(head.start(), end)]
    run = _DIGIT_RUN.match(text, adjacent)
    if run is not None and _digit_count(run.group()) >= _NUMBER_MIN_DIGITS:
        return [run.span()]
    return []


def _digit_runs(text: str, start: int, stop: int) -> list[tuple[int, int]]:
    """Every run of at least six digits that starts in ``text[start:stop]``, dates apart."""
    spans: list[tuple[int, int]] = []
    position = start
    while (run := _DIGIT_RUN.search(text, position, min(stop, len(text)))) is not None:
        # The run may reach beyond the window: match it again without the bound.
        whole = _DIGIT_RUN.match(text, run.start()) or run
        position = whole.end()
        digits = whole.group()
        if _ISO_DATE.fullmatch(digits) is None and _digit_count(digits) >= _NUMBER_MIN_DIGITS:
            spans.append(whole.span())
    return spans


def _weak_number(text: str, term: re.Match[str], adjacent: int) -> list[tuple[int, int]]:
    """A run of digits right after the bare word "account", "Konto" or "Depot"."""
    run = _DIGIT_RUN.match(text, adjacent)
    if run is None:
        return []
    digits = run.group()
    if _digit_count(digits) < _WEAK_MIN_DIGITS or _ISO_DATE.fullmatch(digits) is not None:
        return []
    if not digits.startswith("0"):
        if _AMOUNT_GROUPED.fullmatch(digits) is not None or _is_ledger_account(text, term):
            return []
        rest = text[run.end() : run.end() + 2]
        if len(rest) == 2 and rest[0] in ".," and rest[1].isdigit():
            return []  # a decimal amount
    return [run.span()]


def _after_punctuation(text: str, position: int) -> int:
    gap = _PUNCTUATION_GAP.match(text, position)
    return gap.end() if gap is not None else position


def _is_ledger_account(text: str, term: re.Match[str]) -> bool:
    lookback = text[max(0, term.start() - _LEDGER_LOOKBACK) : term.start()]
    return _LEDGER_ACCOUNT.search(lookback) is not None


def _digit_count(run: str) -> int:
    return sum(char.isdigit() for char in run)
