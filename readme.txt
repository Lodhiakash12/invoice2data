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

17. AI TEMPLATE AUTHOR (optional, config/settings.yml section ai)
  When it is switched on, every normal run (and the test page) gives the AI:
  - pages no template recognizes: the AI names the document type (an existing folder under
    document_types/, or a new one: it then creates the folder with type.yml, fields.yml and
    checks.yml), and writes the template into document_types/<type>/templates/. Fields the
    registry lacks are added to that type's fields.yml.
  - a document an admitted template reads with FAIL: the AI writes the next version of that
    template (same family, version + 1); the old version stays.
  A template the AI writes is used only when
  - it loads without lint errors and its keywords match the document,
  - the document comes out OK (required fields found, arithmetic checks pass),
  - no keyword contains a value of this one document (buyer, number, date, amount),
  - it passes the automatic checks of section 16 (regression, stress, overlap).
  Otherwise the problems go back to the AI (up to max_attempts drafts). A template that never
  passes is removed again, with any folder or fields it added; its last draft is kept in
  samples/ai_drafts/. Each document is given to the AI once; --ai-retry gives it again.
  Every AI exchange is logged in samples/ai_log.jsonl (it contains page text: keep it local).

  Setting it up (NVIDIA build.nvidia.com, free tier for development):
    1. Create a key at build.nvidia.com and set it for the session (PowerShell):
          $env:NVIDIA_API_KEY = "nvapi-..."
    2. List the models and pick one:      python process_invoice.py --ai-models
    3. In config/settings.yml set ai.enabled: true, ai.model: <model id>, and
       ai.send_unanonymised_text: true (test documents only: the page text is sent as it is, and
       the free tier records inputs and outputs).
    4. Check the connection:               python process_invoice.py --ai-test
  Changing to another service later (Claude) changes the provider in ai_provider.py and the
  settings; nothing else.


18. ITEM TABLES READ BY THEIR COLUMN HEADINGS (parser: table)
  A `lines` table reads each row with one fixed pattern ("qty, gross, discount, taxable, ONE
  tax, total"). A document of the same layout with another set of columns (CGST + SGST instead
  of IGST when the seller is in the buyer's state, an extra CESS column, no Qty) then reads no
  rows. `parser: table` reads the header instead and puts each value under the heading above it:

    lines:
      parser: table
      start: '(?m)^Product\s+Title\s+Qty'     # first header line
      end: '(?m)^\s+Total\s+\d'                # line after the table
      columns: {category: '^Product', name: 'Title', qty: 'Qty', gross_amount: 'Gross',
                discount: 'Discount', price_subtotal: 'Taxable', igst_amount: 'IGST',
                cgst_amount: 'CGST', sgst_amount: 'SGST', cess_amount: 'CESS', price_total: 'Total'}
      row: [qty, price_total]                  # a row starts on a line with these values
      text: after                              # description below the amounts (or: before)
      row_end: '^IMEI'                         # optional: a line that closes the row
      extra: ['^\s+IGST:\s*(?P<igst_rate>[\d.]+)\s*%']   # optional patterns for other lines
      prefix: '^SAC:\s*(?P<hsn_sac>\d+)'      # optional: a value in front of the row, no heading
      types: {qty: int, price_total: float, ...}         # numeric columns need a type

  List every column the issuer CAN print, not only those of the first sample. The header ends at
  the first blank line (or after header_lines lines). A value is placed under the nearest heading
  (at most max_gap = 12 characters away), in left-to-right order; a number written one space
  after a word stays part of the text ("Pack of 2"). Two values for one column are an error, never
  a choice. Used by: flipkart_seller_tax_invoice.yml.

  What the table reader handles, and what it refuses:
    - a value under a heading that columns do not list (TCS when only CESS is listed): error that
      names the heading; the value is never put in a neighbouring column
    - two columns whose headings are the same printed word: template error
    - the table continued on the next page: the header printed again is read again (its columns
      may stand elsewhere); footer and policy sentences between the pages are left out
    - numbers: 1,23,456.78 / 1.234,56 (decimal_separator ',') / (135.00) and 135.00- as minus,
      but (18.0) under a rate column stays 18 / currency before or after (Rs, INR, $, EUR) /
      "-" or "NA" as an empty value / "2 Nos" as 2 / "1 Pigeon ..." as serial 1 + description
    - a column title printed over two or three header lines ("Gross" above "Amount", "SGST/"
      above "UGST") is matched by the heading as it reads ('Gross Amount'), the same pattern as
      for a print with the title on one line
    - a table without a description column (Myntra: amount columns only) prints the item name
      and its "HSN: ..." line across the amount columns: such a line is read by the table's own
      extra patterns (a line no pattern reads there is text outside the table, such as a footer)
    - descriptions printed above their amounts with text: after: error asking for text: before
      (unless row_end is set, which places the text of each row explicitly)
    - not handled: a description line long enough to run under the amount columns is left out of
      the name; taxes printed on separate lines under one "Tax Amount" heading (Amazon) need the
      lines parser.

19. ROUNDED TOTALS (total_rounding, total_rounding_mode)
  Some issuers print the total rounded to whole rupees without a round-off line (IRCTC agent
  tickets: 518.15 -> 519). The template says so, and the check compares the rounded sum exactly:

    total_rounding: 1                    # whole rupees
    total_rounding_mode: up              # up | nearest | down (nearest when left out)

  It applies only when the printed total is a whole number; a total that is wrong (519 printed
  for 518.15 under "nearest", or 520) still fails.

  One document cannot always tell the rule: 1378.60 -> 1379 is both "up" and "nearest". The
  template then lists every rule that fits:

    total_rounding_mode: [up, nearest]

  A document passes with any listed rule. The first later document that fits only some of them
  settles it: the engine writes the template line itself, for example
    total_rounding_mode: up   # settled 2026-10-29 by 2147438163.pdf (was up, nearest)
  and the changed template is checked again on every stored sample on the next run. Two documents
  that fit different rules are reported, and the template is not changed. The AI template author
  writes the list when its document fits more than one rule.

20. CHECKS ON TEXT FIELDS (textchecks.py, part of every run)
  Amounts are proven by arithmetic; names, addresses and registration numbers are not. Misreads
  with a recognisable shape make the document CHECK:
    - GSTIN (fields *_tax_id): 15 characters whose last one is the check character computed from
      the first 14, so a single misread character is caught
    - PAN (*_pan): 5 letters, 4 digits, 1 letter; CIN (*_company_reg starting L or U): 21
      characters; FSSAI (*_fssai): 14 digits
    - the PAN inside the GSTIN (characters 3-12) equals the party's PAN field (not compared when
      the printed PAN is another kind of holder, such as a partner's personal PAN on a firm's
      invoice)
    - the first two digits of the GSTIN equal the party's state (*_state_code, *_state: 27,
      Maharashtra, Gujarat(24), IN-GJ, "Bangalore, Karnataka")
    - a name or address (*_name, *_trade_name, *_address) contains no other field's label
      ("Invoice Number #", "PAN: ...", "Sold By:", "Phone:")
    - total_in_words, turned into a number (lakh, crore, thousand, "... Rupees And ... Paisa"),
      equals one of the document's amounts: words cut short (a second line not read) or taken
      from another line are caught in every template
    - a place or state (place_of_supply, place_of_origin, *_state) that is only the first
      word(s) of a state name ("MADHYA" for Madhya Pradesh: a pattern that reads one word)
  Placeholders (NA, UNREGISTERED, URP) and other countries' numbers (DE..., NL...B01, HRB ...) are
  not checked. A finding is a warning, not an error: an issuer can also print a value wrong.

21. BLOCKS READ FROM WORD POSITIONS (parser: region, words.py)
  A digital PDF stores where each word is drawn. A block printed beside other blocks (Bill To |
  Ship To, Sold By | Billing Address) is read from those positions, so the column next to it
  cannot run into it, even where the page text puts the two one space apart:

    buyer_name: {parser: region, heading: 'Bill To', max_lines: 1}
    buyer_address: {parser: region, heading: 'Bill To', skip: 1, stop: '^Phone'}

  The column starts at the heading and ends at the next heading on the heading's line (or the
  page edge). Lines are read downwards until `stop` matches (not included), a vertical gap, or
  max_lines (8). skip: lines left out at the top; same_line: true when the value starts on the
  heading's line ("Sold By: X"); replace: [[pattern, replacement], ...] on the joined value.
  Inside a line, a gap of three characters ends the block (a note at the right edge is not part
  of the last column). A word running across the column's left edge (a centred or right-aligned
  block) gives no value rather than part of one. Without the PDF (a template run on text alone)
  a region field is empty.
  join: what goes between the lines (default one space; ', ' for an address block printed
  without commas); a line that already ends with a separator ("Ahmedabad -", "Road,") is joined
  by a space.
  Label and value on one line ("Address : Flat 12, ..." with more lines hanging under the value,
  "Place of Supply:" further right): same_line: true. The column runs from the value to the next
  column (found on that line, or where the lines just above and below start their next column,
  so a value printed one space before it is still cut right); the next label at the left
  ("Pincode:") ends the block. A colon drawn as a word of its own ("Address", ":") reads as
  "Address:" for the heading.
  Columns printed half a line apart (Shiprocket: State Code / Place of Supply between the
  buyer's lines) are kept apart: rows near the heading that lie wholly right of it, with nothing
  at the block's left edge, mark where the next column starts.
  Used by (buyer, and some seller blocks): flipkart_seller_tax_invoice, flipkart_platform_fee_
  invoice, flipkart_bill_of_supply, flipkart_grocery_invoice, flipkart_seller_own_invoice,
  shiprocket_charges_invoice, shiprocket_credit_note, shiprocket_services_invoice, meesho_tax_
  invoice, meesho_credit_note, meesho_bill_of_supply, meesho_bill_of_supply_credit_note,
  mtalkz_tax_invoice, ishwam_sales_invoice, anthropic_invoice, blinkit_fee_invoice. Each was
  changed only where it read the same values as before on every sample document.
  Kept on patterns: blinkit_goods_invoice (its address takes the pincode from the next label),
  zomato_* (continuation lines start under the label, not under the value), blocks without a
  heading (bol.com, Shopify, Vodafone, QualityHosting) and right-aligned blocks (Amazon).
  A block's lines may have lines of the next column printed between them (Myntra: the seller's
  name on the line between "Bill to / Ship to:" and the buyer's name): the block ends at empty
  space, not at those lines, and never reaches more than a few lines past its last line.
  Tables keep reading the page text: pdftotext widens its columns to fit a crowded row, which
  rebuilt text at one scale does not (tested).

22. VARIANT TRACKER (python process_invoice.py --variants, variants.py)
  One layout is printed in several variants; a template has read only the ones it has seen. For
  every layout the report shows, from the documents in the sample store (read OK):
    seen          variants read on real documents, with how many
    TO COLLECT    variants the template has fields for but no real document proves yet: IGST,
                  CGST+SGST, several items, several pages (several pages once documents with
                  several items are known)
    if available  discount, round-off, cess, a shipping or handling row between the items
    cannot read   a GST kind the template has no fields for: such a document fails and goes to
                  the AI repair
    not expected  listed in document_types/<type>/variants.yml with the reason (Shopify: always
                  IGST; a bill of supply: no GST)
    recent FAIL / CHECK   the last documents of this layout that were not read OK, with the reason
  Variants are worked out from the values read (amount_igst, cgst_amount, number of lines, pages,
  amount_round_off ...), so they hold for every template. What a template can read comes from its
  fields, line patterns and table columns.

    python process_invoice.py --variants            every layout; also writes output\variants.xlsx
    python process_invoice.py --variants x.yml      one layout

  variants.xlsx: sheet "To collect" (one row per document wanted, needed / if available: the list
  for the testing team) and sheet "Coverage" (one row per layout). The test page (gui.py) shows
  the same table under "Variant coverage".
  During normal runs: the first document of a variant a layout had not read before gives the note
  "first CGST+SGST document read OK by <layout>". The sample store keeps an older document while
  it is the only one of a variant (the limit samples_per_layout drops the oldest of the common
  variants first).

23. SECOND OPINION ON TEXT FIELDS (second_opinion.py, needs the AI step)
  The first documents a template reads OK become its stored samples, the values every later
  version must keep reading. Amounts are proven by the checks; names, addresses, registration
  numbers, document numbers and dates are not. So the first documents of every template are read
  a second time by the AI, on its own: it gets the page text and the field names, never the
  template's values. The engine compares field by field:
    agree             the same (spaces, case, punctuation, line joins and "India" aside; dates in
                      any format; numbers character by character)
    differs           WARN: the document becomes CHECK, both values are shown, and it is not
                      stored as a sample
    template missed   WARN: the template read nothing, the AI reads a value printed on the page
    AI unreliable     INFO only: the AI's value is not on the page (made up or reworded), or the
                      AI found nothing; the template's value stands
  Only the template's text and date fields are asked (no amounts, no static values). One question
  per document; a document is never asked about twice by the same template file (read again, it
  gets the same result). After `second_opinion` documents agreed, the template is not asked about
  any more; when the template file changes, it starts again. When the AI cannot be reached, the
  document stays OK and is asked about on a later run; if the AI then disagrees, the document is
  taken out of the sample store.

  config/settings.yml, section ai:
    second_opinion: 3        # documents per template file the AI reads again; 0 = off

    python process_invoice.py --second-opinion          the stored samples of every template
                                                        (at most second_opinion per template)
    python process_invoice.py --second-opinion x.yml    only template x.yml
    python process_invoice.py --trust x.yml             a person checked x.yml's values and they
                                                        are right (the AI misreads that layout):
                                                        no more second opinions for this file
  The test page (gui.py) shows a "Template values are right" button on such a document.
  The AI template author uses the same reading while it writes: each draft's text fields are
  compared with it, and a difference goes back to the AI with the value the page shows (a
  pattern that takes the neighbouring column's text, the wrong word of a line). The reading is
  asked once per document, for every draft and for the check after admission. When the last
  draft still differs, it is admitted and the document shows the difference (CHECK): the AI's
  reading can also be the wrong one.
  Every question and answer is in samples\ai_log.jsonl (purpose: second_opinion).

24. BOTH GST KINDS IN EVERY INDIAN TEMPLATE
  A seller in the buyer's state charges CGST + SGST (or UTGST); a seller in another state
  charges IGST. Every Indian template reads both, except where variants.yml says the issuer never
  prints one (Shopify: always IGST; Flipkart bill of supply: no GST). How each prints the other
  kind is proven on conversions of real samples (tests/gst_variants.py): the same document with
  the other kind's columns, rows and totals, checked by the same arithmetic, and one wrong tax
  value in it makes the document fail. A real document of the other kind that is printed
  differently fails visibly and goes to the AI repair. Meesho, whose seller-same-state print was
  never seen, is read with CGST on the item's line and SGST/UTGST after it or on the line below.
  An item row has only the columns it prints: a CGST row has no IGST column (no empty value).
  Stress test, short decimals: a number standing in its own column keeps its width (212.10 ->
  "212.1 "), because in a PDF the next column does not move; a number printed together with what
  follows just gets shorter ("18.00%" -> "18%", "20.00 INR" -> "20 INR").

25. GARBLED TEXT LAYERS (document.py)
  A PDF re-saved by a compressor or converter that rebuilt its fonts with a wrong character map
  looks right on screen, but its text comes out scrambled ("Tax Invoice" -> "Tmx @jvk`go"). Such
  a page is reported ("the text layer is garbled ... use the original PDF from the issuer") and is
  not given to the AI: no template and no AI can read it. A page counts as garbled when almost none
  of its words is a common invoice word and many words have characters such as ` @ ^ | inside
  them (pages under 40 words are not judged). The fix is the original PDF, or OCR (not yet).

26. TEMPLATE MANAGEMENT: THE WAITING ROOM AND BACKGROUND AI JOBS (waiting.py, jobs.py)
  Reading never waits for the AI. Every document is read at once with the templates there are;
  what needs the AI is put in the waiting room (samples\waiting\, listed in samples\index.json)
  and the AI works on several documents of one layout together, later:
    a template reads it OK           OK, stored as a sample. Same layout: nothing for the AI, however
                                     many small differences the documents have (an extra line such as
                                     "Advanced Receipt Voucher No.", another item count)
    a template matches, reads FAIL   FAIL now; waits for that family's template to be EXTENDED
    no template matches              PENDING now; waits for a NEW template
    OK, but the template's first     OK now, stored; waits for the AI's second opinion (section 23),
    documents (second opinion)       asked for several documents in one request
  A group goes to the AI when it holds ai.batch_size documents (default 3), when its oldest
  document has waited ai.max_wait_days (default 7), or when you ask:
    python process_invoice.py --waiting            what is waiting, and the last AI jobs
    python process_invoice.py --ai-now             every waiting group now (also with PDFs: then
                                                   the run's files the jobs changed are read again)
    test page: "Waiting for the AI" (Send all to the AI now), or "Send to the AI now" on a PENDING page
  process_invoice.py runs the due jobs at the end of a run and writes the changed results again;
  the test page runs them in the background (after every upload and every 5 minutes) and keeps
  reading uploads meanwhile. The AI drafts in a copy of document_types (samples\staging); only an
  admitted template is copied back, and never over a file a person changed during the job.

  Same layout, variant or new? Decided by outcome, not by a percentage of difference:
    - a document a template reads OK is that layout;
    - a document a template matches but cannot read is a variant of that family. The AI first
      EXTENDS the template (given the family's stored samples as documents it must keep reading
      the same, and the waiting documents with their problems): one template that reads every
      print of the layout. Only when no draft passes is a NEW VERSION written (family_v2), and the
      old version stays for the documents it reads;
    - a document no template matches is grouped with other such documents by look (signature.py:
      the short fixed texts of the page, and the issuer's PAN / CIN). Measured on the stored
      samples, documents of one layout share 50% or more of these texts, other layouts mostly
      less, but an issuer's sibling layouts (invoice and credit note) can share 90%: so the look
      only groups, and the group is confirmed by one template reading all of them OK. When none
      does, a template is written for the oldest document alone and the others are read again
      with it: what it reads is that layout, what it cannot read regroups by the result.
      A document printed with a known issuer's PAN that none of the issuer's templates matches is
      grouped by issuer, and gets a new template (the AI may call it a version of that family).
  A wrong grouping costs an AI attempt, never a wrong value: every template still has to pass
  the regression on all stored samples of its family, the stress test and the overlap test.

  Optional fields come from the documents: when a template is written or extended from several
  documents, a required field that some of them print and others do not is moved to
  optional_fields by the engine (identity fields and the total never are). A field none of them
  prints is reported to the AI as a broken pattern.

  Settings (config\settings.yml, section ai; the defaults apply when the lines are missing):
    batch_size: 3          # documents of one layout collected first; 0 = the AI works at once,
                           # while the document is read (as before)
    max_wait_days: 7       # a smaller group goes to the AI after this long
  A group the AI could not handle is tried again when batch_size new documents of it arrive or
  the AI settings change (after an AI service error: after 15 minutes). The template a job
  extended is kept as it was before in samples\template_history\.
