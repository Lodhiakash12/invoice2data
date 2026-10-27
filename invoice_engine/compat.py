"""Corrections to invoice2data behaviour that would otherwise produce silently wrong values.

1. Empty numbers. invoice2data converts an empty value of a field or line column typed int or
   float to 0 (InvoiceTemplate.coerce_type). An optional column the document does not print,
   such as the quantity of a "Restaurant Packaging Charge" row, then comes out as a quantity of
   0: a value the document never stated. Here an empty value stays empty (None) and the engine
   leaves the column out of the row, so a missing number is reported as missing, never as zero.
"""

from __future__ import annotations

from invoice2data.extract.invoice_template import InvoiceTemplate

_original = InvoiceTemplate.coerce_type


def _coerce_type(self, value, target_type):
    if target_type in ("int", "float") and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    return _original(self, value, target_type)


def install() -> None:
    if InvoiceTemplate.coerce_type is not _coerce_type:
        InvoiceTemplate.coerce_type = _coerce_type
