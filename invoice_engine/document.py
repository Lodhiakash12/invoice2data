"""Parses one document with its candidate templates and chooses between them by evidence.

Every candidate layout (template family) is parsed. Within a family the newest version that does
not FAIL represents it. Between families, a result is accepted only when it is at least as good
as every other non-failing result on each measure of evidence (status, checks passed, share of
the template's fields found) and better on at least one. When no result dominates the others the
document is marked CHECK as ambiguous instead of picking one by name."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .registry import Registry
from .rules import to_decimal
from .segment import page_texts, split_into_documents
from .templates import family_of, version_of
from .validate import check_document, status_of


def parse_with(template, texts: list[str], pdf: Path) -> dict:
    """Run one template on the joined page texts. invoice2data's own required-field check is
    switched off for the call so a partial result is still returned (checked in validate)."""
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
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def to_number(text: str):
    """'1' -> 1, '2.5' -> 2.5, 'NA' or '' -> None."""
    try:
        number = Decimal(text.replace(",", ""))
    except Exception:
        return None
    return int(number) if number == number.to_integral_value() else float(number)


def clean_line(line: dict, registry: Registry) -> dict:
    row = {}
    for key, value in line.items():
        value = clean(value)
        if registry.is_numeric_line_field(key) and isinstance(value, str):
            value = to_number(value)
        row[key] = value
    return row


def attempt(template, texts: list[str], pdf: Path, registry: Registry, pages: list[int]) -> dict:
    """Parse + validate with one template; never raises."""
    record = {"pages": pages, "template": template["template_name"],
              "family": family_of(template), "version": version_of(template)}
    try:
        raw = parse_with(template, texts, pdf)
    except Exception as exc:  # a broken template must not stop the other documents
        record.update(status="FAIL", fields={}, lines=[],
                      issues=[{"level": "ERROR", "message": f"template error: {type(exc).__name__}: {exc}"}])
        return record
    lines = [clean_line(l, registry) for l in raw.pop("lines", []) or [] if isinstance(l, dict)]
    tax_lines = [clean_line(l, registry) for l in raw.pop("tax_lines", []) or [] if isinstance(l, dict)]
    raw.pop("desc", None)
    raw.pop("template_name", None)
    fields = {key: clean(value) for key, value in raw.items()}
    issues, evidence = check_document(fields, lines, tax_lines, template, registry)
    defined = [name for name, spec in template["fields"].items()
               if not (isinstance(spec, dict) and spec.get("parser") in ("static", "lines"))]
    evidence.update(fields_found=sum(1 for name in defined if fields.get(name) not in (None, "", [])),
                    fields_defined=len(defined))
    record.update(status=status_of(issues), fields=fields, lines=lines,
                  issues=[{"level": level, "message": msg} for level, msg in issues], evidence=evidence)
    if tax_lines:
        record["tax_lines"] = tax_lines
    return record


STATUS_RANK = {"FAIL": 0, "CHECK": 1, "OK": 2}


def evidence_key(record: dict) -> tuple:
    """What a parse proves, best first: status, checks passed, share of the template's fields found."""
    e = record.get("evidence") or {}
    found, defined = e.get("fields_found", 0), e.get("fields_defined", 0)
    return STATUS_RANK[record["status"]], e.get("checks_passed", 0), found / defined if defined else 0.0


def dominates(a: dict, b: dict) -> bool:
    ka, kb = evidence_key(a), evidence_key(b)
    return ka != kb and all(x >= y for x, y in zip(ka, kb))


def _evidence_text(record: dict) -> str:
    e = record.get("evidence") or {}
    return (f"{record['status']}, {e.get('checks_passed', 0)} check(s) passed, "
            f"{e.get('fields_found', 0)}/{e.get('fields_defined', 0)} fields")


def process_group(group: dict, pdf: Path, registry: Registry) -> dict:
    """Parse the document with every candidate layout and keep the one the evidence supports.

    - within a layout family: versions newest first; the first that does not FAIL represents it
    - one family left that does not FAIL: it is used
    - several: the one that dominates all others is used; if none does, the best-ranked result is
      shown with status CHECK and the alternatives listed (a person decides)
    - all FAIL: the result with the most evidence is reported, so its errors are the useful ones
    """
    attempts, representatives = [], []
    families = list(dict.fromkeys(family_of(t) for t in group["candidates"]))
    for family in families:
        tried = []
        for template in (t for t in group["candidates"] if family_of(t) == family):
            record = attempt(template, group["texts"], pdf, registry, group["pages"])
            tried.append(record)
            if record["status"] != "FAIL":
                break
        attempts.extend(tried)
        representatives.append(next((r for r in tried if r["status"] != "FAIL"), tried[0]))

    alive = [r for r in representatives if r["status"] != "FAIL"]
    ambiguous = False
    if not alive:
        chosen = max(representatives, key=evidence_key)
    else:
        winners = [a for a in alive if all(a is b or dominates(a, b) for b in alive)]
        ambiguous = len(winners) != 1
        chosen = winners[0] if not ambiguous else max(alive, key=evidence_key)

    if ambiguous:
        others = [r for r in alive if r is not chosen]
        message = (f"ambiguous: {chosen['template']} ({_evidence_text(chosen)}) and "
                   + ", ".join(f"{r['template']} ({_evidence_text(r)})" for r in others)
                   + " all fit this document and none has more evidence; check which is right "
                     "and make their keywords exclusive")
        chosen["status"] = "CHECK"
        chosen["issues"].append({"level": "WARN", "message": message})
        chosen["alternatives"] = [{"template": r["template"], "status": r["status"], "evidence": r.get("evidence"),
                                   "fields": r["fields"]} for r in others]
    for other in attempts:
        if other is not chosen:
            errors = sum(1 for i in other["issues"] if i["level"] == "ERROR")
            chosen["issues"].append({"level": "INFO", "message":
                                     f"also tried {other['template']} (version {other['version']}): "
                                     f"{other['status']} with {errors} error(s), "
                                     f"{(other.get('evidence') or {}).get('checks_passed', 0)} check(s) passed"})
    return chosen


def process_pdf(pdf: Path, templates: list, registry: Registry) -> dict:
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
        if group["family"] is None:
            text = "\n".join(group["texts"])
            preview = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
            result["unrecognized_pages"].append(
                {"pages": group["pages"], "preview": preview or ["(no text layer: scanned or blank page)"],
                 "text": text})
            continue
        result["documents"].append(process_group(group, pdf, registry))

    totals: dict = defaultdict(lambda: defaultdict(Decimal))
    for doc in result["documents"]:
        amount = to_decimal(doc["fields"].get("amount"))
        if amount is not None:
            totals[doc["fields"].get("document_type", "unknown")][doc["fields"].get("currency", "?")] += amount
    result["totals_by_document_type"] = {k: {c: float(v) for c, v in by.items()} for k, by in totals.items()}
    return result