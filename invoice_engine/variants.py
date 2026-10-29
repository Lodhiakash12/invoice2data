"""Variant tracker: which kinds of document each template has really read, and which it has not.

One issuer prints the same layout in several variants: tax as IGST (seller in another state) or as
CGST + SGST (same state), one item or many, one page or several, with or without a discount, a
round-off, a cess, a shipping row. A template built from a few documents has seen only some of
them; a variant it has never seen is where a new document fails. The tracker reads the variant of
every stored sample (documents read OK, see samples.py) and compares it with what the template is
built to read:

  seen            a real document of this variant was read OK (with how many)
  not seen yet    the template has fields for it, but no real document proves them: the
                  documents to collect
  cannot read     the template has no fields for it: such a document fails and goes to the
                  AI repair (only for the GST kind, where it is known for certain)
  not expected    the issuer never prints it (document_types/<type>/variants.yml, with the reason)

Documents read FAIL or CHECK are recorded as well (the last ones per template, with the first
reason), so the report shows where the problems are; a document read OK later removes its entry.

Each variant is worked out from the values read, never from the page text, so it needs no pattern
of its own and holds for every template, hand-written or AI-written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# variant labels, in report order; NEEDED variants are the ones that most often break a template
IGST, CGST, MIXED, ZERO = "IGST", "CGST+SGST", "IGST and CGST+SGST", "zero tax"
ONE_ITEM, ITEMS = "1 item", "several items"
ONE_PAGE, PAGES = "1 page", "several pages"
DISCOUNT, ROUND_OFF, CESS, CHARGE = "discount", "round-off", "cess", "charge row"
ORDER = [IGST, CGST, MIXED, ZERO, ONE_ITEM, ITEMS, ONE_PAGE, PAGES, DISCOUNT, ROUND_OFF, CESS, CHARGE]
NEEDED = {IGST, CGST, ITEMS, PAGES}
IF_AVAILABLE = {DISCOUNT, ROUND_OFF, CESS, CHARGE}

_CHARGE_ROW = re.compile(r"(?i)\b(?:shipping|delivery|handling|convenience|packing|packaging|"
                         r"freight|courier|cod|cash on delivery|other charges?)\b")

# fields that show a template is built to read a variant (field names in its fields, its line
# patterns' named groups or its table columns)
_READS = {
    "igst": re.compile(r"\b(?:amount_igst|igst_amount|igst_rate|tax_type)\b"),
    "cgst": re.compile(r"\b(?:amount_cgst|cgst_amount|cgst_rate|tax_type)\b"),   # tax_type: one column naming its kind
    "sgst": re.compile(r"\b(?:amount_sgst|sgst_amount|sgst_rate|amount_utgst)\b"),
    "discount": re.compile(r"\b(?:amount_discount|discount|discount_unit)\b"),
    "round_off": re.compile(r"\b(?:amount_round_off|total_rounding)\b"),
    "cess": re.compile(r"\b(?:amount_cess|cess_amount|cess_rate|additional_cess)\b"),
}


def _number(value) -> float:
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return 0.0


def _total(fields: dict, lines: list, document_fields: tuple, line_fields: tuple) -> float:
    total = sum(_number(fields.get(f)) for f in document_fields)
    return total + sum(_number(line.get(f)) for line in lines for f in line_fields)


def variant_of(sample: dict) -> set[str]:
    """The variant labels of one stored sample (or of one document read: fields, lines, pages)."""
    fields = sample.get("fields") or {}
    lines = [line for line in sample.get("lines") or [] if isinstance(line, dict)]
    labels: set[str] = set()

    igst = _total(fields, lines, ("amount_igst",), ("igst_amount",))
    split = _total(fields, lines, ("amount_cgst", "amount_sgst", "amount_utgst"), ("cgst_amount", "sgst_amount"))
    for line in lines:                       # one tax column with its kind named (Meesho: "IGST @18%")
        kind, amount = str(line.get("tax_type") or "").upper(), _number(line.get("line_tax_amount"))
        if "IGST" in kind:
            igst += amount
        elif "CGST" in kind or "SGST" in kind:
            split += amount
    has_gst = any(k in fields for k in ("amount_igst", "amount_cgst", "amount_sgst", "amount_utgst")) or any(
        k in line for line in lines for k in ("igst_amount", "cgst_amount", "sgst_amount", "tax_type"))
    if igst and split:
        labels.add(MIXED)
    elif igst:
        labels.add(IGST)
    elif split:
        labels.add(CGST)
    elif has_gst:
        labels.add(ZERO)

    if lines:
        labels.add(ITEMS if len(lines) > 1 else ONE_ITEM)
        names = [str(line.get("name") or line.get("service") or "") for line in lines]
        if any(_CHARGE_ROW.search(n) for n in names) and not all(_CHARGE_ROW.search(n) for n in names):
            labels.add(CHARGE)
    labels.add(PAGES if len(sample.get("pages") or [1]) > 1 else ONE_PAGE)
    if _total(fields, lines, ("amount_discount",), ("discount", "discount_unit")):
        labels.add(DISCOUNT)
    if _number(fields.get("amount_round_off")):
        labels.add(ROUND_OFF)
    if _total(fields, lines, ("amount_cess",), ("cess_amount", "additional_cess")):
        labels.add(CESS)
    return labels


def year_of(sample: dict) -> str | None:
    found = re.match(r"(\d{4})-\d{2}-\d{2}", str((sample.get("fields") or {}).get("date") or ""))
    return found.group(1) if found else None


def _template_text(template) -> str:
    """The template as loaded (comments are gone): field names, line patterns, table columns."""
    data = {k: v for k, v in dict(template).items() if k not in ("template_name", "layout", "keywords",
                                                                    "exclude_keywords")}
    return json.dumps(data, default=str)


def readable(templates: list) -> set[str]:
    """The variants the templates of one family are built to read (all their versions together)."""
    text = " ".join(_template_text(t) for t in templates)
    reads = {name for name, pattern in _READS.items() if pattern.search(text)}
    has_lines = any("lines" in (t.get("fields") or {}) for t in templates)
    # goods with a quantity: the tables where a shipping or handling row turns up between the items
    goods = any(re.search(r"\bqty\b", json.dumps((t.get("fields") or {}).get("lines"), default=str))
                for t in templates)
    labels = {ONE_PAGE, PAGES}
    if "igst" in reads:
        labels.add(IGST)
    if "cgst" in reads and "sgst" in reads:
        labels.add(CGST)
    if reads & {"igst", "cgst", "sgst"}:
        labels.add(ZERO)
    if has_lines:
        labels |= {ONE_ITEM, ITEMS}
    if goods:
        labels.add(CHARGE)
    if "discount" in reads:
        labels.add(DISCOUNT)
    if "round_off" in reads:
        labels.add(ROUND_OFF)
    if "cess" in reads:
        labels.add(CESS)
    return labels


def not_expected(types_dir_or_types) -> dict[str, dict[str, str]]:
    """family -> {variant: reason} from every type's variants.yml."""
    folders = [dt.folder for dt in types_dir_or_types] if not isinstance(types_dir_or_types, (str, Path)) \
        else [p for p in Path(types_dir_or_types).iterdir() if p.is_dir()]
    found: dict[str, dict[str, str]] = {}
    for folder in folders:
        path = Path(folder) / "variants.yml"
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        for family, items in (data.get("not_expected") or {}).items():
            if isinstance(items, dict):
                found.setdefault(str(family), {}).update({str(k): str(v) for k, v in items.items()})
    return found


@dataclass
class Coverage:
    family: str
    doc_type: str
    templates: list[str]
    samples: int
    seen: dict[str, int] = field(default_factory=dict)
    to_collect: list[str] = field(default_factory=list)       # needed, readable, not seen
    if_available: list[str] = field(default_factory=list)     # worth a document, not seen
    cannot_read: list[str] = field(default_factory=list)
    not_expected: dict[str, str] = field(default_factory=dict)
    years: list[str] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)        # recent FAIL / CHECK documents


def exception_problems(catalog) -> list[str]:
    """Names in variants.yml that match no layout or no variant (a typo would otherwise leave the
    variant on the collect list without a word)."""
    from .templates import family_of
    families = {family_of(t) for t in catalog.templates}
    problems = []
    for family, items in not_expected(catalog.types).items():
        if family not in families:
            problems.append(f"variants.yml: {family} is not a template of document_types")
        for label in items:
            if label not in ORDER:
                problems.append(f"variants.yml: {family}: {label!r} is not a variant name "
                                f"(names: {', '.join(ORDER)})")
    return problems


def coverage(catalog, store) -> list[Coverage]:
    """The coverage of every template family of the catalog, in catalog order."""
    exceptions = not_expected(catalog.types)
    from .templates import family_of
    report = []
    for dt in catalog.types:
        families: dict[str, list] = {}
        for template in dt.template_set.templates:
            families.setdefault(family_of(template), []).append(template)
        for family, templates in families.items():
            samples = store.samples_of(family)
            seen: dict[str, int] = {}
            for sample in samples:
                for label in variant_of(sample):
                    seen[label] = seen.get(label, 0) + 1
            can = readable(templates)
            skip = exceptions.get(family, {})
            item = Coverage(family, dt.name, sorted(t["template_name"] for t in templates), len(samples),
                            seen={k: seen[k] for k in ORDER if k in seen},
                            not_expected={k: v for k, v in skip.items()},
                            years=sorted({y for y in map(year_of, samples) if y}),
                            problems=list(store.problems_of(family)) if hasattr(store, "problems_of") else [])
            gst = bool(can & {IGST, CGST})
            for label in ORDER:
                if label in seen or label in skip:
                    continue
                if label in (IGST, CGST) and gst and label not in can:
                    item.cannot_read.append(label)
                elif label in can and label in NEEDED:
                    if label == PAGES and ITEMS not in seen:
                        # a second page comes from a long item table: needed once documents with
                        # several items are known to exist (a one-line fee invoice never runs over)
                        item.if_available.append(label)
                    else:
                        item.to_collect.append(label)
                elif label in can and label in IF_AVAILABLE:
                    item.if_available.append(label)
            report.append(item)
    return report


def new_variants(sample_labels: set[str], earlier: list[dict]) -> list[str]:
    """The labels of a new document that none of the earlier samples of its family had."""
    before = set().union(*(variant_of(s) for s in earlier)) if earlier else set()
    return [label for label in ORDER if label in sample_labels and label not in before]


def keep_on_trim(samples: list[dict], limit: int) -> list[dict]:
    """The samples of one family to keep: the most recent `limit`, except that an older sample
    stays while it is the only one of some variant (the only CGST+SGST document must not be
    dropped because twenty IGST documents came after it)."""
    if len(samples) <= limit:
        return list(samples)
    kept = sorted(samples, key=lambda s: s.get("added", ""))
    labels = {id(s): variant_of(s) for s in kept}
    for old in list(kept):
        if len(kept) <= limit:
            break
        others = set().union(*(labels[id(s)] for s in kept if s is not old))
        if labels[id(old)] <= others:
            kept.remove(old)
    return kept


# --- output --------------------------------------------------------------------------------------

def _seen_text(item: Coverage) -> str:
    return ", ".join(f"{k} ({n})" for k, n in item.seen.items()) or "-"


def print_report(report: list[Coverage], problems: list[str] = ()) -> None:
    print("Variant coverage (real documents read OK, from the sample store)\n")
    for problem in problems:
        print(f"  warning: {problem}")
    if problems:
        print()
    for item in report:
        head = f"{item.family}  [{item.doc_type}]  {item.samples} sample(s)"
        if item.years:
            head += f", dated {item.years[0]}" + (f"-{item.years[-1]}" if len(item.years) > 1 else "")
        print(head)
        if not item.samples:
            print("    no real document stored yet: collect any document of this layout")
        else:
            print(f"    seen:          {_seen_text(item)}")
        if item.to_collect:
            print(f"    TO COLLECT:    {', '.join(item.to_collect)}")
        if item.if_available:
            print(f"    if available:  {', '.join(item.if_available)}")
        if item.cannot_read:
            print(f"    cannot read:   {', '.join(item.cannot_read)} (such a document fails and goes to the AI repair)")
        for label, reason in item.not_expected.items():
            print(f"    not expected:  {label}: {reason}")
        for problem in item.problems[-3:]:
            print(f"    recent {problem['status']}:  {problem['name']} pages {problem['pages']}: {problem['reason'][:110]}")
        print()
    wanted = sum(bool(i.to_collect) or not i.samples for i in report)
    print(f"{len(report)} layout(s); {wanted} need more documents (TO COLLECT or no document yet)")


def write_excel(report: list[Coverage], path: Path) -> Path:
    """Two sheets: Coverage (one row per layout) and To collect (one row per document wanted:
    the list for the testing team). Another name when the file is open in another program."""
    import pandas as pd
    coverage_rows, collect_rows = [], []
    for item in report:
        coverage_rows.append({
            "Layout": item.family, "Type": item.doc_type, "Samples": item.samples, "Seen": _seen_text(item),
            "To collect": ", ".join((["any document"] if not item.samples else []) + item.to_collect),
            "If available": ", ".join(item.if_available), "Cannot read": ", ".join(item.cannot_read),
            "Not expected": "; ".join(f"{k}: {v}" for k, v in item.not_expected.items()),
            "Dated": ", ".join(item.years), "Recent problems": len(item.problems),
            "Templates": ", ".join(item.templates)})
        wanted = (["any document"] if not item.samples else []) + item.to_collect
        for label in wanted:
            collect_rows.append({"Layout": item.family, "Document wanted": _describe(label),
                                 "Priority": "needed", "Variant": label})
        for label in item.if_available if item.samples else []:
            collect_rows.append({"Layout": item.family, "Document wanted": _describe(label),
                                 "Priority": "if available", "Variant": label})
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(10):
        candidate = target if attempt == 0 else target.with_name(f"{target.stem}_{attempt}{target.suffix}")
        try:
            with pd.ExcelWriter(candidate) as writer:
                pd.DataFrame(collect_rows or [{"Layout": "-", "Document wanted": "nothing to collect"}]).to_excel(
                    writer, sheet_name="To collect", index=False)
                pd.DataFrame(coverage_rows).to_excel(writer, sheet_name="Coverage", index=False)
            return candidate
        except PermissionError:
            continue
    raise PermissionError(f"{target} and its alternatives are open in another program")


_DESCRIPTIONS = {
    "any document": "any document of this layout",
    IGST: "seller and buyer in different states (tax printed as IGST)",
    CGST: "seller and buyer in the same state (tax printed as CGST + SGST)",
    ITEMS: "a document with two or more items",
    PAGES: "a document that runs over two or more pages",
    DISCOUNT: "a document with a discount",
    ROUND_OFF: "a document with a round-off amount",
    CESS: "a document with cess (tobacco, aerated drinks, cars and similar goods)",
    CHARGE: "a document with a shipping, delivery or handling charge row",
}


def _describe(label: str) -> str:
    return _DESCRIPTIONS.get(label, label)
