INVOICE PARSER - TEMPLATE AND CONFIGURATION REFERENCE
=====================================================

1. HOW THE PARTS FIT TOGETHER
  invoice_engine/      the engine. It contains no issuer names, field names or tax rules,
                       and does not change when a layout or field is added.
  templates/           one YAML file per document layout: where each value is printed.
  config/fields.yml    every field the templates may produce and what it means
                       (text, date, money, rate ...; for money: total, tax, adjustment ...).
  config/checks.yml    arithmetic checks applied to every document.
  tests/               automatic tests; tests/expected/ holds the known-good output
                       of every sample PDF.

  Adding a layout          -> one template file (+ its expected output in tests/expected)
  Adding a field           -> one line in config/fields.yml
  A new tax or total rule  -> one rule in the template's checks (or in config/checks.yml)
  A new layout version     -> a new template with the same family and a higher version
  Two templates fit one page -> chosen by evidence (section 4); a tie goes to review

2. WHAT A TEMPLATE CONTAINS
  issuer                text identifying the layout family (Zomato, Meesho, ...)
  family, version       optional. Templates with the same family are versions of one layout.
                        Default: family = file name, version = 1.
  keywords              text that must appear on the page for the template to apply
  exclude_keywords      text that must NOT appear (separates look-alike layouts)
  continuation_keywords optional. A page that matches no template but contains all of these
                        is attached to the previous document as its next page.
  required_fields       values that must be found, otherwise the document fails
  fields                one regular expression (text pattern) per value, with exactly one
                        capture group ( ... ). Tables are fields with  parser: lines
                        (item table: lines; tax summary table: tax_lines).
  checks                optional extra or replaced arithmetic checks (section 5)
  options               currency, decimal separator, date formats, languages, text clean-up

3. HOW PAGES BECOME DOCUMENTS
  - Every page is matched to templates by keywords; the document number on the page is read
    with the candidates' invoice_number patterns.
  - A page continues the open document while they still share at least one matching layout
    (family) and the page's number is the same, or missing on one of them. The document keeps
    only the layouts that matched every page, so a template that fits page 1 alone cannot
    split a two-page invoice. A different number, or no shared layout, starts a new document.
  - A page that matches no template is reported as unrecognized and its text is saved to
    output/unrecognized/<file>_p<pages>.txt, ready for writing a new template.
    Exception: continuation_keywords (section 2).

4. CHOOSING BETWEEN TEMPLATES: VERSIONS AND LOOK-ALIKES
  When an issuer changes its invoice design, keep the old template and add a new one:
      family: anthropic_invoice
      version: 2
  Two templates with the same family and version are rejected.

  How the template for a document is chosen:
  - Versions of one family: newest first; the first that does not FAIL represents the family.
    Old invoices keep working with the old version, new invoices use the new one.
  - Several families (look-alike layouts that match the same page): every one is parsed and
    their evidence is compared: status (OK > CHECK > FAIL), number of checks that passed, and
    the share of the template's fields that were found. A skipped check counts for nothing.
  - The result that is at least as good on all three and better on one is used; the output
    lists the others under "also tried".
  - If none is better on every measure, the document gets status CHECK with an "ambiguous"
    warning and the alternatives are listed. A person decides, then makes the keywords of the
    two templates exclusive (exclude_keywords, or a vendor-specific keyword).
  - If every candidate fails, the one with the most evidence is reported with its errors.
  Each document's output carries its "evidence" (checks_passed, checks_skipped, fields_found,
  fields_defined), so every decision can be traced.

5. CHECKS
  Default checks live in config/checks.yml. A check is a rule "left == right" that passes when
  both sides differ by at most the tolerance (default 0.01). If a value the rule needs is not
  on the document, the rule is skipped. Column totals are checked automatically for every line
  field with sums_to in config/fields.yml.
  A template can switch defaults off and add its own:
      checks:
        disable: [qty x unit price]
        line:
          - name: nights x rooms x rate
            rule: "qty * rooms * price_unit == price_total"
        document:
          - name: gross + discount = total
            rule: "amount_gross + amount_discount == amount"
  Rules may only use field names, numbers, + - * / and the functions listed in
  config/checks.yml; they cannot run code.
  A document whose total (a field with role total) was found but checked by no rule gets the
  warning "amount is not verified" and status CHECK: nothing proves the number is right.
  One cent / paisa is allowed per rounded value. GST is rounded per line, so a correct invoice
  can be 0.01 off: on the Zomato restaurant invoice 59.00 + 2.96 = 61.96 but 61.95 is printed.

6. FIELD NAMES
  Use the names in config/fields.yml. The main ones:
    invoice_number      the document's own number (invoice, credit note, bill of supply,
                        consignment note; the booking ID for receipts without a number)
    original_document_number   on credit notes: the document being reversed
    date, date_due, date_order, ...
    seller_*, buyer_*, operator_* (platform issuing for the seller), place_of_supply, hsn_sac
    amount (grand total), amount_untaxed, amount_tax (one printed tax figure),
    amount_cgst / amount_sgst / amount_igst / amount_cess (tax parts),
    amount_round_off, amount_gross, amount_discount, amount_paid, amount_due
    Line columns: name, qty, price_unit, gross_amount, discount, price_subtotal (taxable value),
    line_tax_percent, line_tax_amount, cgst_rate, cgst_amount, ..., price_total (incl. tax)
  An amount_* field that is not registered is rejected: its role decides how the checks use it.
  Other unregistered fields are allowed and reported as a warning.

  Terms
    GSTIN           15-character Indian GST registration number (seller_tax_id / buyer_tax_id
                    also hold EU VAT and Dutch BTW numbers)
    HSN / SAC       classification code of the goods (HSN) or service (SAC)
    CGST / SGST     central and state halves of the tax on sales within one state
    IGST            tax on sales between states
    Cess            additional tax on specific goods, on top of GST
    Bill of supply  invoice for tax-exempt supplies
    Credit note     document that reverses all or part of an earlier invoice

7. CHECKS RUN WHEN TEMPLATES ARE LOADED
  A template is REJECTED (not used) when it has: unreadable YAML, missing or non-text keywords
  (quote keywords containing a colon: - 'BILL TO:'), an invalid regex, a regex with more or
  fewer than one capture group, ^ or $ without (?m), numeric dates without date_formats, an
  unregistered amount_* field, a required field the template never defines, a malformed check,
  or a family+version already used by another template.
  Run only the template checks:   python process_invoice.py --lint

8. WRITING A TEMPLATE SAFELY
  - Keep the file ASCII; write other characters as escapes inside patterns:
    \u20b9 (rupee sign), \u20ac (euro sign), \u00fc (u with umlaut).
  - Keep   replace: [['\r', '']]   in options so the template also works with the
    invoice2data command line on Windows.
  - Edit templates in VS Code or Notepad and save as UTF-8, not with Windows PowerShell 5.1
    Get-Content / Set-Content.
  - After every change:   python process_invoice.py --lint
                           python -m pytest -q tests
  - For a new layout, run the PDF once, check the output by hand, then copy
    output/<file>.json to tests/expected/ so the result is protected from future changes.