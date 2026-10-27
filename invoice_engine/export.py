"""JSON, Excel and console output. Column formats and order come from the field registry."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from .registry import Registry
from .rules import to_decimal

SUMMARY_MONEY = {"untaxed", "tax", "total", "tax_total"}


def page_label(pages: list[int]) -> str:
    return str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"


def display(value):
    if isinstance(value, list):
        return "; ".join(str(display(v)) for v in value)
    return value


def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def output_names(pdfs: list[Path]) -> dict[Path, str]:
    """A distinct output name per PDF: its file name without extension, plus _2, _3 ... when two
    inputs (e.g. from different folders) have the same name, so no output overwrites another."""
    names: dict[Path, str] = {}
    used: set[str] = set()
    for pdf in pdfs:
        base = name = pdf.stem
        n = 1
        while name.lower() in used:
            n += 1
            name = f"{base}_{n}"
        used.add(name.lower())
        names[pdf] = name
    return names


def write_json(result: dict, out_dir: Path, name: str | None = None) -> None:
    """One JSON per PDF. Text of unrecognized pages goes to output/unrecognized/ for template authoring."""
    stem = name or Path(result["source_file"]).stem
    clean = dict(result)
    clean["unrecognized_pages"] = []
    for unrec in result["unrecognized_pages"]:
        if unrec.get("text", "").strip():
            folder = out_dir / "unrecognized"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{stem}_p{page_label(unrec['pages'])}.txt"
            target.write_text(unrec["text"], encoding="utf-8")
            unrec = {**unrec, "text_file": str(target.relative_to(out_dir))}
        clean["unrecognized_pages"].append({k: v for k, v in unrec.items() if k != "text"})
    (out_dir / f"{stem}.json").write_text(
        json.dumps(clean, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")


def tax_total(fields: dict, registry: Registry):
    if to_decimal(fields.get("amount_tax")) is not None:
        return fields["amount_tax"]
    parts = [to_decimal(fields.get(f)) for f in registry.document_fields_with_role("tax")]
    parts = [p for p in parts if p is not None]
    return float(sum(parts)) if parts else None


class _Formats:
    """Which columns hold money, across every document type's fields."""

    def __init__(self, catalog):
        self.document = set()
        self.line = set()
        for dt in catalog.types:
            self.document |= {n for n, spec in dt.registry.document.items() if spec.type == "money"}
            self.line |= {n for n, spec in dt.registry.line.items() if spec.type == "money"}

    def is_money(self, name: str, level: str = "document") -> bool:
        return name in (self.document if level == "document" else self.line)


DEFAULT_COLUMNS = ["issuer", "document_type", "invoice_number", "date", "currency", "amount"]


def write_excel(results: list[dict], path: Path, catalog) -> Path:
    """Write the workbook: one summary sheet per document type (its columns from type.yml), then
    Line Items, All Fields and Issues. Returns the path written (another name when the file is
    locked, e.g. open in Excel)."""
    if isinstance(catalog, Registry):                    # older callers: one registry, one type
        from .catalog import Catalog
        catalog = Catalog.single([], catalog)
    by_type: dict[str, list[dict]] = {dt.name: [] for dt in catalog.types}
    line_rows, field_rows, issue_rows = [], [], []
    for res in results:
        for note in res["notes"]:
            issue_rows.append({"file": res["source_file"], "pages": "", "document_number": "",
                               "level": "INFO", "message": note})
        for unrec in res["unrecognized_pages"]:
            issue_rows.append({"file": res["source_file"], "pages": page_label(unrec["pages"]),
                               "document_number": "", "level": "ERROR",
                               "message": f"{unrec.get('reason', 'no template matches')}: " + " | ".join(unrec["preview"])})
        for doc in res["documents"]:
            f = doc["fields"]
            number = display(f.get("invoice_number"))
            dt = catalog.type_named(doc.get("type", "")) or catalog.types[0]
            row = {"file": res["source_file"], "pages": page_label(doc["pages"]), "template": doc["template"],
                   "version": doc.get("version"), "status": doc["status"]}
            for column in dt.summary_columns or DEFAULT_COLUMNS:
                row[column] = tax_total(f, dt.registry) if column == "tax_total" else display(f.get(column))
            row["copy_of"] = page_label(doc["copy_of"]) if doc.get("copy_of") else ""
            row["issues"] = sum(1 for i in doc["issues"] if i["level"] != "INFO")
            by_type.setdefault(dt.name, []).append(row)
            for name, value in f.items():
                field_rows.append({"file": res["source_file"], "pages": page_label(doc["pages"]), "type": dt.name,
                                   "document_number": number, "field": name, "value": display(value),
                                   "page": (doc.get("field_pages") or {}).get(name)})
            for i, line in enumerate(doc["lines"], start=1):
                line_row = {"file": res["source_file"], "type": dt.name, "document_number": number,
                            "document_type": f.get("document_type"), "line": i}
                line_row.update({k: display(v) for k, v in line.items()})
                line_rows.append(line_row)
            for issue in doc["issues"]:
                issue_rows.append({"file": res["source_file"], "pages": page_label(doc["pages"]),
                                   "document_number": number, **issue})

    lines_df = pd.DataFrame(line_rows)
    if not lines_df.empty:
        fixed = ["file", "type", "document_number", "document_type", "line"]
        known = list(dict.fromkeys(c for dt in catalog.types for c in dt.registry.line_column_order()
                                   if c in lines_df.columns))
        other = [c for c in lines_df.columns if c not in fixed and c not in known]
        lines_df = lines_df[fixed + known + other]

    sheets = {_sheet_name(name): pd.DataFrame(rows) for name, rows in by_type.items() if rows}
    if not sheets:
        sheets["Documents"] = pd.DataFrame()
    sheets.update({
        "Line Items": lines_df,
        "All Fields": pd.DataFrame(field_rows),
        "Issues": pd.DataFrame(issue_rows, columns=["file", "pages", "document_number", "level", "message"]),
    })
    formats = _Formats(catalog)
    try:
        _write_sheets(path, sheets, formats)
    except PermissionError:
        # typically: the workbook is open in Excel (Windows locks it). Keep this run's results.
        fallback = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        _write_sheets(fallback, sheets, formats)
        return fallback
    return path


def _sheet_name(type_name: str) -> str:
    """Excel sheet names: at most 31 characters, none of []:*?/\\."""
    return re.sub(r"[\\[\]:*?/]", "_", type_name.replace("_", " ").title())[:31]


def _write_sheets(path: Path, sheets: dict, formats) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            _format(writer.sheets[name], formats, line_sheet=(name == "Line Items"))


def _format(sheet, registry, line_sheet: bool) -> None:
    headers = [c.value for c in sheet[1]]
    sheet.freeze_panes = "A2"
    if headers and headers[0] is not None:
        sheet.auto_filter.ref = sheet.dimensions

    def money(name) -> bool:
        if not isinstance(name, str):
            return False
        if name in SUMMARY_MONEY:
            return True
        return registry.is_money(name, "line" if line_sheet else "document")

    for row in sheet.iter_rows(min_row=2):
        field = row[headers.index("field")].value if "field" in headers else None
        for cell, header in zip(row, headers):
            if isinstance(cell.value, (date, datetime)):
                cell.number_format = "yyyy-mm-dd"
            elif isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                if money(field if header == "value" else header):
                    cell.number_format = "#,##0.00"
    for column in sheet.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in column)
        sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 8), 60)


def print_summary(res: dict) -> None:
    count = res["page_count"]
    print(f"\n{res['source_file']}  ({count} page{'s' if count != 1 else ''})")
    for note in res["notes"]:
        print(f"  ! {note}")
    for doc in res["documents"]:
        f = doc["fields"]
        amount = f.get("amount")
        amount_text = f"{amount:>12,.2f}" if isinstance(amount, float) else f"{str(display(amount)):>12}"
        version = f" v{doc['version']}" if doc.get("version", 1) != 1 else ""
        print(f"  p{page_label(doc['pages']):<6}{str(f.get('document_type')):<18}"
              f"{str(display(f.get('invoice_number'))):<22}{str(display(f.get('date'))):<12}"
              f"{str(f.get('currency')):<5}{amount_text}   {doc['status']:<6}{doc['template']}{version}")
        for issue in doc["issues"]:
            print(f"           {issue['level']}: {issue['message']}")
    for unrec in res["unrecognized_pages"]:
        print(f"  p{page_label(unrec['pages']):<6}NO TEMPLATE MATCHES: {' | '.join(unrec['preview'])}")
