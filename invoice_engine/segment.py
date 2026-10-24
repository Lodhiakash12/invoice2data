"""Page text extraction and grouping of pages into documents."""

from __future__ import annotations

import re
from pathlib import Path

from . import poppler
from .templates import family_of


def page_texts(pdf: Path) -> list[str]:
    """Text of every page, in order. pdftotext ends each page with a form feed.
    Raises poppler.PopplerError with a readable reason (protected, damaged, not found)."""
    pages = poppler.run(["-layout", "-enc", "UTF-8", "-eol", "unix"], pdf).split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return pages


def matching_templates(text: str, templates: list) -> list:
    """All templates whose keywords match this text, in matching order."""
    return [t for t in templates if t.matches_input(t.prepare_input(text))]


def document_number(templates: list, text: str) -> str | None:
    """Document number on one page, read with the first candidate's invoice_number rule that matches."""
    for template in templates:
        spec = template["fields"].get("invoice_number")
        pattern = spec.get("regex") if isinstance(spec, dict) else spec
        if not pattern:
            continue
        prepared = template.prepare_input(text)
        for rx in pattern if isinstance(pattern, list) else [pattern]:
            match = re.search(rx, prepared)
            if match:
                return (match.group(1) if match.groups() else match.group(0)).strip()
    return None


def layout_families(candidates: list) -> list[str]:
    """Template families among the candidates, in matching order, each once."""
    return list(dict.fromkeys(family_of(t) for t in candidates))


def split_into_documents(pages: list[str], templates: list) -> tuple[list[dict], list[str]]:
    """Group consecutive pages into documents.

    A page continues the open document when they still share at least one candidate layout and
    the page's document number (read by any of its candidates) does not differ from the
    document's. Only the layouts that matched every page decide whether the next page continues,
    so a look-alike that fits page 1 alone cannot split a multi-page invoice. The document is
    parsed with every candidate seen on any of its pages; the evidence then decides (document.py).
    A page matching nothing joins the previous document only when a remaining layout's
    continuation_keywords are all on the page; otherwise it is reported as unrecognized.
    """
    groups: list[dict] = []
    notes: list[str] = []
    for page_no, text in enumerate(pages, start=1):
        candidates = matching_templates(text, templates) if text.strip() else []
        families = layout_families(candidates)
        number = document_number(candidates, text) if candidates else None
        if len(families) > 1:
            names = ", ".join(t["template_name"] for t in candidates)
            notes.append(f"page {page_no} matches templates of several layouts ({names}); "
                         "each is parsed and the one with the most evidence is used")

        previous = groups[-1] if groups else None
        shared: list[str] = []
        if previous is not None and previous["families"]:
            if families:
                if number is None or previous["number"] is None or number == previous["number"]:
                    shared = [f for f in previous["families"] if f in families]
            else:
                for family in previous["families"]:
                    first = next(t for t in previous["candidates"] if family_of(t) == family)
                    follow = first.get("continuation_keywords") or []
                    if follow and all(k in text for k in follow):
                        shared.append(family)
                if shared:
                    notes.append(f"page {page_no} attached to the previous document as a continuation page")
        if shared:
            previous["families"] = shared
            previous["family"] = shared[0]
            previous["candidates"] += [t for t in candidates if t not in previous["candidates"]]
            previous["pages"].append(page_no)
            previous["texts"].append(text)
            previous["number"] = previous["number"] or number
        elif previous is not None and previous["family"] is None and not families:
            previous["pages"].append(page_no)          # consecutive unrecognized pages stay together
            previous["texts"].append(text)
        else:
            groups.append({"family": families[0] if families else None, "families": families,
                           "candidates": candidates, "number": number, "pages": [page_no], "texts": [text]})
    return groups, notes