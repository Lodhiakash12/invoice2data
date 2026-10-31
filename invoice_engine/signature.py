"""What a document looks like, for grouping documents no template reads yet.

Two things are taken from the page text:

  ids     the issuer's registration numbers printed on the page: PAN (also the 10 characters
          inside every GSTIN, so two GSTINs of one company in two states give the same PAN) and
          CIN. The same number on two documents points to the same issuer. Numbers printed on the
          documents of many issuers (the buyer's own GSTIN, when every invoice is addressed to the
          same company) say nothing about the issuer and are left out by the caller (common_ids).
  labels  the short fixed texts of the layout: pieces of a line (split at 2+ spaces) without a
          digit and of at most five words ("Invoice Number:", "Place of Supply", "Taxable
          Value"). Item descriptions, addresses with numbers and amounts are left out, so two
          documents of one layout share most of their labels even when their contents differ.

similarity() is the share of labels two documents have in common (Jaccard, 0 to 1). Measured on
the stored samples (60 documents, 27 layouts): documents of one layout 0.50 and more; documents of
different layouts mostly below 0.48, except sibling layouts of one issuer (an invoice and its
credit note can reach 0.9). So similarity is used to put documents together, never to decide
that they are one layout: that is decided by whether one template reads them all (jobs.py).
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][A-Za-z&/.-]*[A-Za-z]")
_GSTIN = re.compile(r"\b\d{2}([A-Z]{5}\d{4}[A-Z])[0-9A-Z]Z[0-9A-Z]\b")
_PAN = re.compile(r"\b[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]\b")
_CIN = re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")

SAME_LAYOUT = 0.5          # labels in common: put together as one layout
SAME_ISSUER = 0.25         # with the same issuer number: a variant of that issuer's layout


def ids_of(text: str) -> list[str]:
    found = {f"PAN:{m.group(1)}" for m in _GSTIN.finditer(text)}
    found |= {f"PAN:{m.group(0)}" for m in _PAN.finditer(text)}
    found |= {f"CIN:{m.group(0)}" for m in _CIN.finditer(text)}
    return sorted(found)


def labels_of(text: str) -> list[str]:
    labels = set()
    for line in text.splitlines():
        for piece in re.split(r"\s{2,}", line.strip()):
            if not piece or re.search(r"\d", piece):
                continue
            words = [w.lower() for w in _WORD.findall(piece)]
            if 0 < len(words) <= 5:
                labels.update(words)
    return sorted(labels)


def signature(text: str) -> dict:
    return {"ids": ids_of(text), "labels": labels_of(text)}


def similarity(a: dict, b: dict) -> float:
    left, right = set(a.get("labels") or []), set(b.get("labels") or [])
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def shared_ids(a: dict, b: dict, common: set[str] = frozenset()) -> set[str]:
    return (set(a.get("ids") or []) & set(b.get("ids") or [])) - set(common)


def common_ids(signatures_by_group: dict[str, list[dict]], limit: int = 3) -> set[str]:
    """Numbers printed on the documents of `limit` or more different groups (issuers): the
    buyer's own registration, not an issuer's."""
    seen: dict[str, set[str]] = {}
    for group, signatures in signatures_by_group.items():
        for sig in signatures:
            for value in sig.get("ids") or []:
                seen.setdefault(value, set()).add(group)
    return {value for value, groups in seen.items() if len(groups) >= limit}


def related(a: dict, b: dict, common: set[str] = frozenset()) -> bool:
    """True when two documents look like one layout: many labels in common, or the same issuer
    number and a fair share of labels."""
    score = similarity(a, b)
    return score >= SAME_LAYOUT or (score >= SAME_ISSUER and bool(shared_ids(a, b, common)))
