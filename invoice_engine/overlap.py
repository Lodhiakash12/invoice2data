"""Overlap test: finds templates of different layouts, in any document type, that compete for the
same documents. Run it before a template goes live (process_invoice.py --overlap).

1. Keywords alone (no PDF needed). When every keyword of template A is contained in a keyword of
   template B, every document B matches also contains all of A's keywords, so A is a candidate
   on all of B's documents.

2. Sample PDFs. Every sample is split into documents and parsed exactly as in a normal run
   (routing, keyword index, every candidate). Wherever templates of two or more layouts match one
   document:
     CLASH          two or more read it without FAIL and none has more evidence: in a normal
                    run the document goes to review (or the layout fingerprint decides)
     OVERLAP        two or more read it without FAIL, the evidence decides today; a later invoice
                    of the same vendor can turn it into a clash or a wrong pick
     KEYWORDS ONLY  the keywords match but only one layout reads it without FAIL (harmless)

3. Coverage. A template that reads no sample document was never tried against the others'
   samples from its own side: NO SAMPLE.

CLASH, OVERLAP and (for the template named) NO SAMPLE fail the test. They are fixed by making the
keywords exclusive: a word only the right vendor prints in its keywords, or a word only the other
one prints in its exclude_keywords. Versions of one layout (same family) are expected to share
keywords and are not compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import poppler
from .document import _evidence_text, break_tie_by_layout, dominant, parse_candidates
from .segment import layout_families, page_texts, split_into_documents
from .templates import family_of

CLASH, OVERLAP, KEYWORDS_ONLY, KEYWORD_SUBSET = "CLASH", "OVERLAP", "KEYWORDS ONLY", "KEYWORD SUBSET"
_TEXT_OPTIONS = ("remove_whitespace", "remove_accents", "lowercase", "replace")


@dataclass
class Finding:
    level: str
    where: str                     # "keywords" or "<file> pages [..]"
    templates: list[str]
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    documents: int = 0                                     # sample documents checked
    read_by: dict[str, int] = field(default_factory=dict)  # template -> documents it reads without FAIL
    unreadable: list[str] = field(default_factory=list)

    def failed(self, focus: str | None = None) -> bool:
        if any(f.level in (CLASH, OVERLAP) for f in self.findings):
            return True
        return focus is not None and self.read_by.get(focus, 0) == 0


def _options(template) -> dict:
    return template.options if hasattr(template, "options") else template.get("options", {})


def _same_text_handling(a, b) -> bool:
    return all(_options(a).get(k) == _options(b).get(k) for k in _TEXT_OPTIONS)


def keyword_subsets(templates: list, focus: str | None = None) -> list[Finding]:
    """Pairs of layouts where one template's keywords are all inside the other's."""
    findings = []
    for a in templates:
        for b in templates:
            if a is b or family_of(a) == family_of(b) or not _same_text_handling(a, b):
                continue
            if focus and focus not in (a["template_name"], b["template_name"]):
                continue
            keys_a, keys_b = a.get("keywords") or [], b.get("keywords") or []
            if not keys_a or not all(any(k in kb for kb in keys_b) for k in keys_a):
                continue
            excludes = a.get("exclude_keywords") or []
            if any(x in kb for x in excludes for kb in keys_b):
                continue                                   # A always excludes B's documents
            same = all(any(k in ka for ka in keys_a) for k in keys_b)
            if same and a["template_name"] > b["template_name"]:
                continue                                   # report identical sets once
            names = [a["template_name"], b["template_name"]]
            if same:
                text = f"{names[0]} and {names[1]} have the same keywords: each matches the other's documents"
            else:
                text = (f"every keyword of {names[0]} is inside the keywords of {names[1]}: "
                        f"{names[0]} matches every document {names[1]} matches")
            if excludes:
                text += f" (unless one of {names[0]}'s exclude_keywords is on the page)"
            findings.append(Finding(KEYWORD_SUBSET, "keywords", names, text))
    return findings


def check_samples(pdfs: list[Path], catalog, focus: str | None = None) -> Report:
    """Parse every sample document with all its candidates and report where layouts compete."""
    report = Report(findings=keyword_subsets(catalog.templates, focus))
    report.read_by = {t["template_name"]: 0 for t in catalog.templates}
    for pdf in pdfs:
        try:
            pages = page_texts(pdf)
        except poppler.PopplerError as exc:
            report.unreadable.append(str(exc))
            continue
        groups, _ = split_into_documents(pages, catalog)
        for group in groups:
            if group["family"] is None:
                continue
            report.documents += 1
            _, representatives = parse_candidates(group, pdf, catalog)
            alive = [r for r in representatives if r["status"] != "FAIL"]
            for record in alive:
                report.read_by[record["template"]] = report.read_by.get(record["template"], 0) + 1
            if len(layout_families(group["candidates"])) < 2:
                continue
            names = [r["template"] for r in representatives]
            if focus and focus not in names:
                continue
            report.findings.append(_classify(alive, representatives, group, pdf, f"{pdf.name} pages {group['pages']}"))
    return report


def _classify(alive: list[dict], representatives: list[dict], group: dict, pdf: Path, where: str) -> Finding:
    described = "; ".join(f"{r['template']}: {_evidence_text(r)}" for r in representatives)
    names = [r["template"] for r in representatives]
    if len(alive) < 2:
        reader = alive[0]["template"] if alive else "none of them"
        return Finding(KEYWORDS_ONLY, where, names,
                       f"keywords of several layouts match; only {reader} reads it without FAIL ({described})")
    winner = dominant(alive)
    if winner is not None:
        return Finding(OVERLAP, where, names,
                       f"several layouts read it without FAIL; the evidence picks {winner['template']} today "
                       f"({described})")
    decided = break_tie_by_layout(alive, group, pdf)
    tail = (f"; the layout fingerprint decides for {decided[0]['template']} today" if decided
            else "; a normal run sends it to review")
    return Finding(CLASH, where, names, f"several layouts read it equally well ({described}){tail}")


def print_report(report: Report, catalog, focus: str | None = None) -> None:
    order = {KEYWORD_SUBSET: 0, CLASH: 1, OVERLAP: 2, KEYWORDS_ONLY: 3}
    for finding in sorted(report.findings, key=lambda f: order[f.level]):
        print(f"{finding.level:<14} {finding.where}")
        print(f"   {finding.message}")
    for problem in report.unreadable:
        print(f"SKIPPED        {problem}")
    names = [focus] if focus else [t["template_name"] for t in catalog.templates]
    missing = [n for n in names if report.read_by.get(n, 0) == 0]
    if missing:
        print("NO SAMPLE      " + ", ".join(missing))
        print("   no document in these PDFs is read by this template, so the other templates were not "
              "tried on its documents; add at least one sample PDF of it")
    counts = {level: sum(f.level == level for f in report.findings) for level in order}
    print(f"\nSample documents: {report.documents}   clash: {counts[CLASH]}   overlap: {counts[OVERLAP]}   "
          f"keywords only: {counts[KEYWORDS_ONLY]}   keyword subsets: {counts[KEYWORD_SUBSET]}   "
          f"templates without a sample: {len(missing)}")
    if report.failed(focus):
        print("Result: NOT EXCLUSIVE. Make the keywords exclusive (README, section 10) or add the missing sample.")
    else:
        print("Result: no two layouts read the same sample document.")
