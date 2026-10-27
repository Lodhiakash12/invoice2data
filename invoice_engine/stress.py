"""Stress test: does a template still read its own documents when they are printed a little
differently?

A template is usually written from one or a few PDFs, and text patterns written from one sample
tend to match that sample's exact spacing and number format. Real documents of the same layout
vary: an older version writes "Name :" instead of "Name:", one month prints 212.1 instead of
212.10, the page is indented differently, sections are further apart.

For every document a template reads in the sample PDFs, this module rewrites the document's text
in one such way at a time, runs the template on the rewritten text and compares the result with
the result on the original text. Every rewrite keeps all values the same, so the right answer is
known: the template should find the same values, with the same status.

Outcomes per rewrite:
  same          same status, same values
  not matched   the template's keywords no longer match: the document would be unrecognized
  lost          the result is worse (OK -> CHECK/FAIL) or values are missing
  WRONG         a value is present but different from the original: the most serious outcome,
                because nothing else may notice it

The test is meant to run after a template is written (by a person or by an automatic step) and
before it is used, so that weaknesses are fixed while the sample that shows them is at hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .document import attempt, process_pdf
from .segment import canonical, page_texts

# --- rewrites (each keeps every value the same) -------------------------------------------------

_LABEL_COLON = re.compile(r"(?<=[A-Za-z)\]])[ \t]*:(?=[ \t])")
_INNER_GAP = re.compile(r"(?<=\S)( {2,})(?=\S)")
_TRAILING_ZERO = re.compile(r"(?<![\d.,])(\d[\d,]*\.\d)0(?![\d.,])")
_ZERO_DECIMALS = re.compile(r"(?<![\d.,])(\d[\d,]*)\.00(?![\d.,])")


def _per_line(text: str, change) -> str:
    return "\n".join(change(line) for line in text.split("\n"))


REWRITES: dict[str, tuple[str, callable]] = {
    "space before colon": ("'Name:' printed as 'Name :'",
                           lambda t: _LABEL_COLON.sub(" :", t)),
    "no space before colon": ("'Name :' printed as 'Name:'",
                              lambda t: _LABEL_COLON.sub(":", t)),
    "wider column gaps": ("columns 2 characters further apart",
                          lambda t: _INNER_GAP.sub(lambda m: m.group(1) + "  ", t)),
    "short decimals": ("212.10 printed as 212.1, 202.00 as 202",
                       lambda t: _TRAILING_ZERO.sub(r"\1", _ZERO_DECIMALS.sub(r"\1", t))),
    "page shifted right": ("every line indented 6 more characters",
                           lambda t: _per_line(t, lambda l: ("      " + l) if l.strip() else l)),
    "more space between sections": ("every blank line printed twice",
                                    lambda t: re.sub(r"\n[ \t]*\n", "\n\n\n", t)),
}


# --- comparison --------------------------------------------------------------------------------

def _norm(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _empty(value) -> bool:
    return value in (None, "", [])


def _same(value, other, change) -> bool:
    """Equal values; a text value may also equal the original text after the same rewrite (a
    description that itself contains "NO:" or an amount is rewritten with the page)."""
    if _norm(other) == _norm(value):
        return True
    if isinstance(value, str) and isinstance(other, str):
        return _norm(change(value)) == _norm(other)
    if isinstance(value, list) and isinstance(other, list) and len(value) == len(other):
        return all(_same(a, b, change) for a, b in zip(value, other))
    return False


def compare(original: dict, rewritten: dict, change=lambda t: t) -> tuple[list[str], list[str]]:
    """(lost, wrong): names of values missing in the rewritten result, and of values that differ."""
    lost, wrong = [], []
    for name, value in original["fields"].items():
        if _empty(value):
            continue
        other = rewritten["fields"].get(name)
        if _empty(other):
            lost.append(name)
        elif not _same(value, other, change):
            wrong.append(name)
    a, b = original.get("lines") or [], rewritten.get("lines") or []
    if len(b) < len(a):
        lost.append(f"lines ({len(a)} -> {len(b)})")
    elif len(b) > len(a):
        wrong.append(f"lines ({len(a)} -> {len(b)})")
    else:
        for i, (x, y) in enumerate(zip(a, b), start=1):
            for key, value in x.items():
                if _empty(value):
                    continue
                if _empty(y.get(key)):
                    lost.append(f"line {i} {key}")
                elif not _same(value, y.get(key), change):
                    wrong.append(f"line {i} {key}")
    return lost, wrong


# --- running -------------------------------------------------------------------------------------

@dataclass
class Outcome:
    rewrite: str
    sample: str
    result: str                    # same / not matched / lost / WRONG
    detail: str = ""


@dataclass
class TemplateReport:
    template: str
    samples: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        result = {"same": 0, "not matched": 0, "lost": 0, "WRONG": 0}
        for o in self.outcomes:
            result[o.result] += 1
        return result


_RANK = {"FAIL": 0, "CHECK": 1, "OK": 2}


def run_one(template, registry, type_name, pdf: Path, pages: list[int], texts: list[str],
            original: dict, rewrite: str) -> Outcome:
    _, change = REWRITES[rewrite]
    changed = [canonical(change(t)) for t in texts]      # as the engine would see that PDF
    sample = f"{pdf.name} p{pages[0]}" + (f"-{pages[-1]}" if len(pages) > 1 else "")
    try:
        matched = template.matches_input(template.prepare_input("\n".join(changed)))
    except Exception:
        matched = False
    if not matched:
        return Outcome(rewrite, sample, "not matched", "keywords no longer match")
    result = attempt(template, changed, pdf, registry, pages, type_name)
    lost, wrong = compare(original, result, change)
    if wrong:
        return Outcome(rewrite, sample, "WRONG", "different value: " + ", ".join(wrong[:6]))
    worse = _RANK.get(result["status"], 0) < _RANK.get(original["status"], 0)
    if lost or worse:
        detail = []
        if worse:
            detail.append(f"{original['status']} -> {result['status']}")
        if lost:
            detail.append("missing: " + ", ".join(lost[:6]) + (" ..." if len(lost) > 6 else ""))
        return Outcome(rewrite, sample, "lost", "; ".join(detail))
    return Outcome(rewrite, sample, "same")


def stress(catalog, pdfs: list[Path], only: set[str] | None = None,
           rewrites: list[str] | None = None) -> list[TemplateReport]:
    """Stress every template (or those in `only`) on the documents it wins in `pdfs`."""
    rewrites = rewrites or list(REWRITES)
    by_name = {t["template_name"]: t for t in catalog.templates}
    reports: dict[str, TemplateReport] = {}
    for pdf in pdfs:
        pdf = Path(pdf)
        result = process_pdf(pdf, catalog)
        if not result["documents"]:
            continue
        all_texts = page_texts(pdf)
        for doc in result["documents"]:
            name = doc["template"]
            if doc["status"] == "FAIL" or doc.get("copy_of") or (only and name not in only):
                continue
            template = by_name[name]
            dt = catalog.type_of(template)
            texts = [all_texts[p - 1] for p in doc["pages"]]
            original = attempt(template, texts, pdf, dt.registry, doc["pages"], dt.name)
            report = reports.setdefault(name, TemplateReport(name))
            report.samples.append(f"{pdf.name} p{doc['pages'][0]}")
            for rewrite in rewrites:
                report.outcomes.append(run_one(template, dt.registry, dt.name, pdf, doc["pages"],
                                               texts, original, rewrite))
    names = sorted(only) if only else sorted(by_name)
    return [reports.get(n, TemplateReport(n)) for n in names]


def print_report(reports: list[TemplateReport]) -> None:
    print("Rewrites (each keeps every value the same):")
    for name, (description, _) in REWRITES.items():
        print(f"  {name:<28} {description}")
    print()
    total = {"same": 0, "not matched": 0, "lost": 0, "WRONG": 0}
    weak = []
    for report in reports:
        if not report.samples:
            print(f"{report.template}: no sample in these PDFs")
            continue
        counts = report.counts()
        for key in total:
            total[key] += counts[key]
        runs = len(report.outcomes)
        flag = "WRONG VALUES" if counts["WRONG"] else ("weak" if counts["same"] < runs else "robust")
        print(f"{report.template}: {counts['same']}/{runs} same   ({len(report.samples)} sample(s))   {flag}")
        if counts["same"] < runs:
            weak.append(report.template)
            seen = set()
            for o in report.outcomes:
                if o.result == "same" or (o.rewrite, o.result, o.detail) in seen:
                    continue
                seen.add((o.rewrite, o.result, o.detail))
                print(f"    {o.result:<12}{o.rewrite:<29}{o.sample}: {o.detail}")
    runs = sum(total.values())
    print(f"\nRuns: {runs}   same: {total['same']}   not matched: {total['not matched']}   "
          f"lost: {total['lost']}   WRONG: {total['WRONG']}")
    print(f"Templates with a weakness: {len(weak)} of {sum(1 for r in reports if r.samples)}")
