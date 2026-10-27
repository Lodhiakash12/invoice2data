"""Engine tests. Run from the project folder:  python -m pytest -q tests

- every template passes the load-time checks
- every sample PDF still gives its expected output (tests/expected/*.json)
- a new field (cess, round-off) works with a YAML-only change
- template mistakes are rejected at load (including bad replacement strings)
- template versions: the version with the most evidence represents the layout
- damaged values are caught by the checks; checks on all-zero values prove nothing
- rules cannot run code
- look-alike templates: chosen by evidence, never by name; ambiguous cases go to review
- audit defects: slow patterns are stopped, one bad file does not stop the run, copies and
  unreadable numbers do not merge documents, outputs do not overwrite each other, scanned pages
  are flagged
- document types: a new type is only a folder; routing keeps types apart; the keyword index
  finds the same templates as a full scan; the overlap test catches competing templates
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from invoice_engine import Registry, layout, load_catalog, load_templates, poppler, process_pdf  # noqa: E402
from invoice_engine.document import process_group  # noqa: E402
from invoice_engine.rules import COMMON_FUNCTIONS, RuleError, parse_rule  # noqa: E402
from invoice_engine.segment import matching_templates, page_texts  # noqa: E402

CATALOG = load_catalog(ROOT)
INVOICE = CATALOG.type_named("invoice")
INVOICE_TEMPLATES = ROOT / "document_types" / "invoice" / "templates"
REGISTRY = INVOICE.registry                      # invoice fields: shared + document_types/invoice
TEMPLATES = INVOICE.template_set
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
    assert CATALOG.rejected == {}, CATALOG.rejected
    assert len(TEMPLATES.templates) >= 14 and CATALOG.type_named("tax_document").template_set.templates


@pytest.mark.parametrize("expected_file", EXPECTED, ids=[p.stem for p in EXPECTED])
def test_sample_pdfs_match_expected_output(expected_file: Path):
    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    pdf = ROOT / expected["source_file"]
    if not pdf.exists():
        pytest.skip(f"{pdf.name} not in the project folder")
    result = json.loads(json.dumps(process_pdf(pdf, CATALOG), default=str))
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
    old = (INVOICE_TEMPLATES / "anthropic_invoice.yml").read_text(encoding="utf-8")
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
    body = (INVOICE_TEMPLATES / "anthropic_invoice.yml").read_text(encoding="utf-8")
    write_template(tmp_path, "a.yml", "family: anthropic_invoice\n" + body)
    write_template(tmp_path, "b.yml", "family: anthropic_invoice\n" + body)
    templates = load_templates(tmp_path, REGISTRY)
    assert "b.yml" in templates.rejected


# ------------------------------------------------------- look-alike templates (regression)
# Each test reproduces one failure found in the look-alike investigation (rnd/eval in the scratch
# folder): before the change the engine chose by alphabetical order or accepted unverified totals.

def derive(folder: Path, source: str, target: str, **changes) -> None:
    """Write templates/<source> with some keys changed (None removes a key) as <folder>/<target>."""
    data = yaml.safe_load((INVOICE_TEMPLATES / source).read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (folder / target).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def pool_with(folder: Path, *extra: str) -> list:
    """All real invoice templates plus the extra files already written to folder."""
    for path in (INVOICE_TEMPLATES).glob("*.yml"):
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
    fee = yaml.safe_load((INVOICE_TEMPLATES / "flipkart_platform_fee_invoice.yml").read_text(encoding="utf-8"))
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
    base = yaml.safe_load((INVOICE_TEMPLATES / "qualityhosting_custom.yml").read_text(encoding="utf-8"))
    base["fields"].pop("lines")
    derive(tmp_path, "qualityhosting_custom.yml", "aaa_hosting_invoice.yml", issuer="AAA Hosting",
           keywords=["QualityHosting AG", "Contract No."], fields=base["fields"])
    result = process_pdf(pdf, pool_with(tmp_path), REGISTRY)
    assert [(d["pages"], d["template"], d["status"]) for d in result["documents"]] == \
        [([1, 2], "qualityhosting_custom.yml", "OK")]


def test_lookalike_on_all_pages_does_not_hide_the_real_template(tmp_path: Path):
    pdf = need("OD437334801620974100.pdf")       # bill of supply on pages 2-3; page 3 matches no real template
    base = yaml.safe_load((INVOICE_TEMPLATES / "flipkart_bill_of_supply.yml").read_text(encoding="utf-8"))
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


# ---------------------------------------------------------- layout fingerprint (tie-breaker)

def test_fingerprint_keeps_label_words_only_and_ignores_rows():
    page = [("Invoice", 0.62, 100.0), ("Nexora", 0.70, 90.0), ("Subtotal", 0.61, 500.0), ("18,500.00", 0.9, 500.0)]
    order = layout.fingerprint([page], {"invoice", "subtotal"})
    assert order == [["invoice", 7], ["subtotal", 7]]                   # no names, no amounts
    longer_table = [(w, x, y + 300 if y > 200 else y) for w, x, y in page]   # totals pushed down
    assert layout.similarity(layout.fingerprint([longer_table], {"invoice", "subtotal"}), order) == 1.0
    moved = [(w, 0.05, y) for w, x, y in page]                           # same words, other column
    assert layout.similarity(layout.fingerprint([moved], {"invoice", "subtotal"}), order) == 0.0


def test_malformed_layout_block_is_rejected(tmp_path: Path):
    derive(tmp_path, "oyo_payment_receipt.yml", "oyo_payment_receipt.yml",
           layout={"columns": 12, "sample_pages": 1, "order": [["receipt", 40]]})
    assert "oyo_payment_receipt.yml" in load_templates(tmp_path, REGISTRY).rejected


def _store_layout(folder: Path, name: str, pdf: Path, pages: list[int]) -> None:
    templates = {t["template_name"]: t for t in load_templates(folder, REGISTRY).templates}
    order = layout.fingerprint(layout.page_words(pdf, pages), layout.label_vocabulary(templates[name]))
    layout.write_layout(folder / name, layout.layout_block(order, 1))


def test_layout_breaks_a_tie_only_when_one_layout_is_clearly_closer(tmp_path: Path):
    meesho, flipkart = need("ms_invoice_326255343871054720_1.pdf"), need("OD437334801620974100.pdf")
    derive(tmp_path, "meesho_tax_invoice.yml", "abc_marketplace_invoice.yml", issuer="ABC Marketplace")
    pool_with(tmp_path)
    _store_layout(tmp_path, "meesho_tax_invoice.yml", meesho, [1])
    _store_layout(tmp_path, "abc_marketplace_invoice.yml", flipkart, [1])    # ABC's sample looks different
    doc = process_pdf(meesho, load_templates(tmp_path, REGISTRY).templates, REGISTRY)["documents"][0]
    assert (doc["template"], doc["status"]) == ("meesho_tax_invoice.yml", "OK")
    assert any(i["message"].startswith("layout decided the tie") for i in doc["issues"])

    _store_layout(tmp_path, "abc_marketplace_invoice.yml", meesho, [1])      # same layout: no decision
    doc = process_pdf(meesho, load_templates(tmp_path, REGISTRY).templates, REGISTRY)["documents"][0]
    assert doc["status"] == "CHECK"
    assert any(i["message"].startswith("ambiguous:") for i in doc["issues"])


# --------------------------------------------------------------- finding and running pdftotext

def _blank_pdf(path: Path) -> None:
    """A valid one-page PDF with no text at all (like a scan without OCR)."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 4\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(out))


def test_pdftotext_is_found_from_settings_without_the_path(tmp_path: Path, monkeypatch):
    real = poppler.configure(None)                           # wherever it is on this machine
    (tmp_path / "settings.yml").write_text(f"poppler_path: {Path(real.path).parent}\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("POPPLER_PATH", raising=False)
    found = poppler.configure(tmp_path)
    assert (found.source, found.version) == ("settings.yml", real.version)
    monkeypatch.setenv("POPPLER_PATH", str(Path(real.path).parent))   # the environment variable wins
    assert poppler.configure(tmp_path).source == "POPPLER_PATH"


def test_missing_pdftotext_explains_where_it_looked(tmp_path: Path, monkeypatch):
    (tmp_path / "settings.yml").write_text("poppler_path: C:\\nowhere\\bin\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("POPPLER_PATH", raising=False)
    with pytest.raises(poppler.PopplerError) as exc:
        poppler.configure(tmp_path)
    assert "nowhere" in str(exc.value) and "settings.yml" in str(exc.value)


def test_unreadable_pdfs_are_reported_not_crashed(tmp_path: Path):
    poppler.configure(None)
    damaged = tmp_path / "damaged.pdf"
    damaged.write_bytes(b"this is not a pdf at all")
    blank = tmp_path / "scan.pdf"
    _blank_pdf(blank)
    result = process_pdf(damaged, TEMPLATES.templates, REGISTRY)
    assert result["documents"] == [] and "damaged or is not a real PDF" in result["notes"][0]
    result = process_pdf(blank, TEMPLATES.templates, REGISTRY)
    assert any("no page has a text layer" in n for n in result["notes"])
    assert "password-protected" in poppler._explain("Command Line Error: Incorrect password")


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


# ------------------------------------------------- audit defects (each reproduced, then fixed)

from invoice_engine import safe_regex  # noqa: E402
from invoice_engine.export import output_names, write_json  # noqa: E402
from invoice_engine.segment import page_mark, split_into_documents  # noqa: E402


def test_slow_pattern_is_stopped_and_only_its_template_fails(tmp_path: Path):
    """D6: a pattern with exponential run time used to hang the whole run."""
    write_template(tmp_path, "acme.yml", CESS_TEMPLATE)
    slow = CESS_TEMPLATE.replace("issuer: Acme Foods", "issuer: Acme Slow").replace(
        "  amount_cess:", "  customer_number: '(?m)^Ref:\\s*((?:a|aa)+)$'\n  amount_cess:")
    write_template(tmp_path, "acme_slow.yml", slow)
    templates = load_templates(tmp_path, REGISTRY).templates
    old = safe_regex.timeout()
    safe_regex.install(0.5)
    try:
        import time
        started = time.time()
        doc = run_text(CESS_TEXT + "Ref: " + "a" * 60 + "b\n", templates)
        assert time.time() - started < 20
    finally:
        safe_regex.install(old)
    assert (doc["template"], doc["status"]) == ("acme.yml", "OK")
    assert any("acme_slow.yml" in i["message"] and "FAIL" in i["message"] for i in doc["issues"])


def test_bad_replacement_is_rejected_at_load(tmp_path: Path):
    """D4: an undefined group in a replacement passed lint and crashed the run on the first match."""
    body = CESS_TEMPLATE.replace("options: {currency: INR,", "options: {replace: [['(a)', '\\\\2']], currency: INR,")
    write_template(tmp_path, "acme.yml", body)
    rejected = load_templates(tmp_path, REGISTRY).rejected
    assert "acme.yml" in rejected and any("replacement" in m for m in rejected["acme.yml"])


def test_one_file_that_breaks_does_not_stop_the_others(tmp_path: Path, monkeypatch):
    """D4: process_invoice.py had no per-file error handling."""
    pdf = need("oyo.pdf")
    for name in ("a.pdf", "b.pdf"):
        shutil.copy(pdf, tmp_path / name)
    import process_invoice
    from invoice_engine.pipeline import Pipeline
    real = Pipeline.process

    def broken(self, path):
        if Path(path).name == "a.pdf":
            raise ValueError("simulated failure")
        return real(self, path)

    monkeypatch.setattr(Pipeline, "process", broken)
    monkeypatch.setattr(process_invoice, "sample_folder", lambda settings: tmp_path / "samples")
    monkeypatch.setattr(sys, "argv", ["process_invoice.py", str(tmp_path), "--output", str(tmp_path / "out")])
    assert process_invoice.main() == 1
    a = json.loads((tmp_path / "out" / "a.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "out" / "b.json").read_text(encoding="utf-8"))
    assert a["notes"][0].startswith("ERROR: processing stopped") and b["documents"][0]["status"] == "OK"


def test_copies_of_one_document_are_reported_separately(tmp_path: Path):
    """D1: original + duplicate copy in one PDF merged into one document that FAILed."""
    pdf = need("oyo.pdf")
    unite = poppler.tool_path("pdfunite")
    if not unite:
        pytest.skip("pdfunite (Poppler) not available")
    import subprocess
    subprocess.run([unite, str(pdf), str(pdf), str(tmp_path / "twice.pdf")], check=True)
    result = process_pdf(tmp_path / "twice.pdf", TEMPLATES.templates, REGISTRY)
    assert [(d["pages"], d["status"]) for d in result["documents"]] == [([1], "OK"), ([2], "OK")]
    assert result["documents"][1]["copy_of"] == [1]
    single = process_pdf(pdf, TEMPLATES.templates, REGISTRY)["totals_by_document_type"]
    assert result["totals_by_document_type"] == single          # the copy is not counted twice


def test_unreadable_number_does_not_join_another_document():
    """D2: a page whose number could not be read was joined to the previous document."""
    page = page_texts(need("Invoice_8626225731.pdf"))[0]
    number = re.search(r"Invoice No\.\s*:\s*(\S+)", page).group(1)
    other = page.replace(number, "ZZ999999")
    damaged = other.replace("Invoice No.", "Invoice N0.")
    assert [g["pages"] for g in split_into_documents([page, other], TEMPLATES.templates)[0]] == [[1], [2]]
    assert [g["pages"] for g in split_into_documents([page, damaged], TEMPLATES.templates)[0]] == [[1], [2]]


def test_page_marks_are_read():
    assert page_mark("....  Page 2 of 5  ...") == (2, 5)
    assert page_mark("Page 1/2") == (1, 2)
    assert page_mark("Seite 3 von 3") == (3, 3)
    assert page_mark("Page 7 of 5") is None and page_mark("no mark here") is None


def test_outputs_of_files_with_the_same_name_do_not_overwrite(tmp_path: Path):
    """D5: a/x.pdf and b/x.pdf both wrote x.json."""
    names = output_names([Path("a/x.pdf"), Path("b/x.pdf"), Path("c/X.pdf")])
    assert sorted(names.values()) == ["X_3", "x", "x_2"]
    for pdf, name in names.items():
        write_json({"source_file": pdf.name, "documents": [], "unrecognized_pages": [], "notes": []}, tmp_path, name)
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_best_version_wins_not_the_newest(tmp_path: Path):
    """D3: a newer version with a warning beat an older version that reads the document cleanly."""
    pdf = need("oyo.pdf")
    data = yaml.safe_load((INVOICE_TEMPLATES / "oyo_payment_receipt.yml").read_text(encoding="utf-8"))
    data.update(family="oyo_payment_receipt", version=2)
    data["fields"]["customer_number"] = "Never printed label:\\s*(\\S+)"
    (tmp_path / "oyo_payment_receipt_v2.yml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    doc = process_pdf(pdf, pool_with(tmp_path), REGISTRY)["documents"][0]
    assert (doc["template"], doc["status"]) == ("oyo_payment_receipt.yml", "OK")


def test_amounts_without_a_passed_check_are_not_ok(tmp_path: Path):
    """D9: a document with amounts could be OK although no check passed."""
    write_template(tmp_path, "acme.yml", CESS_TEMPLATE.replace(
        "  amount_untaxed: 'Taxable value\\s+([\\d.]+)'\n", "").replace(
        "  amount: 'Grand total\\s+([\\d.]+)'\n", "").replace("amount, seller_name]", "seller_name]"))
    body = (tmp_path / "acme.yml").read_text(encoding="utf-8")
    start = body.index("  lines:")
    end = body.index("options:")
    (tmp_path / "acme.yml").write_text(body[:start] + body[end:], encoding="utf-8")
    doc = run_text(CESS_TEXT, load_templates(tmp_path, REGISTRY).templates)
    assert doc["status"] == "CHECK" and any("amounts are not verified" in i["message"] for i in doc["issues"])


def test_checks_on_all_zero_values_are_not_evidence():
    """A check such as 0 + 0 == 0 agrees but proves nothing about the reading."""
    pdf = need("ACK742817410300726.pdf")
    doc = process_pdf(pdf, CATALOG)["documents"][0]
    assert doc["evidence"]["checks_passed"] == 0 and doc["evidence"]["checks_trivial"] == 4
    assert doc["status"] == "CHECK"


def _scan_like_pdf(path: Path, words: str) -> None:
    """One A4 page covered by a 60 x 85 grey picture, with a line of text on top (like a scan
    that a copier stamped with a header)."""
    import zlib
    pixels = zlib.compress(bytes([200]) * (60 * 85))
    content = (b"q 595 0 0 842 0 0 cm /Im1 Do Q BT /F1 10 Tf 40 820 Td (" + words.encode() + b") Tj ET")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
               b"/Resources << /XObject << /Im1 5 0 R >> /Font << /F1 6 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
               b"<< /Type /XObject /Subtype /Image /Width 60 /Height 85 /ColorSpace /DeviceGray "
               b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (len(pixels), pixels),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 7\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(out))


def test_scanned_page_with_a_little_text_is_flagged(tmp_path: Path):
    """D8: a scanned page with a stamped header was only reported as unrecognized."""
    poppler.configure(None)
    if not poppler.tool_path("pdfimages"):
        pytest.skip("pdfimages (Poppler) not available")
    _scan_like_pdf(tmp_path / "scan.pdf", "Scanned by Office Copier 2026-10-01")
    result = process_pdf(tmp_path / "scan.pdf", TEMPLATES.templates, REGISTRY)
    assert any(n.startswith("page 1 looks scanned") for n in result["notes"])
    assert result["unrecognized_pages"][0]["reason"] == "scanned page: OCR needed"


def test_fingerprint_vocabulary_has_no_pattern_syntax():
    """D7: group names such as (?P<name>...) ended up in the layout fingerprint."""
    template = next(t for t in TEMPLATES.templates if t["template_name"] == "flipkart_seller_tax_invoice.yml")
    words = layout.label_vocabulary(template)
    assert not {"name", "price", "code", "category"} & words and "invoice" in words


def test_poppler_problems_on_a_readable_file_are_reported():
    pdf = need("VIL_bill_7573001478_2026-08-22_statement.pdf")
    notes = process_pdf(pdf, TEMPLATES.templates, REGISTRY)["notes"]
    assert any(n.startswith("pdftotext reported") for n in notes)


# ------------------------------------------------- document types, routing, index, overlap gate

from invoice_engine import overlap  # noqa: E402
from invoice_engine.catalog import Catalog  # noqa: E402
from invoice_engine.document import process_group as _process_group  # noqa: E402
from invoice_engine.export import write_excel  # noqa: E402
from invoice_engine.segment import matching_templates  # noqa: E402

BANK_TYPE = """name: bank_statement
description: Bank account statements
signals: [statement of account, opening balance, closing balance]
min_signals: 2
summary_columns: [issuer, invoice_number, date, amount_opening_balance, amount_closing_balance]
"""
BANK_FIELDS = """document:
  amount_opening_balance: {type: money, role: other}
  amount_credits:         {type: money, role: other}
  amount_debits:          {type: money, role: other}
  amount_closing_balance: {type: money, role: other}
"""
BANK_CHECKS = """document:
  - name: opening + credits - debits = closing
    rule: "amount_opening_balance + amount_credits - amount_debits == amount_closing_balance"
"""
BANK_TEMPLATE = r"""
issuer: Example Bank
keywords: ['EXAMPLE BANK LTD', 'Statement of Account']
required_fields: [issuer, invoice_number, date, amount_closing_balance]
fields:
  document_type: {parser: static, value: bank_statement}
  invoice_number: 'Account No:\s*(\d+)'
  date: 'Statement Date:\s*(\d{2}/\d{2}/\d{4})'
  amount_opening_balance: 'Opening Balance\s+([\d,]+\.\d{2})'
  amount_credits: 'Total Credits\s+([\d,]+\.\d{2})'
  amount_debits: 'Total Debits\s+([\d,]+\.\d{2})'
  amount_closing_balance: 'Closing Balance\s+([\d,]+\.\d{2})'
options: {currency: INR, date_formats: ['%d/%m/%Y']}
"""
BANK_TEXT = """EXAMPLE BANK LTD
Statement of Account
Account No: 001234567
Statement Date: 30/09/2026
Opening Balance      10,000.00
Total Credits         5,250.00
Total Debits          3,100.50
Closing Balance      12,149.50
"""


def _types_with_bank(tmp_path: Path) -> Path:
    """A copy of the real document types plus a bank_statement type: the new type is only files."""
    root = tmp_path / "project"
    shutil.copytree(ROOT / "document_types", root / "document_types")
    shutil.copytree(ROOT / "config", root / "config")
    bank = root / "document_types" / "bank_statement"
    (bank / "templates").mkdir(parents=True)
    (bank / "type.yml").write_text(BANK_TYPE, encoding="utf-8")
    (bank / "fields.yml").write_text(BANK_FIELDS, encoding="utf-8")
    (bank / "checks.yml").write_text(BANK_CHECKS, encoding="utf-8")
    (bank / "templates" / "example_bank_statement.yml").write_text(BANK_TEMPLATE, encoding="utf-8")
    return root


def _run_catalog(text: str, catalog) -> dict:
    groups, _ = split_into_documents([text], catalog)
    assert groups and groups[0]["family"], "no template matched"
    return _process_group(groups[0], ROOT / "test.pdf", catalog)


def test_a_new_document_type_is_only_a_folder(tmp_path: Path):
    catalog = load_catalog(_types_with_bank(tmp_path))
    assert catalog.rejected == {}
    doc = _run_catalog(BANK_TEXT, catalog)
    assert (doc["type"], doc["status"], doc["fields"]["amount_closing_balance"]) == ("bank_statement", "OK", 12149.5)
    wrong = _run_catalog(BANK_TEXT.replace("12,149.50", "12,194.50"), catalog)
    assert wrong["status"] == "FAIL"                      # the type's own check catches a misread


def test_a_field_may_be_defined_once(tmp_path: Path):
    root = _types_with_bank(tmp_path)
    (root / "document_types" / "bank_statement" / "fields.yml").write_text(
        BANK_FIELDS + "  amount: {type: money, role: total}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="already defined"):
        load_catalog(root)


def test_routing_keeps_document_types_apart(tmp_path: Path):
    catalog = load_catalog(_types_with_bank(tmp_path))
    assert [dt.name for dt in catalog.route(BANK_TEXT)] == ["bank_statement"]
    assert {dt.name for dt in catalog.route("nothing that names a document type")} == {dt.name for dt in catalog.types}


def test_keyword_index_finds_the_same_templates_as_a_full_scan():
    for pdf in sorted(ROOT.glob("*.pdf")):
        for text in page_texts(pdf):
            allowed = {dt.name for dt in CATALOG.route(text)}
            full = [t for t in matching_templates(text, CATALOG.templates) if CATALOG.type_of(t).name in allowed]
            assert CATALOG.candidates(text) == full, pdf.name


def test_template_names_are_unique_across_types(tmp_path: Path):
    root = _types_with_bank(tmp_path)
    shared_only = ("issuer: Dup\nkeywords: ['DUP LTD']\nfields:\n  invoice_number: 'No:\\s*(\\S+)'\n"
                   "  date: 'Date:\\s*(\\d{4}-\\d{2}-\\d{2})'\n  amount: 'Total\\s+([\\d.]+)'\n"
                   "options: {currency: INR, date_formats: ['%Y-%m-%d']}\n")
    for type_name in ("bank_statement", "invoice"):
        (root / "document_types" / type_name / "templates" / "dup.yml").write_text(shared_only, encoding="utf-8")
    rejected = load_catalog(root).rejected
    assert list(rejected) == ["invoice/dup.yml"] and "already used" in rejected["invoice/dup.yml"][0]


def test_output_carries_document_type_and_field_pages():
    result = process_pdf(need("VIL_bill_7573001478_2026-08-22_statement.pdf"), CATALOG)
    assert result["format_version"] == 2
    doc = result["documents"][0]
    assert doc["type"] == "invoice"
    assert doc["field_pages"]["invoice_number"] == 1 and doc["field_pages"]["amount_sgst"] == 3


def test_excel_has_one_summary_sheet_per_document_type(tmp_path: Path):
    from openpyxl import load_workbook
    results = [process_pdf(need(name), CATALOG) for name in ("oyo.pdf", "ACK742817410300726.pdf")]
    write_excel(results, tmp_path / "documents.xlsx", CATALOG)
    sheets = load_workbook(tmp_path / "documents.xlsx").sheetnames
    assert sheets[:2] == ["Invoice", "Tax Document"] and "Line Items" in sheets


def test_overlap_gate_finds_clash_overlap_and_missing_sample(tmp_path: Path):
    meesho, oyo = need("ms_invoice_326255343871054720_1.pdf"), need("oyo.pdf")
    derive(tmp_path, "meesho_tax_invoice.yml", "abc_marketplace_invoice.yml", issuer="ABC Marketplace")
    (tmp_path / "com.oyo.invoice.yml").write_text(
        "issuer: OYO\nkeywords: [OYO, Oravel, Stays]\n"
        "fields:\n"
        "  amount: 'Grand Total\\s+Rs (\\d+)'\n"
        "  date: 'Date:\\s(\\d{1,2}/\\d{1,2}/\\d{4})'\n"
        "  invoice_number: '([A-Z0-9]+)\\s+Cash at Hotel'\n"
        "options: {currency: INR, date_formats: ['%d/%m/%Y']}\n", encoding="utf-8")
    catalog = Catalog.single(pool_with(tmp_path), REGISTRY)
    report = overlap.check_samples([meesho, oyo], catalog)
    found = {(f.level, f.where) for f in report.findings}
    assert (overlap.CLASH, f"{meesho.name} pages [1]") in found
    assert (overlap.OVERLAP, f"{oyo.name} pages [1]") in found
    assert (overlap.KEYWORD_SUBSET, "keywords") in found and report.failed()
    assert report.read_by["anthropic_invoice.yml"] == 0 and report.failed("anthropic_invoice.yml")


def test_overlap_gate_is_clean_on_the_real_templates():
    samples = sorted(ROOT.glob("*.pdf"))
    if not samples:
        pytest.skip("no sample PDFs in the project folder")
    report = overlap.check_samples(samples, CATALOG)
    assert [f for f in report.findings] == [], [(f.level, f.where, f.message) for f in report.findings]
    # every template whose sample PDF is in the folder must read it (PDFs not copied here are skipped)
    expected = [json.loads(f.read_text(encoding="utf-8")) for f in EXPECTED]
    covered = {d["template"] for e in expected if (ROOT / e["source_file"]).exists() for d in e["documents"]}
    assert all(report.read_by.get(t, 0) > 0 for t in covered), [t for t in covered if not report.read_by.get(t)]


# --- optional_fields ----------------------------------------------------------------------------

def test_optional_field_missing_does_not_make_check(tmp_path: Path):
    """A field listed in optional_fields may be absent without a warning; other absent fields
    still warn."""
    extra = "  customer_number: 'Customer Ref:\\s*(\\S+)'\n  amount_cess:"
    plain = CESS_TEMPLATE.replace("  amount_cess:", extra, 1)
    write_template(tmp_path, "acme.yml", plain)
    doc = run_text(CESS_TEXT, load_templates(tmp_path, REGISTRY).templates)
    assert any("found nothing: customer_number" in i["message"] for i in doc["issues"])

    optional = plain.replace("required_fields:", "optional_fields: [customer_number]\nrequired_fields:", 1)
    write_template(tmp_path, "acme.yml", optional)
    doc = run_text(CESS_TEXT, load_templates(tmp_path, REGISTRY).templates)
    assert not any("customer_number" in i["message"] for i in doc["issues"])
    assert doc["status"] == "OK"


def test_optional_fields_lint():
    from invoice_engine.lint import lint_template
    import yaml as _yaml
    spec = _yaml.safe_load(CESS_TEMPLATE)
    spec["template_name"] = "acme.yml"
    spec["optional_fields"] = ["no_such_field", "invoice_number"]
    errors, _ = lint_template(spec, REGISTRY)
    assert any("'no_such_field' is not one of" in e for e in errors)
    assert any("'invoice_number' is also in required_fields" in e for e in errors)


# --- layout fingerprints built by default ---------------------------------------------------------

def test_build_layouts_learns_only_from_documents_the_template_won():
    from invoice_engine.fingerprints import build_layouts
    pdf = ROOT / "Invoice_8626225731.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not present")
    results = {r.template: r for r in build_layouts(CATALOG, [pdf], write=False)}
    assert results["zomato_restaurant_invoice.yml"].samples == ["Invoice_8626225731.pdf pages 1 (OK)"]
    assert results["zomato_platform_fee_invoice.yml"].samples == ["Invoice_8626225731.pdf pages 2 (OK)"]
    assert results["zomato_restaurant_invoice.yml"].labels > 0
    assert not results["shopify_bill.yml"].samples          # not a sample of other layouts
    assert not any(r.written for r in results.values())   # write=False changes no file


def test_every_template_has_a_layout_block():
    missing = [t["template_name"] for t in CATALOG.templates if not t.get("layout")]
    assert not missing, ("templates without a layout block; run "
                         "python process_invoice.py --fingerprint <sample folder>: " + ", ".join(missing))


# --- a number the document does not print stays empty -----------------------------------------

def test_empty_typed_number_is_none_not_zero(tmp_path: Path):
    """invoice2data turns an empty int/float into 0; the engine keeps it empty (compat.py)."""
    from invoice2data.extract.invoice_template import InvoiceTemplate
    template = load_templates(INVOICE_TEMPLATES, REGISTRY).templates[0]
    assert isinstance(template, InvoiceTemplate)
    assert template.coerce_type("", "int") is None
    assert template.coerce_type("  ", "float") is None
    assert template.coerce_type("12", "int") == 12


# --- standard form of page text and the stress test ---------------------------------------------

def test_canonical_text_removes_print_differences():
    from invoice_engine.segment import canonical
    a = "Invoice No.: A1\nTotal:  5.00\n\nThanks\n"
    b = "      Invoice No. : A1   \n      Total:  5.00\n\n\n\n      Thanks\n"
    assert canonical(a) == canonical(b) == a
    # padding that lines up a column of colons moves after the colon; columns stay in place
    assert canonical("Terms      : Due      Place: X") == "Terms:       Due      Place: X"
    assert canonical("Time 11 : 04 and https://x") == "Time 11 : 04 and https://x"   # not a label


def test_stress_test_reports_a_fragile_pattern(tmp_path: Path):
    from invoice_engine.stress import compare, REWRITES
    change = REWRITES["short decimals"][1]
    original = {"status": "OK", "fields": {"amount": 212.1, "note": "Total 5.00"}, "lines": []}
    same = {"status": "OK", "fields": {"amount": 212.1, "note": "Total 5"}, "lines": []}
    wrong = {"status": "OK", "fields": {"amount": 21.0, "note": "Total 5"}, "lines": []}
    assert compare(original, same, change) == ([], [])                 # rewritten text counts as same
    assert compare(original, wrong, change) == ([], ["amount"])
    assert compare(original, {"status": "OK", "fields": {}, "lines": []}, change)[0] == ["amount", "note"]


def test_templates_survive_print_variations_on_their_samples():
    """No rewrite may change a value silently (WRONG); the zomato templates must read every
    rewrite exactly as the original."""
    from invoice_engine.stress import stress
    pdf = ROOT / "Invoice_8626225731.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not present")
    reports = stress(CATALOG, [pdf])
    assert not [o for r in reports for o in r.outcomes if o.result == "WRONG"]
    for name in ("zomato_restaurant_invoice.yml",):
        report = next(r for r in reports if r.template == name)
        assert report.counts()["same"] == len(report.outcomes)


# --- automatic template checks in the pipeline ---------------------------------------------------

def _pipeline(tmp_path, catalog=None):
    from invoice_engine.pipeline import Pipeline
    from invoice_engine.samples import SampleStore
    return Pipeline(catalog or CATALOG, SampleStore(tmp_path / "samples"), write_layouts=False)


def test_pipeline_admits_on_first_document_and_stores_it(tmp_path: Path):
    pdf = ROOT / "Invoice_8626225731.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not present")
    pipe = _pipeline(tmp_path)
    result = pipe.process(pdf)
    assert [d["status"] for d in result["documents"]] == ["OK", "OK"]
    assert pipe.state["zomato_restaurant_invoice.yml"] == "admitted"
    assert len(pipe.store.data["samples"]) == 2
    # nothing changed: a second pipeline does no checks and stores nothing new
    again = _pipeline(tmp_path)
    again.process(pdf)
    assert not again.events and len(again.store.data["samples"]) == 2


def test_changed_template_with_different_values_is_held(tmp_path: Path):
    """A template edit that keeps the sums right but reads another value is not trusted."""
    import shutil
    pdf = ROOT / "Invoice_8626225731.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not present")
    _pipeline(tmp_path).process(pdf)                        # stores the verified values
    types = tmp_path / "document_types"
    shutil.copytree(ROOT / "document_types", types)
    shutil.copytree(ROOT / "config", tmp_path / "config")
    target = types / "invoice" / "templates" / "zomato_restaurant_invoice.yml"
    text = target.read_text(encoding="utf-8")
    old = "  seller_name: 'Legal Entity Name\\s*:\\s*(.+?)\\s*\\n'"
    assert old in text
    target.write_text(text.replace(old, "  seller_name: 'Restaurant Name\\s*:\\s*(.+?)\\s*\\n'"), encoding="utf-8")
    pipe = _pipeline(tmp_path, load_catalog(types, tmp_path / "config"))
    pipe.prepare()
    assert pipe.state["zomato_restaurant_invoice.yml"] == "held"
    assert any("seller_name" in p for e in pipe.events for p in e.problems)
    assert "zomato_restaurant_invoice.yml" not in {t["template_name"] for t in pipe.catalog.templates}


def test_new_template_that_reads_another_layout_is_held_before_use(tmp_path: Path):
    import shutil
    pdf = ROOT / "Invoice_8626225731.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not present")
    _pipeline(tmp_path).process(pdf)
    types = tmp_path / "document_types"
    shutil.copytree(ROOT / "document_types", types)
    shutil.copytree(ROOT / "config", tmp_path / "config")
    source = (types / "invoice" / "templates" / "zomato_platform_fee_invoice.yml").read_text(encoding="utf-8")
    copy = source.split("\nlayout:")[0].replace("issuer: Zomato (Eternal Limited)", "issuer: Copy Co")
    (types / "invoice" / "templates" / "zz_copy.yml").write_text(copy, encoding="utf-8")
    pipe = _pipeline(tmp_path, load_catalog(types, tmp_path / "config"))
    result = pipe.process(pdf)
    assert pipe.state["zz_copy.yml"] == "held"
    assert [d["status"] for d in result["documents"]] == ["OK", "OK"]   # the real template still reads it
