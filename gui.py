"""
Local test page for the parser: drop PDFs in the browser, see what the engine extracts.

    python gui.py              then open http://127.0.0.1:8765 (opens by itself)
    python gui.py --port 9000  another port

Everything stays on this computer: the page is served only to this machine (127.0.0.1), the PDF
is written to a temporary file, processed with the same engine and templates as
process_invoice.py, and deleted straight after. Templates are re-read for every PDF, so a
template you just edited is used without restarting. Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from invoice_engine import load_catalog, process_pdf, safe_regex
from invoice_engine import poppler
from invoice_engine.export import json_default
from invoice_engine.pipeline import Pipeline
from invoice_engine.samples import SampleStore
from invoice_engine.ai_author import author_from_settings

BASE_DIR = Path(__file__).resolve().parent
MAX_BYTES = 50 * 1024 * 1024


def _code_version() -> str:
    """A fingerprint of the program files (the engine and this page). Templates are re-read for
    every PDF, program files only when the page starts: after an update the running page would
    still use the old engine (and reject templates that need its new parts)."""
    import hashlib
    from invoice_engine.admission import engine_version
    return engine_version() + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:8]


CODE_AT_START = _code_version()
RESTART = ("The program files were updated after this test page was started, so it still runs the old "
           "engine. Close the window where gui.py runs (or press Ctrl+C there) and start it again: "
           "python gui.py")


def _outdated() -> bool:
    try:
        return _code_version() != CODE_AT_START
    except OSError:
        return False
_lock = threading.Lock()          # one PDF at a time: simple and predictable


def _settings(config_dir: Path) -> dict:
    import yaml
    path = config_dir / "settings.yml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}


_stores: dict = {}


def _store(config_dir: Path) -> SampleStore | None:
    """The sample store of process_invoice.py (same settings), or None when checks are off. One
    store object is shared by the requests and the background AI jobs; it is read again from disk
    when another program (process_invoice.py) changed it."""
    settings = _settings(config_dir)
    if not settings.get("automatic_checks", True):
        return None
    folder = Path(settings.get("sample_store") or "samples")
    folder = folder if folder.is_absolute() else BASE_DIR / folder
    index = folder / "index.json"
    cached = _stores.get(str(folder))
    if cached is not None:
        current = index.stat().st_mtime if index.is_file() else None
        if current == cached.mtime:
            return cached
    store = SampleStore(folder, int(settings.get("samples_per_layout") or 20))
    _stores[str(folder)] = store
    return store


def _room(store, catalog, config_dir: Path):
    """The waiting room (documents collected for the AI), or None when the AI works at once."""
    from invoice_engine.ai_provider import AISettings
    from invoice_engine.templates import family_of
    from invoice_engine.waiting import WaitingRoom
    ai = AISettings.from_settings(_settings(config_dir))
    if not ai.enabled or int(ai.batch_size or 0) <= 0:
        return None
    return WaitingRoom(store, ai.batch_size, ai.max_wait_days,
                       {family_of(t): t.get("issuer") for t in catalog.templates})


class _Worker(threading.Thread):
    """Runs the due AI jobs in the background: after every upload, and every few minutes (a group
    can become due by waiting). The page keeps reading uploads meanwhile."""
    INTERVAL = 300

    def __init__(self):
        super().__init__(daemon=True, name="ai-jobs")
        self.signal = threading.Event()
        self.forced: set[str] = set()
        self.force_all = False
        self.running: dict | None = None
        self.last_error = ""
        self.types_dir: Path = BASE_DIR / "document_types"
        self.config_dir: Path = BASE_DIR / "config"

    def wake(self, groups: set[str] = (), everything: bool = False) -> None:
        self.forced |= set(groups)
        self.force_all = self.force_all or everything
        self.signal.set()

    def run(self) -> None:
        while True:
            self.signal.wait(self.INTERVAL)
            self.signal.clear()
            try:
                self._once()
            except Exception as exc:  # the worker must never die
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"AI jobs: stopped on {self.last_error}", flush=True)
            finally:
                self.running = None

    def _once(self) -> None:
        from invoice_engine import jobs
        with _lock:
            catalog = load_catalog(self.types_dir, self.config_dir)
            store = _store(self.config_dir)
            if store is None:
                return
            author, problems = author_from_settings(_settings(self.config_dir), self.types_dir, self.config_dir, store)
            room = _room(store, catalog, self.config_dir) if author is not None else None
            if room is None:
                return
            forced, everything = self.forced, self.force_all
            self.forced, self.force_all = set(), False
            due = room.due(force=everything, setup=author._setup())
            due += [g for g in forced if g not in due and room.members(g)]
        for group in due:
            self.running = {"group": group, "started": time.strftime("%H:%M:%S"),
                            "documents": [m["name"] for m in room.members(group)]}
            jobs.run_due(store, room, author, self.types_dir, self.config_dir, groups=[group], lock=_lock,
                         progress=lambda text: print(text, flush=True))


WORKER = _Worker()


def waiting_report(types_dir: Path, config_dir: Path) -> dict:
    """The documents waiting for the AI, the job running now and the last jobs."""
    from invoice_engine import jobs
    with _lock:
        catalog = load_catalog(types_dir, config_dir)
        store = _store(config_dir)
        if store is None:
            return {"error": "automatic checks are switched off in settings.yml: there is no sample store"}
        room = _room(store, catalog, config_dir)
        if room is None:
            return {"error": "the AI step is off, or ai.batch_size is 0 (the AI works while a document is read)"}
        groups = [{"group": g, "status": room.status_line(m[0]), "kind": m[0]["kind"],
                   "documents": [{"name": e["name"], "pages": e["pages"], "status": e.get("status") or e["kind"],
                                  "since": e.get("added", ""), "reason": e.get("reason", "")} for e in m]}
                  for g, m in room.groups().items()]
        history = [dict(r, line=jobs.describe(r)) for r in reversed(room.data.get("jobs", [])[-20:])]
        return {"groups": groups, "jobs": history, "running": WORKER.running, "error_last": WORKER.last_error,
                "batch_size": room.batch_size, "max_wait_days": room.max_wait.days}


def run_pdf(data: bytes, filename: str, types_dir: Path, config_dir: Path, retry: bool = False) -> dict:
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename).name)[:120] or "upload.pdf"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"
    with _lock, tempfile.TemporaryDirectory(prefix="pdfgui_") as folder:
        path = Path(folder) / safe_name
        path.write_bytes(data)
        started = time.perf_counter()
        catalog = load_catalog(types_dir, config_dir)             # picks up template edits
        store = _store(config_dir)
        if store is None:
            result = process_pdf(path, catalog)
        else:                                                      # same automatic checks as the CLI
            author, ai_problems = author_from_settings(_settings(config_dir), types_dir, config_dir, store,
                                                       retry=retry)
            room = _room(store, catalog, config_dir) if author is not None else None
            pipeline = Pipeline(catalog, store, author=author,
                                reload=lambda: load_catalog(types_dir, config_dir),
                                progress=lambda text: print(text, flush=True),   # in the console window
                                waiting=room)
            result = pipeline.process(path)
            if room is not None:
                from invoice_engine.samples import file_hash
                digest = file_hash(path)
                mine = {e["group"] for e in room.documents if e["pdf"] == digest}
                WORKER.wake(mine if retry else ())
                if mine:
                    result.setdefault("notes", []).append(
                        "the AI works on waiting documents in the background"
                        + (": this file's group(s) were sent now" if retry else
                           " (see 'Waiting for the AI'; drop the file again later to see the new result)"))
            for problem in ai_problems:
                result.setdefault("notes", []).append(f"AI step switched on but cannot run: {problem}")
            for event in pipeline.events:
                text = f"template check: {event.template} {event.state}"
                if event.problems:
                    text += ": " + "; ".join(event.problems[:3])
                result.setdefault("notes", []).append(text)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        result["templates_loaded"] = len(catalog.templates)
        result["templates_rejected"] = catalog.rejected
    for unrec in result.get("unrecognized_pages", []):
        unrec["text"] = unrec.get("text", "")[:20000]
    return json.loads(json.dumps(result, default=json_default))


def variant_report(types_dir: Path, config_dir: Path) -> dict:
    """The variant coverage of every layout (same as process_invoice.py --variants)."""
    from dataclasses import asdict
    from invoice_engine.variants import coverage, exception_problems
    with _lock:
        catalog = load_catalog(types_dir, config_dir)
        store = _store(config_dir)
        if store is None:
            return {"error": "automatic checks are switched off in settings.yml: there is no sample store"}
        return {"layouts": [asdict(c) for c in coverage(catalog, store)], "warnings": exception_problems(catalog)}


def trust_template(name: str, types_dir: Path, config_dir: Path) -> dict:
    """A person checked the template's text values: stop the AI's second opinion (same as
    process_invoice.py --trust)."""
    from invoice_engine import second_opinion
    from invoice_engine.admission import template_hash
    with _lock:
        catalog = load_catalog(types_dir, config_dir)
        store = _store(config_dir)
        if store is None:
            return {"error": "automatic checks are switched off in settings.yml"}
        template = next((t for t in catalog.templates if t["template_name"] == name), None)
        if template is None:
            return {"error": f"{name} is not a usable template"}
        second_opinion.trust(store, name, template_hash(catalog, template))
        return {"template": name, "trusted": True}


def accept_template(name: str, types_dir: Path, config_dir: Path) -> dict:
    """Confirm a deliberate change of values (same as process_invoice.py --accept). The template
    must still pass the stress and overlap checks."""
    with _lock:
        catalog = load_catalog(types_dir, config_dir)
        store = _store(config_dir)
        if store is None:
            return {"error": "automatic checks are switched off in settings.yml"}
        if name not in {t["template_name"] for t in catalog.templates}:
            return {"error": f"{name} is not a usable template"}
        event = Pipeline(catalog, store).accept(name)
        return {"template": name, "state": event.state, "problems": event.problems, "notes": event.notes}


class Handler(BaseHTTPRequestHandler):
    types_dir: Path = BASE_DIR / "document_types"
    config_dir: Path = BASE_DIR / "config"

    def log_message(self, fmt, *args):            # keep the console quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/version":
            body = {"folder": str(BASE_DIR), "code": CODE_AT_START, "outdated": _outdated()}
            self._send(200, json.dumps(body).encode("utf-8"), "application/json")
        elif self.path == "/waiting":
            try:
                answer = waiting_report(self.types_dir, self.config_dir)
                self._send(200, json.dumps(answer, ensure_ascii=False, default=str).encode("utf-8"), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")
        elif self.path == "/variants" and _outdated():
            self._send(409, json.dumps({"error": RESTART}).encode("utf-8"), "application/json")
        elif self.path == "/variants":
            try:
                answer = variant_report(self.types_dir, self.config_dir)
                self._send(200, json.dumps(answer, ensure_ascii=False).encode("utf-8"), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if _outdated():
            self._send(409, json.dumps({"error": RESTART}).encode("utf-8"), "application/json")
            return
        if self.path == "/ai-now":
            WORKER.wake(everything=True)
            self._send(200, json.dumps({"started": True}).encode("utf-8"), "application/json")
            return
        if self.path.startswith("/trust"):
            from urllib.parse import parse_qs, urlparse
            name = (parse_qs(urlparse(self.path).query).get("template") or [""])[0]
            try:
                answer = trust_template(name, self.types_dir, self.config_dir)
                self._send(200, json.dumps(answer, ensure_ascii=False).encode("utf-8"), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")
            return
        if self.path.startswith("/accept"):
            from urllib.parse import parse_qs, urlparse
            name = (parse_qs(urlparse(self.path).query).get("template") or [""])[0]
            try:
                answer = accept_template(name, self.types_dir, self.config_dir)
                self._send(200, json.dumps(answer, ensure_ascii=False).encode("utf-8"), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")
            return
        if self.path != "/process":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._send(413, json.dumps({"error": f"file must be 1 byte to {MAX_BYTES // 2**20} MB"}).encode(),
                       "application/json")
            return
        data = self.rfile.read(length)
        name = self.headers.get("X-Filename", "upload.pdf")
        try:
            from urllib.parse import unquote
            result = run_pdf(data, unquote(name), self.types_dir, self.config_dir,
                             retry=self.headers.get("X-AI-Retry") == "1")
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json")
        except Exception as exc:
            self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF Parser Test Bench</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--quiet:#5f6877;--line:#e3e6eb;--accent:#2f6fde;
--ok:#1f8a4c;--okbg:#e6f4ec;--check:#a46a00;--checkbg:#fdf3dc;--fail:#c0392b;--failbg:#fbe9e7;--info:#5f6877;--code:#f1f3f6}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2027;--ink:#e6e9ee;--quiet:#9aa3b2;--line:#2c323c;
--accent:#6c9cf0;--ok:#5cc98a;--okbg:#17301f;--check:#e3b24f;--checkbg:#352a12;--fail:#f07a6c;--failbg:#3a1d1a;--code:#232832}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:20px 24px 8px;max-width:1200px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}header p{margin:0;color:var(--quiet)}
main{max-width:1200px;margin:0 auto;padding:12px 24px 48px}
#drop{display:block;margin-top:8px;border:2px dashed var(--line);border-radius:12px;background:var(--card);padding:28px;text-align:center;cursor:pointer;transition:.15s}
#drop.over{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--card))}
#drop b{color:var(--accent)}#drop small{display:block;color:var(--quiet);margin-top:6px}
.file{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-top:18px;overflow:hidden}
.filehead{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}
.filehead h2{font-size:16px;margin:0;flex:1 1 auto;word-break:break-all}.meta{color:var(--quiet);font-size:13px}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:12px;letter-spacing:.02em}
.OK{color:var(--ok);background:var(--okbg)}.CHECK{color:var(--check);background:var(--checkbg)}.FAIL{color:var(--fail);background:var(--failbg)}
.NONE{color:var(--quiet);background:var(--code)}
.body{padding:6px 18px 16px}.notes{margin:10px 0;padding:10px 12px;background:var(--code);border-radius:8px;font-size:13px}
.notes div{margin:2px 0}.doc{border:1px solid var(--line);border-radius:10px;margin:14px 0;overflow:hidden}
.dochead{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;padding:10px 14px;background:var(--code)}
.dochead strong{font-size:15px}.evidence{color:var(--quiet);font-size:12.5px}
.issues{padding:8px 14px 0}.issue{font-size:13px;margin:3px 0;display:flex;gap:8px}.issue .lv{font-weight:600;min-width:46px}
.lv.ERROR{color:var(--fail)}.lv.WARN{color:var(--check)}.lv.INFO{color:var(--info)}
.tabs{display:flex;gap:4px;padding:10px 14px 0;border-bottom:1px solid var(--line)}
.tab{border:0;background:none;color:var(--quiet);padding:7px 12px;border-bottom:2px solid transparent;cursor:pointer;font:inherit}
.tab.on{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.pane{display:none;padding:10px 14px 14px;overflow-x:auto}.pane.on{display:block}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--quiet);font-weight:600;white-space:nowrap;position:sticky;top:0;background:var(--card)}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.k{white-space:nowrap;color:var(--quiet)}
td.pg{color:var(--quiet);text-align:center}pre{background:var(--code);padding:10px;border-radius:8px;overflow:auto;max-height:420px;font-size:12px;margin:0}
.unrec{border:1px dashed var(--line);border-radius:10px;margin:14px 0;padding:10px 14px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap}.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;font:inherit;font-size:13px}
.btn:hover{border-color:var(--accent)}.spin{color:var(--quiet);padding:16px 18px}.err{color:var(--fail);padding:14px 18px}
@media (max-width:640px){header,main{padding-left:16px;padding-right:16px}}
</style></head><body>
<header><h1>PDF Parser Test Bench</h1><p>Runs on this computer only. Drop PDFs to see what the engine extracts, which template it used, and why.
<button class="btn" id="variants" style="margin-left:8px">Variant coverage</button>
<button class="btn" id="waiting">Waiting for the AI</button></p>
<p id="ver" class="meta" style="margin-top:4px"></p></header>
<main>
<label id="drop"><input id="pick" type="file" accept="application/pdf,.pdf" multiple hidden>
<b>Drop PDF files here</b> or click to choose<small>Templates are re-read for every file, so template edits apply immediately. Documents read OK are kept in the sample store; documents that need the AI wait in the waiting room.</small></label>
<div id="out"></div>
</main>
<script>
const out=document.getElementById('out'),drop=document.getElementById('drop'),pick=document.getElementById('pick');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const isNum=v=>typeof v==='number';
const fmt=v=>Array.isArray(v)?v.map(fmt).join('; '):isNum(v)?(Number.isInteger(v)?String(v):v.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:6})):esc(v);
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',ev=>run([...ev.dataTransfer.files]));pick.addEventListener('change',()=>{run([...pick.files]);pick.value=''});
fetch('/version').then(r=>r.json()).then(v=>{document.getElementById('ver').textContent=`Program files ${v.code} from ${v.folder}`+(v.outdated?' (UPDATED since start: restart gui.py)':'')}).catch(()=>{});
document.getElementById('variants').onclick=async()=>{
 const box=document.createElement('section');box.className='file';box.innerHTML='<div class="spin">Reading the sample store…</div>';out.prepend(box);
 try{const r=await fetch('/variants');const d=await r.json();if(!r.ok||d.error)throw new Error(d.error||r.statusText);
  const L=d.layouts,need=L.filter(x=>!x.samples||x.to_collect.length).length;
  const seen=x=>Object.entries(x.seen).map(([k,n])=>`${esc(k)} (${n})`).join(', ')||'-';
  box.innerHTML=`<div class="filehead"><h2>Variant coverage</h2><span class="meta">${L.length} layouts · ${need} need more documents · real documents read OK, from the sample store</span></div><div class="body">
  ${(d.warnings||[]).map(w=>`<div class="notes" style="border-left:3px solid var(--check)">${esc(w)}</div>`).join('')}
  <div style="overflow-x:auto"><table><tr><th>Layout</th><th>Samples</th><th>Seen</th><th>To collect</th><th>If available</th><th>Cannot read</th><th>Not expected</th><th>Recent problems</th></tr>
  ${L.map(x=>`<tr><td class="k">${esc(x.family)}</td><td class="num">${x.samples}</td><td>${seen(x)}</td>
   <td><b>${esc((x.samples?[]:['any document']).concat(x.to_collect).join(', '))}</b></td><td>${esc(x.if_available.join(', '))}</td>
   <td style="color:var(--fail)">${esc(x.cannot_read.join(', '))}</td><td>${Object.entries(x.not_expected).map(([k,v])=>`<span title="${esc(v)}">${esc(k)}</span>`).join(', ')}</td>
   <td>${x.problems.slice(-3).map(p=>`<div><span class="pill ${p.status}">${p.status}</span> ${esc(p.name)}: ${esc(p.reason.slice(0,90))}</div>`).join('')}</td></tr>`).join('')}
  </table></div><div class="meta" style="margin-top:8px">Cannot read: the template has no fields for that kind; such a document fails and goes to the AI repair. Not expected: listed in document_types/&lt;type&gt;/variants.yml.</div></div>`}
 catch(e){box.innerHTML=`<div class="err">${esc(e.message)}</div>`}};
document.getElementById('waiting').onclick=()=>waitingPanel();
async function waitingPanel(box){
 if(!box){box=document.createElement('section');box.className='file';out.prepend(box)}
 box.innerHTML='<div class="spin">Reading the waiting room…</div>';
 try{const r=await fetch('/waiting');const d=await r.json();if(!r.ok||d.error)throw new Error(d.error||r.statusText);
  const G=d.groups,n=G.reduce((a,g)=>a+g.documents.length,0);
  box.innerHTML=`<div class="filehead"><h2>Waiting for the AI</h2><span class="meta">${n} document(s) in ${G.length} group(s) · sent when ${d.batch_size} of a layout are collected or after ${d.max_wait_days} days</span>
  <span class="toolbar"><button class="btn" data-now>Send all to the AI now</button><button class="btn" data-again>Refresh</button></span></div><div class="body">
  ${d.running?`<div class="notes" style="border-left:3px solid var(--accent)"><b>Working now</b> (since ${esc(d.running.started)}): ${esc(d.running.group)}: ${esc(d.running.documents.join(', '))}</div>`:''}
  ${d.error_last?`<div class="notes" style="border-left:3px solid var(--fail)">Last background error: ${esc(d.error_last)}</div>`:''}
  ${G.length?G.map(g=>`<div class="unrec"><b>${esc(g.group)}</b><div class="meta" style="margin:4px 0 6px">${esc(g.status)}</div>
   <table><tr><th>Document</th><th>Pages</th><th>Read as</th><th>Since</th><th>Reason</th></tr>${g.documents.map(x=>`<tr><td>${esc(x.name)}</td><td class="pg">${x.pages.join(',')}</td><td><span class="pill ${x.status==='FAIL'?'FAIL':x.status==='OK'?'OK':'NONE'}">${esc(x.status)}</span></td><td class="k">${esc(String(x.since).slice(0,16).replace('T',' '))}</td><td>${esc(x.reason)}</td></tr>`).join('')}</table></div>`).join(''):'<div class="notes">No document is waiting.</div>'}
  <h3 style="font-size:14px;margin:16px 0 6px">Last AI jobs</h3>${d.jobs.length?`<table><tr><th>When</th><th>Group</th><th>Result</th></tr>${d.jobs.map(j=>`<tr><td class="k">${esc(String(j.when||'').slice(0,16).replace('T',' '))}</td><td class="k">${esc(j.group)}</td><td>${esc(j.line)}${(j.reread||[]).length?`<div class="meta">${j.reread.map(x=>`${esc(x.name)}: ${esc(x.status)}`).join(' · ')}</div>`:''}</td></tr>`).join('')}</table>`:'<div class="meta">None yet.</div>'}</div>`;
  box.querySelector('[data-again]').onclick=()=>waitingPanel(box);
  box.querySelector('[data-now]').onclick=async()=>{await fetch('/ai-now',{method:'POST'});setTimeout(()=>waitingPanel(box),1500)}}
 catch(e){box.innerHTML=`<div class="err">${esc(e.message)}</div>`}}
async function run(files){for(const f of files.filter(f=>/\.pdf$/i.test(f.name)||f.type==='application/pdf'))await one(f)}
async function one(file,retry,box){
 if(!box){box=document.createElement('section');box.className='file';out.prepend(box)}
 box.file=file;
 box.innerHTML=`<div class="filehead"><h2>${esc(file.name)}</h2><span class="meta">${(file.size/1024).toFixed(0)} KB</span></div><div class="spin">${retry?'Sending to the AI again… (this can take several minutes)':'Processing…'}</div>`;
 try{const r=await fetch('/process',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name),'X-AI-Retry':retry?'1':'0'},body:file});
  const data=await r.json();if(!r.ok||data.error)throw new Error(data.error||r.statusText);render(box,file.name,data)}
 catch(e){box.querySelector('.spin').outerHTML=`<div class="err">${esc(e.message)}</div>`}}
function render(box,name,d){
 const docs=d.documents||[],un=d.unrecognized_pages||[];
 const counts=['OK','CHECK','FAIL'].map(s=>[s,docs.filter(x=>x.status===s).length]).filter(x=>x[1]);
 let h=`<div class="filehead"><h2>${esc(name)}</h2>${counts.map(([s,n])=>`<span class="pill ${s}">${n} ${s}</span>`).join('')}${un.length?`<span class="pill NONE">${un.length} ${un.every(u=>u.status==='PENDING')?'PENDING':'unrecognized'}</span>`:''}
 <span class="meta">${d.page_count} page(s) · ${d.elapsed_ms} ms · ${d.templates_loaded} templates</span>
 <span class="toolbar"><button class="btn" data-dl>Download JSON</button></span></div><div class="body">`;
 const rej=Object.entries(d.templates_rejected||{});
 if(rej.length)h+=`<div class="notes"><b>Rejected templates</b>${rej.map(([n,p])=>`<div>${esc(n)}: ${esc(p.join('; '))}</div>`).join('')}</div>`;
 (d.held_templates||[]).forEach(t=>{h+=`<div class="notes" style="border-left:3px solid var(--check)"><b>Template ${esc(t.template)} is held</b> (switched off by the automatic checks, so its pages are not read)${t.problems.map(p=>`<div>• ${esc(p)}</div>`).join('')}${t.changed_values?`<div style="margin-top:6px">It reads values differently from earlier documents. If the new values above are the correct ones (for example a template fix), confirm them: <button class="btn" data-accept="${esc(t.template)}">Accept new values</button></div>`:'<div style="margin-top:6px">The template needs a fix before it can be used again.</div>'}</div>`});
 const notes=(d.notes||[]).filter(n=>!/is held by the automatic checks/.test(n));
 if(notes.length)h+=`<div class="notes"><b>Notes</b>${notes.map(n=>`<div>${esc(n)}</div>`).join('')}</div>`;
 docs.forEach((doc,i)=>h+=docHtml(doc,i));
 un.forEach(u=>h+=`<div class="unrec"><b>${u.status==='PENDING'?'<span class="pill NONE">PENDING</span> ':''}Pages ${u.pages.join(', ')}: ${esc(u.reason||'no template matches')}</b>${u.waiting?`<div class="meta" style="margin-top:4px">${esc(u.waiting)}</div>`:''}
  <div class="meta" style="margin:4px 0 8px">${esc((u.preview||[]).join(' | '))}</div>
  <div style="margin:0 0 8px"><button class="btn" data-retry>Send to the AI now</button> <span class="meta">hands this file's group to the AI at once, without waiting for more documents of the layout</span></div><details><summary>Page text (for writing a template)</summary><pre>${esc(u.text||'')}</pre></details></div>`);
 if(!docs.length&&!un.length)h+='<div class="notes">No pages were read.</div>';
 box.innerHTML=h+'</div>';
 box.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>one(box.file,true,box));
 box.querySelectorAll('[data-trust]').forEach(b=>b.onclick=async()=>{b.disabled=true;
  try{const r=await fetch('/trust?template='+encodeURIComponent(b.dataset.trust),{method:'POST'});const a=await r.json();
   b.outerHTML=a.error?`<span class="err">${esc(a.error)}</span>`:'<b>Trusted.</b> Drop the PDF again to read it without the second opinion.'}
  catch(e){b.outerHTML=`<span class="err">${esc(e.message)}</span>`}});
 box.querySelectorAll('[data-accept]').forEach(b=>b.onclick=async()=>{b.disabled=true;b.textContent='Checking…';
  try{const r=await fetch('/accept?template='+encodeURIComponent(b.dataset.accept),{method:'POST'});const a=await r.json();
   b.outerHTML=a.error?`<span class="err">${esc(a.error)}</span>`:a.state==='admitted'?`<b>Accepted.</b> Drop the PDF again to read it with this template.`:`<span class="err">Still held: ${esc((a.problems||[]).join('; '))}</span>`}
  catch(e){b.outerHTML=`<span class="err">${esc(e.message)}</span>`}});
 box.querySelector('[data-dl]').onclick=()=>{const b=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download=name.replace(/\.pdf$/i,'')+'.json';a.click();URL.revokeObjectURL(a.href)};
 box.querySelectorAll('.doc').forEach(el=>el.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  el.querySelectorAll('.tab,.pane').forEach(x=>x.classList.remove('on'));t.classList.add('on');el.querySelector(`.pane[data-p="${t.dataset.p}"]`).classList.add('on')}))}
function docHtml(doc,i){
 const f=doc.fields||{},ev=doc.evidence||{},pages=doc.field_pages||{},lines=doc.lines||[];
 const issues=(doc.issues||[]).filter(x=>x.level!=='INFO'),info=(doc.issues||[]).filter(x=>x.level==='INFO');
 let h=`<div class="doc"><div class="dochead"><span class="pill ${doc.status}">${doc.status}</span><strong>${esc(f.document_type||'document')} ${esc(fmt(f.invoice_number))}</strong>
 <span class="meta">pages ${doc.pages.join('–')} · ${esc(doc.type||'')} · ${esc(doc.template)}${doc.version>1?' v'+doc.version:''}${doc.copy_of?' · copy of pages '+doc.copy_of.join(','):''}</span>
 <span class="evidence">${ev.checks_passed??0} checks passed${ev.checks_trivial?`, ${ev.checks_trivial} trivial`:''} · ${ev.fields_found??0}/${ev.fields_defined??0} fields found</span></div>`;
 if(issues.length)h+=`<div class="issues">${issues.map(x=>`<div class="issue"><span class="lv ${x.level}">${x.level}</span><span>${esc(x.message)}</span></div>`).join('')}</div>`;
 if(issues.some(x=>x.level==='WARN'&&/^second opinion:/.test(x.message)))h+=`<div class="issues"><div class="issue"><span class="lv INFO">AI</span><span>The AI reads some text fields differently. Open the PDF and compare. If the template's values are right: <button class="btn" data-trust="${esc(doc.template)}">Template values are right</button> (no more second opinions for this template file). If the AI is right, the template needs a fix.</span></div></div>`;
 const cols=[...new Set(lines.flatMap(l=>Object.keys(l)))];
 h+=`<div class="tabs"><button class="tab on" data-p="f${i}">Fields (${Object.keys(f).length})</button><button class="tab" data-p="l${i}">Line items (${lines.length})</button>${(doc.tax_lines||[]).length?`<button class="tab" data-p="t${i}">Tax summary</button>`:''}<button class="tab" data-p="i${i}">Decision log (${info.length})</button></div>`;
 h+=`<div class="pane on" data-p="f${i}"><table><tr><th>Field</th><th>Value</th><th>Page</th></tr>${Object.entries(f).map(([k,v])=>`<tr><td class="k">${esc(k)}</td><td class="${isNum(v)?'num':''}">${fmt(v)}</td><td class="pg">${pages[k]??''}</td></tr>`).join('')}</table></div>`;
 h+=`<div class="pane" data-p="l${i}">${lines.length?`<table><tr><th>#</th>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>${lines.map((l,n)=>`<tr><td class="pg">${n+1}</td>${cols.map(c=>`<td class="${isNum(l[c])?'num':''}">${fmt(l[c])}</td>`).join('')}</tr>`).join('')}</table>`:'<div class="meta">No line items in this template.</div>'}</div>`;
 if((doc.tax_lines||[]).length){const tc=[...new Set(doc.tax_lines.flatMap(l=>Object.keys(l)))];
  h+=`<div class="pane" data-p="t${i}"><table><tr>${tc.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>${doc.tax_lines.map(l=>`<tr>${tc.map(c=>`<td class="${isNum(l[c])?'num':''}">${fmt(l[c])}</td>`).join('')}</tr>`).join('')}</table></div>`}
 h+=`<div class="pane" data-p="i${i}">${info.length?info.map(x=>`<div class="issue"><span class="lv INFO">INFO</span><span>${esc(x.message)}</span></div>`).join(''):'<div class="meta">Only one template matched; nothing else was tried.</div>'}</div>`;
 return h+'</div>'}
</script></body></html>"""


class _Server(ThreadingHTTPServer):
    """The test page's server: the port is its own. Windows lets a second server open a port that
    is already in use (address reuse), and requests can then still reach the first, outdated one."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _already_running(port: int) -> str | None:
    """What answers on this port already: a test page (with its folder), something else, or None."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=2) as answer:
            info = json.loads(answer.read().decode("utf-8"))
            same = info.get("code") == CODE_AT_START
            return (f"started from {info.get('folder')}, "
                    + ("the same program files" if same else "older or other program files"))
    except urllib.error.HTTPError:
        return "an older test page (from before this check existed)"
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Local test page for the PDF parser.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--types", default=str(BASE_DIR / "document_types"))
    parser.add_argument("--config", default=str(BASE_DIR / "config"))
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("invoice2data").setLevel(logging.ERROR)

    config = Path(args.config)
    settings_file = config / "settings.yml"
    if settings_file.is_file():
        import yaml
        settings = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
        safe_regex.install(settings.get("regex_timeout_seconds"))
    try:
        tool = poppler.configure(config)
    except poppler.PopplerError as exc:
        sys.exit(str(exc))
    catalog = load_catalog(Path(args.types), config)
    print(f"Templates: {len(catalog.templates)} usable, {len(catalog.rejected)} rejected; "
          f"pdftotext {tool.version}")

    Handler.types_dir, Handler.config_dir = Path(args.types), config
    WORKER.types_dir, WORKER.config_dir = Path(args.types), config
    running = _already_running(args.port)
    if running:
        sys.exit(f"Another test page is already running on port {args.port} ({running}). On Windows a second "
                 f"one would share the port and the browser could keep talking to the old one. Close the other "
                 f"window where gui.py runs (Ctrl+C there), or stop it with:\n"
                 f"  Get-NetTCPConnection -LocalPort {args.port} -State Listen | ForEach-Object "
                 f"{{ Stop-Process -Id $_.OwningProcess -Force }}\n"
                 f"then start this one again; or use another port: python gui.py --port {args.port + 1}")
    try:
        server = _Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        sys.exit(f"Port {args.port} cannot be opened ({exc}). Close the program that uses it, or start with "
                 f"another port: python gui.py --port {args.port + 1}")
    url = f"http://127.0.0.1:{args.port}"
    WORKER.start()
    WORKER.wake()                                   # groups that became due while the page was closed
    print(f"Test page: {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
