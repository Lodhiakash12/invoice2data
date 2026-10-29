r"""
Local PDF document parser - command line.

The engine (invoice_engine/) contains no issuer-, type- or field-specific code:
  document_types/<type>/   one folder per document type (invoice, tax_document, ...):
      type.yml             routing signals and Excel summary columns
      fields.yml           the fields this type adds to config/fields.yml
      checks.yml           default arithmetic checks of this type
      templates/           one YAML per layout (what to read and where)
  config/fields.yml        fields shared by every type (identity, dates, parties, total)
  config/settings.yml      engine settings (Poppler folder, pattern time limit)

Usage (PowerShell, from the project folder)
  python process_invoice.py                          # every PDF in this folder
  python process_invoice.py Invoice_8626225731.pdf   # one PDF
  python process_invoice.py C:\Work\invoice2data\Docs  # every PDF in another folder
  python process_invoice.py --lint                   # only check the templates
  python process_invoice.py --overlap samples        # do two layouts read the same sample document?
  python process_invoice.py --overlap x.yml samples  # the same, only pairs that involve template x.yml
  python process_invoice.py --fingerprint samples    # write every template's layout from sample PDFs
  python process_invoice.py --fingerprint x.yml a.pdf b.pdf
                                                     # the same, only for template x.yml
  python process_invoice.py --stress samples         # do templates survive small layout variations?
  python process_invoice.py --variants               # which variants each template has read; what to collect
  python process_invoice.py --second-opinion         # the AI reads the stored samples' text fields again
  python process_invoice.py --trust x.yml            # x.yml's text values were checked by a person
  python process_invoice.py --accept x.yml           # confirm a deliberate change of values read by x.yml
  python process_invoice.py --ai-test                # check the AI settings and connection
  python process_invoice.py --ai-models              # list the models of the AI service
  python process_invoice.py --no-ai ...              # a run without the AI step
  python process_invoice.py --debug ...              # also show invoice2data's own log
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from invoice_engine import load_catalog, process_pdf, safe_regex
from invoice_engine import overlap, poppler
from invoice_engine import stress as stress_test
from invoice_engine.fingerprints import build_layouts
from invoice_engine.pipeline import Pipeline
from invoice_engine.samples import SampleStore
from invoice_engine.variants import coverage as variant_coverage, print_report as print_variants, write_excel as write_variants
from invoice_engine.variants import exception_problems as variant_exception_problems
from invoice_engine.ai_author import author_from_settings
from invoice_engine.ai_provider import AIError, AISettings, make_provider
from invoice_engine.export import output_names, print_summary, write_excel, write_json

BASE_DIR = Path(__file__).resolve().parent


def report_templates(catalog) -> None:
    for name, problems in catalog.rejected.items():
        print(f"REJECTED {name}")
        for problem in problems:
            print(f"   - {problem}")
    for name, problems in catalog.warnings.items():
        if name in catalog.rejected:
            continue
        print(f"warning  {name}")
        for problem in problems:
            print(f"   - {problem}")
    counts = ", ".join(f"{dt.name} {len(dt.template_set.templates)}" for dt in catalog.types)
    print(f"Templates: {len(catalog.templates)} usable ({counts}), {len(catalog.rejected)} rejected")


def collect_pdfs(targets: list[str]) -> list[Path]:
    pdfs: list[Path] = []
    for target in targets or [str(BASE_DIR)]:
        path = Path(target)
        if path.is_dir():
            pdfs.extend(sorted(p for p in path.iterdir() if p.suffix.lower() == ".pdf"))
        elif path.is_file() and path.suffix.lower() == ".pdf":
            pdfs.append(path)
        else:
            print(f"Skipped (not a PDF file or folder): {target}")
    return pdfs


def find_template(catalog, name: str):
    name = Path(name).name
    return next((t for t in catalog.templates if t["template_name"] == name), None)


def store_fingerprints(focus: str, pdfs: list[Path], catalog) -> int:
    """Write the layout block of every template (or only `focus`) from the sample PDFs."""
    only = None
    if focus:
        template = find_template(catalog, focus)
        if template is None:
            print(f"{Path(focus).name} is not a usable template in document_types")
            return 1
        only = {template["template_name"]}
    if not pdfs:
        print("No sample PDFs given.")
        return 1
    print(f"Sample PDFs: {len(pdfs)}")
    results = build_layouts(catalog, pdfs, only)
    for r in results:
        if r.written:
            print(f"  layout written  {r.template}: {r.labels} labels from {len(r.samples)} sample(s)")
            for sample in r.samples:
                print(f"                    {sample}")
        elif r.samples:
            print(f"  NOT written     {r.template}: its samples share no label words")
    missing = [r.template for r in results if not r.written]
    have = sum(1 for t in catalog.templates if t.get("layout"))
    print(f"\nTemplates with a layout: {have} of {len(catalog.templates)}")
    if missing:
        print("No sample in these PDFs (layout not built; add a sample PDF and run again):")
        for name in missing:
            print(f"  {name}")
    return 1 if (focus and missing) else 0


def check_overlap(focus_arg: str, pdfs: list[Path], catalog) -> int:
    """Exit code 0 when the layouts are exclusive on these samples, 1 otherwise."""
    focus = Path(focus_arg).name if focus_arg else None
    if focus and find_template(catalog, focus) is None:
        print(f"{focus} is not a usable template in document_types")
        return 1
    if not pdfs:
        print("No sample PDFs given.")
        return 1
    print(f"Sample PDFs: {len(pdfs)}" + (f"   checking pairs with {focus}" if focus else ""))
    report = overlap.check_samples(pdfs, catalog, focus)
    overlap.print_report(report, catalog, focus)
    return 1 if report.failed(focus) else 0


def make_author(ai: AISettings, args, store):
    """The AI template author, or None when the AI step is off or cannot run (says why)."""
    if args.no_ai:
        return None
    author, problems = author_from_settings({"ai": ai.__dict__}, Path(args.types), Path(args.config),
                                            store, retry=args.ai_retry)
    if problems:
        print("AI step is switched on but cannot run:")
        for problem in problems:
            print(f"  - {problem}")
    elif author is not None:
        print(f"AI step: {ai.model} at {ai.base_url} (page text is sent without anonymisation)")
    return author


def ai_tools(ai: AISettings, list_models: bool) -> int:
    print(f"AI settings: enabled={ai.enabled} provider={ai.provider} base_url={ai.base_url} model={ai.model!r}")
    for problem in ai.problems():
        print(f"  - {problem}")
    try:
        provider = make_provider(ai)
        if list_models:
            for model in provider.models():
                print(f"  {model}")
            return 0
        if not ai.model:
            return 1
        answer = provider.complete("Answer with one word.", "Reply with the word: ready")
        print(f"Answer from {ai.model}: {answer.strip()[:80]!r}")
        return 0
    except AIError as exc:
        print(f"AI service error: {exc}")
        return 1


def report_ai(results) -> None:
    print("\nAI template author:")
    for r in results:
        if r.outcome == "admitted":
            print(f"  wrote     {r.template}  ({r.action}, type {r.doc_type}{', NEW TYPE' if r.new_type else ''}, "
                  f"{r.attempts} draft(s), passed the automatic checks)  <- {r.pdf} p{r.pages}")
        elif r.outcome == "failed":
            print(f"  FAILED    {r.pdf} p{r.pages}: {r.action} not accepted after {r.attempts} draft(s)")
            for problem in r.problems[:4]:
                print(f"              - {problem[:150]}")
        else:
            print(f"  skipped   {r.pdf} p{r.pages}: {r.problems[0] if r.problems else ''}")


def sample_folder(settings: dict) -> Path:
    folder = Path(settings.get("sample_store") or "samples")
    return folder if folder.is_absolute() else BASE_DIR / folder


def report_template_checks(pipeline, store) -> list[str]:
    """Print the template checks decided in this run; returns the templates held."""
    final = {e.template: e for e in pipeline.events if e.state != "new"}   # last decision per template
    if final:
        print("\nAutomatic template checks:")
    for event in sorted(final.values(), key=lambda e: (e.state != "held", e.template)):
        if event.state == "admitted":
            print(f"  admitted  {event.template}  ({event.samples} sample(s))")
        else:
            print(f"  HELD      {event.template}  (not used until a corrected version passes)")
            for problem in event.problems[:6]:
                print(f"              - {problem[:150]}")
    held = [n for n, s in pipeline.state.items() if s == "held"]
    waiting = sorted(n for n, s in pipeline.state.items() if s == "new")
    if held and not pipeline.events:
        print("\nTemplates held by the automatic checks: " + ", ".join(sorted(held)))
    print(f"Sample store: {len(store.data['samples'])} document(s) in {store.folder}"
          + (f"; {len(waiting)} template(s) waiting for a first sample" if waiting else ""))
    return held


def report_variants(catalog, settings: dict, focus: str, out_dir: Path) -> int:
    store = SampleStore(sample_folder(settings), int(settings.get("samples_per_layout") or 20))
    report = variant_coverage(catalog, store)
    if focus:
        wanted = Path(focus).stem
        report = [r for r in report if r.family == wanted or wanted in {Path(t).stem for t in r.templates}]
        if not report:
            print(f"{focus} is not a usable template in document_types")
            return 1
    print_variants(report, variant_exception_problems(catalog))
    if not focus:
        path = write_variants(report, out_dir / "variants.xlsx")
        print(f"Written: {path}  (sheet 'To collect' is the list for the testing team)")
    return 0


def trust_template(catalog, settings: dict, name: str) -> int:
    from invoice_engine.admission import template_hash
    from invoice_engine import second_opinion
    template = find_template(catalog, name)
    if template is None:
        print(f"{Path(name).name} is not a usable template in document_types")
        return 1
    store = SampleStore(sample_folder(settings), int(settings.get("samples_per_layout") or 20))
    second_opinion.trust(store, template["template_name"], template_hash(catalog, template))
    print(f"{template['template_name']}: its text values are trusted; no second opinion until the file changes")
    return 0


def second_opinion_on_samples(catalog, settings: dict, ai: AISettings, args) -> int:
    from invoice_engine import second_opinion
    only = None
    if args.second_opinion:
        template = find_template(catalog, args.second_opinion)
        if template is None:
            print(f"{Path(args.second_opinion).name} is not a usable template in document_types")
            return 1
        only = template["template_name"]
    store = SampleStore(sample_folder(settings), int(settings.get("samples_per_layout") or 20))
    author, problems = author_from_settings({"ai": ai.__dict__}, Path(args.types), Path(args.config), store)
    if author is None:
        print("The second opinion needs the AI step: " + ("; ".join(problems) or "ai.enabled is false in "
                                                         "config/settings.yml"))
        return 1
    per_template = max(1, int(ai.second_opinion or 3))
    print(f"Second opinion on stored samples (at most {per_template} per template; model {ai.model})\n")
    report = second_opinion.check_samples(catalog, store, author, only, per_template)
    for item in report:
        verdict = "not available" if item["error"] else ("agrees" if item["agreed"] else "DIFFERS")
        print(f"  {verdict:<13} {item['template']}  {item['document']} p{','.join(map(str, item['pages']))}")
        for line in ([item["error"]] if item["error"] else [f for f in item["findings"] if "differs" in f or "read nothing" in f]):
            print(f"                - {line[:170]}")
    differs = sum(1 for i in report if not i["error"] and not i["agreed"])
    print(f"\n{len(report)} document(s) asked; {differs} with differences"
          + ("" if not differs else ": check them; when the template is right, python process_invoice.py --trust <template>"))
    return 1 if differs else 0


def read_settings(config_dir: Path) -> dict:
    path = config_dir / "settings.yml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse PDF documents with template-driven document types.")
    parser.add_argument("inputs", nargs="*", help="PDF files or folders (default: this folder)")
    parser.add_argument("--types", default=str(BASE_DIR / "document_types"), help="document types folder")
    parser.add_argument("--config", default=str(BASE_DIR / "config"), help="folder with fields.yml and settings.yml")
    parser.add_argument("--output", default=str(BASE_DIR / "output"), help="output folder")
    parser.add_argument("--lint", action="store_true", help="only check the templates and exit")
    parser.add_argument("--debug", action="store_true", help="show invoice2data's own log")
    parser.add_argument("--fingerprint", nargs="?", const="", metavar="TEMPLATE",
                        help="write the layout block of every template (or only TEMPLATE) from the "
                             "sample PDFs given as inputs")
    parser.add_argument("--overlap", nargs="?", const="", metavar="TEMPLATE",
                        help="check that no two layouts read the same sample document (optionally only "
                             "pairs that involve TEMPLATE); the PDFs given as inputs are the samples")
    parser.add_argument("--stress", nargs="?", const="", metavar="TEMPLATE",
                        help="rewrite each sample document in ways real variants differ and check that "
                             "every template (or only TEMPLATE) still reads the same values")
    parser.add_argument("--accept", metavar="TEMPLATE",
                        help="confirm a deliberate correction: check TEMPLATE again, allowing the values "
                             "it reads on its stored samples to change")
    parser.add_argument("--variants", nargs="?", const="", metavar="TEMPLATE",
                        help="show which variants (IGST / CGST+SGST, several items, several pages ...) each "
                             "template (or only TEMPLATE) has read on real documents, and which documents to "
                             "collect; also writes variants.xlsx in the output folder")
    parser.add_argument("--trust", metavar="TEMPLATE",
                        help="a person checked the text values TEMPLATE reads and they are right: stop the "
                             "AI's second opinion on its documents (until the template file changes)")
    parser.add_argument("--second-opinion", nargs="?", const="", metavar="TEMPLATE",
                        help="let the AI read the text fields of the stored samples of every template (or "
                             "only TEMPLATE) on its own and report where it reads them differently")
    parser.add_argument("--no-ai", action="store_true", help="do not use the AI step in this run")
    parser.add_argument("--ai-retry", action="store_true",
                        help="give documents to the AI again even if it could not handle them before")
    parser.add_argument("--ai-test", action="store_true", help="check the AI settings and connection, then exit")
    parser.add_argument("--ai-models", action="store_true", help="list the models of the AI service, then exit")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    level = logging.DEBUG if args.debug else logging.ERROR
    logging.basicConfig(level=level)
    logging.getLogger("invoice2data").setLevel(level)

    settings = read_settings(Path(args.config))
    if not safe_regex.install(settings.get("regex_timeout_seconds")):
        print("warning: this invoice2data version cannot be given a pattern time limit")
    catalog = load_catalog(Path(args.types), Path(args.config))
    report_templates(catalog)
    if args.lint:
        return 1 if catalog.rejected else 0
    if args.variants is not None:
        return report_variants(catalog, settings, args.variants, Path(args.output))
    try:
        tool = poppler.configure(Path(args.config))
    except poppler.PopplerError as exc:
        sys.exit(str(exc))
    print(f"pdftotext: {tool.path} (version {tool.version}, from {tool.source})")
    if args.overlap is not None:
        focus, inputs = args.overlap, list(args.inputs)
        if focus and Path(focus).suffix.lower() not in (".yml", ".yaml", ".json"):
            focus, inputs = "", [focus] + inputs         # "--overlap samples": a folder, not a template
        return check_overlap(focus, collect_pdfs(inputs), catalog)
    ai = AISettings.from_settings(settings)
    if args.ai_models or args.ai_test:
        return ai_tools(ai, args.ai_models)
    if args.trust:
        return trust_template(catalog, settings, args.trust)
    if args.second_opinion is not None:
        return second_opinion_on_samples(catalog, settings, ai, args)
    if args.accept:
        template = find_template(catalog, args.accept)
        if template is None:
            print(f"{Path(args.accept).name} is not a usable template in document_types")
            return 1
        settings_store = SampleStore(sample_folder(settings), int(settings.get("samples_per_layout") or 20))
        event = Pipeline(catalog, settings_store).accept(template["template_name"])
        print(f"{event.template}: {event.state} ({event.samples} sample(s))")
        for line in event.problems + event.notes:
            print(f"  - {line[:160]}")
        return 0 if event.state == "admitted" else 1
    if args.stress is not None:
        focus, inputs = args.stress, list(args.inputs)
        if focus and Path(focus).suffix.lower() not in (".yml", ".yaml", ".json"):
            focus, inputs = "", [focus] + inputs
        only = None
        if focus:
            template = find_template(catalog, focus)
            if template is None:
                print(f"{Path(focus).name} is not a usable template in document_types")
                return 1
            only = {template["template_name"]}
        pdfs = collect_pdfs(inputs)
        print(f"Sample PDFs: {len(pdfs)}\n")
        reports = stress_test.stress(catalog, pdfs, only)
        stress_test.print_report(reports)
        return 1 if any(o.result == "WRONG" for r in reports for o in r.outcomes) else 0
    if args.fingerprint is not None:
        focus, inputs = args.fingerprint, list(args.inputs)
        if focus and Path(focus).suffix.lower() not in (".yml", ".yaml", ".json"):
            focus, inputs = "", [focus] + inputs         # "--fingerprint samples": a folder
        return store_fingerprints(focus, collect_pdfs(inputs), catalog)
    if not catalog.templates:
        sys.exit("No usable templates.")

    pdfs = collect_pdfs(args.inputs)
    if not pdfs:
        sys.exit("No PDF files to process.")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"PDFs: {len(pdfs)}")

    store = SampleStore(sample_folder(settings), int(settings.get("samples_per_layout") or 20))
    author = make_author(ai, args, store) if settings.get("automatic_checks", True) else None
    reload = lambda: load_catalog(Path(args.types), Path(args.config))   # noqa: E731
    pipeline = (Pipeline(catalog, store, author=author, reload=reload, progress=lambda text: print(text, flush=True))
                if settings.get("automatic_checks", True) else None)
    if pipeline:
        pipeline.prepare()                          # checks templates that are new or changed
    results = []
    names = output_names(pdfs)
    for pdf in pdfs:
        try:
            result = pipeline.process(pdf) if pipeline else process_pdf(pdf, catalog)
        except Exception as exc:  # one bad file must not stop the others
            result = {"source_file": pdf.name, "page_count": 0, "documents": [], "unrecognized_pages": [],
                      "notes": [f"ERROR: processing stopped on this file: {type(exc).__name__}: {exc}"],
                      "totals_by_document_type": {}}
        result["source_path"] = str(pdf)
        write_json(result, out_dir, names[pdf])
        print_summary(result)
        results.append(result)
    workbook = write_excel(results, out_dir / "documents.xlsx", catalog)
    if workbook.name != "documents.xlsx":
        print(f"documents.xlsx is open in another program; this run's workbook is {workbook.name}")

    held = report_template_checks(pipeline, store) if pipeline else []
    if pipeline and pipeline.ai_results:
        report_ai(pipeline.ai_results)
    documents = [d for r in results for d in r["documents"]]
    counts = {s: sum(d["status"] == s for d in documents) for s in ("OK", "CHECK", "FAIL")}
    crashed = [r["source_file"] for r in results if any(n.startswith("ERROR: processing stopped") for n in r["notes"])]
    if crashed:
        print(f"Files that could not be processed: {', '.join(crashed)}")
    unrecognized = sum(len(r["unrecognized_pages"]) for r in results)
    print(f"\nDocuments: {len(documents)}   OK: {counts['OK']}   CHECK: {counts['CHECK']}   "
          f"FAIL: {counts['FAIL']}   unrecognized page groups: {unrecognized}")
    print_attention(results)
    if unrecognized:
        print(f"Text of unrecognized pages: {out_dir / 'unrecognized'}")
    print(f"Output: {out_dir}")
    return 1 if counts["FAIL"] or unrecognized or crashed or catalog.rejected or held else 0


def print_attention(results: list[dict]) -> None:
    """Lists, file by file, every document that is not OK and every unrecognized page group,
    FAIL first, with the first reason, so it is clear which files to open."""
    rows = []
    for r in results:
        name = r["source_file"]
        for doc in r["documents"]:
            if doc["status"] == "OK":
                continue
            reasons = [i["message"] for i in doc.get("issues", []) if i.get("level") == "ERROR"] or \
                      [i["message"] for i in doc.get("issues", []) if i.get("level") == "WARN"]
            number = doc.get("fields", {}).get("invoice_number") or "-"
            pages = ",".join(str(p) for p in doc["pages"])
            rows.append((0 if doc["status"] == "FAIL" else 1, name, doc["status"],
                         f"pages {pages}  {number}  [{doc['template']}]", reasons[0] if reasons else ""))
        for u in r["unrecognized_pages"]:
            pages = ",".join(str(p) for p in u["pages"])
            rows.append((2, name, "NO MATCH", f"pages {pages}", u.get("reason") or "no template matches"))
        if any(n.startswith("ERROR") for n in r["notes"]) and not r["documents"] and not r["unrecognized_pages"]:
            rows.append((0, name, "ERROR", "", next(n for n in r["notes"] if n.startswith("ERROR"))))
    if not rows:
        return
    print("\nNeeds attention:")
    for _, name, status, where, reason in sorted(rows, key=lambda x: (x[0], x[1].lower())):
        print(f"  {status:<9}{name}  {where}")
        if reason:
            print(f"           -> {reason[:160]}")


if __name__ == "__main__":
    sys.exit(main())
