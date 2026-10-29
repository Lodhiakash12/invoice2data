"""AI template author: writes a template for a document no template reads, or a new version of a
template that fails on a new variant of its layout. Nobody approves the result: a template the AI
writes is used only when it passes the same automatic checks as every other template.

For a page group no template recognizes:
  1. classify   the AI names the document type (one of document_types/, or a new one with its
                signal words and fields) and says whether it is a known issuer's layout
  2. write      the AI writes the template, given the format rules, the type's field names and
                the most similar existing template as an example
  3. test       the engine reads the document with it. Accepted only when
                  - the template loads (lint) and its keywords match the document
                  - the document comes out OK (required fields found, arithmetic checks pass)
                  - no keyword contains a value of this one document (buyer, number, date, amount)
                  - the automatic checks pass: regression on the layout's stored samples, stress
                    test, overlap with every other layout (admission.py)
  4. correct    otherwise the problems go back to the AI, up to ai.max_attempts drafts
  5. keep       the accepted template is written to document_types/<type>/templates/ (with new
                fields in the type's fields.yml, or a new type folder); a template that never
                passes is removed again and its last draft kept in <sample store>/ai_drafts/

For a document a template reads with FAIL, the AI writes the next version of that template
(same family, version + 1). The old version stays; regression on the stored samples makes sure
the new version still reads every earlier document the same way.

Each document is given to the AI once (by file content and pages): a document that could not be
handled is not sent again on every run. --ai-retry sends it again.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .admission import check_template, record
from .ai_provider import AIError
from .catalog import load_catalog
from .document import process_pdf
from .samples import file_hash
from .templates import family_of, version_of

MAX_TEXT = 24000            # characters of page text sent to the AI
_NAME = re.compile(r"[^a-z0-9_]+")


def slug(text: str, fallback: str = "document") -> str:
    value = _NAME.sub("_", str(text or "").lower()).strip("_")
    return value[:60] or fallback


@dataclass
class AuthorResult:
    pdf: str
    pages: list[int]
    action: str                         # new template / new version / skipped
    outcome: str                        # admitted / failed / skipped
    template: str | None = None
    doc_type: str | None = None
    new_type: bool = False
    attempts: int = 0
    problems: list[str] = field(default_factory=list)


# --- the instructions given to the AI ------------------------------------------------------------

SYSTEM = """You write extraction templates for a PDF parsing engine (invoice2data YAML format with
extensions). You are precise: every regular expression you write is tested against the document
text, and a template that reads a wrong value is rejected. Answer exactly in the format asked."""

FORMAT_RULES = r"""TEMPLATE FORMAT (YAML; use single quotes around every regular expression)
Shapes: fields, options, checks, new_fields and new_line_fields are MAPPINGS (name: value), never
lists; keywords, exclude_keywords, continuation_keywords, required_fields and optional_fields are
LISTS of texts.
issuer: <company that issues the document>
keywords: [2 to 4 texts printed on EVERY document of this layout and on no other issuer's
           documents: the issuer's name, its fixed headings. NEVER a value that changes per
           document (customer name or address, numbers, dates, amounts). Each keyword is copied
           exactly from the text and lies inside one column: never across a gap of 2+ spaces.]
exclude_keywords: [optional: texts that appear only on a look-alike layout]
continuation_keywords: [texts printed on EVERY following page of the document that carries no
                        document number (GST details, terms, instructions, annexures); such a
                        page is attached to the document only when all of these are on it.
                        Needed whenever the document has more than one page.]
required_fields: [issuer, document_type, invoice_number, date, amount, seller_name, ...]
optional_fields: [fields printed on some documents of this layout only]
fields:
  document_type: {parser: static, value: tax_invoice}     # tax_invoice, credit_note, receipt, ...
  invoice_number: 'Invoice No\.\s*:\s*(\S+)'             # exactly ONE capture group
  date: 'Invoice Date\s*:\s*(\d{2}/\d{2}/\d{4})'
  seller_address: {parser: regex, regex: '(?s)Sold By\s*\n(.*?)\nGSTIN', replace: [['\s+', ' ']]}
  tax_rate: {parser: regex, regex: '([\d.]+)%\s+IGST', type: float}
  amount: 'Grand Total\s+(\d[\d,]*(?:\.\d{1,3})?)'
  lines:                                                  # the item table
    parser: lines
    start: 'Description\s+Qty\s+Amount'                   # a regex on the header line
    end: '(?m)^\s*Total'                                  # a regex on the line after the table
    first_line: '^\s*(?P<name>\S.*?)\s{2,}(?P<qty>\d+)\s+(?P<price_total>\d[\d,]*(?:\.\d+)?)\s*$'
    line: '^\s{4,}(?P<name>\S.*?)\s*$'                   # continuation lines of a row (optional)
    skip_line: '^\s*Page \d'                              # optional
    types: {qty: int, price_total: float}
  # PREFERRED for item tables that have a header row: read each value by its column heading.
  # Columns the document does not print are simply missing; a printed column you do not list is
  # not read. List every column a document of this issuer CAN print (IGST, or CGST + SGST/UTGST
  # when the seller is in the buyer's state, CESS, Discount), even if this document lacks some.
  # lines:
  #   parser: table
  #   start: '(?m)^\s*Description\s+Qty'          # a regex on the first header line
  #   end: '(?m)^\s*Total'                          # a regex on the line after the table
  #   columns: {name: 'Description', qty: 'Qty', price_unit: 'Rate', price_subtotal: 'Taxable',
  #             igst_amount: 'IGST', cgst_amount: 'CGST', sgst_amount: 'SGST', price_total: 'Total'}
  #   row: [price_total]                             # a row starts on a line with these values
  #   text: after                                    # description lines below the amounts row
  #   extra: ['^\s+HSN:\s*(?P<hsn_sac>\d+)']        # optional patterns for other row lines
  #   types: {qty: int, price_subtotal: float, igst_amount: float, cgst_amount: float, ...}
checks:                                                   # optional, see CHECKS
  disable: [qty x unit price]
  line: [{name: ..., rule: "qty * (price_unit - discount_unit) == price_total", tolerance: "0.01"}]
  document: [{name: ..., rule: "amount_untaxed + amount_cgst + amount_sgst == amount"}]
options:
  currency: INR
  decimal_separator: '.'
  date_formats: ['%d/%m/%Y']                              # REQUIRED when dates are numeric
  replace: [['\r', '']]

TEXT FORM: the text below is what the engine gives every template: pdftotext -layout output,
put in a standard form: "Label :" is written "Label:", trailing spaces removed, shared
indentation removed, at most one blank line in a row. Columns are separated by 2+ spaces.

PATTERN RULES
- Accept small print variations: \s* around colons, \s+ between words, amounts as
  (\d[\d,]*(?:\.\d{1,3})?) so 212.1, 212.10 and 17.582 all match.
- Use (?m) with ^ or $, (?s) when . must cross lines. Never write a pattern that can take a
  value from a different column of the same line: stop at 2+ spaces (\s{2,}) when a right-hand
  column follows.
- Blocks printed side by side (Bill To | Ship To, Sold By | Billing Address): read a block with
  parser: region, which takes the words below a heading inside its column from their positions on
  the page, so the next column cannot run into it:
    buyer_name: {parser: region, heading: 'Bill To', max_lines: 1}
    buyer_address: {parser: region, heading: 'Bill To', skip: 1, stop: '^Phone'}
  (skip: lines left out at the top; stop: the line that ends the block, not included; join: what
  goes between the lines, default one space, ', ' for an address printed without commas). A
  label with its value on the same line and more lines hanging under the value ("Address: Flat
  12, ..." | "Place of Supply: ..." on the right) is read with same_line: true; the next label
  at the left ("Pincode:") ends it:
    buyer_address: {parser: region, heading: '^Address:', same_line: true}
  The heading is a regex on the heading's line as it reads ("Address:" even when the PDF draws
  the colon apart). Use region for left-aligned blocks; a block aligned right or centred under
  its heading, or one without a heading, needs a regex.
- Only field names from the FIELD LIST below. A field the list lacks may be declared in
  new_fields (document level) or new_line_fields (item table), with type text, date, money,
  rate, count or number; money fields that are part of the total need a role
  (tax, untaxed, total, other).
- invoice_number is the document's own number and contains digits. When the document prints its
  invoice number as N.A. or NA (e-tickets, booking slips), read the number that identifies this
  transaction instead (transaction ID, booking or order number), never a label or heading.
- amount is the final total payable of the document. Read every printed total you can (taxable
  value, each tax, round off) so the arithmetic checks can prove the values.
- ASCII only in the YAML; write a non-ASCII character as an escape such as '₹'.

INDIAN INVOICE VARIATIONS: the template must read the NEXT documents of this layout too, not only
this one. Cover these even when this document does not show them:
- Tax: a seller in another state charges IGST; in the buyer's state CGST + SGST (UTGST in a union
  territory); CESS can be added. An item table lists igst_amount, cgst_amount AND sgst_amount
  (heading 'SGST|UTGST'), and cess_amount when a CESS heading may appear; document totals read
  amount_igst, amount_cgst and amount_sgst as optional_fields. Use parser: table for the items.
- Rows: several items; descriptions wrapping over lines; charge rows (shipping, handling, COD,
  gift wrap, platform or convenience fee) with or without qty and discount.
- Pages: the table can continue on the next page with its header printed again; later pages need
  continuation_keywords printed on every page.
- Numbers: 1,499.00 / 1499 / 1,499.0 / 1,23,456.00; minus as -135.00, (135.00) or 135.00-; the
  rupee sign before or after, joined or apart. Amount patterns: (-?\d[\d,]*(?:\.\d{1,3})?).
- Units: weight in g or kg; quantity as 2 or "2 Nos".
- Optional parts: buyer GSTIN (business purchases only), CIN, FSSAI, state, place of supply are
  printed on some documents only: list them in optional_fields, never in required_fields.

CHECKS: by default the engine checks, where the values exist (names in brackets):
  per line: qty * price_unit - discount = line total [qty x unit price]; gross - discount
  [gross - discount]; taxable + taxes = line total [taxable value + tax]; rate * taxable = tax
  [cgst rate], [sgst rate], [igst rate], [cess rate], [line tax rate]
  per document: [line tax vs document tax]; [untaxed + tax + adjustments = total];
  [paid + due = total]; [due = total]; and for every line column the sum of the lines equals the
  document total it adds up to ("sum of line price_total vs amount", ...).
A document is OK only when at least one check with non-zero values passes. Disable a default
check (by its name in brackets) only when the document's own arithmetic differs (for example MRP
includes tax), and add the rule it does follow. The "sum of line ..." checks can never be
disabled: when the lines do not add up to the total, a row is missing or misread, so fix the
line patterns (every row of the table must be read, including fees and charges). Only when every
row is read and the document prints its total rounded (to whole rupees) without a round-off
line, add the top-level settings total_rounding: 1 and total_rounding_mode: up (the total is the
sum rounded up, e.g. 518.15 -> 519), nearest (1378.40 -> 1378) or down; the check then compares the
rounded sum exactly. When this document fits more than one rule (1378.60 -> 1379 is both up and
nearest), list every rule that fits: total_rounding_mode: [up, nearest]; a later document settles it."""


def _classify_prompt(text: str, catalog) -> str:
    types = []
    for dt in catalog.types:
        issuers = sorted({f"{t.get('issuer')} ({t['template_name']})" for t in dt.template_set.templates})
        types.append(f"- {dt.name}: {dt.description}. Signals: {', '.join(dt.signals)}.\n"
                     f"  Known layouts: {'; '.join(issuers) or 'none'}")
    return f"""Classify this document.

DOCUMENT TYPES (folders of the engine):
{chr(10).join(types)}

Answer with one JSON object only:
{{"document_type": "<one of the type names above, or a new snake_case name>",
  "is_new_type": false,
  "new_type": null,
  "issuer": "<company that issued the document>",
  "variant_of": "<template file name from Known layouts when this is the same issuer's same kind of
                  document in a changed layout, else null>",
  "template_name": "<snake_case name for a new template: issuer and document kind>",
  "reason": "<one sentence>"}}
Use a new type only when no type fits (a bank statement is not an invoice). For a new type,
new_type is {{"name": ..., "description": ..., "signals": [4 to 6 lower-case words or phrases
that appear on such documents], "min_signals": 2, "summary_columns": [field names]}}.

DOCUMENT TEXT:
{text}"""


def _field_list(registry) -> str:
    doc = ", ".join(f"{n} ({s.type}{', ' + s.role if s.role else ''})" for n, s in registry.document.items())
    line = ", ".join(f"{n} ({s.type})" for n, s in registry.line.items())
    return f"Document fields: {doc}\nItem table (line) fields: {line}"


def _write_prompt(text: str, registry, example: str | None, decision: dict, base: str | None) -> str:
    parts = [FORMAT_RULES, "", "FIELD LIST", _field_list(registry), ""]
    if base:
        parts += ["CURRENT TEMPLATE OF THIS LAYOUT (it fails on this document; write the corrected "
                  "next version: keep everything that works, change what this document needs):", base, ""]
    elif example:
        parts += ["EXAMPLE OF A WORKING TEMPLATE (another issuer; for the format only):", example, ""]
    parts += [f"Document type: {decision.get('document_type')}; issuer: {decision.get('issuer')}.",
              "Write the template for the document below. Answer with the YAML in one ```yaml block. "
              "Add new_fields / new_line_fields at the top level only when needed.", "",
              "DOCUMENT TEXT:", text]
    return "\n".join(parts)


def _fix_prompt(draft: str, problems: list[str], read: dict | None, corrections: list[str] = (),
                text: str = "") -> str:
    shown = ""
    if read:
        shown = ("\nWHAT THE ENGINE READ WITH THIS DRAFT:\n"
                 + json.dumps({"status": read.get("status"), "fields": read.get("fields"),
                               "lines": (read.get("lines") or [])[:8]}, default=str, indent=1)[:6000])
    fixed = ("\nTHE ENGINE ALREADY CORRECTED IN YOUR DRAFT (keep these corrections):\n- "
             + "\n- ".join(corrections[:10]) + "\n") if corrections else ""
    return ("Your template was tested on the document and rejected:\n- " + "\n- ".join(problems[:12])
            + shown + fixed + "\n\nWrite the corrected complete template in one ```yaml block. Change only what "
            "the problems above require and keep every other line of your draft exactly as it is: parts "
            "that are not listed work. Check every pattern you change against the DOCUMENT TEXT below.\n\n"
            "YOUR DRAFT:\n" + draft + ("\n\nDOCUMENT TEXT (the same document):\n" + text if text else ""))


# --- reading the AI's answers ---------------------------------------------------------------------

def _block(answer: str, language: str) -> str | None:
    found = re.findall(r"```(?:" + language + r")?\s*\n(.*?)```", answer, re.S | re.I)
    return found[-1] if found else None


def _json(answer: str) -> dict:
    """The JSON object in an answer: in a ```json block, or the first {...} that parses."""
    answer = re.sub(r"(?s)<think>.*?</think>", "", answer)
    block = _block(answer, "json")
    if block:
        return json.loads(block)
    decoder = json.JSONDecoder()
    for start in [m.start() for m in re.finditer(r"\{", answer)]:
        try:
            value, _ = decoder.raw_decode(answer[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"no JSON object in the answer (it begins: {answer.strip()[:120]!r})")


def _yaml(answer: str) -> tuple[str, dict]:
    answer = re.sub(r"(?s)<think>.*?</think>", "", answer)
    text = _block(answer, "ya?ml") or answer
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("the answer is not a YAML mapping")
    # a common slip: the whole template under one key (template: {issuer: ..., keywords: ...})
    if "keywords" not in data and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, dict) and "keywords" in inner:
            data = inner
    return text, data


def _fit_keywords(data: dict, text: str) -> tuple[list[str], list[str]]:
    """Keywords must be exact pieces of the page text. A model often writes column text with
    single spaces ("Booked From To") where the page has many. Such a keyword is replaced by the
    exact text from the page (it then matches by construction); an exclude keyword printed on
    this very document is dropped (it would block it). Returns (problems, corrections)."""
    problems, notes = [], []
    for key in ("keywords", "continuation_keywords"):
        fitted = []
        for keyword in data.get(key) or []:
            keyword = str(keyword)
            if keyword in text:
                fitted.append(keyword)
                continue
            words = keyword.split()
            found = re.search(r"[ \t]+".join(map(re.escape, words)), text) if words else None
            if found:
                # a text across a column gap (2+ spaces) breaks when the columns shift: keep the
                # longest piece inside one column
                piece = max(re.split(r"[ \t]{2,}", found.group(0)), key=len)
                if len(piece) >= 6:
                    fitted.append(piece)
                    notes.append(f"{key}: {keyword!r} written as printed in one column: {piece!r}")
                elif key == "keywords":
                    problems.append(f"keyword {keyword!r} spans several columns of the page; use a text "
                                    "inside one column")
            elif key == "keywords":
                problems.append(f"keyword {keyword!r} is not in the document text (not even with other "
                                "spacing): choose a text that is printed on the page, on one line")
        if key in data:
            data[key] = fitted
    excluded = [k for k in data.get("exclude_keywords") or [] if str(k) in text]
    if excluded:
        data["exclude_keywords"] = [k for k in data["exclude_keywords"] if k not in excluded]
        notes += [f"exclude_keywords: {k!r} dropped (this document prints it)" for k in excluded]
    if not data.get("keywords"):
        problems.append("no keyword is left that is printed on this document")
    return problems, notes


def _rounding_hint(doc: dict) -> list[str]:
    """A total off by less than one unit and printed as a whole number may be rounded. Every rule
    that turns the sum into the printed total is named: one sample can fit more than one."""
    from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
    for issue in doc.get("issues", []):
        found = re.search(r"sum of line price_total vs amount: calculated ([\d.]+), printed ([\d.]+)", issue["message"])
        if not found:
            continue
        calculated, printed = Decimal(found.group(1)), Decimal(found.group(2))
        if printed != printed.to_integral_value() or not 0 < abs(printed - calculated) < 1:
            continue
        rounded = {mode: calculated.quantize(Decimal(1), how) for mode, how in
                   (("up", ROUND_CEILING), ("nearest", ROUND_HALF_UP), ("down", ROUND_FLOOR))}
        fits = [mode for mode, value in rounded.items() if value == printed]
        if not fits:
            continue
        setting = fits[0] if len(fits) == 1 else "[" + ", ".join(fits) + "]"
        return [f"the lines add up to {calculated:.2f} and the printed total {printed:.2f} is that sum rounded "
                f"({' or '.join(fits)}) to whole rupees: if every row is read and the document prints no round-off "
                f"line, add the top-level settings total_rounding: 1 and total_rounding_mode: {setting}"
                + (" (this document fits more than one rule: list them all; a later document settles it)"
                   if len(fits) > 1 else "")]
    return []


def _duplicate_fields(data: dict) -> list[str]:
    """Two amount fields read with the same pattern hold the same value by construction: one of them
    is not what its name says (amount_untaxed read from the total line makes a check pass with a
    wrong value)."""
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    seen: dict[str, str] = {}
    problems = []
    for name, spec in fields.items():
        pattern = spec.get("regex") if isinstance(spec, dict) else spec
        if not name.startswith("amount") or not isinstance(pattern, str):
            continue
        if pattern in seen:
            problems.append(f"{name} and {seen[pattern]} use the same pattern, so they read the same value; "
                            f"read {name} from its own printed label, or leave it out if the document does "
                            "not print it")
        seen.setdefault(pattern, name)
    return problems


# printed on some documents of a layout only (business purchases, registered sellers, same-state sales)
_SOMETIMES_PRINTED = {"buyer_tax_id", "buyer_state", "buyer_state_code", "seller_company_reg", "seller_fssai",
                      "place_of_supply", "amount_igst", "amount_cgst", "amount_sgst", "amount_utgst",
                      "amount_cess", "amount_discount", "amount_round_off"}
# tax amounts of which a document prints one kind (IGST) or the other (CGST + SGST)
_GST_DOCUMENT = ("amount_igst", "amount_cgst", "amount_sgst")
_GST_LINE = ("igst_amount", "cgst_amount", "sgst_amount")


def _item_table(data: dict) -> dict | None:
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    spec = fields.get("lines")
    if isinstance(spec, dict) and spec.get("parser") in ("lines", "table"):
        return spec
    return data.get("lines") if isinstance(data.get("lines"), dict) else None


def _line_groups(spec: dict) -> set[str]:
    groups = set()
    for key in ("first_line", "line", "last_line"):
        value = spec.get(key)
        for rx in value if isinstance(value, list) else [value] if value else []:
            try:
                groups |= set(re.compile(rx).groupindex)
            except (re.error, TypeError):
                pass
    return groups


def _gst_coverage(data: dict) -> list[str]:
    """A template written from an IGST document must also read CGST + SGST documents of the same
    layout (and the other way round): the seller's state decides which a document prints."""
    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    if str(options.get("currency", "INR")).upper() != "INR":
        return []
    problems = []
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    present = [f for f in _GST_DOCUMENT if f in fields]
    if present and len(present) < len(_GST_DOCUMENT):
        missing = [f for f in _GST_DOCUMENT if f not in fields]
        problems.append(f"the totals read {', '.join(present)} but not {', '.join(missing)}: a document of this "
                        "layout prints IGST (seller in another state) or CGST + SGST (same state); read all three "
                        "and list them in optional_fields")
    spec = _item_table(data)
    if spec:
        if spec.get("parser") == "table":
            columns = spec.get("columns") if isinstance(spec.get("columns"), dict) else {}
            listed = [c for c in _GST_LINE if c in columns]
            if listed and len(listed) < len(_GST_LINE):
                missing = [c for c in _GST_LINE if c not in columns]
                problems.append(f"the item table lists {', '.join(listed)} but not {', '.join(missing)}: add those "
                                "columns (with their headings and type float); a column the document does not "
                                "print is simply not read")
            heading = columns.get("sgst_amount")
            try:
                if heading and not re.search(heading, "UTGST"):
                    problems.append("the sgst_amount heading should also match UTGST (union territories): "
                                    "for example 'SGST|UTGST'")
            except (re.error, TypeError):
                pass
        else:
            groups = _line_groups(spec)
            has_igst = "igst_amount" in groups
            has_split = "cgst_amount" in groups and "sgst_amount" in groups
            if (has_igst or "cgst_amount" in groups or "sgst_amount" in groups) and not (has_igst and has_split):
                problems.append("the item rows are read for one kind of GST only; a document of this layout can "
                                "print IGST or CGST + SGST columns: use parser: table with columns igst_amount, "
                                "cgst_amount and sgst_amount (or row patterns for both)")
    return problems


_STRESS_HINTS = {
    "short decimals": "amounts can be printed without decimals (202) or with one (212.1): use "
                      "(\\d[\\d,]*(?:\\.\\d{1,3})?)",
    "wider column gaps": "columns can stand further apart: use \\s+ or \\s{2,}, never a fixed number of spaces",
    "page shifted right": "lines can start further right: allow leading spaces (^\\s*) instead of ^ at the "
                          "first character",
    "space before colon": "a label can be printed as 'Name :' : use \\s*: after labels",
    "no space before colon": "a label can be printed as 'Name:' : use \\s*: after labels",
    "more space between sections": "blank lines can repeat: use \\s* or (?:\\n\\s*)+ where a pattern crosses lines",
}


def _stress_problems(notes: list[str]) -> list[str]:
    """The automatic stress test rewrites the document as other prints of the layout would show it
    (spacing, decimals). A template written by the AI must keep every value on each rewrite."""
    problems = []
    for note in notes:
        found = re.match(r"stress: (.+?) on (.+?): (.*)", note)
        if found:
            hint = _STRESS_HINTS.get(found.group(1), "")
            problems.append(f"when the document is printed with {found.group(1)}, the template loses values "
                            f"({found.group(3)[:160]})" + (f": {hint}" if hint else ""))
    return problems


def _empty_fields(data: dict, doc: dict) -> list[str]:
    """A template is written from this one document, so each field it defines must find a value on
    it: a field that finds nothing has a broken pattern (or reads something this document does
    not print, which cannot be tested). Either way it would stay empty on every later document."""
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    found = doc.get("fields") or {}
    optional = set(data.get("optional_fields") or []) if isinstance(data.get("optional_fields"), list) else set()
    # the other GST kind (CGST + SGST on an IGST document) is expected to be empty on this document
    variant = optional & set(_GST_DOCUMENT + ("amount_utgst", "amount_cess"))
    empty = [name for name, spec in fields.items()
             if not (isinstance(spec, dict) and spec.get("parser") in ("static", "lines", "table"))
             and name not in variant and found.get(name) in (None, "", [])]
    if not empty:
        return []
    return [f"these fields found nothing on this document: {', '.join(empty)}. Check each pattern against "
            "the DOCUMENT TEXT character by character (labels, dots, single vs. 2+ spaces) and fix it, "
            "or remove the field if this document does not print it"]


def _needless_disables(template, doc: dict, catalog, type_dir: Path) -> list[str]:
    """Default checks the draft disables that do not fail on this document: disabling them only
    removes protection from later documents of the layout."""
    from .validate import check_document
    checks = template.get("checks") or {}
    disabled = list(checks.get("disable") or [])
    registry = next((t.registry for t in catalog.types if Path(t.folder) == Path(type_dir)), None)
    if not disabled or registry is None:
        return []
    needless = []
    for name in disabled:
        trial = dict(template)
        trial["checks"] = dict(checks, disable=[n for n in disabled if n != name])
        try:
            issues, _ = check_document(dict(doc.get("fields") or {}), list(doc.get("lines") or []),
                                       list(doc.get("tax_lines") or []), trial, registry)
        except Exception:
            continue
        if not any(level == "ERROR" for level, _ in issues):
            needless.append(name)
    return needless


def _page_texts(text: str) -> dict[int, str]:
    """The joined document text ("=== page n ===" marks) split back into pages."""
    parts = re.split(r"(?m)^=== page (\d+) ===$\n?", text)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _leftover_pages(text: str, pages, doc: dict) -> list[str]:
    """Pages of the document the template did not take: without continuation_keywords that are on
    them, the engine reports them as a separate, unrecognized document."""
    left = [p for p in pages if p not in doc.get("pages", [])]
    if not left:
        return []
    texts = _page_texts(text)
    suggestions = []
    for p in left:
        labels = [line.strip() for line in (texts.get(p) or "").splitlines()
                  if re.fullmatch(r"[A-Za-z][A-Za-z /&()'.-]{3,40}:", line.strip())]
        suggestions.append(f"page {p}: " + (", ".join(repr(x) for x in labels[:6]) or "no heading found"))
    return [f"page(s) {', '.join(map(str, left))} of this document were not attached to it (they carry no "
            "document number). Add continuation_keywords: 1 to 3 fixed headings printed on every such "
            "page, each inside one column, never a value of this document. Headings found: "
            + "; ".join(suggestions)]


def _table_feedback(spec: dict, page: str) -> list[str]:
    """For a table read by its headings: which headings were found in the header and which not."""
    from . import table
    try:
        start = re.search(spec["start"], page)
    except (re.error, TypeError):
        return []
    if not start:
        return ["the table start pattern does not match the page text, so no rows are read"]
    line_start = page.rfind("\n", 0, start.start()) + 1
    header = []
    for line in page[line_start:].split("\n"):
        if not line.strip() or len(header) >= int(spec.get("header_lines", 4)):
            break
        header.append(line)
    columns = spec.get("columns") if isinstance(spec.get("columns"), dict) else {}
    try:
        found = {c.field for c in table.find_columns(header, columns, spec.get("types") or {})}
    except re.error:
        return []
    missing = [f"{f} ({h!r})" for f, h in columns.items() if f not in found]
    return ["table headings found in the header: " + (", ".join(sorted(found)) or "none")
            + ("; not found: " + ", ".join(missing) if missing else "")
            + ". Every amount on a row must sit under a heading listed in columns, and the header "
            "must end with a blank line (or set header_lines)"]


def _unread_rows(data: dict, text: str) -> list[str]:
    """When the lines do not add up: the rows between the table's start and end that none of the
    line patterns read. Those are the rows the model has to cover."""
    spec = (data.get("fields") or {}).get("lines") if isinstance(data.get("fields"), dict) else None
    spec = spec if isinstance(spec, dict) else data.get("lines") if isinstance(data.get("lines"), dict) else None
    if not spec or not spec.get("start") or not spec.get("end"):
        return []
    page = re.sub(r"(?m)^=== page \d+ ===$", "", text)
    if spec.get("parser") == "table":
        return _table_feedback(spec, page)
    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    for pair in options.get("replace") or []:          # the template's own clean-up comes first
        try:
            page = re.sub(pair[0], pair[1], page)
        except (re.error, TypeError, IndexError):
            pass
    try:
        start = re.search(spec["start"], page)
        end = re.compile(spec["end"]).search(page, start.end()) if start else None
        patterns = [re.compile(spec[k]) for k in ("first_line", "line", "last_line", "skip_line") if spec.get(k)]
    except (re.error, TypeError, KeyError):
        return []
    if not start or not end:
        return ["the table start or end pattern does not match the page text, so no rows are read"]
    rows = page[start.end():end.start()].split("\n")
    unread = [r.strip() for r in rows if r.strip() and not any(p.search(r) for p in patterns)]
    if not unread:
        return ["every row between the table's start and end is read; check that end does not cut off the "
                "last rows and that amounts with commas (1,234.00) are matched"]
    return ["rows of the table that no line pattern reads (each must be read): "
            + " | ".join(repr(r[:100]) for r in unread[:8])]


_ANCHOR = re.compile(r"(?<!\\)[\^$]")


def _repair_mechanical(data: dict) -> list[str]:
    """Mechanical slips the meaning of which is clear: a field pattern with ^ or $ but without
    (?m) means "at a line start/end", so (?m) is added. Returns notes."""
    notes = []
    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    for key in ("total_rounding", "total_rounding_mode"):
        if key in options:
            value = options.pop(key)
            data.setdefault(key, value)
            notes.append(f"{key} moved from options to the top level (the engine reads it there)")
    # parts printed on some documents only are optional, never required
    required = data.get("required_fields") if isinstance(data.get("required_fields"), list) else []
    fields_ = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    moved = [n for n in required if n in _SOMETIMES_PRINTED]
    if moved:
        data["required_fields"] = [n for n in required if n not in _SOMETIMES_PRINTED]
        optional = data.get("optional_fields") if isinstance(data.get("optional_fields"), list) else []
        data["optional_fields"] = optional + [n for n in moved if n in fields_ and n not in optional]
        notes.append(f"moved from required_fields to optional_fields (printed on some documents only): "
                     f"{', '.join(moved)}")
    for name, spec in (data.get("fields") or {}).items():
        holder, key = (spec, "regex") if isinstance(spec, dict) else (data["fields"], name)
        pattern = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(pattern, str) and _ANCHOR.search(pattern.replace("[^", "")) and "(?m)" not in pattern:
            flags = re.match(r"\(\?([a-z]+)\)", pattern)
            holder[key] = f"(?m{flags.group(1)})" + pattern[flags.end():] if flags else "(?m)" + pattern
            notes.append(f"field {name}: (?m) added (the pattern uses ^ or $)")
    return notes


def _drop_broken_optional(data: dict, errors: list[str]) -> tuple[list[str], list[str]]:
    """Lint errors of fields that are not required: the field is removed (a wrong pattern for a
    minor field must not sink a template whose required fields and arithmetic are right).
    Returns (remaining errors, notes)."""
    required = set(data.get("required_fields") or [])
    fields = data.get("fields") or {}
    remaining, notes = [], []
    for error in errors:
        found = re.match(r"field (\w+)", error)
        name = found.group(1) if found else None
        if name and name in fields and name not in required and name != "lines":
            del fields[name]
            notes.append(f"field {name} removed: {error}")
        else:
            remaining.append(error)
    return remaining, notes


def _structure_problems(data: dict) -> list[str]:
    """What invoice2data needs to read a template at all (it skips the file silently otherwise)."""
    problems = []
    if not data.get("keywords"):
        problems.append("the template has no keywords: it needs a top-level list `keywords:` with 2 to 4 "
                        f"texts from the document (top-level keys found: {', '.join(map(str, data)) or 'none'})")
    if not isinstance(data.get("fields"), dict) or not data.get("fields"):
        problems.append("the template has no top-level `fields:` mapping (name: pattern)")
    if not data.get("issuer"):
        problems.append("the template has no top-level `issuer:`")
    return problems


def _example_for(catalog, type_name: str, text: str) -> str | None:
    """The existing template of this type that shares the most words with the document."""
    dt = catalog.type_named(type_name)
    if dt is None or not dt.template_set.templates:
        return None
    words = set(re.findall(r"[a-z]{4,}", text.lower()))
    def score(t):
        return len(words & set(re.findall(r"[a-z]{4,}", " ".join(str(k) for k in t.get("keywords") or []).lower())))
    best = max(dt.template_set.templates, key=score)
    return _template_text(dt.folder / "templates" / best["template_name"])


def _template_text(path: Path) -> str:
    return re.split(r"(?m)^layout:\s*$", path.read_text(encoding="utf-8"))[0].rstrip() + "\n"


def _keyword_problems(data: dict, read: dict | None) -> list[str]:
    """Keywords must not contain values of this one document."""
    if not read:
        return []
    values = []
    for name, value in (read.get("fields") or {}).items():
        if name.startswith(("buyer_", "invoice_number", "order_number", "date", "amount", "irn")):
            for v in (value if isinstance(value, list) else [value]):
                v = str(v)
                # a number, date or reference has digits; a word without digits read into such a
                # field is a misread label (reported by the other checks), not a document value
                if name.startswith("buyer_") or re.search(r"\d", v):
                    values.append(v)
    problems = []
    for keyword in list(data.get("keywords") or []) + list(data.get("continuation_keywords") or []):
        for value in values:
            if len(value) >= 4 and value.lower() in str(keyword).lower():
                problems.append(f"keyword {keyword!r} contains {value!r}, a value of this one document; "
                                "keywords must be texts every document of the layout prints")
    return problems


# --- the author ---------------------------------------------------------------------------------

class TemplateAuthor:
    def __init__(self, provider, settings, types_dir: Path, config_dir: Path, store, retry: bool = False):
        self.provider = provider
        self.settings = settings
        self.types_dir = Path(types_dir)
        self.config_dir = Path(config_dir)
        self.store = store
        self.retry = retry

    # entry points --------------------------------------------------------------------------
    def new_template(self, pdf: Path, pages: list[int], texts: list[str], catalog) -> AuthorResult:
        result = AuthorResult(Path(pdf).name, pages, "new template", "failed")
        if self._seen(pdf, pages, result):
            return result
        text = self._joined(texts, pages)
        decision = self._classify(text, catalog, pdf, result)
        if decision is None:
            return self._done(pdf, pages, result)
        base_name = decision.get("variant_of")
        base = next((t for t in catalog.templates if t["template_name"] == base_name), None) if base_name else None
        if base is not None:
            result.action = "new version"
            return self._write(pdf, pages, text, catalog, decision, result, base)
        return self._write(pdf, pages, text, catalog, decision, result, None)

    def _classify(self, text: str, catalog, pdf, result) -> dict | None:
        """The AI's classification; asked again once when the answer cannot be read; without a
        readable answer the engine's own routing (signal words) picks the type."""
        prompt = _classify_prompt(text, catalog)
        for attempt in range(2):
            try:
                decision = _json(self._ask(prompt, "classify", pdf))
                if isinstance(decision, dict) and decision.get("document_type"):
                    return decision
                problem = "the JSON object has no document_type"
            except ValueError as exc:
                problem = str(exc)
            except AIError as exc:
                result.problems.append(f"AI service: {exc}")
                return None
            prompt = (_classify_prompt(text, catalog) + "\n\nYour previous answer could not be read "
                      f"({problem}). Answer with the JSON object only, no other text.")
        routed = catalog.route(text)
        if len(routed) != 1:
            result.problems.append(f"classification failed twice ({problem}) and the signal words do not "
                                   "point to one document type")
            return None
        first = next((line.strip() for line in text.splitlines() if line.strip() and not line.startswith("===")), "")
        return {"document_type": routed[0].name, "issuer": first[:60], "variant_of": None,
                "template_name": slug(first, "document")[:40], "reason": "routing by signal words"}

    def new_version(self, pdf: Path, doc: dict, texts: list[str], catalog) -> AuthorResult:
        result = AuthorResult(Path(pdf).name, doc["pages"], "new version", "failed")
        if self._seen(pdf, doc["pages"], result):
            return result
        base = next((t for t in catalog.templates if t["template_name"] == doc["template"]), None)
        if base is None:
            result.outcome, result.problems = "skipped", ["template not found"]
            return result
        decision = {"document_type": catalog.type_of(base).name, "issuer": base.get("issuer"),
                    "template_name": family_of(base)}
        return self._write(pdf, doc["pages"], self._joined(texts, doc["pages"]), catalog, decision, result, base,
                           first_problems=[i["message"] for i in doc.get("issues", []) if i["level"] == "ERROR"])

    # drafting and testing --------------------------------------------------------------------
    def _write(self, pdf, pages, text, catalog, decision, result, base, first_problems=None) -> AuthorResult:
        type_name = slug(decision.get("document_type"), "invoice")
        new_type = catalog.type_named(type_name) is None
        result.doc_type, result.new_type = type_name, new_type
        if new_type and not decision.get("new_type"):
            result.problems.append(f"the AI named an unknown document type {type_name!r} without describing it")
            return self._done(pdf, pages, result)
        undo = _Undo()
        try:
            type_dir = self.types_dir / type_name
            if new_type:
                self._create_type(type_dir, decision["new_type"], undo)
            registry = load_catalog(self.types_dir, self.config_dir).type_named(type_name).registry
            if base is not None:
                base_path = catalog.type_of(base).folder / "templates" / base["template_name"]
                name, prompt = self._version_name(catalog, base), _write_prompt(
                    text, registry, None, decision, _template_text(base_path))
                if first_problems:
                    prompt += "\n\nTHE CURRENT TEMPLATE FAILS ON THIS DOCUMENT WITH:\n- " + "\n- ".join(first_problems[:8])
            else:
                name = self._free_name(catalog, decision.get("template_name") or decision.get("issuer"))
                prompt = _write_prompt(text, registry, _example_for(catalog, type_name, text), decision, None)
            target = type_dir / "templates" / name
            draft, read, data = "", None, None
            for attempt in range(1, max(1, self.settings.max_attempts) + 1):
                result.attempts = attempt
                answer = self._ask(prompt, f"write {name} (draft {attempt})", pdf)
                try:
                    draft, data = _yaml(answer)
                    problems, read = self._test(draft, data, target, type_dir, base, pdf, pages, undo, text)
                except (ValueError, yaml.YAMLError) as exc:
                    problems, read = [f"the answer is not a valid YAML template: {exc}"], None
                except AIError:
                    raise
                except Exception as exc:  # a draft in an unexpected shape must not stop the run
                    problems, read = [f"the draft could not be tested ({type(exc).__name__}: {exc}); "
                                      "check that every section has the shape shown in the format"], None
                if not problems:
                    result.outcome, result.template, result.problems = "admitted", name, []
                    undo.commit()
                    return self._done(pdf, pages, result)
                result.problems = problems
                corrections = getattr(self, "corrections", [])
                shown = draft or answer
                if corrections and isinstance(data, dict):     # the draft as the engine corrected it
                    shown = yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=200)
                prompt = _fix_prompt(shown, problems, read, corrections, text)
            self._keep_draft(name, draft, result.problems)
        except AIError as exc:
            result.problems.append(f"AI service: {exc}")
        except Exception as exc:  # never lose the run to the AI step
            result.problems.append(f"AI step stopped: {type(exc).__name__}: {exc}")
        undo.rollback()
        return self._done(pdf, pages, result)

    def _test(self, draft: str, data: dict, target: Path, type_dir: Path, base, pdf, pages, undo,
              text: str = "") -> tuple[list[str], dict | None]:
        """Write the draft, load it, read the document, run the automatic checks."""
        new_fields = data.pop("new_fields", None) or {}
        new_line_fields = data.pop("new_line_fields", None) or {}
        from .lint import _shape_errors
        shape = _shape_errors(data) + [f"{key} must be a mapping (name: {{type: ...}}), not a list"
                                       for key, value in (("new_fields", new_fields), ("new_line_fields", new_line_fields))
                                       if not isinstance(value, dict)]
        shape += _structure_problems(data)
        disabled = (data.get("checks") or {}).get("disable") if isinstance(data.get("checks"), dict) else None
        if any("sum of line" in str(name) or "sum of each" in str(name) for name in disabled or []):
            shape.append("the sum checks (sum of line ... vs ...) cannot be disabled: when the lines do not add up "
                         "to the total, a row is missing or misread; fix the line patterns instead")
        if shape:                                   # never write a file in the wrong shape
            return shape, None
        self.corrections = _repair_mechanical(data)
        page_text = re.sub(r"(?m)^=== page \d+ ===$", "", text)
        missing, fitted = _fit_keywords(data, page_text) if page_text else ([], [])
        self.corrections += fitted
        if missing:
            return missing, None
        undo.restore_fields(type_dir / "fields.yml")
        added = self._add_fields(type_dir / "fields.yml", new_fields, new_line_fields)
        if base is not None:
            data["family"], data["version"] = family_of(base), version_of(base) + 1
        data.pop("layout", None)
        target.parent.mkdir(parents=True, exist_ok=True)
        header = (f"# Written by the AI template author on {datetime.now():%Y-%m-%d} from {Path(pdf).name}"
                  + (f"; new fields: {', '.join(added)}" if added else "") + "\n")
        body = yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=200)
        target.write_text(header + body, encoding="utf-8")
        undo.created(target)

        catalog = load_catalog(self.types_dir, self.config_dir)
        rejected = catalog.rejected.get(f"{type_dir.name}/{target.name}")
        if rejected:
            rejected, dropped = _drop_broken_optional(data, rejected)
            if dropped and not rejected:
                self.corrections += dropped
                target.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=200),
                                  encoding="utf-8")
                catalog = load_catalog(self.types_dir, self.config_dir)
                rejected = catalog.rejected.get(f"{type_dir.name}/{target.name}")
        if rejected:
            return [f"the template does not load: {p}" for p in rejected], None
        template = next(t for t in catalog.templates if t["template_name"] == target.name)
        result = process_pdf(Path(pdf), catalog)
        doc = next((d for d in result["documents"] if d["template"] == target.name
                    and set(d["pages"]) & set(pages)), None)
        if doc is None:
            other = next((d for d in result["documents"] if set(d["pages"]) & set(pages)), None)
            if other:
                return [f"another template, {other['template']}, reads this document better; make the "
                        "keywords specific to this layout"], None
            return ["the keywords are printed on the page, but the template still does not match it: check "
                    "exclude_keywords and options.replace (keywords are compared after replace)"], None
        problems = [f"{i['level']}: {i['message']}" for i in doc.get("issues", []) if i["level"] in ("ERROR", "WARN")]
        problems += _leftover_pages(text, pages, doc)
        problems += _duplicate_fields(data)
        problems += _gst_coverage(data)
        problems += _empty_fields(data, doc)
        if (any("untaxed + tax" in p for p in problems) and not any("sum of line" in p for p in problems)
                and doc.get("lines")):
            problems.append("the item rows add up to the total, but the taxable value and taxes do not: the "
                            "document's tax block covers only part of the payment (fees or charges are added "
                            "on top). Keep the item rows; do not read that partial taxable value as "
                            "amount_untaxed (declare it in new_fields without a role, e.g. taxable_fare), and "
                            "read the taxes only if they belong to the whole total")
        if any("sum of line" in p for p in problems):
            problems += _unread_rows(data, text)
            problems += _rounding_hint(doc)
        if doc["status"] != "OK":
            problems.insert(0, f"the document is read with status {doc['status']}, it must be OK")
        problems += _keyword_problems(data, doc)
        if problems:
            return problems, doc
        trimmed = _needless_disables(template, doc, catalog, type_dir)
        if trimmed:
            data["checks"]["disable"] = [n for n in data["checks"]["disable"] if n not in trimmed]
            if not data["checks"]["disable"]:
                del data["checks"]["disable"]
            if not data["checks"]:
                del data["checks"]
            target.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=200),
                              encoding="utf-8")
            self.corrections.append(f"checks re-enabled (they pass or cannot run on this document, so "
                                    f"disabling them only weakens later documents): {', '.join(trimmed)}")
            catalog = load_catalog(self.types_dir, self.config_dir)
            template = next(t for t in catalog.templates if t["template_name"] == target.name)
        verdict = check_template(catalog, template, self.store, [Path(pdf)])
        if not verdict.passed:
            return [f"automatic check: {p}" for p in verdict.problems], doc
        stressed = _stress_problems([n for n in verdict.notes if n.startswith("stress:")])
        if stressed:
            return stressed, doc
        record(self.store, catalog, template, verdict)
        return [], doc

    # files -----------------------------------------------------------------------------------
    def _create_type(self, folder: Path, spec: dict, undo) -> None:
        if folder.exists():
            return
        (folder / "templates").mkdir(parents=True)
        undo.created(folder)
        signals = [str(s).lower() for s in spec.get("signals") or []][:8]
        columns = [slug(c) for c in spec.get("summary_columns") or []] or ["issuer", "document_type",
                                                                           "invoice_number", "date", "amount"]
        type_yml = {"name": folder.name, "description": str(spec.get("description") or folder.name),
                    "signals": signals, "min_signals": int(spec.get("min_signals") or 2),
                    "summary_columns": columns}
        (folder / "type.yml").write_text(f"# DOCUMENT TYPE: {folder.name} (created by the AI template author "
                                         f"on {datetime.now():%Y-%m-%d})\n"
                                         + yaml.safe_dump(type_yml, sort_keys=False), encoding="utf-8")
        (folder / "fields.yml").write_text(f"# {folder.name.upper()} FIELDS (adds to config/fields.yml)\n\n"
                                           "document: {}\nline: {}\n", encoding="utf-8")
        (folder / "checks.yml").write_text("# checks for every document of this type\ndocument: []\nline: []\n",
                                           encoding="utf-8")

    def _add_fields(self, path: Path, document: dict, line: dict) -> list[str]:
        """Add fields the AI declared and the registry lacks; returns their names."""
        registry_fields = load_catalog(self.types_dir, self.config_dir)
        known = set()
        for dt in registry_fields.types:
            if dt.folder == path.parent:
                known = set(dt.registry.document) | set(dt.registry.line)
        added = []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        data = data or {}
        for level, entries in (("document", document), ("line", line)):
            if not isinstance(entries, dict):
                continue
            section = data.get(level) or {}
            for raw, spec in entries.items():
                name = slug(raw, "")
                if not name or name in known or name in section:
                    continue
                spec = spec if isinstance(spec, dict) else {"type": str(spec)}
                kind = spec.get("type", "text")
                if kind not in ("text", "date", "money", "rate", "count", "number", "list"):
                    continue
                entry = {"type": kind}
                if spec.get("role") in ("tax", "untaxed", "total", "other", "adjustment", "discount",
                                        "subtotal", "gross", "paid", "due", "tax_total"):
                    entry["role"] = spec["role"]
                section[name] = entry
                added.append(name)
            data[level] = section
        if added:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            comments = "".join(l + "\n" for l in text.splitlines() if l.startswith("#"))
            path.write_text(comments + "\n" + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return added

    def _free_name(self, catalog, wanted: str) -> str:
        base = slug(wanted, "document")
        names = {t["template_name"] for t in catalog.templates} | {
            p.name for p in self.types_dir.glob("*/templates/*.yml")}
        name, n = f"{base}.yml", 2
        while name in names:
            name, n = f"{base}_{n}.yml", n + 1
        return name

    def _version_name(self, catalog, base) -> str:
        family, version = family_of(base), version_of(base) + 1
        names = {p.name for p in self.types_dir.glob("*/templates/*.yml")}
        name = f"{family}_v{version}.yml"
        while name in names:
            version += 1
            name = f"{family}_v{version}.yml"
        return name

    def _keep_draft(self, name: str, draft: str, problems: list[str]) -> None:
        folder = self.store.folder / "ai_drafts"
        folder.mkdir(parents=True, exist_ok=True)
        notes = "".join(f"# - {p}\n" for p in problems[:15])
        (folder / name).write_text(f"# Last AI draft, not accepted:\n{notes}\n{draft}", encoding="utf-8")

    # bookkeeping -----------------------------------------------------------------------------
    def _joined(self, texts: list[str], pages: list[int]) -> str:
        text = "\n".join(f"=== page {p} ===\n{t}" for p, t in zip(pages, texts))
        return text[:MAX_TEXT]

    def _ask(self, prompt: str, purpose: str, pdf) -> str:
        try:
            answer = self.provider.complete(SYSTEM, prompt)
        except AIError as exc:
            self._log(pdf, purpose, prompt, f"ERROR: {exc}")
            raise
        self._log(pdf, purpose, prompt, answer)
        return answer

    def _log(self, pdf, purpose: str, prompt: str, answer: str) -> None:
        log = self.store.folder / "ai_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "pdf": Path(pdf).name,
                                     "purpose": purpose, "model": getattr(self.settings, "model", ""),
                                     "prompt_chars": len(prompt), "prompt": prompt[:40000],
                                     "meta": getattr(self.provider, "last_meta", {}),
                                     "answer": answer[:20000]}) + "\n")

    def _key(self, pdf, pages) -> str:
        return f"{file_hash(Path(pdf))}:{','.join(str(p) for p in pages)}"

    def _seen(self, pdf, pages, result) -> bool:
        attempts = self.store.data.setdefault("ai_attempts", {})
        if not self.retry and self._key(pdf, pages) in attempts:
            result.action, result.outcome = "skipped", "skipped"
            result.problems = ["already given to the AI before (run with --ai-retry to try again)"]
            return True
        return False

    def _done(self, pdf, pages, result) -> AuthorResult:
        self.store.data.setdefault("ai_attempts", {})[self._key(pdf, pages)] = {
            "when": datetime.now().isoformat(timespec="seconds"), "outcome": result.outcome,
            "template": result.template, "problems": result.problems[:10]}
        self.store.save()
        return result


class _Undo:
    """Files and folders created, and fields.yml contents changed, by one authoring attempt."""

    def __init__(self):
        self.paths: list[Path] = []
        self.fields: dict[Path, str | None] = {}

    def created(self, path: Path) -> None:
        if path not in self.paths:
            self.paths.append(path)

    def restore_fields(self, path: Path) -> None:
        """Remember fields.yml before the first change; put it back before every new draft."""
        if path not in self.fields:
            self.fields[path] = path.read_text(encoding="utf-8") if path.is_file() else None
        elif self.fields[path] is not None:
            path.write_text(self.fields[path], encoding="utf-8")

    def commit(self) -> None:
        self.paths, self.fields = [], {}

    def rollback(self) -> None:
        for path, text in self.fields.items():
            if text is not None and path.parent.exists():
                path.write_text(text, encoding="utf-8")
        for path in reversed(self.paths):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        self.commit()


def author_from_settings(settings: dict, types_dir: Path, config_dir: Path, store, retry: bool = False):
    """(TemplateAuthor or None, reasons it cannot run). None with no reasons: the AI step is off."""
    from .ai_provider import AISettings, make_provider
    ai = AISettings.from_settings(settings)
    if not ai.enabled:
        return None, []
    problems = ai.problems()
    if problems:
        return None, problems
    return TemplateAuthor(make_provider(ai), ai, types_dir, config_dir, store, retry=retry), []

