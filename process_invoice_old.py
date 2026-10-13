"""
Local PDF invoice parser built on invoice2data templates.

How it works
  1. pdftotext reads the text of every page (layout kept, UTF-8, Unix line endings).
  2. Each page is matched to a template (by its keywords) and the document number on
     that page is read with the template's own invoice_number rule.
  3. Consecutive pages with the same template and the same (or no) document number
     form one document. A different template or a different number starts a new one.
     Pages that match no template are reported, never merged silently.
  4. Each document is parsed by its template on the joined text of its pages.
  5. Every document is checked: required fields, line items, and arithmetic
     (allowing one cent/paisa per rounded value).
  6. Results: output/<pdf name>.json per PDF and output/invoices.xlsx for the run.

Usage (PowerShell, from the project folder)
  python process_invoice.py                          # every PDF in this folder
  python process_invoice.py Invoice_8626225731.pdf   # one PDF
  python process_invoice.py D:\\Invoices              # every PDF in another folder
  python process_invoice.py --debug ...              # also show invoice2data's own log
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
from invoice2data.extract.loader import read_templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
EXCEL_NAME = "invoices.xlsx"

CENT = Decimal("0.01")
DEFAULT_REQUIRED = ["date", "amount", "invoice_number", "issuer"]

# Fields that must hold exactly one value. Several different values means the template
# matched text in more than one place, usually because two documents were parsed as one.
SINGLE_VALUE_FIELDS = {"invoice_number", "document_type", "currency", "seller_name", "buyer_name"}

# Numbers printed on line items. Text such as "NA" in these columns becomes empty.
NUMERIC_LINE_FIELDS = {
    "qty", "rooms", "price_unit", "gross_amount", "discount", "price_subtotal",
    "line_tax_percent", "line_tax_amount", "cgst_rate", "cgst_amount", "sgst_rate",
    "sgst_amount", "igst_rate", "igst_amount", "price_total", "weight_grams", "goods_value",
}
LINE_TAXES = (  # (tax amount field, matching rate field) on a line
    ("line_tax_amount", "line_tax_percent"),
    ("cgst_amount", "cgst_rate"),
    ("sgst_amount", "sgst_rate"),
    ("igst_amount", "igst_rate"),
)
DOC_TAX_PARTS = ("amount_cgst", "amount_sgst", "amount_igst")
SUM_CHECKS = (  # (document total field, line field that should add up to it)
    ("amount_untaxed", "price_subtotal"),
    ("amount_gross", "gross_amount"),
    ("amount_discount", "discount"),
    ("amount_cgst", "cgst_amount"),
    ("amount_sgst", "sgst_amount"),
    ("amount_igst", "igst_amount"),
    ("amount", "price_total"),
)
MONEY_COLUMNS = {
    "untaxed", "tax", "total", "price_unit", "gross_amount", "discount", "price_subtotal",
    "line_tax_amount", "cgst_amount", "sgst_amount", "igst_amount", "price_total", "goods_value",
}
LINE_COLUMN_ORDER = [
    "pos", "code", "name", "hsn_sac", "service", "qty", "rooms", "price_unit",
    "gross_amount", "discount", "price_subtotal", "tax_type", "line_tax_percent",
    "line_tax_amount", "cgst_rate", "cgst_amount", "sgst_rate", "sgst_amount",
    "igst_rate", "igst_amount", "price_total", "weight_grams", "goods_value",
    "date_start", "date_end", "line_note",
]


# --------------------------------------------------------------------------- text

def page_texts(pdf: Path) -> list[str]:
    """Text of every page, in order. pdftotext ends each page with a form feed."""
    cmd = ["pdftotext", "-layout", "-enc", "UTF-8", "-eol", "unix", str(pdf), "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pdftotext failed: {message}")
    pages = proc.stdout.decode("utf-8").split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return pages


# ---------------------------------------------------------------- page grouping

def matching_templates(text: str, templates: list) -> list:
    """All templates whose keywords match this text, highest priority first."""
    return [t for t in templates if t.matches_input(t.prepare_input(text))]


def document_number(template, text: str) -> str | None:
    """Read the document number on one page with the template's invoice_number rule."""
    spec = template["fields"].get("invoice_number")
    pattern = spec.get("regex") if isinstance(spec, dict) else spec
    if not pattern:
        return None
    prepared = template.prepare_input(text)
    for rx in pattern if isinstance(pattern, list) else [pattern]:
        match = re.search(rx, prepared)
        if match:
            return (match.group(1) if match.groups() else match.group(0)).strip()
    return None


def split_into_documents(pages: list[str], templates: list) -> tuple[list[dict], list[str]]:
    """Group pages into documents. Returns (groups, notes about the grouping)."""
    groups: list[dict] = []
    notes: list[str] = []
    for page_no, text in enumerate(pages, start=1):
        candidates = matching_templates(text, templates) if text.strip() else []
        template = candidates[0] if candidates else None
        number = document_number(template, text) if template else None
        if len(candidates) > 1:
            names = ", ".join(t["template_name"] for t in candidates)
            notes.append(f"page {page_no} matches several templates ({names}); "
                         f"used {template['template_name']}")

        previous = groups[-1] if groups else None
        continues_previous = (
            previous is not None
            and previous["template"] is template
            and (number is None or previous["number"] is None or number == previous["number"])
        )
        if continues_previous:
            previous["pages"].append(page_no)
            previous["texts"].append(text)
            previous["number"] = previous["number"] or number
        else:
            groups.append({"template": template, "number": number,
                           "pages": [page_no], "texts": [text]})
    return groups, notes


# ---------------------------------------------------------------------- parsing

def parse_group(template, texts: list[str], pdf: Path) -> dict:
    """Run one template on the joined text of a document's pages.

    invoice2data's own required-field check is switched off for the call so that a
    partial result is still returned; required fields are checked in validate().
    """
    text = "\n".join(texts)
    saved = template.get("required_fields")
    template["required_fields"] = []
    try:
        return template.extract(template.prepare_input(text), str(pdf), None)
    finally:
        if saved is None:
            del template["required_fields"]
        else:
            template["required_fields"] = saved


def clean(value):
    """Tidy one extracted value: dates without time, single-spaced text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def clean_line(line: dict) -> dict:
    row = {}
    for key, value in line.items():
        value = clean(value)
        if key in NUMERIC_LINE_FIELDS and isinstance(value, str):
            value = to_number(value)
        row[key] = value
    return row


def to_number(text: str):
    """'1' -> 1, '2.5' -> 2.5, 'NA' or '' -> None."""
    try:
        number = Decimal(text.replace(",", ""))
    except Exception:
        return None
    return int(number) if number == number.to_integral_value() else float(number)


# ------------------------------------------------------------------- validation

def dec(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return Decimal(str(value))


def money(value: Decimal) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP)}"


def validate(fields: dict, lines: list[dict], tax_lines: list[dict],
             required: list[str], template_fields: list[str], has_line_rules: bool) -> list[tuple[str, str]]:
    """Return (level, message) pairs. ERROR = wrong or missing data, WARN = worth a look."""
    issues: list[tuple[str, str]] = []

    def compare(label: str, expected: Decimal, actual: Decimal, allowed: Decimal) -> None:
        difference = abs(expected - actual)
        if difference > allowed:
            issues.append(("ERROR", f"{label}: calculated {money(expected)}, printed {money(actual)} "
                                    f"(off by {money(difference)}, allowed {money(allowed)})"))

    # 1. Required fields
    for name in required:
        if fields.get(name) in (None, "", []):
            issues.append(("ERROR", f"required field not found: {name}"))

    # 2. Fields that must have one value
    for name, value in fields.items():
        if isinstance(value, list) and (name in SINGLE_VALUE_FIELDS or name.startswith(("amount", "date"))):
            issues.append(("ERROR", f"{name} has several different values {value}"))

    # 3. Template fields that found nothing
    for name in template_fields:
        if name not in fields and name not in required:
            issues.append(("WARN", f"template field found nothing: {name}"))

    # 4. Line items
    if has_line_rules and not lines:
        issues.append(("ERROR", "no line items found although the template defines line rules"))

    n = Decimal(max(len(lines), 1))
    for i, line in enumerate(lines, start=1):
        qty, unit = dec(line.get("qty")), dec(line.get("price_unit"))
        gross, discount = dec(line.get("gross_amount")), dec(line.get("discount"))
        subtotal, total = dec(line.get("price_subtotal")), dec(line.get("price_total"))
        taxes = [dec(line.get(a)) for a, _ in LINE_TAXES if dec(line.get(a)) is not None]

        if qty is not None and unit is not None and (subtotal is not None or total is not None):
            rooms = dec(line.get("rooms")) or Decimal(1)
            compare(f"line {i}: qty x unit price - discount",
                    qty * unit * rooms - (discount or 0), subtotal if subtotal is not None else total, CENT)

        if gross is not None and discount is not None:
            net = gross - discount
            targets = [v for v in (subtotal, total) if v is not None]
            if targets and all(abs(net - v) > CENT for v in targets):
                issues.append(("ERROR", f"line {i}: gross {money(gross)} - discount {money(discount)} = "
                                        f"{money(net)} matches neither taxable value nor line total"))

        if subtotal is not None and total is not None and taxes:
            compare(f"line {i}: taxable value + tax", subtotal + sum(taxes), total, CENT * len(taxes))

        for amount_key, rate_key in LINE_TAXES:
            amount, rate = dec(line.get(amount_key)), dec(line.get(rate_key))
            if amount is not None and rate is not None and subtotal is not None:
                expected = (subtotal * rate / 100).quantize(CENT, ROUND_HALF_UP)
                compare(f"line {i}: {rate_key} {rate}% of {money(subtotal)}", expected, amount, CENT)

    # 5. Line values add up to the document totals
    for doc_key, line_key in SUM_CHECKS:
        doc_value = dec(fields.get(doc_key))
        values = [dec(line.get(line_key)) for line in lines]
        if doc_value is not None and lines and all(v is not None for v in values):
            compare(f"sum of line {line_key} vs {doc_key}", sum(values), doc_value, CENT * n)

    # 6. Tax: printed total, else the printed CGST / SGST / IGST parts
    tax_total = dec(fields.get("amount_tax"))
    parts = [dec(fields.get(k)) for k in DOC_TAX_PARTS if dec(fields.get(k)) is not None]
    if tax_total is None and parts:
        tax_total = sum(parts)
    if tax_total is not None and lines:
        line_tax = [sum(dec(l.get(a)) for a, _ in LINE_TAXES if dec(l.get(a)) is not None) for l in lines]
        if all(any(dec(l.get(a)) is not None for a, _ in LINE_TAXES) for l in lines):
            compare("sum of line tax vs document tax", sum(line_tax), tax_total, CENT * n)
        elif all(dec(l.get("line_tax_percent")) is not None and dec(l.get("price_subtotal")) is not None
                 for l in lines):
            computed = sum((dec(l["price_subtotal"]) * dec(l["line_tax_percent"]) / 100).quantize(CENT, ROUND_HALF_UP)
                           for l in lines)
            compare("line tax rates x taxable values vs document tax", computed, tax_total, CENT * n)

    # 7. Untaxed + tax = total
    total = dec(fields.get("amount"))
    untaxed = dec(fields.get("amount_untaxed"))
    if untaxed is None and lines and all(dec(l.get("price_subtotal")) is not None for l in lines):
        untaxed = sum(dec(l["price_subtotal"]) for l in lines)
    if total is not None and untaxed is not None and tax_total is not None:
        compare("untaxed amount + tax vs total", untaxed + tax_total, total, CENT * n)

    # 8. Independent totals: paid / due must agree with the total
    paid, due = dec(fields.get("amount_paid")), dec(fields.get("amount_due"))
    if total is not None and paid is not None:
        compare("amount paid + amount due vs total", paid + (due or 0), total, CENT)
    elif total is not None and due is not None:
        compare("amount due vs total", due, total, CENT)

    # 9. Tax summary table (if the template reads one)
    for doc_key, key in (("amount_untaxed", "price_subtotal"), ("amount_tax", "line_tax_amount"),
                         ("amount", "price_total")):
        doc_value = dec(fields.get(doc_key))
        values = [dec(t.get(key)) for t in tax_lines]
        if doc_value is not None and tax_lines and all(v is not None for v in values):
            compare(f"tax summary {key} vs {doc_key}", sum(values), doc_value, CENT * len(tax_lines))

    return issues


def status_of(issues: list[tuple[str, str]]) -> str:
    levels = {level for level, _ in issues}
    return "FAIL" if "ERROR" in levels else "CHECK" if "WARN" in levels else "OK"


def tax_total_of(fields: dict):
    if dec(fields.get("amount_tax")) is not None:
        return fields["amount_tax"]
    parts = [dec(fields.get(k)) for k in DOC_TAX_PARTS if dec(fields.get(k)) is not None]
    return float(sum(parts)) if parts else None


# ------------------------------------------------------------------ one PDF

def process_pdf(pdf: Path, templates: list) -> dict:
    result = {"source_file": pdf.name, "page_count": 0, "documents": [],
              "unrecognized_pages": [], "notes": []}
    try:
        pages = page_texts(pdf)
    except RuntimeError as exc:
        result["notes"].append(f"ERROR: {exc}")
        return result
    result["page_count"] = len(pages)

    groups, notes = split_into_documents(pages, templates)
    result["notes"].extend(notes)

    for group in groups:
        template = group["template"]
        if template is None:
            preview = [ln.strip() for ln in "\n".join(group["texts"]).splitlines() if ln.strip()][:3]
            result["unrecognized_pages"].append(
                {"pages": group["pages"], "preview": preview or ["(no text layer: scanned or blank page)"]})
            continue

        try:
            raw = parse_group(template, group["texts"], pdf)
        except Exception as exc:  # a broken template must not stop the other documents
            result["documents"].append({
                "pages": group["pages"], "template": template["template_name"], "status": "FAIL",
                "issues": [{"level": "ERROR", "message": f"template error: {type(exc).__name__}: {exc}"}],
                "fields": {}, "lines": []})
            continue
        lines = [clean_line(l) for l in raw.pop("lines", []) or [] if isinstance(l, dict)]
        tax_lines = [clean_line(l) for l in raw.pop("tax_lines", []) or [] if isinstance(l, dict)]
        raw.pop("desc", None)
        raw.pop("template_name", None)
        fields = {key: clean(value) for key, value in raw.items()}

        required = list(template.get("required_fields") or DEFAULT_REQUIRED)
        template_fields = [k for k, v in template["fields"].items()
                           if not (isinstance(v, dict) and v.get("parser") in ("static", "lines"))]
        issues = validate(fields, lines, tax_lines, required, template_fields, "lines" in template)

        document = {"pages": group["pages"], "template": template["template_name"],
                    "status": status_of(issues),
                    "issues": [{"level": level, "message": msg} for level, msg in issues],
                    "fields": fields, "lines": lines}
        if tax_lines:
            document["tax_lines"] = tax_lines
        result["documents"].append(document)

    totals: dict = defaultdict(lambda: defaultdict(Decimal))
    for doc in result["documents"]:
        amount, fields = dec(doc["fields"].get("amount")), doc["fields"]
        if amount is not None:
            totals[fields.get("document_type", "unknown")][fields.get("currency", "?")] += amount
    result["totals_by_document_type"] = {
        kind: {cur: float(value) for cur, value in by_cur.items()} for kind, by_cur in totals.items()
    }
    return result


# ------------------------------------------------------------------ output

def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def page_label(pages: list[int]) -> str:
    return str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"


def display(value):
    if isinstance(value, list):
        return "; ".join(str(display(v)) for v in value)
    return value


def write_excel(results: list[dict], path: Path) -> None:
    documents, line_rows, field_rows, issue_rows = [], [], [], []
    for res in results:
        for note in res["notes"]:
            issue_rows.append({"file": res["source_file"], "pages": "", "document_number": "",
                               "level": "WARN", "message": note})
        for unrec in res["unrecognized_pages"]:
            issue_rows.append({"file": res["source_file"], "pages": page_label(unrec["pages"]),
                               "document_number": "", "level": "ERROR",
                               "message": "no template matches: " + " | ".join(unrec["preview"])})
        for doc in res["documents"]:
            f = doc["fields"]
            number = display(f.get("invoice_number"))
            documents.append({
                "file": res["source_file"], "pages": page_label(doc["pages"]),
                "template": doc["template"], "status": doc["status"],
                "issuer": f.get("issuer"), "document_type": f.get("document_type"),
                "document_number": number, "date": display(f.get("date")),
                "seller": display(f.get("seller_name")), "buyer": display(f.get("buyer_name")),
                "currency": f.get("currency"), "untaxed": f.get("amount_untaxed"),
                "tax": tax_total_of(f), "total": display(f.get("amount")),
                "issues": len(doc["issues"]),
            })
            for name, value in f.items():
                field_rows.append({"file": res["source_file"], "pages": page_label(doc["pages"]),
                                   "document_number": number, "field": name, "value": display(value)})
            for i, line in enumerate(doc["lines"], start=1):
                row = {"file": res["source_file"], "document_number": number,
                       "document_type": f.get("document_type"), "line": i}
                row.update({k: display(v) for k, v in line.items()})
                line_rows.append(row)
            for issue in doc["issues"]:
                issue_rows.append({"file": res["source_file"], "pages": page_label(doc["pages"]),
                                   "document_number": number, **issue})

    lines_df = pd.DataFrame(line_rows)
    if not lines_df.empty:
        fixed = ["file", "document_number", "document_type", "line"]
        known = [c for c in LINE_COLUMN_ORDER if c in lines_df.columns]
        other = [c for c in lines_df.columns if c not in fixed and c not in known]
        lines_df = lines_df[fixed + known + other]

    sheets = {
        "Documents": pd.DataFrame(documents),
        "Line Items": lines_df,
        "All Fields": pd.DataFrame(field_rows),
        "Issues": pd.DataFrame(issue_rows, columns=["file", "pages", "document_number", "level", "message"]),
    }
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            format_sheet(writer.sheets[name])


def is_money(name) -> bool:
    return isinstance(name, str) and (name in MONEY_COLUMNS or name.startswith("amount"))


def format_sheet(sheet) -> None:
    """Freeze the header, add filters, size columns; two decimals only for money."""
    headers = [c.value for c in sheet[1]]
    sheet.freeze_panes = "A2"
    if headers and headers[0] is not None:
        sheet.auto_filter.ref = sheet.dimensions
    for row in sheet.iter_rows(min_row=2):
        field = row[headers.index("field")].value if "field" in headers else None
        for cell, header in zip(row, headers):
            if isinstance(cell.value, (date, datetime)):
                cell.number_format = "yyyy-mm-dd"
            elif isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                if is_money(field if header == "value" else header):
                    cell.number_format = "#,##0.00"
    for column in sheet.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 8), 60)


def print_summary(res: dict) -> None:
    print(f"\n{res['source_file']}  ({res['page_count']} page{'s' if res['page_count'] != 1 else ''})")
    for note in res["notes"]:
        print(f"  ! {note}")
    for doc in res["documents"]:
        f = doc["fields"]
        amount = f.get("amount")
        amount_text = f"{amount:>12,.2f}" if isinstance(amount, float) else f"{str(display(amount)):>12}"
        print(f"  p{page_label(doc['pages']):<6}{str(f.get('document_type')):<18}"
              f"{str(display(f.get('invoice_number'))):<22}{str(display(f.get('date'))):<12}"
              f"{str(f.get('currency')):<5}{amount_text}   {doc['status']:<6}{doc['template']}")
        for issue in doc["issues"]:
            print(f"           {issue['level']}: {issue['message']}")
    for unrec in res["unrecognized_pages"]:
        print(f"  p{page_label(unrec['pages']):<6}NO TEMPLATE MATCHES: {' | '.join(unrec['preview'])}")


# ---------------------------------------------------------------------- main

def load_templates(folder: Path) -> list:
    if not folder.is_dir():
        sys.exit(f"Template folder not found: {folder}")
    templates = read_templates(str(folder))
    loaded = {t["template_name"] for t in templates}
    files = [p.name for p in sorted(folder.rglob("*")) if p.suffix.lower() in (".yml", ".yaml", ".json")]
    broken = [name for name in files if name not in loaded]
    if broken:
        print("WARNING: these template files could not be loaded (YAML error or no keywords): "
              + ", ".join(broken))
    if not templates:
        sys.exit("No templates loaded.")
    needs_dateparser = [t["template_name"] for t in templates if t.options.get("languages")]
    if needs_dateparser and importlib.util.find_spec("dateparser") is None:
        print("WARNING: dateparser is not installed; month names in other languages will not parse "
              f"({', '.join(needs_dateparser)}). Fix: pip install dateparser")
    return sorted(templates, key=lambda t: t.get("priority", 5), reverse=True)


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
    parser.add_argument("--templates", default=str(TEMPLATE_DIR), help="template folder")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="output folder")
    parser.add_argument("--debug", action="store_true", help="show invoice2data's own log")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.ERROR)
    logging.getLogger("invoice2data").setLevel(logging.DEBUG if args.debug else logging.ERROR)

    if shutil.which("pdftotext") is None:
        sys.exit("pdftotext (Poppler) is not on PATH.")

    templates = load_templates(Path(args.templates))
    pdfs = collect_pdfs(args.inputs)
    if not pdfs:
        sys.exit("No PDF files to process.")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Templates: {len(templates)}   PDFs: {len(pdfs)}")

    results = []
    for pdf in pdfs:
        res = process_pdf(pdf, templates)
        results.append(res)
        (out_dir / f"{pdf.stem}.json").write_text(
            json.dumps(res, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")
        print_summary(res)

    write_excel(results, out_dir / EXCEL_NAME)

    documents = [d for r in results for d in r["documents"]]
    counts = {s: sum(d["status"] == s for d in documents) for s in ("OK", "CHECK", "FAIL")}
    unrecognized = sum(len(r["unrecognized_pages"]) for r in results)
    print(f"\nDocuments: {len(documents)}   OK: {counts['OK']}   CHECK: {counts['CHECK']}   "
          f"FAIL: {counts['FAIL']}   unrecognized page groups: {unrecognized}")
    print(f"Output: {out_dir}")
    return 1 if counts["FAIL"] or unrecognized else 0


if __name__ == "__main__":
    sys.exit(main())
