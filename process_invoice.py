"""
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
  python process_invoice.py D:\\Invoices              # every PDF in another folder
  python process_invoice.py --lint                   # only check the templates
  python process_invoice.py --overlap samples        # do two layouts read the same sample document?
  python process_invoice.py --overlap x.yml samples  # the same, only pairs that involve template x.yml
  python process_invoice.py --fingerprint samples    # write every template's layout from sample PDFs
  python process_invoice.py --fingerprint x.yml a.pdf b.pdf
                                                     # the same, only for template x.yml
  python process_invoice.py --stress samples         # do templates survive small layout variations?
  python process_invoice.py --accept x.yml           # confirm a deliberate change of values read by x.yml
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


def sample_folder(settings: dict) -> Path:
    folder = Path(settings.get("sample_store") or "samples")
    return folder if folder.is_absolute() else BASE_DIR / folder


def report_template_checks(pipeline, store) -> list[str]:
    """Print the template checks decided in this run; returns the templates held."""
    final = {e.template: e for e in pipeline.events}            # last decision per template
    if final:
        print("\nAutomatic template checks:")
    for event in sorted(final.values(), key=lambda e: (e.state != "held", e.template)):
        if event.state == "admitted":
            print(f"  admitted  {event.template}  ({event.samples} sample(s))")
        elif event.state == "new":
            print(f"  waiting   {event.template}  (no overlap with stored samples; checked fully on "
                  "the first document it reads)")
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
    pipeline = Pipeline(catalog, store) if settings.get("automatic_checks", True) else None
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
