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
family, the most recent ones.

    samples/
      index.json      the samples (file, pages, template, values) and the template checks
      pdfs/<hash>.pdf
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

INDEX = "index.json"
PER_FAMILY = 20


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
        self.data = {"samples": [], "templates": {}}
        index = self.folder / INDEX
        if index.is_file():
            try:
                self.data = json.loads(index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass                                  # unreadable index: start again, files stay
        self.data.setdefault("samples", [])
        self.data.setdefault("templates", {})

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
        for sample in self.data["samples"]:
            if sample["pdf"] == digest and sample["pages"] == pages:
                if sample["template"] != document["template"]:      # read by a newer version now
                    sample.update(template=document["template"], fields=document["fields"],
                                  lines=document.get("lines", []), status=document["status"])
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

    def update_values(self, sample: dict, document: dict) -> None:
        sample.update(template=document["template"], fields=document["fields"],
                      lines=document.get("lines", []), status=document["status"])

    def _trim(self, family: str) -> None:
        own = [s for s in self.data["samples"] if s["family"] == family]
        for old in sorted(own, key=lambda s: s["added"])[:-self.per_family]:
            self.data["samples"].remove(old)
        used = {s["pdf"] for s in self.data["samples"]}
        for path in (self.folder / "pdfs").glob("*.pdf"):
            if path.stem not in used:
                path.unlink(missing_ok=True)

    # --- template check results ---------------------------------------------------------------
    def template_state(self, name: str) -> dict:
        return self.data["templates"].get(name, {})

    def set_template_state(self, name: str, state: dict) -> None:
        self.data["templates"][name] = state
        self.save()

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / (INDEX + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.folder / INDEX)
