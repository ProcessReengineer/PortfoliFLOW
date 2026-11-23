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
  unless its first token is exactly a valid ISIN (a table row that starts
  with an ISIN and has figures behind it) and it does not stand right behind
  a bank term;
* right behind a bank term (punctuation between only, ``|`` included): the
  identifier that stands there — an IBAN-shaped one whatever its check digits,
  or a number of at least six digits, grouped by one or two blanks, dots,
  hyphens, a slash or a zero-width space, or wrapped after a group of four.
  A date or year range there is skipped and kept, and so is an amount — a
  number followed by a currency or unit (``4 750 000 EUR``, ``1.250.000
  units``);
* after ``IBAN``, ``Bankverbindung``, ``Kontoverbindung``, ``Bank details``
  and ``Payment details``: each further identifier that follows with
  punctuation only between (``IBANs: DE89…, LU28…``). After these labels a
  number in groups of three is an amount and stays, since a payment block
  also holds the amount; nothing further behind a label goes;
* after ``IBAN``: an IBAN-shaped identifier within :data:`_WINDOW` characters
  at its country's full length; and every run of at least six digits in the
  window, as after a number term;
* after a number term (``account number``, ``Acc. No.``, ``Kontonummer``,
  ``BLZ``, ``sort code``, ``routing``, ``ABA``, ``acct``, ``Kto`` …): every
  further run of at least six digits that starts within :data:`_WINDOW`
  characters, also on the next line or after words ("account number is …"),
  unless it is written as an amount in groups of three or carries a currency;
* after ``BIC`` or ``SWIFT``: a BIC in upper case right after the term;
* after the bare words ``account``, ``Konto`` or ``Depot``: a run of at least
  eight digits right after the word, unless it is written as an amount in groups
  of three or follows "capital account" and its like; a run with a leading
  zero is always removed; and a valid IBAN right after it, ISIN-like or not;
* a BIC that directly follows a removed identifier.

What is kept: amounts, dates, percentages, ISINs and any number that does not
follow a bank term. The term itself stays, so the text still says that a bank
detail stood there.

Known limits, accepted (closing report of strand RD-1): a bare number without
any term in the same string; a number more than :data:`_WINDOW` characters
behind its term; a number in groups of three right after a label
("Bankverbindung: 532 013 000") or separated from a number term by words
("account number is 123 456 789"); words between a label, a bare word or
``BIC`` and the identifier ("Bankverbindung lautet …", "BIC is …"); a number
grouped by dots or a slash that is separated from its term by words; an
account number followed by a currency code; after the bare word "account" a
number in groups of three or of seven digits; a BIC in lower case or printed
in groups; any eight- or eleven-letter upper-case word right after ``BIC`` or
a removed identifier counts as a BIC; an IBAN with invalid check digits and no
term beside it; a table row that starts with a country code and check digits
other than an ISIN (``CY24 …``) and whose figures happen to give valid check
digits (about one row in 97); separators other than those named above (a
double blank counts right behind a term only); full-width characters;
the short list of ledger words before "account". A second pass over filtered
text may remove more than the first.

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
    rf"|(?P<number>(?:account|acc|Konto|Kto|Depot){_JOINER}{_NUMBER_WORD}"
    r"|BLZ|Bankleitzahl|sort[ \-_]?codes?|routing|ABA|acct|a/c|Kto"
    r"|Kontonummern?|Kontonr|Bankkonto|Depotnummern?|Depotnr)"
    r"|(?P<label>Bankverbindung(?:en)?|Kontoverbindung(?:en)?|(?:bank|payment)[ \-_]?details)"
    r"|(?P<weak>account|Konto|Depot)"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)

# Right after a term: blanks, line breaks and punctuation only.
_PUNCTUATION_GAP = re.compile(r"[\s.:;=#()\[\],/|\-\u2013\u2014\u00a0]{0,12}+")
_DIGIT_RUN = re.compile(r"[0-9](?:[ \u00a0\u2007\u2009\u202f\-]?+[0-9])*+")
# Right behind a term a number may also be grouped by dots, split by a slash, spaced by
# two blanks or a zero-width space, or wrapped onto the next line after a group of four.
_DIGIT_RUN_AT_TERM = re.compile(
    r"[0-9](?:(?:[ \u00a0\u2007\u2009\u202f]{1,2}+|[\-./\u200b]"
    r"|(?<=[0-9]{4})(?<![0-9]{5})\n)?+[0-9])*+"
)
_BIC = re.compile(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?+(?![A-Za-z0-9])")
_BIC_AFTER_REMOVED = re.compile(
    r"(?<=\[bank detail removed\])(?P<gap>[ \t\u00a0,;/|]{0,6}+)"
    r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?+(?![A-Za-z0-9])"
)

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A date or a year range right behind a term: skipped, never removed.
_DATE_AT = re.compile(
    r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|(?:0?[1-9]|[12][0-9]|3[01])([./])(?:0?[1-9]|1[0-2])\1(?:(?:19|20)[0-9]{2}|[0-9]{2})"
    r"|(?:0?[1-9]|1[0-2])[./](?:19|20)[0-9]{2}"
    r"|(?:19|20)[0-9]{2}[/\-](?:(?:19|20)[0-9]{2}|[0-9]{2})"
    r")(?![0-9])"
)
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_ISIN_LENGTH = 12
_AMOUNT_GROUPED = re.compile(r"[1-9][0-9]{0,2}(?:[ \u00a0\u2007\u2009\u202f][0-9]{3})+")
_AMOUNT_DOTTED = re.compile(r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+")
_BLANKS = re.compile(r"[ \u00a0\u2007\u2009\u202f]")
# A currency or unit behind a number makes it an amount.
_UNIT_AFTER = re.compile(
    r"[ \u00a0]{1,2}+(?:EUR|USD|GBP|CHF|JPY|SEK|NOK|DKK|AUD|CAD"
    r"|units|shares|Anteile|St\u00fcck)(?![A-Za-z])"
)
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
    r"|depot[ ._\-]*(?:numbers?|nos?|nrs?|num|#)"
    r"|bankverbindung(?:en)?|bankkonto|bankkonten|bankdaten"
    r"|zahlungsverbindung(?:en)?|zahlungsanweisung(?:en)?|zahlungsinstruktion(?:en)?"
    r"|bank(?:ing)?[ \-_]+(?:accounts?|details|data|information|instructions|coordinates)"
    r"|acc(?:ount)?[ ._\-]*(?:numbers?|nos?|nrs?|num|#|details)"
    r"|(?:beneficiary|payee|escrow)[ \-_]+accounts?|bank[ \-_]+codes?"
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
    terms = list(_TERM.finditer(text))
    adjacent = [_after_punctuation(text, term.end()) for term in terms]
    spans = _valid_iban_spans(text, frozenset(adjacent))
    spans.extend(_spans_after_terms(text, terms, adjacent))
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


def _valid_iban_spans(text: str, touching: frozenset[int]) -> list[tuple[int, int]]:
    """Find every IBAN of a registered country and length with valid check digits.

    ``touching`` holds the offsets right behind a bank term: there an ISIN-like
    first token is no reason to keep the identifier.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    while (head := _IBAN_HEAD.search(text, position)) is not None:
        position = head.start() + 1
        length = _IBAN_LENGTHS.get(head.group()[:2].upper())
        if length is None:
            continue
        compact, end = _take_identifier(text, head.start(), length, stop_at_words=False)
        if (
            len(compact) == length
            and _has_valid_check_digits(compact)
            and (head.start() in touching or not _starts_with_isin(text, head.start()))
        ):
            spans.append((head.start(), end))
            position = end
    return spans


def _starts_with_isin(text: str, start: int) -> bool:
    """Whether the token at ``start`` is exactly a valid ISIN (not glued to ``IBAN``)."""
    if start >= 4 and text[start - 4 : start].upper() == "IBAN":
        return False
    token = text[start : start + _ISIN_LENGTH + 1]
    if len(token) > _ISIN_LENGTH and (token[-1].isascii() and token[-1].isalnum()):
        return False
    return _is_valid_isin(token[:_ISIN_LENGTH])


def _is_valid_isin(token: str) -> bool:
    """ISO 6166: letters read as 10 to 35, then the Luhn check over the digits."""
    if _ISIN.fullmatch(token) is None:
        return False
    digits = "".join(str(int(char, 36)) for char in token)
    total = 0
    for offset, char in enumerate(reversed(digits)):
        value = int(char) * (2 if offset % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


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


def _spans_after_terms(
    text: str, terms: list[re.Match[str]], adjacent: list[int]
) -> list[tuple[int, int]]:
    """Find the identifiers that follow a bank term."""
    spans: list[tuple[int, int]] = []
    for term, start in zip(terms, adjacent, strict=True):
        if term.group("iban") is not None:
            spans.extend(_in_a_row(text, start, keep_grouped=False))
            spans.extend(_iban_in_window(text, term.end(), start))
            spans.extend(_window_runs(text, term.end(), start))
        elif term.group("bic") is not None:
            if (bic := _BIC.match(text, start)) is not None:
                spans.append(bic.span())
        elif term.group("label") is not None:
            spans.extend(_in_a_row(text, start, keep_grouped=True))
        elif term.group("number") is not None:
            if _is_ledger_account(text, term) and term.group().lower().startswith("account"):
                continue
            item = _item_at(text, start, keep_grouped=False)
            if item is not None and item[1] is not None:
                spans.append(item[1])
            spans.extend(_window_runs(text, term.end(), start))
        else:
            spans.extend(_weak_number(text, term, start))
    return spans


def _in_a_row(text: str, position: int, *, keep_grouped: bool) -> list[tuple[int, int]]:
    """The identifiers right behind a term, one after another with punctuation between.

    Dates are skipped, amounts kept; the row ends at the first word.
    """
    spans: list[tuple[int, int]] = []
    while (item := _item_at(text, position, keep_grouped=keep_grouped)) is not None:
        end, span = item
        if span is not None:
            spans.append(span)
        position = _after_punctuation(text, end)
    return spans


def _item_at(
    text: str, position: int, *, keep_grouped: bool
) -> tuple[int, tuple[int, int] | None] | None:
    """What stands at ``position``: its end, and its span if it is a bank detail.

    ``None`` if neither a date, an IBAN-shaped identifier nor a number starts there.
    """
    if (date := _DATE_AT.match(text, position)) is not None:
        return date.end(), None
    if (head := _IBAN_HEAD.match(text, position)) is not None:
        wanted = _IBAN_LENGTHS.get(head.group()[:2].upper(), _IBAN_MAX_LENGTH)
        compact, end = _take_identifier(text, position, wanted, stop_at_words=True)
        if len(compact) >= _IBAN_MIN_TAKEN:
            return end, (position, end)
        return None
    run = _DIGIT_RUN_AT_TERM.match(text, position)
    if run is None:
        return None
    digits = run.group()
    if (
        _digit_count(digits) < _NUMBER_MIN_DIGITS
        or _is_amount_with_unit(text, run)
        or (keep_grouped and _AMOUNT_GROUPED.fullmatch(digits) is not None)
    ):
        return run.end(), None
    return run.end(), run.span()


def _iban_in_window(text: str, term_end: int, adjacent: int) -> list[tuple[int, int]]:
    """An IBAN-shaped identifier further behind ``IBAN``, at its country's full length."""
    position = term_end
    stop = min(term_end + _WINDOW, len(text))
    while (head := _IBAN_HEAD.search(text, position, stop)) is not None:
        position = head.start() + 1
        length = _IBAN_LENGTHS.get(head.group()[:2].upper())
        if length is None or head.start() == adjacent:
            continue  # "ref. FY24 …" is not an IBAN; the one right behind is taken already
        compact, end = _take_identifier(text, head.start(), length, stop_at_words=True)
        if len(compact) == length:
            return [(head.start(), end)]
    return []


def _window_runs(text: str, term_end: int, adjacent: int) -> list[tuple[int, int]]:
    """Every run of six digits that starts in the window behind a term, the first apart.

    A run is kept if it is written as an amount in groups of three, carries a
    currency or unit, or is a date.
    """
    spans: list[tuple[int, int]] = []
    position = term_end
    if (item := _item_at(text, adjacent, keep_grouped=False)) is not None:
        position = item[0]
    stop = min(term_end + _WINDOW, len(text))
    while (run := _DIGIT_RUN.search(text, position, stop)) is not None:
        # The run may reach beyond the window: match it again without the bound.
        whole = _DIGIT_RUN.match(text, run.start()) or run
        position = whole.end()
        digits = whole.group()
        if (
            _digit_count(digits) >= _NUMBER_MIN_DIGITS
            and _ISO_DATE.fullmatch(digits) is None
            and _AMOUNT_GROUPED.fullmatch(digits) is None
            and not _is_amount_with_unit(text, whole)
        ):
            spans.append(whole.span())
    return spans


def _is_amount_with_unit(text: str, run: re.Match[str]) -> bool:
    """A number without a leading zero, followed by a currency or a unit."""
    digits = run.group()
    if digits.startswith("0") or _UNIT_AFTER.match(text, run.end()) is None:
        return False
    return (
        digits.isdigit()
        or _AMOUNT_DOTTED.fullmatch(digits) is not None
        or _ends_in_grouped_amount(digits)
    )


def _ends_in_grouped_amount(digits: str) -> bool:
    """Whether a run ends in an amount in groups of three ("2025 4 750 000" does)."""
    if "-" in digits:
        return False
    groups = _BLANKS.split(digits)
    last = len(groups) - 1
    first = last
    while first > 0 and len(groups[first]) == 3:
        first -= 1
    if first == last:
        return False  # no group of three at the end
    if not 1 <= len(groups[first]) <= 3 or groups[first].startswith("0"):
        first += 1  # "2025 750 000": the amount starts at the next group
    return first < last and not groups[first].startswith("0")


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
