"""Background jobs: the AI works on the groups of the waiting room (waiting.py) that are due.

    reports = run_due(store, room, author, types_dir, config_dir)

For each due group, oldest first:

  variant   documents a known layout family reads FAIL. First the family's template is EXTENDED:
            the AI changes it so that it reads these documents and keeps reading the family's
            stored samples (given to it as documents it reads OK) with the same values. Only when
            no draft passes is a NEW VERSION written for the waiting documents (the old version
            stays for the documents it reads). One template where one can do, two where needed.
  unknown   documents no template recognizes, grouped by look. One template is written for all
            of them; when none passes, a template for the oldest document alone (the others are
            read again with it and grouped by the result).
  opinion   documents a template read OK while its text values were not yet confirmed: the AI
            reads them in one request; a difference makes the document CHECK and takes it out of
            the sample store.

The AI writes its drafts in a copy of document_types (<sample store>/staging), never in the
folder the engine reads from: documents read meanwhile (the test page keeps working) never see a
half-tested draft. Only the files of an admitted template (and its new fields) are copied back,
and only files the AI changed, so a template edited by a person meanwhile is not overwritten.

After a job every document of the group is read again with the templates as they are now:
  read OK or CHECK      it leaves the waiting room (and is stored as a sample when OK)
  read FAIL by a family it joins that family's group (the outcome decides, not the look)
  still unrecognized    it stays in its group; the group is not tried again with the same AI
                        settings until ai.batch_size new documents join it
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .waiting import OPINION, UNKNOWN, VARIANT, WaitingRoom

MAX_DOCUMENTS = 5          # documents of one group given to the AI in one job


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): _digest(p) for p in folder.rglob("*") if p.is_file()}


def _sync(staging: Path, live: Path, before: dict[str, str], keep: Path | None = None) -> tuple[list[str], list[str]]:
    """Copy back the files the AI added or changed in the staging copy: (copied, conflicts). A file
    a person changed in the live folder during the job is not overwritten (a conflict); the AI's
    version is kept in `keep` for that person to compare."""
    changed, conflicts = [], []
    for path in sorted(staging.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(staging))
        if before.get(relative) == _digest(path):
            continue
        target = live / relative
        now = _digest(target) if target.is_file() else None
        if now != before.get(relative):
            conflicts.append(relative)
            if keep is not None:
                keep.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, keep / (relative.replace("/", "__").replace("\\", "__")))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copyfile(path, tmp)
        tmp.replace(target)
        changed.append(relative)
    return changed, conflicts


def _named_copy(room: WaitingRoom, entry: dict, folder: Path) -> Path:
    """The waiting document under its own file name (so samples and notes show that name)."""
    target = folder / entry["pdf"][:8] / entry["name"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        shutil.copyfile(room.pdf_path(entry), target)
    return target


def _doc(path: Path, pages: list[int], status: str = "", problems: list[str] = ()):
    from .ai_author import MAX_TEXT, Doc
    from .segment import page_texts
    texts = page_texts(path)
    text = "\n".join(f"=== page {p} ===\n{texts[p - 1]}" for p in pages if 0 < p <= len(texts))
    return Doc(path, list(pages), text[:MAX_TEXT], status, list(problems))


def _latest(catalog, family: str):
    from .templates import family_of, version_of
    own = [t for t in catalog.templates if family_of(t) == family]
    return max(own, key=version_of) if own else None


def _service_problem(result) -> bool:
    """The attempt stopped on the AI service (network, usage limit, cut-off answer): no fault of the
    documents, tried again soon. Running out of time (ai.max_minutes) is a failed attempt."""
    return any(str(p).startswith(("AI service:", "AI step stopped:")) and "ai.max_minutes" not in str(p)
               for p in result.problems)


def run_due(store, room: WaitingRoom, author, types_dir: Path, config_dir: Path, force: bool = False,
            progress=None, write_layouts: bool = True, groups: list[str] | None = None, lock=None) -> list[dict]:
    """Hand every due group to the AI; returns one report per group (also kept in the store).
    lock: held while the live templates are changed and the documents read again (the test page
    reads uploads meanwhile); the AI's drafting runs without it."""
    reports = []
    setup = author._setup()
    for group in (groups if groups is not None else room.due(force=force, setup=setup)):
        members = room.members(group)
        if not members:
            continue
        if progress:
            progress(f"AI job: {group} ({len(members)} document(s): "
                     + ", ".join(m["name"] for m in members[:MAX_DOCUMENTS]) + ")")
        try:
            if members[0]["kind"] == OPINION:
                report = _opinion_job(store, room, author, group, members, types_dir, config_dir)
            else:
                report = _template_job(store, room, author, group, members, types_dir, config_dir, write_layouts,
                                       lock)
        except Exception as exc:  # one job must never stop the others or the reading
            report = {"group": group, "kind": members[0]["kind"], "outcome": "failed",
                      "documents": [m["name"] for m in members], "reread": [],
                      "problems": [f"job stopped: {type(exc).__name__}: {exc}"]}
            room.tried(group, "failed", report["problems"], setup, True)
        report["when"] = datetime.now().isoformat(timespec="seconds")
        room.record_job(report)
        reports.append(report)
        if progress:
            progress(f"  -> {describe(report)}")
    return reports


def describe(report: dict) -> str:
    """One line about a finished job."""
    outcome = report.get("outcome")
    if report.get("kind") == OPINION:
        text = (f"second opinion on {report.get('template')}: {report.get('confirmed', 0)} confirmed, "
                f"{len(report.get('doubted', []))} with differences")
        if report.get("problems"):
            text += "; " + "; ".join(report["problems"][:2])
        return text
    if outcome == "admitted":
        text = f"{report.get('action')} {report.get('template')}"
        if report.get("optional"):
            text += f" (optional by the documents: {', '.join(report['optional'])})"
    else:
        text = "no template passed: " + "; ".join(" ".join(str(p).split())[:160]
                                                  for p in report.get("problems", [])[:3])
    resolved = [r for r in report.get("reread", []) if r.get("resolved")]
    return text + f"; read again: {len(resolved)} of {len(report.get('reread', []))} now read"


# --- template jobs -----------------------------------------------------------------------------------

def _template_job(store, room, author, group, members, types_dir, config_dir, write_layouts, lock=None) -> dict:
    from .catalog import load_catalog
    kind = members[0]["kind"]
    family = members[0].get("family")
    report = {"group": group, "kind": kind, "family": family, "documents": [m["name"] for m in members],
              "outcome": "failed", "action": None, "template": None, "problems": [], "attempts": [],
              "reread": [], "changed_files": []}
    work = members[:MAX_DOCUMENTS]
    staging = store.folder / "staging"
    shutil.rmtree(staging, ignore_errors=True)
    stage_types = staging / "document_types"
    shutil.copytree(types_dir, stage_types, ignore=shutil.ignore_patterns("__pycache__"))
    before = _snapshot(stage_types)
    live, author.types_dir = author.types_dir, stage_types
    results, conflicts, admitted = [], [], None
    try:
        with tempfile.TemporaryDirectory(prefix="waiting_") as folder:
            docs = [_doc(_named_copy(room, e, Path(folder)), e["pages"], e.get("status", ""),
                         e.get("problems") or ([e["reason"]] if e.get("reason") else [])) for e in work]
            catalog = load_catalog(stage_types, config_dir)
            base = _latest(catalog, family) if kind == VARIANT and family else None
            if base is not None:
                samples = sorted(store.samples_of(family), key=lambda s: s.get("added", ""), reverse=True)[:2]
                read_ok = []
                for sample in samples:                 # under its own name, as the AI is shown it
                    named = Path(folder) / "samples" / sample["pdf"][:8] / sample.get("name", "sample.pdf")
                    named.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(store.pdf_path(sample), named)
                    read_ok.append(_doc(named, sample["pages"], "OK"))
                results.append(author.extend(base, docs, read_ok, catalog))
                if results[-1].outcome != "admitted" and not _service_problem(results[-1]):
                    results.append(author.version_from(base, docs, load_catalog(stage_types, config_dir)))
            else:
                results.append(author.new_template_from(docs, catalog))
                if results[-1].outcome != "admitted" and len(docs) > 1 and not _service_problem(results[-1]):
                    results.append(author.new_template_from(docs[:1], load_catalog(stage_types, config_dir)))
        admitted = next((r for r in results if r.outcome == "admitted"), None)
        if admitted is not None:
            with lock or contextlib.nullcontext():
                report["changed_files"], conflicts = _sync(stage_types, Path(types_dir), before,
                                                           store.folder / "ai_drafts")
            if conflicts:
                report["problems"].append("changed by a person while the AI worked, so not replaced (the AI's "
                                          f"version is in {store.folder / 'ai_drafts'}): {', '.join(conflicts)}")
    finally:
        author.types_dir = live
        shutil.rmtree(staging, ignore_errors=True)
    report["attempts"] = [{"action": r.action, "outcome": r.outcome, "drafts": r.attempts,
                           "problems": r.problems[:5]} for r in results]
    if admitted is not None and any(Path(c).name == admitted.template for c in conflicts):
        admitted = None                       # not in use: a person's edit of the same file stands
    if admitted is not None:
        report.update(outcome="admitted", action=admitted.action, template=admitted.template,
                      optional=admitted.optional, documents_written_for=admitted.documents)
    else:
        report["problems"] += [p for r in results for p in r.problems][:6]
    service = bool(results) and admitted is None and _service_problem(results[-1])
    with lock or contextlib.nullcontext():
        report["reread"] = reread(store, room, members, types_dir, config_dir, write_layouts)
    if room.members(group):
        room.tried(group, report["outcome"], report["problems"], author._setup(), service)
    return report


def reread(store, room, members, types_dir, config_dir, write_layouts: bool = True) -> list[dict]:
    """Read each waiting document again with the templates as they are now; resolve or regroup it."""
    from .catalog import load_catalog
    from .pipeline import Pipeline
    from .templates import family_of
    catalog = load_catalog(types_dir, config_dir)
    pipeline = Pipeline(catalog, store, write_layouts=write_layouts)
    by_name = {t["template_name"]: t for t in catalog.templates}
    done = []
    with tempfile.TemporaryDirectory(prefix="reread_") as folder:
        for entry in list(members):
            path = _named_copy(room, entry, Path(folder))
            try:
                result = pipeline.process(path)
            except Exception as exc:  # keep it waiting; the next job reads it again
                done.append({"name": entry["name"], "pages": entry["pages"], "status": "ERROR",
                             "resolved": False, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            docs = [d for d in result["documents"] if set(d["pages"]) & set(entry["pages"])]
            line = {"name": entry["name"], "pages": entry["pages"],
                    "status": docs[0]["status"] if docs else "PENDING",
                    "template": docs[0]["template"] if docs else None, "resolved": False}
            if docs and all(d["status"] != "FAIL" for d in docs):
                line["resolved"] = True
                room.remove(entry)
            elif docs and docs[0]["template"] in by_name and pipeline.state.get(docs[0]["template"]) == "admitted":
                family = family_of(by_name[docs[0]["template"]])
                reasons = [i["message"] for i in docs[0].get("issues", []) if i["level"] == "ERROR"]
                moved = room.add(path, entry["pages"], VARIANT, family=family, template=docs[0]["template"],
                                 status="FAIL", reason=reasons[0] if reasons else "", problems=reasons)
                line["group"] = moved["group"]
            done.append(line)
    return done


# --- second opinion jobs -------------------------------------------------------------------------------

def _opinion_job(store, room, author, group, members, types_dir, config_dir) -> dict:
    from . import second_opinion as opinion
    from .admission import template_hash
    from .catalog import load_catalog
    from .document import process_pdf
    from .segment import page_texts
    from .templates import family_of
    name = members[0]["template"]
    report = {"group": group, "kind": OPINION, "template": name, "documents": [m["name"] for m in members],
              "outcome": "failed", "confirmed": 0, "doubted": [], "problems": [], "reread": []}
    catalog = load_catalog(types_dir, config_dir)
    template = next((t for t in catalog.templates if t["template_name"] == name), None)
    digest = template_hash(catalog, template) if template is not None else None
    stale = [m for m in members if template is None or m.get("template_hash") != digest]
    for entry in stale:                       # the template changed since: its own documents count now
        room.remove(entry)
    members = [m for m in members if m not in stale]
    if not members:
        report.update(outcome="skipped", problems=["the template changed since these documents were read"])
        return report
    if opinion.trusted(store, name, digest):
        for entry in members:
            room.remove(entry)
        report.update(outcome="skipped", problems=["a person confirmed the template's values (--trust)"])
        return report
    registry = catalog.registry_for(template)
    names = opinion.fields_to_check(template, registry)
    notes = getattr(registry, "notes", {})
    question = opinion.question_of(names, notes)
    with tempfile.TemporaryDirectory(prefix="opinion_") as folder:
        work, docs, texts = [], [], []
        for entry in members:
            path = _named_copy(room, entry, Path(folder))
            read = next((d for d in process_pdf(path, catalog)["documents"]
                         if d["template"] == name and d["pages"] == entry["pages"]), None)
            if read is None:                   # not read by this template any more
                room.remove(entry)
                continue
            work.append((entry, path))
            docs.append(read)
            texts.append(opinion.page_text(page_texts(path), entry["pages"]))
        log = lambda purpose, prompt, answer: author._log(work[0][1] if work else "batch", purpose, prompt, answer)  # noqa: E731
        found = opinion.review_many(author.provider, docs, texts, names, log, notes) if work else []
        errors = []
        for (entry, path), doc, result in zip(work, docs, found):
            if result.error:
                errors.append(result.error)
                continue
            key = f"{entry['pdf']}:{','.join(str(p) for p in entry['pages'])}"
            opinion.record(store, name, digest, key, result, opinion.source_name(path, entry["pages"]), question)
            if result.agreed:
                report["confirmed"] += 1
            else:
                messages = [f.message() for f in result.findings if f.level == "WARN"]
                report["doubted"].append({"name": entry["name"], "pages": entry["pages"], "findings": messages})
                store.remove(entry["pdf"], entry["pages"])       # its values are in doubt
                opinion.apply(doc, result)
                store.note_problem(path, doc, family_of(template))
            report["reread"].append({"name": entry["name"], "pages": entry["pages"],
                                     "status": "OK" if result.agreed else "CHECK", "template": name,
                                     "resolved": True})
            room.remove(entry)
    if errors:
        report["problems"] = list(dict.fromkeys(errors))[:3]
        room.tried(group, "failed", report["problems"], author._setup(), True)
    report["outcome"] = "done" if not errors else ("partly" if report["reread"] else "failed")
    return report