"""Corrections to invoice2data behaviour that would otherwise produce silently wrong values.

1. Empty numbers. invoice2data converts an empty value of a field or line column typed int or
   float to 0 (InvoiceTemplate.coerce_type). An optional column the document does not print,
   such as the quantity of a "Restaurant Packaging Charge" row, then comes out as a quantity of
   0: a value the document never stated. Here an empty value stays empty (None) and the engine
   leaves the column out of the row, so a missing number is reported as missing, never as zero.

2. Joined rows. In a lines table, a row's `line` pattern reads continuation lines of the row that
   first_line started; when it also matches the following ROWS, their values are joined into one
   ("1340\n17.7\n20"). invoice2data then fails with "Decimal separator cannot appear more than
   once", which does not say what is wrong. Here the error names the cause and the fix.
"""

from __future__ import annotations

from invoice2data.extract.invoice_template import InvoiceTemplate

_original = InvoiceTemplate.coerce_type


class JoinedRowsError(ValueError):
    """Several table rows were read as one row."""


def _coerce_type(self, value, target_type):
    if target_type in ("int", "float") and isinstance(value, str) and "\n" in value.strip():
        shown = value.strip().replace("\n", " | ")[:80]
        raise JoinedRowsError(
            f"several rows of the item table were read as one row (a number column got {shown!r}): "
            "the `line` pattern also matches the following rows, so they become continuation lines of "
            "the row first_line started. When every row has the same form, use only `line` (no "
            "first_line); otherwise make `line` match the continuation text only, never a whole row")
    if target_type in ("int", "float") and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    return _original(self, value, target_type)


def install() -> None:
    if InvoiceTemplate.coerce_type is not _coerce_type:
        InvoiceTemplate.coerce_type = _coerce_type
    # 3. the `table` field parser (tables read by their column headings, see table.py)
    from invoice2data.extract import invoice_template
    from . import table
    invoice_template.PARSERS_MAPPING.setdefault("table", table)
    # 4. the `region` field parser (a block of the page below its heading, see words.py)
    from . import words
    invoice_template.PARSERS_MAPPING.setdefault("region", words)
