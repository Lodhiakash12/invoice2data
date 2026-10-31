"""The waiting room: documents that need the AI, collected until there are enough of one layout.

Reading never waits for the AI. A document is read at once with the templates there are, and the
result is given at once. What needs the AI is put here and handled later by jobs.py, several
documents together:

  unknown   pages no template recognizes. Reported as PENDING: a template is written when a
            group of them is due, then they are read again.
  variant   a document that a template of a known layout family matches but reads FAIL (or an
            unknown document printed with that family's issuer numbers, see below). The family's
            template is then extended to read it; only when one template cannot read all of
            them is a new version written.
  opinion   a document a template reads OK while that template's text values are not yet
            confirmed by the AI's second opinion (its first ai.second_opinion documents). It is
            reported OK, stored as a sample, and asked about together with the next ones.

Documents are put in groups:
  - opinion documents by template file;
  - variant documents by layout family;
  - unknown documents by look (signature.py): with the same issuer number (PAN / CIN) as a known
    layout family and a fair share of its labels, it joins that issuer's group (no keyword of the
    family's template matched, so it is more likely another kind of document of the issuer than
    the same layout: a new template is written, which the AI may make a version of the family);
    otherwise it joins the unknown group whose documents share most labels with it, or starts its
    own group.
A wrong grouping costs an AI attempt, never a wrong value: a template is admitted only when it
reads each document of the group OK, and a document it does not read is grouped again by how
the new templates read it.

A group is due when it holds ai.batch_size documents (default 3), when its oldest document has
waited ai.max_wait_days (default 7), or when a person asks for it (--ai-now, or the test page).
A group the AI could not handle is not tried again with the same AI settings until
ai.batch_size new documents have joined it (or the oldest new one has waited max_wait_days).

The documents are kept in <sample store>/waiting/ (one copy per file) and listed in index.json
under "waiting". They stay on this computer.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import signature as sig
from .samples import file_hash

UNKNOWN, VARIANT, OPINION = "unknown", "variant", "opinion"
RETRY_AFTER_SERVICE_ERROR = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now()


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


class WaitingRoom:
    def __init__(self, store, batch_size: int = 3, max_wait_days: float = 7, issuers: dict | None = None):
        """issuers: family -> issuer name, to tell the buyer's own registration numbers (printed on
        the documents of many issuers) from an issuer's own."""
        self.store = store
        self.batch_size = max(1, int(batch_size or 1))
        self.max_wait = timedelta(days=float(max_wait_days if max_wait_days is not None else 7))
        self.issuers = issuers or {}
        self.data = store.data.setdefault("waiting", {})
        self.data.setdefault("documents", [])
        self.data.setdefault("groups", {})
        self.data.setdefault("jobs", [])
        store.data.setdefault("signatures", {})

    # --- documents ---------------------------------------------------------------------------
    @property
    def documents(self) -> list[dict]:
        return self.data["documents"]

    def pdf_path(self, entry: dict) -> Path:
        return self.store.folder / "waiting" / f"{entry['pdf']}.pdf"

    def find(self, digest: str, pages: list[int], kind: str | None = None) -> dict | None:
        return next((e for e in self.documents if e["pdf"] == digest and e["pages"] == list(pages)
                     and (kind is None or e["kind"] == kind)), None)

    def add(self, pdf: Path, pages: list[int], kind: str, text: str = "", *, family: str | None = None,
            template: str | None = None, template_hash: str | None = None, status: str = "",
            reason: str = "", needed: int | None = None, problems: list[str] = ()) -> dict:
        """Put one document in the waiting room (the same file and pages again: updated, not
        added twice). Returns its entry, with its group."""
        digest = file_hash(pdf)
        pages = list(pages)
        entry = self.find(digest, pages, kind)
        if entry is None:
            for other in [e for e in self.documents if e["pdf"] == digest and e["pages"] == pages]:
                self.documents.remove(other)           # read differently now (unknown -> variant ...)
            entry = {"pdf": digest, "name": Path(pdf).name, "pages": pages, "kind": kind,
                     "added": _now().isoformat(timespec="seconds")}
            self.documents.append(entry)
            target = self.pdf_path(entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                shutil.copyfile(pdf, target)
        entry.update(status=status, reason=reason[:300], problems=[str(p)[:300] for p in problems][:6])
        if kind == OPINION:
            entry.update(template=template, template_hash=template_hash, needed=needed,
                         group=f"opinion:{template}")
        elif kind == VARIANT:
            entry.update(family=family, template=template, group=f"family:{family}")
        else:
            entry["signature"] = sig.signature(text) if text else entry.get("signature") or {"ids": [], "labels": []}
            self._place_unknown(entry)
        self.store.save()
        return entry

    def remove(self, entry: dict) -> None:
        if entry in self.documents:
            self.documents.remove(entry)
        if not any(e["pdf"] == entry["pdf"] for e in self.documents):
            self.pdf_path(entry).unlink(missing_ok=True)
        if not self.members(entry.get("group")):
            self.data["groups"].pop(entry.get("group"), None)
        self.store.save()

    def members(self, group: str | None) -> list[dict]:
        return [e for e in self.documents if e.get("group") == group]

    def groups(self) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = {}
        for entry in self.documents:
            found.setdefault(entry["group"], []).append(entry)
        for members in found.values():
            members.sort(key=lambda e: e.get("added", ""))
        return found

    # --- grouping of unknown documents ---------------------------------------------------------
    def _sample_signatures(self) -> dict[str, list[dict]]:
        """family -> signatures of its stored samples (computed once per sample, kept in the store)."""
        from .segment import page_texts
        cache = self.store.data["signatures"]
        found: dict[str, list[dict]] = {}
        for sample in self.store.data.get("samples", []):
            key = f"{sample['pdf']}:{','.join(map(str, sample['pages']))}"
            if key not in cache:
                path = self.store.pdf_path(sample)
                if not path.is_file():
                    continue
                try:
                    texts = page_texts(path)
                except Exception:  # an unreadable copy: no signature, no grouping by it
                    continue
                cache[key] = sig.signature("\n".join(texts[p - 1] for p in sample["pages"] if 0 < p <= len(texts)))
            found.setdefault(sample["family"], []).append(cache[key])
        return found

    def _common_ids(self, by_family: dict[str, list[dict]]) -> set[str]:
        by_issuer: dict[str, list[dict]] = {}
        for family, signatures in by_family.items():
            issuer = str(self.issuers.get(family) or family).lower()
            by_issuer.setdefault(issuer, []).extend(signatures)
        for entry in self.documents:                      # unknown documents count as their own issuer
            if entry["kind"] == UNKNOWN and entry.get("signature"):
                by_issuer.setdefault(f"waiting:{entry.get('group') or entry['pdf']}", []).append(entry["signature"])
        return sig.common_ids(by_issuer)

    def _place_unknown(self, entry: dict) -> None:
        mine = entry["signature"]
        by_family = self._sample_signatures()
        common = self._common_ids(by_family)
        best, best_score = None, 0.0
        for family, signatures in by_family.items():         # a known issuer's changed layout
            for other in signatures:
                score = sig.similarity(mine, other)
                if sig.shared_ids(mine, other, common) and score >= sig.SAME_ISSUER and score > best_score:
                    best, best_score = family, score
        if best is not None:
            # no keyword of that family's template matched, so its fixed texts differ: more likely
            # another kind of document of the same issuer than the same layout. Grouped with the
            # issuer's other unknown documents; a new template is written (the AI may still call it
            # a version of the issuer's layout)
            entry.update(family=best, group=f"issuer:{best}",
                         reason=(entry.get("reason") or "").split(" [")[0]
                                + f" [printed with the issuer numbers of {best}; labels {best_score:.2f} alike]")
            return
        best_group, best_score = None, 0.0
        for other in self.documents:                         # another unknown document of this layout
            if other is entry or other["kind"] != UNKNOWN or not other.get("signature"):
                continue
            score = sig.similarity(mine, other["signature"])
            if sig.related(mine, other["signature"], common) and score > best_score:
                best_group, best_score = other["group"], score
        entry["group"] = best_group or f"new:{entry['pdf'][:12]}:{','.join(map(str, entry['pages']))}"

    # --- when a group is due -----------------------------------------------------------------
    def due(self, force: bool = False, now: datetime | None = None, setup: str = "") -> list[str]:
        """The groups to hand to the AI now, oldest first."""
        now = now or _now()
        ready = []
        for group, members in self.groups().items():
            if force:
                ready.append((members[0].get("added", ""), group))
                continue
            tried = self.data["groups"].get(group, {}).get("tried")
            fresh = members
            if tried:
                last = _when(tried.get("when")) or now
                if tried.get("service_error"):
                    if now - last < RETRY_AFTER_SERVICE_ERROR:
                        continue
                elif tried.get("setup") == setup:
                    fresh = [m for m in members if (_when(m.get("added")) or now) > last]
                    if not fresh:
                        continue
            size = self.batch_size
            if members[0]["kind"] == OPINION:
                size = min(size, max(1, int(members[0].get("needed") or size)))
            oldest = _when(fresh[0].get("added")) or now
            if len(fresh) >= size or now - oldest >= self.max_wait:
                ready.append((members[0].get("added", ""), group))
        return [group for _, group in sorted(ready)]

    def tried(self, group: str, outcome: str, problems: list[str], setup: str, service_error: bool) -> None:
        self.data["groups"].setdefault(group, {})["tried"] = {
            "when": _now().isoformat(timespec="seconds"), "outcome": outcome, "problems": problems[:5],
            "setup": setup, "service_error": service_error}
        self.store.save()

    def status_line(self, entry: dict) -> str:
        """What a person is told about a waiting document."""
        members = self.members(entry.get("group"))
        size = self.batch_size
        if entry["kind"] == OPINION:
            size = min(size, max(1, int(entry.get("needed") or size)))
        oldest = min((_when(m.get("added")) or _now() for m in members), default=_now())
        deadline = (oldest + self.max_wait).strftime("%Y-%m-%d")
        tried = self.data["groups"].get(entry["group"], {}).get("tried")
        where = {UNKNOWN: "a new template for this layout" + (
                     f" (same issuer as {entry['family']})" if entry.get("family") else ""),
                 VARIANT: f"the template of {entry.get('family')} to be extended to this variant",
                 OPINION: f"the AI's second opinion on {entry.get('template')}"}[entry["kind"]]
        text = (f"waiting for {where}: {len(members)} of {size} document(s) collected; sent to the AI when "
                f"{size} are collected or on {deadline} at the latest")
        if tried and not tried.get("service_error"):
            text += (f" (the AI tried on {str(tried.get('when'))[:10]}: {tried.get('outcome')}; tried again "
                     f"when {size} more documents of this layout arrive)")
        return text

    def record_job(self, report: dict) -> None:
        self.data["jobs"].append(report)
        del self.data["jobs"][:-50]
        self.store.save()
