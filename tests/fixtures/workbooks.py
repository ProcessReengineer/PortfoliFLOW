# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""In-memory Excel-import workbooks for tests that must not depend on a data file.

:func:`minimal_import_workbook` builds the smallest workbook the import
route accepts end to end: two investments, one NAV and one paid-in row
each, and none of the optional sheets (no benchmarks, no FX rates, no
limit sets). Tests that need a full, realistic book use the committed
``sample_data/PortfoliFLOW_example_portfolio.xlsx`` instead.
"""

from __future__ import annotations

import datetime
import io

import openpyxl


def minimal_import_workbook() -> bytes:
    """Return the bytes of a minimal Excel-import workbook with two investments."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sheet_names = [
        "Attributes",
        "Cash Flow In actual",
        "Cash Flow In plan",
        "Cash Flow Out actual",
        "Cash Flow Out plan",
        "NAVs actual",
        "NAVs plan",
        "total return actual",
        "total return plan",
        "interest rates",
    ]
    for n in sheet_names:
        wb.create_sheet(n)

    ws = wb["Attributes"]
    ws.append([None, "Investition A", "Investition B"])
    ws.append([None, "Aktien", "Private Equity"])
    ws.append([None, "Large Cap", "Buyout"])
    ws.append(["Region", "Europa", "USA"])
    ws.append(["Asset Class", "listed_equity", "private_equity"])
    ws.append(["Manager / Fondsname", "GP A", "GP B"])
    ws.append(["Vintage Year", 2020, 2021])
    ws.append(["Währung", "EUR", "EUR"])

    d = datetime.datetime(2024, 1, 1)
    ws = wb["NAVs actual"]
    ws.append([None, "Investition A", "Investition B"])
    ws.append([None, "Aktien", "Private Equity"])
    ws.append([None, "Large Cap", "Buyout"])
    ws.append([d, 100, 200])

    ws = wb["Cash Flow Out actual"]
    ws.append([None, "Investition A", "Investition B"])
    ws.append([None, "Aktien", "Private Equity"])
    ws.append([None, "Large Cap", "Buyout"])
    ws.append([d, -50, -75])

    for n in (
        "Cash Flow In actual",
        "Cash Flow In plan",
        "Cash Flow Out plan",
        "NAVs plan",
        "total return actual",
        "total return plan",
    ):
        ws = wb[n]
        ws.append([None, "Investition A", "Investition B"])
        ws.append([None, "Aktien", "Private Equity"])
        ws.append([None, "Large Cap", "Buyout"])
        ws.append([d, None, None])

    ws = wb["interest rates"]
    ws.append([None, "risk free rate"])
    ws.append([None, None])
    ws.append([None, None])
    ws.append([d, 0.04])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
