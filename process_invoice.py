"""
Local PDF invoice parser - command line.

The engine (invoice_engine/) contains no issuer- or field-specific code:
  templates/          one YAML per document layout (what to read and where)
  config/fields.yml   every field and what it means (type, role, column sums)
  config/checks.yml   default arithmetic checks; templates can add or disable checks

Usage (PowerShell, from the project folder)
  python process_invoice.py                          # every PDF in this folder
  python process_invoice.py Invoice_8626225731.pdf   # one PDF
  python process_invoice.py D:\\Invoices              # every PDF in another folder
  python process_invoice.py --lint                   # only check the templates
  python process_invoice.py --debug ...              # also show invoice2data's own log
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from invoice_engine import Registry, load_templates, process_pdf
from invoice_engine.export import print_summary, write_excel, write_json

BASE_DIR = Path(__file__).resolve().parent


def report_templates(template_set) -> None:
    for name, problems in template_set.rejected.items():
        print(f"REJECTED {name}")
        for problem in problems:
            print(f"   - {problem}")
    for name, problems in template_set.warnings.items():
        if name in template_set.rejected:
            continue
        print(f"warning  {name}")
        for problem in problems:
            print(f"   - {problem}")


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse PDF invoices with invoice2data templates.")
    parser.add_argument("inputs", nargs="*", help="PDF files or folders (default: this folder)")
    parser.add_argument("--templates", default=str(BASE_DIR / "templates"), help="template folder")
    parser.add_argument("--config", default=str(BASE_DIR / "config"), help="folder with fields.yml and checks.yml")
    parser.add_argument("--output", default=str(BASE_DIR / "output"), help="output folder")
    parser.add_argument("--lint", action="store_true", help="only check the templates and exit")
    parser.add_argument("--debug", action="store_true", help="show invoice2data's own log")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    level = logging.DEBUG if args.debug else logging.ERROR
    logging.basicConfig(level=level)
    logging.getLogger("invoice2data").setLevel(level)

    registry = Registry(Path(args.config))
    template_set = load_templates(Path(args.templates), registry)
    report_templates(template_set)
    print(f"Templates: {len(template_set.templates)} usable, {len(template_set.rejected)} rejected")
    if args.lint:
        return 1 if template_set.rejected else 0
    if not template_set.templates:
        sys.exit("No usable templates.")
    if shutil.which("pdftotext") is None:
        sys.exit("pdftotext (Poppler) is not on PATH.")

    pdfs = collect_pdfs(args.inputs)
    if not pdfs:
        sys.exit("No PDF files to process.")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"PDFs: {len(pdfs)}")

    results = []
    for pdf in pdfs:
        result = process_pdf(pdf, template_set.templates, registry)
        write_json(result, out_dir)
        print_summary(result)
        results.append(result)
    write_excel(results, out_dir / "invoices.xlsx", registry)

    documents = [d for r in results for d in r["documents"]]
    counts = {s: sum(d["status"] == s for d in documents) for s in ("OK", "CHECK", "FAIL")}
    unrecognized = sum(len(r["unrecognized_pages"]) for r in results)
    print(f"\nDocuments: {len(documents)}   OK: {counts['OK']}   CHECK: {counts['CHECK']}   "
          f"FAIL: {counts['FAIL']}   unrecognized page groups: {unrecognized}")
    if unrecognized:
        print(f"Text of unrecognized pages: {out_dir / 'unrecognized'}")
    print(f"Output: {out_dir}")
    return 1 if counts["FAIL"] or unrecognized or template_set.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
