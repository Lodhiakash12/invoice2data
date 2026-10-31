"""Sample store: documents the engine has read correctly, kept as proof of what each template
must keep reading.

Every document read with status OK is stored automatically: one copy of the PDF (named by its
content hash, so the same file is kept once) and the values read from it. The store is what makes
the checks of a new or changed template possible without anyone collecting PDFs by hand:

  - the stress test and the fingerprint use a template's stored samples;
  - the regression check compares a changed template's values with the stored ones;
  - the overlap check runs every other template on them.

The store holds real documents. It stays on this computer (the folder is set by sample_store in
config/settings.yml, default: samples/) and keeps at most `per_family` documents per layout
family, the most recent ones; an older document stays while it is the only one of some variant
(variants.py), so the only CGST + SGST document of a layout is not dropped for newer IGST ones.

Documents a checked template read FAIL or CHECK are noted too (name, pages, first reason, the
last PROBLEMS_PER_FAMILY per family), for the variant report; reading the same pages OK later
removes the note.

    samples/
      index.json      the samples (file, pages, template, values) and the template checks
      pdfs/<hash>.pdf
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

INDEX = "index.json"
PER_FAMILY = 20
PROBLEMS_PER_FAMILY = 10


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:20]


class SampleStore:
    def __init__(self, folder: Path, per_family: int = PER_FAMILY):
        self.folder = Path(folder)
        self.per_family = per_family
        self.lock = threading.RLock()                 # the test page's background jobs share the store
        self.data = {"samples": [], "templates": {}}
        index = self.folder / INDEX
        if index.is_file():
            try:
                self.data = json.loads(index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass                                  # unreadable index: start again, files stay
        self.data.setdefault("samples", [])
        self.data.setdefault("templates", {})
        self.data.setdefault("problems", [])
        self.mtime = index.stat().st_mtime if index.is_file() else None   # changed by another program?

    # --- samples -------------------------------------------------------------------------------
    def pdf_path(self, sample: dict) -> Path:
        return self.folder / "pdfs" / f"{sample['pdf']}.pdf"

    def samples_of(self, family: str) -> list[dict]:
        return [s for s in self.data["samples"] if s["family"] == family and self.pdf_path(s).is_file()]

    def all_pdfs(self) -> list[Path]:
        seen, result = set(), []
        for sample in self.data["samples"]:
            path = self.pdf_path(sample)
            if sample["pdf"] not in seen and path.is_file():
                seen.add(sample["pdf"])
                result.append(path)
        return result

    def add(self, pdf: Path, document: dict, family: str) -> bool:
        """Store one OK document. False when this file and pages are already stored."""
        digest = file_hash(pdf)
        pages = list(document["pages"])
        forgot = self._forget_problem(digest, pages)
        for sample in self.data["samples"]:
            if sample["pdf"] == digest and sample["pages"] == pages:
                if sample["template"] != document["template"]:      # read by a newer version now
                    sample.update(template=document["template"], fields=document["fields"],
                                  lines=document.get("lines", []), status=document["status"])
                    forgot = True
                if forgot:
                    self.save()
                return False
        target = self.folder / "pdfs" / f"{digest}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            shutil.copyfile(pdf, target)
        self.data["samples"].append({
            "pdf": digest, "name": Path(pdf).name, "pages": pages, "template": document["template"],
            "family": family, "status": document["status"], "fields": document["fields"],
            "lines": document.get("lines", []), "added": datetime.now().isoformat(timespec="seconds")})
        self._trim(family)
        self.save()
        return True

    def remove(self, digest: str, pages: list[int]) -> bool:
        """Take one document out of the store (its values are in doubt). True when it was there."""
        before = len(self.data["samples"])
        self.data["samples"] = [s for s in self.data["samples"] if not (s["pdf"] == digest and s["pages"] == list(pages))]
        if len(self.data["samples"]) == before:
            return False
        if not any(s["pdf"] == digest for s in self.data["samples"]):
            (self.folder / "pdfs" / f"{digest}.pdf").unlink(missing_ok=True)
        self.save()
        return True

    def update_values(self, sample: dict, document: dict) -> None:
        sample.update(template=document["template"], fields=document["fields"], pages=document["pages"],
                      lines=document.get("lines", []), status=document["status"])

    def _trim(self, family: str) -> None:
        from .variants import keep_on_trim
        own = [s for s in self.data["samples"] if s["family"] == family]
        kept = {id(s) for s in keep_on_trim(own, self.per_family)}
        for old in own:
            if id(old) not in kept:
                self.data["samples"].remove(old)
        used = {s["pdf"] for s in self.data["samples"]}
        for path in (self.folder / "pdfs").glob("*.pdf"):
            if path.stem not in used:
                path.unlink(missing_ok=True)

    # --- documents not read OK ----------------------------------------------------------------
    def note_problem(self, pdf: Path, document: dict, family: str) -> None:
        """Note a FAIL or CHECK document of a checked template (replaces an older note of the same
        file and pages)."""
        digest = file_hash(pdf)
        pages = list(document["pages"])
        issues = document.get("issues") or []
        reasons = [i.get("message", "") for i in issues if i.get("level") == "ERROR"] or \
                  [i.get("message", "") for i in issues if i.get("level") == "WARN"]
        self.data["problems"] = [p for p in self.data["problems"] if not (p["pdf"] == digest and p["pages"] == pages)]
        self.data["problems"].append({
            "pdf": digest, "name": Path(pdf).name, "pages": pages, "family": family,
            "template": document.get("template"), "status": document.get("status"),
            "reason": reasons[0] if reasons else "", "added": datetime.now().isoformat(timespec="seconds")})
        own = [p for p in self.data["problems"] if p["family"] == family]
        for old in own[:-PROBLEMS_PER_FAMILY]:
            self.data["problems"].remove(old)
        self.save()

    def problems_of(self, family: str) -> list[dict]:
        return [p for p in self.data["problems"] if p["family"] == family]

    def _forget_problem(self, digest: str, pages: list[int]) -> bool:
        before = len(self.data["problems"])
        self.data["problems"] = [p for p in self.data["problems"] if not (p["pdf"] == digest and p["pages"] == pages)]
        return len(self.data["problems"]) != before

    # --- template check results ---------------------------------------------------------------
    def template_state(self, name: str) -> dict:
        return self.data["templates"].get(name, {})

    def set_template_state(self, name: str, state: dict) -> None:
        self.data["templates"][name] = state
        self.save()

    def save(self) -> None:
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            for attempt in range(5):
                try:                                  # another thread may change the data meanwhile
                    text = json.dumps(self.data, indent=1, ensure_ascii=False, default=str)
                    break
                except RuntimeError:
                    if attempt == 4:
                        raise
            tmp = self.folder / (INDEX + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.folder / INDEX)
            self.mtime = (self.folder / INDEX).stat().st_mtime
