"""Page text extraction and grouping of pages into documents."""

from __future__ import annotations

import re
from pathlib import Path

from . import poppler, safe_regex
from .templates import family_of


# --- one standard form of the page text ---------------------------------------------------------
# The same layout is printed with small differences: "Name :" in one version and "Name:" in
# another, the whole page indented differently, sections further apart. Every page is put into
# one standard form before any template sees it, so these differences cannot break a template:
#   1. "Label :"  -> "Label:"; "Label     :" (padding that lines up a column of colons)
#      -> "Label:     ", so the rest of the line keeps its columns
#   2. trailing spaces removed; lines holding only spaces become empty
#   3. the indentation all lines share is removed (a page shifted right moves back)
#   4. two or more blank lines in a row become one
_SPACE_COLON = re.compile(r"(?<=[A-Za-z.)\]])([ \t]+):(?=[ \t]|$)", re.M)
_BLANK_RUN = re.compile(r"\n{3,}")


def canonical(text: str) -> str:
    """The standard form of one page's text (see above)."""
    # one space before the colon is dropped; wider padding (aligning columns) moves after it
    text = _SPACE_COLON.sub(lambda m: ":" if len(m.group(1)) == 1 else ":" + m.group(1), text)
    lines = [line.rstrip() for line in text.split("\n")]
    filled = [line for line in lines if line]
    if filled:
        common = min(len(line) - len(line.lstrip(" ")) for line in filled)
        if common:
            lines = [line[common:] if line else line for line in lines]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines))


def page_texts(pdf: Path, warnings: list[str] | None = None) -> list[str]:
    """Text of every page, in order. pdftotext ends each page with a form feed.
    Raises poppler.PopplerError with a readable reason (protected, damaged, not found).
    Poppler's warnings on a file it could still read are appended to `warnings`."""
    pages = poppler.run(["-layout", "-enc", "UTF-8", "-eol", "unix"], pdf, warnings).split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return [canonical(page) for page in pages]


def matching_templates(text: str, templates: list, errors: list[str] | None = None) -> list:
    """All templates whose keywords match this text, in matching order. A template whose text
    preparation fails (a slow or broken replace pattern) is skipped and reported in `errors`."""
    found = []
    for template in templates:
        try:
            if template.matches_input(template.prepare_input(text)):
                found.append(template)
        except Exception as exc:  # one broken template must not stop the others
            if errors is not None:
                errors.append(f"template {template['template_name']} skipped: {type(exc).__name__}: {exc}")
    return found


def document_number(templates: list, text: str) -> str | None:
    """Document number on one page, read with the first candidate's invoice_number rule that matches."""
    for template in templates:
        spec = template["fields"].get("invoice_number")
        pattern = spec.get("regex") if isinstance(spec, dict) else spec
        if not pattern:
            continue
        try:
            prepared = template.prepare_input(text)
            for rx in pattern if isinstance(pattern, list) else [pattern]:
                match = safe_regex.search(rx, prepared)
                if match:
                    return (match.group(1) if match.groups() else match.group(0)).strip()
        except Exception:
            continue
    return None


def layout_families(candidates: list) -> list[str]:
    """Template families among the candidates, in matching order, each once."""
    return list(dict.fromkeys(family_of(t) for t in candidates))


# "Page 2 of 5", "Page 2/5", "Pagina 2 van 5", "Seite 2 von 5"
_PAGE_MARK = re.compile(r"(?i)\b(?:page|pagina|seite|pg\.?)\s*(\d{1,3})\s*(?:of|/|van|von)\s*(\d{1,3})\b")
# words that differ between the copies of one document (original / duplicate / triplicate ...)
_COPY_WORDS = re.compile(r"(?i)\b(?:original|duplicate|triplicate|quadruplicate|copy|copie|kopie|"
                         r"for\s+(?:recipient|buyer|supplier|seller|transporter|transport|customer|office))\b")


def page_mark(text: str) -> tuple[int, int] | None:
    """(this page, of pages) when the page prints its own page number, else None."""
    match = _PAGE_MARK.search(text)
    if not match:
        return None
    this, total = int(match.group(1)), int(match.group(2))
    return (this, total) if 1 <= this <= total else None


def copy_key(text: str) -> str:
    """The page text without copy labels and spacing: equal keys = copies of one page."""
    return re.sub(r"\s+", " ", _COPY_WORDS.sub(" ", text)).strip().lower()


def _continues(group: dict, text: str) -> list[str]:
    """Layouts of the open document whose continuation_keywords are all on this page."""
    found = []
    for family in group["families"]:
        first = next(t for t in group["candidates"] if family_of(t) == family)
        follow = first.get("continuation_keywords") or []
        if follow and all(k in text for k in follow):
            found.append(family)
    return found


def split_into_documents(pages: list[str], templates) -> tuple[list[dict], list[str]]:
    """Group consecutive pages into documents. templates = a Catalog (document types, routing and
    keyword index) or a plain list of templates.

    A page continues the open document when they still share at least one candidate layout and
    the page's document number agrees with the document's. Agreeing means: the same number, or
    no number on either page when the page also prints no "page 1 of n" mark. A page whose number
    cannot be read while the open document has one starts a new document, unless its own page
    mark says it is a later page of that document. A page that repeats the open document's first
    page word for word (apart from "original" / "duplicate" labels) starts a new document marked
    as a copy. Only the layouts that matched every page decide whether the next page continues,
    so a look-alike that fits page 1 alone cannot split a multi-page invoice. The document is
    parsed with every candidate seen on any of its pages; the evidence then decides (document.py).
    A page matching nothing joins the previous document only when a remaining layout's
    continuation_keywords are all on the page; otherwise it is reported as unrecognized.
    """
    groups: list[dict] = []
    notes: list[str] = []
    for page_no, text in enumerate(pages, start=1):
        errors: list[str] = []
        if not text.strip():
            candidates = []
        elif hasattr(templates, "candidates"):           # a Catalog: routing + keyword index
            candidates = templates.candidates(text, errors)
        else:
            candidates = matching_templates(text, templates, errors)
        notes.extend(f"page {page_no}: {e}" for e in errors)
        families = layout_families(candidates)
        number = document_number(candidates, text) if candidates else None
        mark = page_mark(text)
        if len(families) > 1:
            names = ", ".join(t["template_name"] for t in candidates)
            notes.append(f"page {page_no} matches templates of several layouts ({names}); "
                         "each is parsed and the one with the most evidence is used")

        previous = groups[-1] if groups else None
        shared: list[str] = []
        copy_of = None
        if previous is not None and previous["families"]:
            if families:
                if mark is not None and mark[0] == 1:
                    agree = False                      # the page says it starts a document
                elif number is not None and previous["number"] is not None:
                    agree = number == previous["number"]
                elif number is None and previous["number"] is not None:
                    # unreadable number: only a page mark or the layout's continuation keywords link it
                    agree = (mark is not None and mark[0] > 1) or bool(_continues(previous, text))
                else:
                    agree = True                       # the layout prints no number on this page
                if copy_key(text) == previous["first_key"]:
                    agree = False
                    copy_of = list(previous["pages"])
                    notes.append(f"page {page_no} repeats page {previous['pages'][0]} "
                                 "(another copy of the same document): reported separately")
                elif not agree and number is None and previous["number"] is not None and mark is None:
                    notes.append(f"page {page_no}: document number not found; not joined to the "
                                 f"document on page {previous['pages'][0]}")
                if agree:
                    follow = _continues(previous, text) if number is None else []
                    shared = [f for f in previous["families"] if f in families or f in follow]
            else:
                shared = _continues(previous, text)
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
                           "candidates": candidates, "number": number, "pages": [page_no], "texts": [text],
                           "first_key": copy_key(text), "copy_of": copy_of})
    return groups, notes
