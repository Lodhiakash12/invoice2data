"""Second opinion on text fields: the AI reads a new template's first documents on its own, and
the engine compares the two readings.

Amounts are proven by arithmetic; names, addresses, registration numbers, document numbers and
dates are not. The text checks (textchecks.py) catch misreads with a recognisable shape; a value
of the right shape taken from the wrong place (the Ship To address read as the Bill To address,
the seller's GSTIN read as the buyer's, an address cut off after its first line) passes them. And
the first documents a template reads OK become its stored samples: the values every later version
must keep reading. A wrong text value stored there is locked in.

So the first documents of every template (setting ai.second_opinion, default 3; it starts again
when the template file changes) are read a second time by the AI, independently: the AI gets the
page text and the field names, never the template's values (shown the values, a model tends to
agree with them). The engine compares, field by field:

  agree             the same text (spaces, case and punctuation aside), the same number, date
  differ            WARN: the document becomes CHECK, is not stored as a sample, and shows both
  template missed   WARN: the template read nothing, the page shows the value the AI gives
  AI unreliable     INFO: the AI's value is not on the page (made up or reworded), or the AI
                    found nothing: the template's value stands

When a person has checked a template's values and the template is right (the AI misreads a field
of that layout), `python process_invoice.py --trust <template>` stops the second opinion for
that template file. A template that agreed on `second_opinion` documents is not asked about again.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

SYSTEM = """You copy values from business documents exactly as printed. You never guess: a value
that is not printed is null. Answer with one JSON object only."""

SKIP = {"issuer", "document_type", "currency", "tax_label", "tax_note"}
MAX_FIELDS = 30
MAX_TEXT = 24000
_COUNTRY = {"india", "in", "bharat"}
_ID_SUFFIXES = ("_tax_id", "_pan", "_company_reg", "_fssai", "number", "_reference", "iban", "bic",
                "_account", "_ifsc", "irn", "hsn_sac", "_tax_number")
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y",
                 "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y", "%d %b, %Y", "%d/%m/%y", "%d-%m-%y")

_PARTIES = {
    "seller": "the seller: the party that issues the document (Sold By, supplier, from)",
    "buyer": "the buyer: the party billed (Bill To, Billing Address, customer, recipient)",
    "operator": "the e-commerce or platform operator, when it is printed apart from the seller",
    "ship_from": "the place the goods are sent from (Ship From, Ship-from Address, dispatch from)",
    "ship_to": "the place the goods are delivered to (Ship To, Shipping Address, delivery address)",
    "taxpayer": "the taxpayer the document is about",
    "hotel": "the hotel",
}
_KINDS = {
    "name": "legal or person name",
    "trade_name": "shop or brand name, when printed apart from the legal name",
    "address": "postal address",
    "tax_id": "GSTIN (15 characters) or VAT number",
    "pan": "PAN (10 characters)",
    "company_reg": "CIN or company registration number",
    "fssai": "FSSAI licence number",
    "state": "state, as printed",
    "state_code": "state code, as printed",
    "email": "e-mail address",
}


def describe(name: str, notes: dict | None = None) -> str:
    """A short description of a field for the AI: the note written after it in fields.yml when
    there is one ("number / name of the train"), else one made from its name."""
    note = (notes or {}).get(name)
    base = _describe_name(name)
    return f"{base} ({note})" if note and note.lower() not in base.lower() else base


def _describe_name(name: str) -> str:
    for party, who in sorted(_PARTIES.items(), key=lambda item: -len(item[0])):
        if name.startswith(party + "_"):
            kind = name[len(party) + 1:]
            text = f"{_KINDS.get(kind, kind.replace('_', ' '))} of {who}"
            if kind == "address":
                text += ("; every line of the address block in reading order, joined with ', ', without the "
                         "name, phone, e-mail, GSTIN, PAN or other labelled lines")
            return text
    if name == "invoice_number":
        return "the document's own number (invoice / bill / note number), not an order or reference number"
    if name == "date":
        return "the document's own date (invoice date, bill date), as YYYY-MM-DD"
    if name.startswith("date_"):
        return name[5:].replace("_", " ") + " date, as YYYY-MM-DD"
    return name.replace("_", " ")


# --- comparing -----------------------------------------------------------------------------------

def _is_id(name: str) -> bool:
    return name.endswith(_ID_SUFFIXES)


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(text)).upper()


def _words(text: str) -> list[str]:
    text = str(text).casefold().replace("&", " and ")
    return re.findall(r"[0-9a-zऀ-ॿ]+", text)


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _differences(ours: str, theirs: str) -> tuple[list[str], list[str]]:
    """Pieces of text only in ours (extra) and only in theirs (missing), spaces and punctuation
    aside; country names and single characters are left out (a convention, not a misread)."""
    a, b = "".join(_words(ours)), "".join(_words(theirs))
    extra, missing = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete") and i2 - i1 > 1 and a[i1:i2] not in _COUNTRY:
            extra.append(a[i1:i2])
        if op in ("replace", "insert") and j2 - j1 > 1 and b[j1:j2] not in _COUNTRY:
            missing.append(b[j1:j2])
    return extra, missing


def _on_page(value: str, page: str, name: str) -> bool:
    """The AI's value is printed on the page (not made up or reworded)."""
    if _is_id(name):
        return len(_alnum(value)) >= 3 and _alnum(value) in _alnum(page)
    words = [w for w in _words(value) if w not in _COUNTRY]
    if not words:
        return False
    page_words = set(_words(page))
    joined = "".join(_words(page))
    found = sum(1 for w in words if w in page_words or (len(w) > 3 and w in joined))
    return found >= 0.9 * len(words)


@dataclass
class Finding:
    field: str
    outcome: str                 # agree / differ / template_missed / ai_unreliable / ai_missing
    template_value: object = None
    ai_value: object = None
    detail: str = ""

    @property
    def level(self) -> str:
        return "WARN" if self.outcome in ("differ", "template_missed") else "INFO"

    def message(self) -> str:
        if self.outcome == "differ":
            return (f"second opinion: {self.field} differs: template read {str(self.template_value)[:120]!r}, "
                    f"the AI reads {str(self.ai_value)[:120]!r}{self.detail}")
        if self.outcome == "template_missed":
            return (f"second opinion: the template read nothing for {self.field}, the AI reads "
                    f"{str(self.ai_value)[:120]!r} on the page")
        if self.outcome == "ai_unreliable":
            return f"second opinion: the AI's {self.field} {str(self.ai_value)[:80]!r} is not on the page (ignored)"
        if self.outcome == "ai_missing":
            return f"second opinion: the AI found no {self.field} (the template's value stands)"
        return f"second opinion: {self.field} agrees"


def compare(name: str, ours, theirs, page: str, other_values: list[str] = ()) -> Finding:
    """One field: the template's value against the AI's, with the page text to tell a value the
    AI made up from one that is printed."""
    if _empty(theirs):
        return Finding(name, "agree" if _empty(ours) else "ai_missing", ours, None)
    if isinstance(theirs, (dict, list)):
        return Finding(name, "ai_unreliable", ours, json.dumps(theirs)[:80])
    theirs = str(theirs).strip()
    if name == "date" or name.startswith("date_") or isinstance(ours, date):
        mine, other = _as_date(ours), _as_date(theirs)
        if other is None:
            return Finding(name, "ai_unreliable", ours, theirs)
        if _empty(ours):
            return Finding(name, "template_missed", None, other.isoformat())
        if mine == other:
            return Finding(name, "agree", ours, theirs)
        return Finding(name, "differ", mine.isoformat() if mine else ours, other.isoformat())
    if not _on_page(theirs, page, name):
        return Finding(name, "ai_unreliable", ours, theirs)
    if _empty(ours):
        # the AI's value is one the template read into another field: the AI put a value it saw
        # under the wrong name, not a value the template missed
        if any(_alnum(theirs) == _alnum(v) or "".join(_words(theirs)) in "".join(_words(v)) for v in other_values):
            return Finding(name, "ai_unreliable", None, theirs)
        return Finding(name, "template_missed", None, theirs)
    if _is_id(name):
        same = _alnum(ours) == _alnum(theirs)
        return Finding(name, "agree" if same else "differ", ours, theirs)
    if "".join(_words(ours)) == "".join(_words(theirs)) or sorted(_words(ours)) == sorted(_words(theirs)):
        return Finding(name, "agree", ours, theirs)
    extra, missing = _differences(str(ours), theirs)
    if not extra and not missing:
        return Finding(name, "agree", ours, theirs)
    detail = ""
    if extra:
        detail += f"; only in the template's value: {', '.join(repr(p) for p in extra[:3])}"
    if missing:
        detail += f"; not in the template's value: {', '.join(repr(p) for p in missing[:3])}"
    return Finding(name, "differ", ours, theirs, detail)


# --- asking --------------------------------------------------------------------------------------

def fields_to_check(template, registry) -> list[str]:
    """The template's text and date fields, except static ones and labels."""
    names = []
    for name, spec in (template.get("fields") or {}).items():
        if name == "lines" or name in SKIP:
            continue
        if isinstance(spec, dict) and spec.get("parser") == "static":
            continue
        kind = registry.document.get(name)
        if kind is None or kind.type not in ("text", "date"):
            continue
        names.append(name)
    return names[:MAX_FIELDS]


def prompt(page_text: str, names: list[str], notes: dict | None = None) -> str:
    listing = "\n".join(f"- {name}: {describe(name, notes)}" for name in names)
    return (f"""Read this document and copy the value of each field below exactly as printed (same
spelling, same characters; dates as YYYY-MM-DD). A field that is not printed on the document is
null. Do not take a value of one party for another (seller and buyer are different parties).

FIELDS
{listing}

Answer with one JSON object, every field above as a key:
```json
{{{", ".join(f'"{n}": "..."' for n in names[:3])}{", ..." if len(names) > 3 else ""}}}
```

DOCUMENT TEXT
{page_text[:MAX_TEXT]}""")


@dataclass
class Opinion:
    findings: list[Finding] = field(default_factory=list)
    error: str = ""

    @property
    def agreed(self) -> bool:
        return not self.error and not any(f.level == "WARN" for f in self.findings)


def ask(provider, page_text: str, names: list[str], log=None, notes: dict | None = None) -> tuple[dict | None, str]:
    """The AI's own reading of the fields (no template values are sent): (values, error)."""
    from .ai_author import _json
    from .ai_provider import AIError
    question = prompt(page_text, names, notes)
    problem = ""
    for attempt in range(2):
        try:
            answer = provider.complete(SYSTEM, question)
        except AIError as exc:
            if log:
                log("second_opinion", question, f"ERROR: {exc}")
            return None, f"AI service: {exc}"
        if log:
            log("second_opinion", question, answer)
        try:
            data = _json(answer)
            if isinstance(data, dict):
                return data, ""
            problem = "the answer is not a JSON object"
        except ValueError as exc:
            problem = str(exc)
        question = (prompt(page_text, names, notes) + f"\n\nYour previous answer could not be read ({problem}). "
                    "Answer with the JSON object only.")
    return None, f"the AI's answer could not be read twice ({problem})"


def review(provider, document: dict, page_text: str, names: list[str], log=None, notes: dict | None = None) -> Opinion:
    """Ask the AI and compare its reading with the document's fields."""
    if not names:
        return Opinion()
    data, error = ask(provider, page_text, names, log, notes)
    if data is None:
        return Opinion(error=error)
    fields = document.get("fields") or {}
    findings = []
    for name in names:
        others = [str(v) for k, v in fields.items() if k != name and isinstance(v, str) and v.strip()]
        findings.append(compare(name, fields.get(name), data.get(name), page_text, others))
    return Opinion(findings=findings)


# --- state per template --------------------------------------------------------------------------
# store.data["second_opinions"][template] = {hash, trusted, documents: {"<file hash>:<pages>":
# {agreed, issues, source, when}}}. A document is asked about once per template file; read again,
# it gets the same result without a new question. A changed template file starts again.

def _entry(store, name: str, template_hash: str) -> dict:
    table = store.data.setdefault("second_opinions", {})
    saved = table.get(name)
    if not saved or saved.get("hash") != template_hash:
        saved = {"hash": template_hash, "trusted": False, "documents": {}}
        table[name] = saved
    saved.setdefault("documents", {})
    return saved


def agreed_count(store, name: str, template_hash: str) -> int:
    saved = store.data.get("second_opinions", {}).get(name) or {}
    if saved.get("hash") != template_hash:
        return 0
    return sum(1 for d in (saved.get("documents") or {}).values() if d.get("agreed"))


def question_of(names: list[str], notes: dict | None = None) -> str:
    """A fingerprint of the question asked (fields and their descriptions): an answer to an earlier,
    different question (a field described better since) does not stand for this one."""
    import hashlib
    text = json.dumps([[n, describe(n, notes)] for n in names])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def previous(store, name: str, template_hash: str, document_key: str, question: str | None = None) -> dict | None:
    """The result of an earlier second opinion on this document by this template file (and, when
    given, the same question)."""
    saved = store.data.get("second_opinions", {}).get(name) or {}
    if saved.get("hash") != template_hash:
        return None
    found = (saved.get("documents") or {}).get(document_key)
    if found is not None and question is not None and found.get("question") != question:
        return None
    return found


def wanted(store, name: str, template_hash: str, limit: int) -> bool:
    """Whether a new document of this template gets a second opinion."""
    if limit <= 0:
        return False
    saved = store.data.get("second_opinions", {}).get(name) or {}
    if saved.get("hash") == template_hash and saved.get("trusted"):
        return False
    return agreed_count(store, name, template_hash) < limit


def record(store, name: str, template_hash: str, document_key: str, opinion: Opinion, source: str,
           question: str | None = None) -> None:
    """Keep the result of one document (not when the AI could not answer: asked again next time)."""
    if opinion.error:
        return
    saved = _entry(store, name, template_hash)
    saved["documents"][document_key] = {
        "agreed": opinion.agreed, "source": source, "when": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "issues": [{"level": f.level, "message": f.message()} for f in opinion.findings
                   if f.outcome != "agree"][:20]}
    store.save()


def trust(store, name: str, template_hash: str) -> None:
    """A person checked the template's values: no more second opinions for this template file, and
    documents it differed on before are no longer marked."""
    saved = _entry(store, name, template_hash)
    saved["trusted"] = True
    saved["trusted_when"] = datetime.now().isoformat(timespec="seconds")
    store.save()


def trusted(store, name: str, template_hash: str) -> bool:
    saved = store.data.get("second_opinions", {}).get(name) or {}
    return saved.get("hash") == template_hash and bool(saved.get("trusted"))


def apply_previous(document: dict, result: dict) -> None:
    """The issues of an earlier second opinion on the same document (no new question)."""
    issues = document.setdefault("issues", [])
    issues.extend(dict(i) for i in result.get("issues", []))
    issues.append({"level": "INFO", "message": f"second opinion given before ({result.get('when', '')})"})
    if not result.get("agreed") and document.get("status") == "OK":
        document["status"] = "CHECK"


def page_text(texts: list[str], pages: list[int]) -> str:
    return "\n".join(f"=== page {p} ===\n{texts[p - 1]}" for p in pages if 0 < p <= len(texts))


def apply(document: dict, opinion: Opinion) -> None:
    """Add the findings to the document: a WARN makes an OK document CHECK."""
    issues = document.setdefault("issues", [])
    if opinion.error:
        issues.append({"level": "INFO", "message": f"second opinion not available: {opinion.error}"})
        return
    for finding in opinion.findings:
        if finding.outcome != "agree":
            issues.append({"level": finding.level, "message": finding.message()})
    agreeing = sum(f.outcome == "agree" for f in opinion.findings)
    issues.append({"level": "INFO", "message": f"second opinion: {agreeing} of {len(opinion.findings)} "
                                               f"text fields read the same by the AI"})
    if not opinion.agreed and document.get("status") == "OK":
        document["status"] = "CHECK"


def source_name(pdf: Path, pages: list[int]) -> str:
    return f"{Path(pdf).name} p{','.join(str(p) for p in pages)}"


# --- stored samples ------------------------------------------------------------------------------

def check_samples(catalog, store, author, only: str | None = None, per_template: int = 3) -> list[dict]:
    """A second opinion on the stored samples of every template (or one): the documents read
    before the second opinion existed. At most per_template documents each, the most recent first;
    documents asked about before are not asked again. Returns one entry per document asked."""
    from .admission import template_hash
    from .segment import page_texts
    report = []
    for template in catalog.templates:
        name = template["template_name"]
        if only and name != only:
            continue
        digest = template_hash(catalog, template)
        if trusted(store, name, digest):
            continue
        registry = catalog.registry_for(template)
        names = fields_to_check(template, registry)
        if not names:
            continue
        notes = getattr(registry, "notes", {})
        question = question_of(names, notes)
        samples = sorted((s for s in store.data["samples"] if s.get("template") == name and store.pdf_path(s).is_file()),
                         key=lambda s: s.get("added", ""), reverse=True)
        asked = 0
        for sample in samples:
            key = f"{sample['pdf']}:{','.join(str(p) for p in sample['pages'])}"
            if previous(store, name, digest, key, question) is not None:
                continue
            if asked >= per_template or agreed_count(store, name, digest) >= per_template:
                break
            pdf = store.pdf_path(sample)
            texts = page_texts(pdf)
            log = lambda purpose, prompt, answer, pdf=pdf: author._log(pdf, purpose, prompt, answer)  # noqa: E731
            found = review(author.provider, sample, page_text(texts, sample["pages"]), names, log, notes)
            record(store, name, digest, key, found, f"{sample.get('name', pdf.name)} p{','.join(map(str, sample['pages']))} (stored sample)",
                   question)
            asked += 1
            report.append({"template": name, "document": sample.get("name", pdf.name), "pages": sample["pages"],
                           "error": found.error, "agreed": found.agreed,
                           "findings": [f.message() for f in found.findings if f.outcome != "agree"]})
    return report
