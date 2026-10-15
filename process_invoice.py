"""
Local PDF invoice parser - command line.

The engine (invoice_engine/) contains no issuer- or field-specific code:
  templates/          one YAML per document layout (what to read and where)
  config/fields.yml   every field and what it means (type, role, column sums)
  config/checks.yml   default arithmetic checks; templates can add or disable checks

Usage (PowerShell, from the project folder)
  python process_invoice.py                          # every PDF in this folder
  python process_invoice.py Invoice_8626225731.pdf   # one PDF
  python process_invoice.py C:\Work\invoice2data\doc             # every PDF in another folder
  python process_invoice.py --lint                   # only check the templates
  python process_invoice.py --debug ...              # also show invoice2data's own log
  python process_invoice.py --fingerprint templates\\x.yml a.pdf b.pdf
                                                     # store x.yml's label layout from sample PDFs
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from invoice_engine import Registry, load_templates, process_pdf
from invoice_engine import layout as layout_fp
from invoice_engine.document import attempt
from invoice_engine.segment import page_texts, split_into_documents
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


def store_fingerprint(template_file: Path, pdfs: list[Path], template_set, registry) -> int:
    """Write the layout block of one template from the documents it reads in the given PDFs."""
    template = next((t for t in template_set.templates if t["template_name"] == template_file.name), None)
    if template is None:
        print(f"{template_file.name} is not a usable template in the template folder")
        return 1
    vocabulary = layout_fp.label_vocabulary(template)
    samples = []
    for pdf in pdfs:
        groups, _ = split_into_documents(page_texts(pdf), template_set.templates)
        for group in groups:
            if template not in group["candidates"]:
                continue
            record = attempt(template, group["texts"], pdf, registry, group["pages"])
            if record["status"] != "FAIL":         # it reads this document, whichever template won
                samples.append(layout_fp.fingerprint(layout_fp.page_words(pdf, group["pages"]), vocabulary))
                print(f"  sample: {pdf.name} pages {group['pages']} ({record['status']})")
    if not samples:
        print("No document in these PDFs was read by this template without FAIL; nothing written.")
        return 1
    order = layout_fp.merge(samples)
    layout_fp.write_layout(template_file, layout_fp.layout_block(order, len(samples)))
    print(f"Layout of {template_file.name}: {len(order)} labels from {len(samples)} sample(s) written.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse PDF invoices with invoice2data templates.")
    parser.add_argument("inputs", nargs="*", help="PDF files or folders (default: this folder)")
    parser.add_argument("--templates", default=str(BASE_DIR / "templates"), help="template folder")
    parser.add_argument("--config", default=str(BASE_DIR / "config"), help="folder with fields.yml and checks.yml")
    parser.add_argument("--output", default=str(BASE_DIR / "output"), help="output folder")
    parser.add_argument("--lint", action="store_true", help="only check the templates and exit")
    parser.add_argument("--debug", action="store_true", help="show invoice2data's own log")
    parser.add_argument("--fingerprint", metavar="TEMPLATE",
                        help="store this template's label layout, read from the PDFs given as inputs")
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
    if args.fingerprint:
        return store_fingerprint(Path(args.fingerprint), collect_pdfs(args.inputs), template_set, registry)
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