"""Engine tests. Run from the project folder:  python -m pytest -q tests

- every template passes the load-time checks
- every sample PDF still gives its expected output (tests/expected/*.json)
- a new field (cess, round-off) works with a YAML-only change
- an unregistered amount field and other template mistakes are rejected at load
- template versions: newest first, fall back to the older version when it fails
- damaged values are caught by the checks
- rules cannot run code
- look-alike templates: chosen by evidence, never by name; ambiguous cases go to review
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from invoice_engine import Registry, load_templates, process_pdf  # noqa: E402
from invoice_engine.document import process_group  # noqa: E402
from invoice_engine.rules import COMMON_FUNCTIONS, RuleError, parse_rule  # noqa: E402
from invoice_engine.segment import matching_templates, page_texts  # noqa: E402

REGISTRY = Registry(ROOT / "config")
TEMPLATES = load_templates(ROOT / "templates", REGISTRY)
EXPECTED = sorted((ROOT / "tests" / "expected").glob("*.json"))


def run_text(text: str, templates: list) -> dict:
    """Parse one page of text the way process_pdf does."""
    candidates = matching_templates(text, templates)
    assert candidates, "no template matched"
    group = {"candidates": candidates, "texts": [text], "pages": [1]}
    return process_group(group, ROOT / "test.pdf", REGISTRY)


def write_template(folder: Path, name: str, body: str) -> None:
    (folder / name).write_text(body, encoding="utf-8")


# ----------------------------------------------------------------------------- templates

def test_all_templates_load_without_errors():
    assert TEMPLATES.rejected == {}, TEMPLATES.rejected
    assert len(TEMPLATES.templates) >= 14


@pytest.mark.parametrize("expected_file", EXPECTED, ids=[p.stem for p in EXPECTED])
def test_sample_pdfs_match_expected_output(expected_file: Path):
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    pdf = ROOT / expected["source_file"]
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not in the project folder")
    result = json.loads(json.dumps(process_pdf(pdf, TEMPLATES.templates, REGISTRY), default=str))
    assert len(result["documents"]) == len(expected["documents"])
    for got, want in zip(result["documents"], expected["documents"]):
        for key in ("pages", "template", "status", "fields", "lines"):
            assert got[key] == want[key], f"{pdf.name} pages {want['pages']}: {key} changed"


# ------------------------------------------------------------- new fields need YAML only

CESS_TEMPLATE = r"""
issuer: Acme Foods
keywords: ['ACME FOODS PVT LTD', 'TAX INVOICE']
required_fields: [issuer, document_type, invoice_number, date, amount, seller_name]
fields:
  document_type: {parser: static, value: tax_invoice}
  invoice_number: 'Invoice No:\s*(\S+)'
  date: 'Invoice Date:\s*(\d{2}/\d{2}/\d{4})'
  seller_name: '\A\s*(ACME FOODS PVT LTD)'
  amount_untaxed: 'Taxable value\s+([\d.]+)'
  amount_cgst: '(?m)^CGST\s+([\d.]+)'
  amount_sgst: '(?m)^SGST\s+([\d.]+)'
  amount_cess: '(?m)^Cess\s+([\d.]+)'
  amount_round_off: 'Round off\s+(-?[\d.]+)'
  amount: 'Grand total\s+([\d.]+)'
  lines:
    parser: lines
    start: 'Item\s+Qty'
    end: 'Taxable value'
    first_line: '^(?P<name>\S.*?)\s{2,}(?P<qty>\d+)\s+(?P<price_unit>[\d.]+)\s+(?P<price_subtotal>[\d.]+)\s+(?P<cgst_rate>[\d.]+)%\s+(?P<cgst_amount>[\d.]+)\s+(?P<sgst_rate>[\d.]+)%\s+(?P<sgst_amount>[\d.]+)\s+(?P<cess_rate>[\d.]+)%\s+(?P<cess_amount>[\d.]+)\s+(?P<price_total>[\d.]+)\s*$'
    line: '^\s+(?P<name>\S.*?)\s*$'
    types: {qty: int, price_unit: float, price_subtotal: float, cgst_rate: float, cgst_amount: float,
            sgst_rate: float, sgst_amount: float, cess_rate: float, cess_amount: float, price_total: float}
options: {currency: INR, date_formats: ['%d/%m/%Y']}
"""

CESS_TEXT = """ACME FOODS PVT LTD
TAX INVOICE
Invoice No: AF-001
Invoice Date: 05/10/2026

Item              Qty    Rate   Taxable   CGST          SGST          Cess          Total
Masala Tea          3   66.50    199.50   2.5%   4.99   2.5%   4.99   1.0%   2.00   211.48

Taxable value   199.50
CGST            4.99
SGST            4.99
Cess            2.00
Round off       0.52
Grand total     212.00
"""


def test_new_tax_and_round_off_work_without_code_changes(tmp_path: Path):
    write_template(tmp_path, "acme.yml", CESS_TEMPLATE)
    templates = load_templates(tmp_path, REGISTRY)
    assert templates.rejected == {}
    doc = run_text(CESS_TEXT, templates.templates)
    assert doc["status"] == "OK", doc["issues"]
    assert doc["fields"]["amount_cess"] == 2.0 and doc["fields"]["amount_round_off"] == 0.52


def test_wrong_cess_amount_is_caught(tmp_path: Path):
    write_template(tmp_path, "acme.yml", CESS_TEMPLATE)
    templates = load_templates(tmp_path, REGISTRY)
    doc = run_text(CESS_TEXT.replace("Cess            2.00", "Cess            3.00"), templates.templates)
    assert doc["status"] == "FAIL"


# ------------------------------------------------------------------- load-time checks

def test_template_mistakes_are_rejected(tmp_path: Path):
    write_template(tmp_path, "bad.yml", r"""
issuer: Bad
keywords: [Bad Ltd, BILL TO: Someone]
required_fields: [issuer, invoice_number, date, amount, seller_name]
fields:
  invoice_number: 'Invoice (No|Number):\s*(\S+)'
  date: 'Date:\s*(\d{2}-\d{2}-\d{4})'
  amount_tcs: 'TCS\s+([\d.]+)'
  amount: '^Total\s+([\d.]+)'
lines:
  start: 'Item'
  end: '^Total'
  line: '^(?P<name>.+)$'
checks:
  document: [{rule: "__import__('os') == 1"}]
""")
    templates = load_templates(tmp_path, REGISTRY)
    problems = " ".join(templates.rejected["bad.yml"])
    assert "not text" in problems                       # unquoted 'BILL TO:' became a mapping
    assert "exactly 1 capture group" in problems
    assert "amount field is not in config/fields.yml" in problems
    assert "without (?m)" in problems
    assert "date_formats" in problems
    assert "seller_name" in problems                    # required but never defined
    assert "not allowed" in problems                    # code in a rule


def test_rules_cannot_run_code():
    for text in ("__import__('os').system('dir') == 0", "amount.real == 1", "[1][0] == 1", "lambda: 1 == 1"):
        with pytest.raises(RuleError):
            parse_rule(text, COMMON_FUNCTIONS)


# ------------------------------------------------------------------------- versions

def test_newer_version_is_tried_first_and_older_is_the_fallback(tmp_path: Path):
    old = (ROOT / "templates" / "anthropic_invoice.yml").read_text(encoding="utf-8")
    new = ("family: anthropic_invoice\nversion: 2\n"
           + old.replace(r"amount: '(?m)^\s+Total[ \t]{2,}", r"amount: '(?m)^\s+Total amount[ \t]{2,}"))
    write_template(tmp_path, "anthropic_invoice.yml", old)
    write_template(tmp_path, "anthropic_invoice_v2.yml", new)
    templates = load_templates(tmp_path, REGISTRY)
    assert templates.rejected == {}
    pdf = ROOT / "Invoice-EXQCNVGN-0004.pdf"
    if not pdf.exists():
        pytest.skip("Anthropic sample PDF not in the project folder")
    old_layout = page_texts(pdf)[0]
    new_layout = old_layout.replace("    Total   ", "    Total amount", 1)

    doc = run_text(old_layout, templates.templates)          # old PDF: v2 fails, v1 used
    assert (doc["version"], doc["status"]) == (1, "OK")
    assert any("also tried anthropic_invoice_v2.yml" in i["message"] for i in doc["issues"])

    doc = run_text(new_layout, templates.templates)          # new PDF: v2 used directly
    assert (doc["version"], doc["status"]) == (2, "OK")


def test_duplicate_version_is_rejected(tmp_path: Path):
    body = (ROOT / "templates" / "anthropic_invoice.yml").read_text(encoding="utf-8")
    write_template(tmp_path, "a.yml", "family: anthropic_invoice\n" + body)
    write_template(tmp_path, "b.yml", "family: anthropic_invoice\n" + body)
    templates = load_templates(tmp_path, REGISTRY)
    assert "b.yml" in templates.rejected


# ------------------------------------------------------- look-alike templates (regression)
# Each test reproduces one failure found in the look-alike investigation (rnd/eval in the scratch
# folder): before the change the engine chose by alphabetical order or accepted unverified totals.

def derive(folder: Path, source: str, target: str, **changes) -> None:
    """Write templates/<source> with some keys changed (None removes a key) as <folder>/<target>."""
    data = yaml.safe_load((ROOT / "templates" / source).read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (folder / target).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def pool_with(folder: Path, *extra: str) -> list:
    """All real templates plus the extra files already written to folder."""
    for path in (ROOT / "templates").glob("*.yml"):
        if not (folder / path.name).exists():
            shutil.copy(path, folder / path.name)
    templates = load_templates(folder, REGISTRY)
    assert templates.rejected == {}, templates.rejected
    return templates.templates


def need(pdf_name: str) -> Path:
    pdf = ROOT / pdf_name
    if not pdf.exists():
        pytest.skip(f"{pdf_name} not in the project folder")
    return pdf


def test_identical_lookalike_is_sent_to_review_not_chosen_by_name(tmp_path: Path):
    pdf = need("ms_invoice_326255343871054720_1.pdf")
    derive(tmp_path, "meesho_tax_invoice.yml", "abc_marketplace_invoice.yml", issuer="ABC Marketplace")
    doc = process_pdf(pdf, pool_with(tmp_path), REGISTRY)["documents"][0]
    assert doc["status"] == "CHECK"
    assert any(i["level"] == "WARN" and i["message"].startswith("ambiguous:") for i in doc["issues"])
    assert [a["template"] for a in doc["alternatives"]] == ["meesho_tax_invoice.yml"]


def test_wrong_template_that_does_not_fail_loses_to_better_evidence(tmp_path: Path):
    pdf = need("OD437334801620974100.pdf")
    shared = ["Tax Invoice", "Sold By:", "Invoice Number #"]
    fee = yaml.safe_load((ROOT / "templates" / "flipkart_platform_fee_invoice.yml").read_text(encoding="utf-8"))
    fee["fields"].pop("lines")                       # a look-alike without an item table never FAILs on it
    derive(tmp_path, "flipkart_seller_tax_invoice.yml", "flipkart_seller_tax_invoice.yml",
           keywords=shared, exclude_keywords=None)
    derive(tmp_path, "flipkart_platform_fee_invoice.yml", "flipkart_platform_fee_invoice.yml",
           keywords=shared, exclude_keywords=None, fields=fee["fields"])
    docs = process_pdf(pdf, pool_with(tmp_path), REGISTRY)["documents"]
    by_pages = {tuple(d["pages"]): d for d in docs}
    for pages in ((1,), (8,)):
        assert (by_pages[pages]["template"], by_pages[pages]["status"]) == ("flipkart_seller_tax_invoice.yml", "OK")


def test_evidence_poor_duplicate_does_not_win_by_alphabetical_order(tmp_path: Path):
    pdf = need("oyo.pdf")
    (tmp_path / "com.oyo.invoice.yml").write_text(          # like invoice2data's own OYO template
        "issuer: OYO\nkeywords: [OYO, Oravel, Stays]\n"
        "fields:\n"
        "  amount: 'Grand Total\\s+Rs (\\d+)'\n"
        "  date: 'Date:\\s(\\d{1,2}/\\d{1,2}/\\d{4})'\n"
        "  invoice_number: '([A-Z0-9]+)\\s+Cash at Hotel'\n"
        "options: {currency: INR, date_formats: ['%d/%m/%Y']}\n", encoding="utf-8")
    doc = process_pdf(pdf, pool_with(tmp_path), REGISTRY)["documents"][0]
    assert (doc["template"], doc["status"]) == ("oyo_payment_receipt.yml", "OK")
    assert any("also tried com.oyo.invoice.yml" in i["message"] for i in doc["issues"])


GENERIC = r"""
issuer: Generic
keywords: [Invoice]
required_fields: [issuer, invoice_number, date, amount]
fields:
  invoice_number: '(?i)invoice\s*(?:number|no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})'
  date: '(?i)(?:date of issue|invoice date)\s*:?\s*([A-Z][a-z]+ \d{1,2}, \d{4})'
  amount: '(?m)^\s*(?:Grand\s+)?Total\s+\D{0,4}([\d,]+\.\d{2})\s*$'
options: {currency: INR}
"""


def test_total_that_no_check_verified_is_not_accepted(tmp_path: Path):
    pdf = need("Invoice-EXQCNVGN-0004.pdf")
    write_template(tmp_path, "generic_invoice.yml", GENERIC)
    templates = load_templates(tmp_path, REGISTRY)          # the Anthropic template is not there
    doc = process_pdf(pdf, templates.templates, REGISTRY)["documents"][0]
    assert doc["fields"]["amount"] == 2399.0 and doc["evidence"]["checks_passed"] == 0
    assert doc["status"] == "CHECK"
    assert any("amount is not verified" in i["message"] for i in doc["issues"])


def test_catch_all_template_never_beats_the_real_one(tmp_path: Path):
    write_template(tmp_path, "generic_invoice.yml", GENERIC)
    templates = pool_with(tmp_path)
    for name in ("Invoice-EXQCNVGN-0004.pdf", "Invoice_8626225731.pdf", "ms_invoice_326255343871054720_1.pdf"):
        pdf = need(name)
        for doc in process_pdf(pdf, templates, REGISTRY)["documents"]:
            assert doc["template"] != "generic_invoice.yml" and doc["status"] == "OK", (name, doc["pages"])


def test_lookalike_matching_page_one_only_does_not_split_the_invoice(tmp_path: Path):
    pdf = need("QualityHosting.pdf")
    base = yaml.safe_load((ROOT / "templates" / "qualityhosting_custom.yml").read_text(encoding="utf-8"))
    base["fields"].pop("lines")
    derive(tmp_path, "qualityhosting_custom.yml", "aaa_hosting_invoice.yml", issuer="AAA Hosting",
           keywords=["QualityHosting AG", "Contract No."], fields=base["fields"])
    result = process_pdf(pdf, pool_with(tmp_path), REGISTRY)
    assert [(d["pages"], d["template"], d["status"]) for d in result["documents"]] == \
        [([1, 2], "qualityhosting_custom.yml", "OK")]


def test_lookalike_on_all_pages_does_not_hide_the_real_template(tmp_path: Path):
    pdf = need("OD437334801620974100.pdf")       # bill of supply on pages 2-3; page 3 matches no real template
    base = yaml.safe_load((ROOT / "templates" / "flipkart_bill_of_supply.yml").read_text(encoding="utf-8"))
    base["fields"].pop("lines")
    derive(tmp_path, "flipkart_bill_of_supply.yml", "aaa_supply_note.yml", issuer="AAA Supply",
           keywords=["VILLAGE SANPKA", "DISTRICT GURUGRAM"], fields=base["fields"], continuation_keywords=None)
    docs = process_pdf(pdf, pool_with(tmp_path), REGISTRY)["documents"]
    doc = next(d for d in docs if d["pages"] == [2, 3])
    assert (doc["template"], doc["status"]) == ("flipkart_bill_of_supply.yml", "OK")


def test_shared_layouts_do_not_chain_separate_invoices(tmp_path: Path):
    pdf = need("Invoice_8626225731.pdf")
    for name in ("zomato_restaurant_invoice.yml", "zomato_platform_fee_invoice.yml"):
        derive(tmp_path, name, name, keywords=["Invoice No"], exclude_keywords=None)
    result = process_pdf(pdf, pool_with(tmp_path), REGISTRY)
    assert [(d["pages"], d["template"], d["status"]) for d in result["documents"]] == \
        [([1], "zomato_restaurant_invoice.yml", "OK"), ([2], "zomato_platform_fee_invoice.yml", "OK")]


# ------------------------------------------------------------------- damaged values

DAMAGE = [
    ("Invoice_8626225731.pdf", 0, "10.38", "18.38"),
    ("Invoice_8626225731.pdf", 0, "99.89", "98.89"),
    ("Invoice_8626225731.pdf", 1, "Platform fee", "Platform\nfee"),
    ("Invoice_8626225731.pdf", 1, "Invoice No:", "Invoice Number:"),
    ("ms_invoice_326255343871054720_1.pdf", 0, ":Rs.8.70", ":Rs.8.07"),
    ("Invoice-EXQCNVGN-0004.pdf", 0, "365.95", "356.95"),
    ("OD437334801620974100.pdf", 0, "30.95", "39.95"),
]


@pytest.mark.parametrize("pdf_name,page,old,new", DAMAGE)
def test_damaged_values_are_caught(pdf_name, page, old, new):
    pdf = ROOT / pdf_name
    if not pdf.exists():
        pytest.skip(f"{pdf_name} not in the project folder")
    text = page_texts(pdf)[page]
    assert old in text
    doc = run_text(text.replace(old, new, 1), TEMPLATES.templates)
    assert doc["status"] == "FAIL", f"not caught: {old} -> {new}"