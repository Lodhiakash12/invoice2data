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
  5. With the AI step switched on (ai_author.py): pages no template recognizes get a new template,
     and a document a template reads with FAIL gets a new version of that template. A template
     the AI writes is used only after it passes the checks of step 1; the PDF is then read again.

Everything stays on this computer. Nothing here changes how a document is read; it decides
which templates are trusted and keeps the evidence for the next check.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .admission import ADMITTED, HELD, NEW, check_template, engine_version, record, template_hash
from .document import process_pdf
from .fingerprints import build_layouts
from .segment import page_texts
from . import second_opinion as opinion
from .samples import file_hash
from .variants import IF_AVAILABLE, MIXED, NEEDED, ZERO, new_variants, variant_of
from .templates import family_of
from .validate import rounding_modes


@dataclass
class Event:
    template: str
    state: str                       # admitted / held / new
    samples: int
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


REPORTED = NEEDED | IF_AVAILABLE | {ZERO, MIXED}


class Pipeline:
    def __init__(self, catalog, store, write_layouts: bool = True, author=None, reload=None, progress=None):
        self.full = catalog                            # every usable template
        self.store = store
        self.write_layouts = write_layouts
        self.author = author                           # TemplateAuthor, or None: no AI step
        self.reload = reload                           # () -> Catalog, after the AI added templates
        self.progress = progress                       # (text) -> None: shows the checks as they run
        self.ai_results: list = []
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
            held_by_older_engine = saved.get("state") == HELD and saved.get("engine") != engine_version()
            if saved.get("hash") == template_hash(self.full, template) and not held_by_older_engine:
                self.state[name] = saved.get("state", NEW)
            else:
                self.state[name] = NEW
                pending.append(template)
        if pending and self.progress:
            self.progress(f"Checking {len(pending)} new or changed template(s) against the stored samples "
                          f"({len(self.store.data['samples'])} documents). This happens once after a template "
                          f"or engine update and can take a few minutes:")
        for number, template in enumerate(pending, start=1):   # after the known states are set
            started = time.perf_counter()
            state = self._admit(template)
            if self.progress:
                shown = {NEW: "no stored sample yet (checked on its first document)"}.get(state, state)
                self.progress(f"  [{number}/{len(pending)}] {template['template_name']}: {shown} "
                              f"({time.perf_counter() - started:.0f} s)")

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

    # --- 2-5. one PDF ------------------------------------------------------------------------------
    def process(self, pdf: Path) -> dict:
        result = self._process(pdf)
        self._note_problems(Path(pdf), result)
        return result

    def _note_problems(self, pdf: Path, result: dict) -> None:
        """FAIL and CHECK documents of checked templates go to the store's problem notes (the
        variant report shows them by layout)."""
        by_name = {t["template_name"]: t for t in self.catalog.templates}
        for doc in result.get("documents", []):
            template = by_name.get(doc["template"])
            if template is not None and doc["status"] in ("FAIL", "CHECK") and not doc.get("copy_of") \
                    and self.state.get(doc["template"]) == ADMITTED:
                try:
                    self.store.note_problem(pdf, doc, family_of(template))
                except OSError:
                    pass

    def _process(self, pdf: Path) -> dict:
        result = self._read(pdf)
        if self.author is None:
            return result
        results = self._ai(Path(pdf), result)
        if not results:
            return result
        self.ai_results += results
        if any(r.outcome == "admitted" for r in results) and self.reload is not None:
            self.full = self.reload()                  # the new templates and fields
            self.state, self._prepared = {}, False
            result = self._read(pdf)
        result["ai"] = [asdict(r) for r in results]
        for r in results:
            if r.outcome == "admitted":
                text = (f"AI wrote {r.template} ({r.action}, type {r.doc_type}"
                        + (", new type" if r.new_type else "") + f", {r.attempts} draft(s)); it passed the checks")
            elif r.outcome == "failed":
                text = f"AI could not write a template for pages {r.pages}: " + "; ".join(r.problems[:3])
            elif r.outcome == "skipped" and r.problems:
                text = f"AI step for pages {r.pages}: " + r.problems[0]
            else:
                continue
            result.setdefault("notes", []).append(text)
        return result

    def _ai(self, pdf: Path, result: dict) -> list:
        """Give unrecognized pages and FAIL documents of admitted templates to the AI author."""
        work = []
        for group in result.get("unrecognized_pages", []):
            if not any(word in str(group.get("reason", "")) for word in ("scanned", "garbled")):   # nothing to read
                work.append(("new", group))
        for doc in result.get("documents", []):
            if doc["status"] == "FAIL" and self.state.get(doc["template"]) == ADMITTED:
                work.append(("version", doc))
        if not work:
            return []
        texts = page_texts(pdf)
        done = []
        from .ai_author import AuthorResult
        for kind, item in work:
            pages = item["pages"]
            page_text = [texts[p - 1] for p in pages if 0 < p <= len(texts)]
            try:
                if kind == "new":
                    done.append(self.author.new_template(pdf, pages, page_text, self.full))
                else:
                    done.append(self.author.new_version(pdf, item, page_text, self.full))
            except Exception as exc:  # the AI step never stops the reading of a file
                done.append(AuthorResult(pdf.name, pages, "new template" if kind == "new" else "new version",
                                         "failed", problems=[f"AI step stopped: {type(exc).__name__}: {exc}"]))
            if done[-1].outcome == "admitted" and self.reload is not None:
                self.full = self.reload()              # the next group sees the new template
        return done

    def _read(self, pdf: Path) -> dict:
        self.prepare()
        pdf = Path(pdf)
        catalog = self.catalog
        by_name = {t["template_name"]: t for t in catalog.templates}
        result = process_pdf(pdf, catalog)
        texts: list = []                               # page texts, read once if the AI needs them
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
                self._second_opinion(pdf, doc, template, texts)
            if state == ADMITTED and doc["status"] == "OK" and not doc.get("copy_of"):
                earlier = list(self.store.samples_of(family_of(template)))
                if self.store.add(pdf, doc, family_of(template)):
                    fresh = [v for v in new_variants(variant_of(doc), earlier) if v in REPORTED] if earlier else []
                    if fresh:
                        result.setdefault("notes", []).append(
                            f"first {', '.join(fresh)} document read OK by {family_of(template)}: this variant "
                            f"is now proven by a real document (stored as a sample)")
                    if self.write_layouts:
                        self._refresh_layout(template)
                note = self._settle_rounding(template, (doc.get("evidence") or {}).get("rounding_fits"), pdf)
                if note:
                    result.setdefault("notes", []).append(note)
        if self.held:
            result["held_templates"] = self.held_report()
            for item in result["held_templates"]:
                result.setdefault("notes", []).append(
                    f"template {item['template']} is held by the automatic checks and not used: "
                    + "; ".join(item["problems"][:3])
                    + (f". If the new values are the correct ones, confirm with: python process_invoice.py "
                       f"--accept {item['template']}" if item["changed_values"] else ""))
        return result

    def _second_opinion(self, pdf: Path, doc: dict, template, texts: list) -> None:
        """The first documents of a template: the AI reads the text fields on its own; a
        difference makes the document CHECK (and keeps it out of the sample store)."""
        limit = int(getattr(getattr(self.author, "settings", None), "second_opinion", 0) or 0)
        if self.author is None or limit <= 0:
            return
        name = doc["template"]
        digest = template_hash(self.full, template)
        if opinion.trusted(self.store, name, digest):
            return
        registry = self.full.registry_for(template)
        names = opinion.fields_to_check(template, registry)
        if not names:
            return
        notes = getattr(registry, "notes", {})
        question = opinion.question_of(names, notes)
        key = f"{file_hash(pdf)}:{','.join(str(p) for p in doc['pages'])}"
        earlier = opinion.previous(self.store, name, digest, key, question)
        if earlier is not None:                         # asked before: the same answer, no new question
            opinion.apply_previous(doc, earlier)
            if not earlier.get("agreed"):
                self._trust_hint(doc, name)
            return
        if not opinion.wanted(self.store, name, digest, limit):
            return
        if not texts:
            try:
                texts.extend(page_texts(pdf))
            except Exception as exc:  # the text was read a moment ago; never stop the run
                doc.setdefault("issues", []).append({"level": "INFO", "message": f"second opinion not available: {exc}"})
                return
        log = lambda purpose, prompt, answer: self.author._log(pdf, purpose, prompt, answer)  # noqa: E731
        try:
            found = opinion.review(self.author.provider, doc, opinion.page_text(texts, doc["pages"]), names, log, notes)
        except Exception as exc:  # the second opinion never stops the reading of a file
            found = opinion.Opinion(error=f"{type(exc).__name__}: {exc}")
        opinion.apply(doc, found)
        opinion.record(self.store, name, digest, key, found, opinion.source_name(pdf, doc["pages"]), question)
        if found.error:
            return
        if not found.agreed:
            self._trust_hint(doc, name)
            digest_pdf, pages = key.split(":")[0], doc["pages"]
            if self.store.remove(digest_pdf, pages):     # stored earlier, while the AI could not answer
                doc["issues"].append({"level": "INFO", "message": "taken out of the sample store until its "
                                                                  "values are confirmed"})
        else:
            doc.setdefault("issues", []).append({"level": "INFO", "message":
                f"second opinion: {opinion.agreed_count(self.store, name, digest)} of {limit} documents "
                f"of {name} agreed"})

    @staticmethod
    def _trust_hint(doc: dict, name: str) -> None:
        doc.setdefault("issues", []).append({"level": "INFO", "message":
            f"if the template's values are right, stop the second opinion for {name} with: "
            f"python process_invoice.py --trust {name}"})

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

    def _template_path(self, name: str) -> Path | None:
        for dt in self.full.types:
            path = Path(dt.folder) / "templates" / name
            if path.is_file():
                return path
        return None

    def _settle_rounding(self, template, fits, pdf: Path) -> str | None:
        """A template whose first sample fitted more than one rounding rule lists them all
        (total_rounding_mode: [up, nearest]). A document that fits fewer of them settles the rule:
        the template file keeps only those. Its content changes, so the next run checks it again on
        every stored sample. A document that fits none of the rules left in the file (another
        document of this run settled it differently) is reported, the file is not changed."""
        if not fits:
            return None
        path = self._template_path(template["template_name"])
        if path is None:
            return None
        import re
        import yaml
        text = path.read_text(encoding="utf-8")
        line = re.search(r"(?m)^total_rounding_mode:[ \t]*([^#\n]*?)[ \t]*(#.*)?$", text)
        if not line:
            return None
        try:
            current = rounding_modes(yaml.safe_load(line.group(1)))
        except yaml.YAMLError:
            return None
        if len(current) < 2:
            if current[0] in fits:
                return None
            return (f"template {template['template_name']}: {pdf.name} fits rounding {', '.join(fits)} but the "
                    f"template was settled on {current[0]} by another document: these documents disagree, check them")
        kept = [m for m in current if m in fits]
        if not kept:
            return (f"template {template['template_name']}: {pdf.name} fits rounding {', '.join(fits)}, none of "
                    f"{', '.join(current)}: check the document")
        if len(kept) == len(current):
            return None
        from datetime import date
        value = kept[0] if len(kept) == 1 else "[" + ", ".join(kept) + "]"
        new_line = (f"total_rounding_mode: {value}   # settled {date.today().isoformat()} by {pdf.name} "
                    f"(was {', '.join(current)})")
        path.write_text(text[:line.start()] + new_line + text[line.end():], encoding="utf-8")
        return (f"template {template['template_name']}: rounding rule settled to {value} by {pdf.name} "
                f"(it fits only {', '.join(fits)}); the template is checked again on the next run")

    def _refresh_layout(self, template) -> None:
        pdfs = list(dict.fromkeys(self.store.pdf_path(s) for s in self.store.samples_of(family_of(template))))
        if pdfs:
            build_layouts(self.full, pdfs, only={template["template_name"]})