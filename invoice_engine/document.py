"""Parses one document with its candidate templates and chooses between them by evidence.

Every candidate layout (template family) is parsed. Within a family every version is parsed and the
one with the most evidence represents it (on equal evidence, the newest). Between families, a result is accepted only when it is at least as good
as every other non-failing result on each measure of evidence (status, checks passed, share of
the template's fields found) and better on at least one. When no result dominates the others the
document is marked CHECK as ambiguous instead of picking one by name."""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import layout as layout_fp
from . import poppler, safe_regex, words
from .catalog import Catalog
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
    """One item row; a column the row does not print is left out (not kept as an empty value):
    a row of one GST kind has no columns of the other, a charge row has no quantity."""
    row = {}
    for key, value in line.items():
        value = clean(value)
        if registry.is_numeric_line_field(key) and isinstance(value, str):
            value = to_number(value)
        if value is None or value == "":
            continue
        row[key] = value
    return row


def field_pages(template, texts: list[str], pages: list[int], fields: dict) -> dict[str, int]:
    """For every field read with a pattern, the page its pattern first matches on (provenance).
    A value whose pattern only matches across a page break gets no page."""
    def read(spec):
        return not (isinstance(spec, dict) and spec.get("parser") in ("static", "lines", "table"))
    if len(pages) == 1:
        return {name: pages[0] for name in fields if name in template["fields"] and read(template["fields"][name])}
    found: dict[str, int] = {}
    prepared = []
    for text in texts:
        try:
            prepared.append(template.prepare_input(text))
        except Exception:
            prepared.append(text)
    for name in fields:
        spec = template["fields"].get(name)
        if isinstance(spec, dict) and spec.get("parser") in ("static", "lines", "table"):
            continue
        pattern = spec.get("regex") if isinstance(spec, dict) else spec
        for rx in (pattern if isinstance(pattern, list) else [pattern]) if pattern else []:
            try:
                page = next((pg for pg, text in zip(pages, prepared) if safe_regex.search(rx, text)), None)
            except Exception:
                page = None
            if page is not None:
                found[name] = page
                break
    return found


def attempt(template, texts: list[str], pdf: Path, registry: Registry, pages: list[int],
            type_name: str | None = None, rewrite=None, positions: bool = False) -> dict:
    """Parse + validate with one template; never raises. positions: the texts are this PDF's own
    page texts (or the stress test's `rewrite` of them), so the parsers that read word positions
    may read the PDF's words (words.current) instead; a caller that passes other texts (a test)
    leaves it off."""
    record = {"pages": pages, "template": template["template_name"],
              "family": family_of(template), "version": version_of(template)}
    if type_name:
        record["type"] = type_name
    token = words.current.set({"pdf": Path(pdf) if (pdf and positions) else None, "pages": list(pages),
                               "rewrite": rewrite})
    try:
        raw = parse_with(template, texts, pdf)
    except Exception as exc:  # a broken template must not stop the other documents
        words.current.reset(token)
        record.update(status="FAIL", fields={}, lines=[],
                      issues=[{"level": "ERROR", "message": f"template error: {type(exc).__name__}: {exc}"}])
        return record
    words.current.reset(token)
    lines = [clean_line(l, registry) for l in raw.pop("lines", []) or [] if isinstance(l, dict)]
    tax_lines = [clean_line(l, registry) for l in raw.pop("tax_lines", []) or [] if isinstance(l, dict)]
    raw.pop("desc", None)
    raw.pop("template_name", None)
    fields = {key: clean(value) for key, value in raw.items()}
    issues, evidence = check_document(fields, lines, tax_lines, template, registry)
    defined = [name for name, spec in template["fields"].items()
               if not (isinstance(spec, dict) and spec.get("parser") in ("static", "lines", "table"))]
    evidence.update(fields_found=sum(1 for name in defined if fields.get(name) not in (None, "", [])),
                    fields_defined=len(defined))
    record.update(status=status_of(issues), fields=fields, lines=lines,
                  issues=[{"level": level, "message": msg} for level, msg in issues], evidence=evidence,
                  field_pages=field_pages(template, texts, pages, fields))
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


def break_tie_by_layout(tied: list[dict], group: dict, pdf: Path):
    """When no result dominates, compare the document's label positions with each tied
    template's stored fingerprint. Returns (record, note) when one is clearly closest, else None.
    Needs a fingerprint on every tied template and the PDF itself (for word positions)."""
    templates = {t["template_name"]: t for t in group["candidates"]}
    blocks = {r["template"]: templates[r["template"]].get("layout") for r in tied}
    if any(not isinstance(b, dict) or not b.get("order") for b in blocks.values()) or not Path(pdf).exists():
        return None
    try:
        words = layout_fp.page_words(Path(pdf), group["pages"])
    except (RuntimeError, OSError):
        return None
    scores = []
    for record in tied:
        template, block = templates[record["template"]], blocks[record["template"]]
        page = layout_fp.fingerprint(words, layout_fp.label_vocabulary(template), block.get("columns", layout_fp.COLUMNS))
        stored = [list(x) for x in block["order"]]
        scores.append((layout_fp.similarity(page, stored), record))
    scores.sort(key=lambda s: s[0], reverse=True)
    for score, record in scores:
        record.setdefault("evidence", {})["layout_similarity"] = round(score, 2)
    (best, winner), (second, _) = scores[0], scores[1]
    if best < layout_fp.MIN_SIMILARITY or best - second < layout_fp.MIN_MARGIN:
        return None
    shown = ", ".join(f"{r['template']} {s:.2f}" for s, r in scores)
    return winner, f"layout decided the tie: label positions match {winner['template']} best ({shown})"


def _registry(source, template) -> Registry:
    """source = a Registry (one type) or a Catalog (the template's own type's registry)."""
    return source.registry_for(template) if hasattr(source, "registry_for") else source


def _type_name(source, template) -> str | None:
    return source.type_of(template).name if hasattr(source, "type_of") else None


def parse_candidates(group: dict, pdf: Path, source) -> tuple[list[dict], list[dict]]:
    """Parse the document with every candidate. Returns (every attempt, one representative per
    layout family). Every version of a family is tried; the one with the most evidence represents
    the layout, and on equal evidence the newest (versions arrive newest first, max() keeps the first)."""
    attempts, representatives = [], []
    for family in dict.fromkeys(family_of(t) for t in group["candidates"]):
        tried = [attempt(t, group["texts"], pdf, _registry(source, t), group["pages"], _type_name(source, t),
                         positions=True)
                 for t in group["candidates"] if family_of(t) == family]
        attempts.extend(tried)
        representatives.append(max(tried, key=evidence_key))
    return attempts, representatives


def dominant(alive: list[dict]) -> dict | None:
    """The one result at least as good as every other on all measures, if there is one."""
    winners = [a for a in alive if all(a is b or dominates(a, b) for b in alive)]
    return winners[0] if len(winners) == 1 else None


def process_group(group: dict, pdf: Path, registry) -> dict:
    """Parse the document with every candidate layout and keep the one the evidence supports.

    - within a layout family: every version is parsed; the one with the most evidence represents
      it (on equal evidence the newest)
    - one family left that does not FAIL: it is used
    - several: the one that dominates all others is used; if none does, the best-ranked result is
      shown with status CHECK and the alternatives listed (a person decides)
    - all FAIL: the result with the most evidence is reported, so its errors are the useful ones
    """
    attempts, representatives = parse_candidates(group, pdf, registry)

    alive = [r for r in representatives if r["status"] != "FAIL"]
    ambiguous = False
    if not alive:
        chosen = max(representatives, key=evidence_key)
    else:
        winner = dominant(alive)
        ambiguous = winner is None
        chosen = winner if winner is not None else max(alive, key=evidence_key)

    if ambiguous:
        decided = break_tie_by_layout(alive, group, pdf)
        if decided is not None:
            chosen, note = decided
            chosen["issues"].append({"level": "INFO", "message": note})
            ambiguous = False

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


FORMAT_VERSION = 2           # JSON output format; 2 = document types, field pages, copies
THIN_TEXT_CHARS = 200        # a page with less text than this is checked for a page-sized picture
SCAN_COVERAGE = 0.5          # share of the page covered by pictures that marks it as scanned


def picture_coverage(pdf: Path) -> dict[int, float] | None:
    """Share of each page covered by pictures, from Poppler's pdfimages and pdfinfo.
    None when those programs are not available next to pdftotext."""
    images, info = poppler.tool_path("pdfimages"), poppler.tool_path("pdfinfo")
    if not images or not info:
        return None
    try:
        listing = subprocess.run([images, "-list", str(pdf)], capture_output=True, timeout=60).stdout
        sizes = subprocess.run([info, "-f", "1", "-l", "100000", str(pdf)], capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    area = {int(m.group(1)): float(m.group(2)) * float(m.group(3)) / 72 / 72
            for m in re.finditer(r"Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+)", sizes.decode("utf-8", "replace"))}
    covered: dict[int, float] = defaultdict(float)
    for line in listing.decode("utf-8", "replace").splitlines()[2:]:
        cols = line.split()
        if len(cols) < 14 or cols[2] != "image":
            continue
        try:
            page, width, height, xppi, yppi = int(cols[0]), int(cols[3]), int(cols[4]), float(cols[12]), float(cols[13])
        except ValueError:
            continue
        if xppi > 0 and yppi > 0:
            covered[page] += (width / xppi) * (height / yppi)
    return {page: min(covered.get(page, 0.0) / a, 1.0) for page, a in area.items() if a > 0}


# A garbled text layer: the letters come out as other characters ("Tax Invoice" -> "Tmx @jvk`go").
# A PDF re-saved by a compressor or converter that rebuilt its fonts with a wrong character map
# looks right on screen, but its text cannot be read by any template or by the AI. Two signs
# together mark it: almost none of its words is a common invoice word (English, Hindi-English
# invoice terms, German, Dutch), and many words have characters such as ` @ ^ | inside them.
_COMMON_WORDS = set("""the and of to in for on is are with by from at as or this that be not your you our all any
invoice tax total date amount number gst gstin pan no bill ship address name state code place supply
qty quantity price rate value discount payment order customer seller buyer details item description
rs inr only rupees paise round off charges fee service goods hsn sac igst cgst sgst
de der die das und van het een en voor op te met rechnung datum betrag factuur totaal bedrag""".split())
_ODD_INSIDE = re.compile(r"[`^|\\{}~@]")


def garbled_pages(pages: list[str]) -> list[tuple[int, str]]:
    """(page, sample of its text) for pages whose text layer is garbled (see above). Short pages
    (under 40 words) are not judged."""
    found = []
    for number, text in enumerate(pages, start=1):
        tokens = [t for t in re.findall(r"\S+", text) if re.search(r"[A-Za-z]", t)]
        if len(tokens) < 40:
            continue
        common = sum(1 for t in tokens if re.sub(r"[^a-z]", "", t.lower()) in _COMMON_WORDS) / len(tokens)
        odd = sum(1 for t in tokens if _ODD_INSIDE.search(t) and not re.fullmatch(r"\S+@\S+\.\S+", t)) / len(tokens)
        if common < 0.03 and odd > 0.15:
            found.append((number, " ".join(tokens[:4])))
    return found


def scanned_pages(pdf: Path, pages: list[str]) -> list[tuple[int, int, float]]:
    """(page, characters of text, picture coverage) for pages that look scanned: little or no
    text and a picture over at least half the page. Checked only when a page has little text."""
    chars = [len(re.sub(r"\s", "", t)) for t in pages]
    thin = [n for n, c in enumerate(chars, start=1) if c < THIN_TEXT_CHARS]
    if not thin:
        return []
    coverage = picture_coverage(pdf)
    if coverage is None:
        return []
    return [(n, chars[n - 1], coverage.get(n, 0.0)) for n in thin if coverage.get(n, 0.0) >= SCAN_COVERAGE]


def process_pdf(pdf: Path, catalog, registry: Registry | None = None) -> dict:
    """Process one PDF. catalog = a Catalog of document types, or (with registry) a plain list
    of templates that all use that registry."""
    if registry is not None:
        catalog = Catalog.single(catalog, registry)
    result = {"format_version": FORMAT_VERSION, "source_file": pdf.name, "page_count": 0, "documents": [],
              "unrecognized_pages": [], "notes": []}
    warnings: list[str] = []
    try:
        pages = page_texts(pdf, warnings)
    except RuntimeError as exc:
        result["notes"].append(f"ERROR: {exc}")
        return result
    result["page_count"] = len(pages)
    if warnings:
        result["notes"].append(f"pdftotext reported {len(warnings)} problem(s) in this file, so it may be "
                               f"damaged and some text may be missing (first: {warnings[0]})")
    blank = [n for n, text in enumerate(pages, start=1) if not text.strip()]
    scanned = scanned_pages(pdf, pages)
    scanned_numbers = {n for n, _, _ in scanned}
    if pages and len(blank) == len(pages):
        result["notes"].append("ERROR: no page has a text layer: a scanned or image-only PDF. "
                               "OCR is needed, which is not supported yet")
    else:
        if [n for n in blank if n not in scanned_numbers]:
            result["notes"].append(f"pages {[n for n in blank if n not in scanned_numbers]} are blank")
        for n, count, cover in scanned:
            result["notes"].append(f"page {n} looks scanned: {count} characters of text and a picture over "
                                   f"{cover:.0%} of the page. OCR is needed, which is not supported yet")
    garbled = garbled_pages(pages)
    garbled_numbers = {n for n, _ in garbled}
    for n, sample in garbled:
        result["notes"].append(
            f"page {n}: the text layer is garbled ({sample!r}...): the letters come out as other characters, "
            "so no template and no AI can read it. This happens when a PDF is re-saved by a compressor or "
            "converter that rebuilds its fonts wrongly. Use the original PDF from the issuer (or OCR, not "
            "supported yet)")
    groups, notes = split_into_documents(pages, catalog)
    result["notes"].extend(notes)

    for group in groups:
        if group["family"] is None:
            text = "\n".join(group["texts"])
            preview = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
            reason = ("scanned page: OCR needed" if any(n in scanned_numbers for n in group["pages"])
                      else "garbled text layer: use the original PDF" if any(n in garbled_numbers for n in group["pages"])
                      else "no template matches")
            result["unrecognized_pages"].append(
                {"pages": group["pages"], "reason": reason,
                 "preview": preview or ["(no text layer: scanned or blank page)"], "text": text})
            continue
        document = process_group(group, pdf, catalog)
        if group.get("copy_of"):
            document["copy_of"] = group["copy_of"]
            document["issues"].append({"level": "INFO", "message":
                                       f"another copy of the document on pages {group['copy_of']}; "
                                       "not counted again in the totals"})
        result["documents"].append(document)

    totals: dict = defaultdict(lambda: defaultdict(Decimal))
    for doc in result["documents"]:
        amount = to_decimal(doc["fields"].get("amount"))
        if amount is not None and not doc.get("copy_of"):
            totals[doc["fields"].get("document_type", "unknown")][doc["fields"].get("currency", "?")] += amount
    result["totals_by_document_type"] = {k: {c: float(v) for c, v in by.items()} for k, by in totals.items()}
    return result