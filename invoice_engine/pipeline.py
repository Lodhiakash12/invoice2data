"""The processing pipeline: reading a PDF plus the automatic upkeep of the templates.

    pipeline = Pipeline(catalog, store)
    result = pipeline.process(pdf)

For every PDF, with nobody running a separate command:

  1. Before the first PDF, every template whose file is new or changed is checked (admission.py):
     on the samples of its layout in the sample store, or, without samples, for overlap with
     every stored sample. A template that fails is held: it takes no part in reading.
  2. The PDF is read with the templates that are not held (process_pdf).
  3. A template that reads its first document is checked on it; until it passes, that document
     is reported as CHECK, never as OK.
  4. A document read OK by an admitted template is stored as a sample; when a template gains a
     sample, its layout fingerprint is rebuilt from all its samples.

Everything stays on this computer. Nothing here changes how a document is read; it decides
which templates are trusted and keeps the evidence for the next check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .admission import ADMITTED, HELD, NEW, check_template, record, template_hash
from .document import process_pdf
from .fingerprints import build_layouts
from .templates import family_of


@dataclass
class Event:
    template: str
    state: str                       # admitted / held / new
    samples: int
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, catalog, store, write_layouts: bool = True):
        self.full = catalog                            # every usable template
        self.store = store
        self.write_layouts = write_layouts
        self.events: list[Event] = []                  # checks decided during this run
        self.state: dict[str, str] = {}
        self._prepared = False

    @property
    def held(self) -> set[str]:
        return {name for name, state in self.state.items() if state == HELD}

    @property
    def catalog(self):
        """The templates that take part in reading: every usable one except those held."""
        return self.full.without(self.held)

    # --- 1. check new or changed templates -----------------------------------------------------
    def prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        pending = []
        for template in self.full.templates:
            name = template["template_name"]
            saved = self.store.template_state(name)
            if saved.get("hash") == template_hash(self.full, template):
                self.state[name] = saved.get("state", NEW)
            else:
                self.state[name] = NEW
                pending.append(template)
        for template in pending:                       # after the known states are set
            self._admit(template)

    def _admit(self, template, extra_pdfs: list[Path] = (), accept_changes: bool = False) -> str:
        name = template["template_name"]
        others_held = self.held - {name}
        verdict = check_template(self.full.without(others_held), template, self.store, extra_pdfs,
                                 accept_changes)
        saved = record(self.store, self.full, template, verdict)
        self.state[name] = saved["state"]
        self.events.append(Event(name, saved["state"], verdict.samples, verdict.problems, verdict.notes))
        if saved["state"] == ADMITTED and self.write_layouts:
            self._refresh_layout(template)
        return saved["state"]

    def accept(self, name: str) -> Event:
        """Check one template again, allowing changed values on its stored samples (a deliberate
        correction). Stress and overlap must still pass."""
        self.prepare()
        template = next(t for t in self.full.templates if t["template_name"] == name)
        self._admit(template, accept_changes=True)
        return self.events[-1]

    # --- 2-4. one PDF ------------------------------------------------------------------------------
    def process(self, pdf: Path) -> dict:
        self.prepare()
        pdf = Path(pdf)
        catalog = self.catalog
        by_name = {t["template_name"]: t for t in catalog.templates}
        result = process_pdf(pdf, catalog)
        for doc in result["documents"]:
            template = by_name.get(doc["template"])
            if template is None:
                continue
            name = doc["template"]
            if self.state.get(name) == NEW and doc["status"] != "FAIL":
                self._admit(template, [pdf])
            state = self.state.get(name)
            if state == HELD:
                problems = self.store.template_state(name).get("problems", [])
                self._downgrade(doc, f"template {name} failed its automatic check on this document: "
                                     + "; ".join(problems[:3]))
            elif state == NEW:
                self._downgrade(doc, f"template {name} is not checked yet")
            elif state == ADMITTED and doc["status"] == "OK" and not doc.get("copy_of"):
                if self.store.add(pdf, doc, family_of(template)) and self.write_layouts:
                    self._refresh_layout(template)
        if self.held:
            result["held_templates"] = self.held_report()
            for item in result["held_templates"]:
                result.setdefault("notes", []).append(
                    f"template {item['template']} is held by the automatic checks and not used: "
                    + "; ".join(item["problems"][:3])
                    + (f". If the new values are the correct ones, confirm with: python process_invoice.py "
                       f"--accept {item['template']}" if item["changed_values"] else ""))
        return result

    def held_report(self) -> list[dict]:
        """Every held template with its reasons; changed_values: held only because it reads
        values differently from before (confirm with accept() when the new values are right)."""
        report = []
        for name in sorted(self.held):
            problems = self.store.template_state(name).get("problems", [])
            changed = bool(problems) and all("reads different values than before" in p for p in problems)
            report.append({"template": name, "problems": problems, "changed_values": changed})
        return report

    @staticmethod
    def _downgrade(doc: dict, message: str) -> None:
        doc.setdefault("issues", []).append({"level": "WARN", "message": message})
        if doc["status"] == "OK":
            doc["status"] = "CHECK"

    def _refresh_layout(self, template) -> None:
        pdfs = list(dict.fromkeys(self.store.pdf_path(s) for s in self.store.samples_of(family_of(template))))
        if pdfs:
            build_layouts(self.full, pdfs, only={template["template_name"]})