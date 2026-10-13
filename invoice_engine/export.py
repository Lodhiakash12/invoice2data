"""JSON, Excel and console output. Column formats and order come from the field registry."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from .registry import Registry
from .rules import to_decimal

SUMMARY_MONEY = {"untaxed", "tax", "total"}


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


def write_json(result: dict, out_dir: Path) -> None:
    """One JSON per PDF. Text of unrecognized pages goes to output/unrecognized/ for template authoring."""
    stem = Path(result["source_file"]).stem
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


def write_excel(results: list[dict], path: Path, registry: Registry) -> None:
    documents, line_rows, field_rows, issue_rows = [], [], [], []
    for res in results:
        for note in res["notes"]:
            issue_rows.append({"file": res["source_file"], "pages": "", "document_number": "",
                               "level": "INFO", "message": note})
        for unrec in res["unrecognized_pages"]:
            issue_rows.append({"file": res["source_file"], "pages": page_label(unrec["pages"]),
                               "document_number": "", "level": "ERROR",
                               "message": "no template matches: " + " | ".join(unrec["preview"])})
        for doc in res["documents"]:
            f = doc["fields"]
            number = display(f.get("invoice_number"))
            documents.append({
                "file": res["source_file"], "pages": page_label(doc["pages"]), "template": doc["template"],
                "version": doc.get("version"), "status": doc["status"], "issuer": f.get("issuer"),
                "document_type": f.get("document_type"), "document_number": number,
                "date": display(f.get("date")), "seller": display(f.get("seller_name")),
                "buyer": display(f.get("buyer_name")), "currency": f.get("currency"),
                "untaxed": f.get("amount_untaxed"), "tax": tax_total(f, registry),
                "total": display(f.get("amount")),
                "issues": sum(1 for i in doc["issues"] if i["level"] != "INFO"),
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
        known = [c for c in registry.line_column_order() if c in lines_df.columns]
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
            _format(writer.sheets[name], registry, line_sheet=(name == "Line Items"))


def _format(sheet, registry: Registry, line_sheet: bool) -> None:
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
