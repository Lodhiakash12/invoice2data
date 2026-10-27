PDF DOCUMENT PARSER - TEMPLATE AND CONFIGURATION REFERENCE
==========================================================

1. HOW THE PARTS FIT TOGETHER
  invoice_engine/            the engine. It contains no issuer names, document types, field names
                             or tax rules, and does not change when a layout, field or type is added.
  document_types/<type>/     one folder per kind of document (invoice, tax_document, ...):
      type.yml               name, routing signals, columns of its Excel sheet
      fields.yml             the fields this type adds to the shared ones
      checks.yml             arithmetic checks every document of this type gets
      templates/             one YAML per layout: where each value is printed
  config/fields.yml          fields every type shares: identity, dates, parties, bank, total
  config/settings.yml        engine settings: poppler_path, regex_timeout_seconds
  tests/                     automatic tests; tests/expected/ holds the reviewed output of every
                             sample PDF (it contains real names and amounts: keep it local)

  Adding a layout            -> one template file in its type's templates/ (+ expected output)
  Adding a field             -> one line in the type's fields.yml (or config/fields.yml if shared)
  A new tax or total rule    -> one rule in the template's checks, or in the type's checks.yml
  A new layout version       -> a new template with the same family and a higher version
  A new kind of document     -> a new folder under document_types/ (section 2)
  Two templates fit one page -> chosen by evidence (section 5); a tie goes to review

  Commands (PowerShell, from the project folder)
    python process_invoice.py                       every PDF in this folder
    python process_invoice.py D:\Invoices           every PDF in another folder
    python process_invoice.py --lint                only check the templates
    python process_invoice.py --overlap samples     the overlap test (section 13)
    python -m pytest -q tests                       all automatic tests

2. DOCUMENT TYPES
  A document type is a folder; the engine reads every folder under document_types/ that has a
  type.yml. Today: invoice (invoices, credit notes, receipts, bills) and tax_document (ITR
  acknowledgement).

  type.yml
      name: bank_statement
      description: Bank account statements
      signals: [statement of account, opening balance, closing balance]
      min_signals: 2
      summary_columns: [issuer, invoice_number, date, amount_opening_balance, amount_closing_balance]
    signals      words (any case) that show a page belongs to this type. A page is matched only
                 against the templates of the types whose signals it carries (at least
                 min_signals of them). A page with the signals of no type is matched against
                 every type, so a missing signal costs time, never a document.
    summary_columns  the columns of this type's sheet in documents.xlsx (tax_total = the total
                 tax of the document).

  fields.yml and checks.yml use the format of config/fields.yml and of the checks in section 6.
  A field name may be defined once only: in config/fields.yml or in one type's fields.yml.
  A type's templates may use the shared fields and their own type's fields, nothing else.

  Adding a type, step by step (example: bank statements)
    1. create document_types/bank_statement/ with type.yml, fields.yml, checks.yml, templates/
    2. fields.yml: the amounts and values of a statement (opening and closing balance, totals of
       credits and debits; transaction lines as line fields)
    3. checks.yml: what must add up, e.g.
         document:
           - name: opening + credits - debits = closing
             rule: "amount_opening_balance + amount_credits - amount_debits == amount_closing_balance"
    4. one template per bank layout, a sample PDF of each, then --lint, --overlap and the tests.
  Until a type exists, its documents come out as unrecognized (or FAIL), never as another type.

3. WHAT A TEMPLATE CONTAINS
  issuer                text identifying the layout family (Zomato, Meesho, ...)
  family, version       optional. Templates with the same family are versions of one layout.
                        Default: family = file name, version = 1.
  keywords              text that must appear on the page for the template to apply
  exclude_keywords      text that must NOT appear (separates look-alike layouts)
  continuation_keywords optional. A page that matches no template but contains all of these
                        is attached to the previous document as its next page.
  required_fields       values that must be found, otherwise the document fails
  optional_fields       optional. Fields printed on only some documents of this layout (an
                        e-mail line, an IRN, a reference to an original invoice). When one of
                        these is not found, the document is not marked CHECK for it. Any other
                        template field that finds nothing gives a warning (CHECK).
  fields                one regular expression (text pattern) per value, with exactly one
                        capture group ( ... ). Tables are fields with  parser: lines
                        (item table: lines; tax summary table: tax_lines).
  checks                optional extra or replaced arithmetic checks (section 6)
  min_checks            optional, default 1: arithmetic checks that must pass before a document
                        with amounts can be OK. 0 only for documents that print no arithmetic.
  options               currency, decimal separator, date formats, languages, text clean-up

4. HOW PAGES BECOME DOCUMENTS
  - Each page is routed to the document types whose signals it carries (section 2).
  - Within those types a keyword index skips every template whose most specific keyword is not
    on the page; only the rest get the full keyword test. The cost grows with the number of
    keywords, not templates (measured: about 10 ms per page with 5,000 templates).
  - A page continues the open document while they share a matching layout and the document
    number agrees:
      the same number                            -> same document
      the page prints "Page 1 of n"              -> a new document
      the page's number cannot be read           -> a new document, unless the page says
                                                    "Page 2 of n" (or later) or carries the
                                                    layout's continuation_keywords
      the layout prints no number on either page -> same document
  - A page that repeats the open document's first page word for word (apart from words such as
    ORIGINAL / DUPLICATE / FOR RECIPIENT) is another copy of that document: it is reported as a
    separate document with "copy_of" and not counted again in the totals.
  - A page that matches no template is reported as unrecognized and its text is saved to
    output/unrecognized/<file>_p<pages>.txt, ready for writing a new template.
  - A page with little text and a picture over at least half of it is reported as scanned
    ("OCR needed, not supported yet") instead of unrecognized.

5. CHOOSING BETWEEN TEMPLATES: VERSIONS AND LOOK-ALIKES
  When an issuer changes its invoice design, keep the old template and add a new one:
      family: anthropic_invoice
      version: 2
  Two templates with the same family and version are rejected.

  How the template for a document is chosen:
  - Versions of one family: every version reads the document; the one with the most evidence
    represents the family (on equal evidence the newest). Old invoices keep working with the
    old version, new invoices use the new one.
  - Several families (look-alike layouts that match the same page): every one is parsed and
    their evidence is compared: status (OK > CHECK > FAIL), number of checks that passed, and
    the share of the template's fields that were found. A skipped check counts for nothing, and
    neither does a check whose values are all zero.
  - The result that is at least as good on all three and better on one is used; the output
    lists the others under "also tried".
  - If none is better on every measure, the layout fingerprint may decide (section 12); if it
    cannot, the document gets status CHECK with an "ambiguous" warning and the alternatives. A
    person decides, then makes the keywords of the two templates exclusive.
  - If every candidate fails, the one with the most evidence is reported with its errors.
  Each document's output carries its "evidence" (checks_passed, checks_trivial, checks_skipped,
  fields_found, fields_defined), so every decision can be traced.

6. CHECKS
  Default checks live in each type's checks.yml. A check is a rule "left == right" that passes
  when both sides differ by at most the tolerance (default 0.01). If a value the rule needs is
  not on the document, the rule is skipped. Column totals are checked automatically for every
  line field with sums_to.
  A template can switch defaults off and add its own:
      checks:
        disable: [qty x unit price]
        line:
          - name: nights x rooms x rate
            rule: "qty * rooms * price_unit == price_total"
        document:
          - name: gross + discount = total
            rule: "amount_gross + amount_discount == amount"
  Rules may only use field names, numbers, + - * / and the functions listed in the invoice
  checks.yml; they cannot run code.
  A check whose two sides are both zero (0 == 0) agrees but proves nothing about the reading:
  it counts as "trivial", not as passed.
  A document with amounts on which fewer than min_checks checks passed (default 1) gets the
  warning "... is not verified" and status CHECK: nothing proves the numbers were read right.
  One cent / paisa is allowed per rounded value. GST is rounded per line, so a correct invoice
  can be 0.01 off: on the Zomato restaurant invoice 59.00 + 2.96 = 61.96 but 61.95 is printed.

7. FIELD NAMES
  Use the names in config/fields.yml and in the type's fields.yml. The main ones:
    invoice_number      the document's own number, for every type (invoice, credit note, bill
                        of supply, consignment note; the acknowledgement number of an ITR)
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
    IRN             invoice reference number of a GST e-invoice
    Bill of supply  invoice for tax-exempt supplies
    Credit note     document that reverses all or part of an earlier invoice

8. OUTPUT
  output/<file>.json, one per PDF (format_version 2). Per document:
    pages, type (document type), template, family, version, status, fields, lines, issues,
    evidence, field_pages (the page each value was read from), copy_of (for copies)
  Two input PDFs with the same name (from different folders) get <name>.json and <name>_2.json.
  output/documents.xlsx: one summary sheet per document type, then Line Items, All Fields (with
  the page of each value) and Issues. If documents.xlsx is open in Excel, the run writes
  documents_<date>_<time>.xlsx instead of failing.

9. SAFETY
  - Every template pattern runs with a time limit (config/settings.yml, regex_timeout_seconds,
    default 2 s). A pattern that runs longer is stopped: only its template fails on that
    document, with the message "pattern took longer than ...".
  - A file that cannot be processed is reported in its own JSON ("ERROR: processing stopped on
    this file") and the run goes on with the next file.
  - pdftotext warnings on a file it could still read (a sign of a damaged file) are reported in
    the notes.
  - Nothing is sent over the network: the engine and its tests run with networking disabled.

10. CHECKS RUN WHEN TEMPLATES ARE LOADED
  A template is REJECTED (not used) when it has: unreadable YAML, missing or non-text keywords
  (quote keywords containing a colon: - 'BILL TO:'), an invalid regex, a regex with more or
  fewer than one capture group, ^ or $ without (?m), numeric dates without date_formats, an
  unregistered amount_* field, a required field the template never defines, a malformed check,
  a replacement that names a group its pattern does not have (\2 with one group), a bad
  min_checks, a family+version already used, or a file name or family already used by
  another document type.
  Run only the template checks:   python process_invoice.py --lint

11. WRITING A TEMPLATE SAFELY
  - Keep the file ASCII; write other characters as escapes inside patterns:
    \u20b9 (rupee sign), \u20ac (euro sign), \u00fc (u with umlaut).
  - Keep   replace: [['\r', '']]   in options so the template also works with the
    invoice2data command line on Windows.
  - Edit templates in VS Code or Notepad and save as UTF-8, not with Windows PowerShell 5.1
    Get-Content / Set-Content.
  - Every template ships with at least one sample PDF in the samples folder.
  - After every change:   python process_invoice.py --lint
                           python process_invoice.py --overlap samples
                           python -m pytest -q tests
  - For a new layout, run the PDF once, check the output by hand, then copy
    output/<file>.json to tests/expected/ so the result is protected from future changes.

12. LAYOUT FINGERPRINT (a block at the end of every template)
  Every template carries a layout block, built when the template is made, so that a tie
  between two look-alike layouts can be decided the first time it happens. It is written by a
  command, never by hand:
      python process_invoice.py --fingerprint samples          every template, from a folder
      python process_invoice.py --fingerprint x.yml a.pdf b.pdf only template x.yml
  A document is a sample of a template only when the engine, with every template competing,
  chose that template for it and the result is not FAIL (copies are left out). So a template
  never learns another layout's pages. The block lists the template's label words in reading
  order with the column they start in (page width cut into 12 strips):
      layout:
        columns: 12
        sample_pages: 2
        order:
          - ['invoice', 7]
          - ['subtotal', 7]
  - Only words that also occur in the template's keywords and patterns are stored: no names,
    addresses or amounts, and no pattern syntax such as group names.
  - Rows are not stored, so a longer item table or a taller page does not change the fingerprint.
  - With several samples, only labels found in the same place on at least half of them are kept.
  - Run the command again after changing a template's patterns or when more samples arrive; it
    replaces the old block.
  - Lint warns about a template without the block, and a test fails until every template has one.
  - The block is only read to break a tie; it never overrides the checks.
  - Layouts that are truly the same page design (the Meesho invoice, credit note and bill of
    supply) get fingerprints too similar to separate them; those are kept apart by keywords
    and exclude_keywords, which the overlap test (section 13) proves.
  - An automatic template-writing step calls the same function after a new template has read
    its sample correctly:  build_layouts(catalog, [sample_pdf], only={"<new template>.yml"})
    (invoice_engine/fingerprints.py).

13. OVERLAP TEST (run before a template goes live)
  Two templates of different layouts, in any document type, must not both read the same
  document. Keep one or more sample PDFs of every template in one folder (for example samples\)
  and run:
      python process_invoice.py --overlap samples
      python process_invoice.py --overlap <new>.yml samples     (only pairs with <new>)
  Every sample is split into documents and parsed exactly as in a normal run. Results:
    KEYWORD SUBSET  every keyword of one template is inside the other's keywords, so it matches
                    all of the other's documents (found without any PDF)
    CLASH           two layouts read the document equally well: a normal run sends it to review
    OVERLAP         two layouts read it without FAIL; the evidence picks one today, but a later
                    invoice of the same vendor can turn it into a clash or a wrong pick
    KEYWORDS ONLY   several layouts' keywords match, only one reads it without FAIL (harmless)
    NO SAMPLE       no document in the samples is read by this template, so the others were
                    never tried on its documents: add a sample PDF of it
  The command ends with exit code 1 on CLASH or OVERLAP (and on NO SAMPLE for the template
  named), so it can stop a template from going live.
  How to fix a CLASH or OVERLAP, for templates A (right for this document) and B (wrong):
  - add to B's exclude_keywords a word only A's documents print, or
  - add to B's keywords a word only B's documents print.
  Then run the test again. If both templates are for the same vendor and layout, one of them is a
  duplicate: keep the better one, or make it a new version of the same family.

14. STANDARD FORM OF PAGE TEXT
  The same layout is printed with small differences from one month or one system version to
  the next. Before any template sees a page, its text is put into one standard form, so these
  differences cannot break a template:
  - "Label :" becomes "Label:". Padding that lines up a column of colons
    ("Terms        : Due") moves after the colon, so the rest of the line keeps its columns.
  - Spaces at line ends are removed; lines holding only spaces become empty.
  - Indentation shared by every line of the page is removed (a page shifted right moves back).
  - Two or more blank lines in a row become one.
  Templates are written against this form: a pattern never needs a space before a colon and
  never counts blank lines. The text shown for unrecognized pages is in this form too.

15. STRESS TEST (run after writing or changing a template)
      python process_invoice.py --stress samples          every template
      python process_invoice.py --stress x.yml samples    only template x.yml
  For every document a template reads in the sample PDFs, the text is rewritten in ways real
  versions of a layout differ, one at a time, and the template is run on each rewrite. Every
  rewrite keeps all values the same, so the right answer is known:
      space before colon / no space before colon   "Name:" vs "Name :"
      wider column gaps                            columns further apart
      short decimals                               212.10 printed as 212.1, 202.00 as 202
      page shifted right                           every line indented more
      more space between sections                  blank lines doubled
  Results per rewrite:
      same          same status and values
      not matched   the keywords no longer match (the document would be unrecognized)
      lost          a worse status or missing values (fails safely: the document is flagged)
      WRONG         a value is present but different: the dangerous case, since nothing else
                    may notice it. The command exits with code 1 when any WRONG is found.
  A template must have no WRONG before it is used. "lost" under "short decimals" only matters
  for issuers whose system drops trailing zeros (Zomato does); amount patterns of new
  templates should accept 1 to 3 decimals or none: (\d[\d,]*(?:\.\d{1,3})?).
  The test finds fragile patterns. It cannot find a value that is already wrong on the original
  document: check the output of a new template by eye once, then keep it in tests/expected.

16. AUTOMATIC TEMPLATE CHECKS (part of every run)
  The checks of sections 12, 13 and 15 run by themselves inside every normal run
  (process_invoice.py and the test page); nobody runs them or approves a template.
  - Sample store. Every document read OK is kept as a sample in samples/ (on this computer):
    one copy of the PDF, named by its content hash, and the values read from it. At most
    samples_per_layout (default 20) documents per layout are kept, the most recent ones.
  - New or changed template. The engine notices that a template file is new or edited and checks
    it before trusting it:
      regression  every stored sample of its layout is still read, as well as before, with the
                  same values
      stress      its samples, printed slightly differently, give the same values (section 15)
      overlap     it reads no other layout's stored samples, and they read none of its (13)
    A template without samples yet gets the overlap check at once and the full check on the
    first document it reads; until then its documents are CHECK, never OK.
  - Admitted: the template is used; its fingerprint is rebuilt automatically each time it gains
    a sample (section 12).
  - Held: the template takes no part in reading until a corrected version passes, so the
    templates that work go on working; a held new version leaves the previous version in
    charge. The reasons are printed in every run under "Automatic template checks".
  - A deliberate correction of earlier values (a template fixed because a stored value was
    wrong) is confirmed with
      python process_invoice.py --accept <template>.yml
    which allows the changed values; stress and overlap must still pass.
  Settings (config/settings.yml): automatic_checks (default true), sample_store (default
  samples), samples_per_layout (default 20). The sample store holds real documents: keep it out
  of shared repositories.
  The commands --stress, --overlap and --fingerprint remain for looking into a problem by hand.

