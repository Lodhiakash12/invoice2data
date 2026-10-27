"""Admission of templates: a new or changed template is checked automatically before its
results are trusted. Nobody has to run the checks or approve the template.

A template's file is identified by a hash of its text (the layout block is left out, since the
engine rewrites it). When the hash is new, the template is checked on the samples of its layout
family in the sample store, or, for a template that has no sample yet, on the first document it
reads. The checks:

  1. regression   every stored sample of its family is still read by this family, with a status
                  at least as good and the same values: a stored value was read and verified
                  before, so a template that reads it differently is not trusted. A deliberate
                  correction of earlier values is confirmed with
                      python process_invoice.py --accept <template>
  2. stress       its samples, rewritten the ways real versions of a layout differ, still give
                  the same values (stress.py); a different value (WRONG) or a document no longer
                  recognized fails the check
  3. overlap      no template of another layout reads its samples, and it reads none of theirs
                  (overlap.py)

A template without any sample yet gets the overlap check at once (it must not read any other
layout's stored samples) and the full check on the first document it reads.

Passed: the template is "admitted"; its fingerprint is built from its samples.
Failed: the template is "held": it takes no part in reading documents until a corrected version
passes, so the templates that work go on working (a held new version leaves the old one in
charge). Its reasons are reported in every run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import overlap
from .document import process_pdf
from .stress import compare, stress
from .templates import family_of

ADMITTED, HELD, NEW = "admitted", "held", "new"
_RANK = {"FAIL": 0, "CHECK": 1, "OK": 2}


def template_file(catalog, template) -> Path:
    return catalog.type_of(template).folder / "templates" / template["template_name"]


def template_hash(catalog, template) -> str:
    text = template_file(catalog, template).read_text(encoding="utf-8")
    text = re.split(r"(?m)^layout:\s*$", text)[0].strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Verdict:
    template: str
    passed: bool
    samples: int = 0
    problems: list[str] = field(default_factory=list)      # reasons it is held
    notes: list[str] = field(default_factory=list)         # worth knowing, not blocking


def check_template(catalog, template, store, extra_pdfs: list[Path] = (),
                   accept_changes: bool = False) -> Verdict:
    """Run the three checks for one template on its stored samples plus `extra_pdfs`.
    accept_changes: changed values on stored samples are allowed (a deliberate correction);
    the stored values are replaced when the template passes."""
    name, family = template["template_name"], family_of(template)
    verdict = Verdict(name, passed=True)
    stored = store.samples_of(family)

    # 1. regression on stored samples of this family
    corrected = []
    for sample in stored:
        result = process_pdf(store.pdf_path(sample), catalog)
        doc = next((d for d in result["documents"] if d["pages"] == sample["pages"]), None)
        where = f"{sample['name']} p{sample['pages'][0]}"
        if doc is None or family_of_name(catalog, doc["template"]) != family:
            verdict.problems.append(f"regression: {where} is no longer read by this layout"
                                    + (f" (now {doc['template']})" if doc else ""))
            continue
        lost, changed = compare(sample, doc)
        if _RANK.get(doc["status"], 0) < _RANK.get(sample["status"], 0):
            verdict.problems.append(f"regression: {where} {sample['status']} -> {doc['status']}")
        elif lost:
            verdict.problems.append(f"regression: {where} lost {', '.join(lost[:5])}")
        elif changed and accept_changes:
            verdict.notes.append(f"regression: {where} changed {', '.join(changed[:5])} (accepted)")
            corrected.append((sample, doc))
        elif changed:
            verdict.problems.append(f"regression: {where} reads different values than before: "
                                    + "; ".join(_old_new(sample, doc, key) for key in changed[:3]))

    pdfs = [store.pdf_path(s) for s in stored] + [Path(p) for p in extra_pdfs]
    pdfs = list(dict.fromkeys(pdfs))

    # 2. stress on every sample it reads
    report = stress(catalog, pdfs, only={name})[0]
    verdict.samples = len(report.samples)
    for outcome in report.outcomes:
        text = f"stress: {outcome.rewrite} on {outcome.sample}: {outcome.detail}"
        if outcome.result in ("WRONG", "not matched"):
            verdict.problems.append(text)
        elif outcome.result == "lost":
            verdict.notes.append(text)

    # 3. overlap with every other layout, on all stored samples and the new ones
    every = list(dict.fromkeys(store.all_pdfs() + [Path(p) for p in extra_pdfs]))
    found = overlap.check_samples(every, catalog, name)
    for finding in found.findings:
        if finding.level in (overlap.CLASH, overlap.OVERLAP) and name in finding.templates:
            verdict.problems.append(f"overlap: {finding.level} {finding.where}: {finding.message}")

    # stored files are named by hash: show the name the document arrived with
    names = {f"{s['pdf']}.pdf": s["name"] for s in store.data["samples"]}
    def readable(text: str) -> str:
        for stored_name, original in names.items():
            text = text.replace(stored_name, original)
        return text
    verdict.problems = [readable(p) for p in verdict.problems]
    verdict.notes = [readable(n) for n in verdict.notes]
    verdict.problems = list(dict.fromkeys(verdict.problems))
    verdict.notes = list(dict.fromkeys(verdict.notes))
    verdict.passed = not verdict.problems
    if verdict.passed and corrected:
        for sample, doc in corrected:
            store.update_values(sample, doc)
        store.save()
    return verdict


def _value(record: dict, key: str):
    """A field value, or a line value for keys like "line 2 name"."""
    parts = key.split(" ", 2)
    if parts[0] == "line" and len(parts) == 3 and parts[1].isdigit():
        lines = record.get("lines") or []
        index = int(parts[1]) - 1
        return lines[index].get(parts[2]) if 0 <= index < len(lines) else None
    return (record.get("fields") or {}).get(key)


def _old_new(before: dict, after: dict, key: str) -> str:
    """"name '...(Tub) (HSN-' -> '...(Tub) (HSN- 21050000)'": long texts are shown from a little
    before the first difference, so the part that changed is visible."""
    if key.startswith("lines ("):                      # a different number of lines
        return key
    old, new = str(_value(before, key)), str(_value(after, key))
    same = 0
    while same < min(len(old), len(new)) and old[same] == new[same]:
        same += 1
    start = max(0, same - 20) if max(len(old), len(new)) > 60 else 0

    def part(text: str) -> str:
        shown = text[start:start + 60]
        return ("..." if start else "") + shown + ("..." if len(text) > start + 60 else "")
    return f"{key} '{part(old)}' -> '{part(new)}'"


def family_of_name(catalog, template_name: str) -> str | None:
    template = next((t for t in catalog.templates if t["template_name"] == template_name), None)
    return family_of(template) if template is not None else None


def record(store, catalog, template, verdict: Verdict) -> dict:
    # passed without any document of its own to check on: it waits for its first document
    outcome = (ADMITTED if verdict.samples else NEW) if verdict.passed else HELD
    state = {"hash": template_hash(catalog, template), "state": outcome,
             "checked": datetime.now().isoformat(timespec="seconds"), "samples": verdict.samples,
             "problems": verdict.problems, "notes": verdict.notes[:20]}
    store.set_template_state(template["template_name"], state)
    return state