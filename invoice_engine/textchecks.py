"""Checks on text fields: values no arithmetic can prove (registration numbers, names, addresses).

A wrong amount fails a sum; a wrong text value used to pass unnoticed: a seller name that ran into
the invoice number printed on the same line, a buyer address with the seller's PAN at its end. These
checks catch the misreads that have a recognisable shape:

- format: a GSTIN has 15 characters and a check character (the last one is computed from the
  first 14, so one misread character is caught); a PAN is 5 letters, 4 digits, 1 letter; a CIN is
  21 characters; an FSSAI licence is 14 digits
- agreement: the PAN inside a GSTIN (characters 3-12) is the party's PAN; the first 2 digits of a
  GSTIN are the party's state code
- no labels inside values: a name or an address that contains another field's label ("Invoice
  Number #", "PAN:", "GSTIN -") has run into the next field
- the amount in words spells one of the document's amounts: words cut short (a second line not
  read) or taken from another line do not
- a place or state is not just the first word(s) of a state name ("MADHYA" for Madhya Pradesh:
  a pattern that reads one word)

Each finding is a warning (the document becomes CHECK): the value is probably misread, but an
issuer can also print it wrong. Placeholders printed instead of a number (NA, UNREGISTERED, URP)
and numbers of other countries (DE232446240, NL820471616B01) are left alone.
"""

from __future__ import annotations

import re

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_LIKE = re.compile(r"\d{2}[A-Z0-9]{13}")
PAN = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
CIN = re.compile(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}")
FSSAI = re.compile(r"\d{14}")
_PLACEHOLDER = re.compile(r"(?i)^(?:n\.?\s*a\.?|nil|none|null|unregistered|urp|not applicable|-+)$")

# GST state codes (the first two digits of a GSTIN)
STATE_CODES = {
    "jammu and kashmir": "01", "himachal pradesh": "02", "punjab": "03", "chandigarh": "04",
    "uttarakhand": "05", "haryana": "06", "delhi": "07", "rajasthan": "08", "uttar pradesh": "09",
    "bihar": "10", "sikkim": "11", "arunachal pradesh": "12", "nagaland": "13", "manipur": "14",
    "mizoram": "15", "tripura": "16", "meghalaya": "17", "assam": "18", "west bengal": "19",
    "jharkhand": "20", "odisha": "21", "orissa": "21", "chhattisgarh": "22", "madhya pradesh": "23",
    "gujarat": "24", "daman and diu": "25", "dadra and nagar haveli and daman and diu": "26",
    "dadra and nagar haveli": "26", "maharashtra": "27", "karnataka": "29", "goa": "30",
    "lakshadweep": "31", "kerala": "32", "tamil nadu": "33", "puducherry": "34", "pondicherry": "34",
    "andaman and nicobar islands": "35", "telangana": "36", "andhra pradesh": "37", "ladakh": "38",
    "new delhi": "07",
}
# ISO 3166-2:IN subdivision codes, as Flipkart prints them (IN-GJ)
ISO_CODES = {
    "JK": "01", "HP": "02", "PB": "03", "CH": "04", "UT": "05", "UK": "05", "HR": "06", "DL": "07",
    "RJ": "08", "UP": "09", "BR": "10", "SK": "11", "AR": "12", "NL": "13", "MN": "14", "MZ": "15",
    "TR": "16", "ML": "17", "AS": "18", "WB": "19", "JH": "20", "OR": "21", "OD": "21", "CT": "22",
    "CG": "22", "MP": "23", "GJ": "24", "DD": "25", "DN": "26", "DH": "26", "MH": "27", "KA": "29",
    "GA": "30", "LD": "31", "KL": "32", "TN": "33", "PY": "34", "AN": "35", "TG": "36", "TS": "36",
    "AP": "37", "LA": "38",
}

# a label followed by its separator: the value has run into the next field. Multi-word labels in
# any case (SOLD BY:, Sold By:); the short ones (PAN, CIN, GSTIN) only in capitals, so a company or
# street name ending in "...pan:" is not taken for one
LABEL = re.compile(
    r"(?i:\b(?:Invoice\s+(?:Number|No\.?|Date|Details)|Order\s+(?:ID|Number|No\.?|Date)|"
    r"GST\s+Registration(?:\s+No\.?)?|FSSAI(?:\s+Lic(?:ense|ence)?(?:\s+No\.?)?)?|"
    r"Place\s+of\s+(?:Supply|Delivery)|State\s*(?:/\s*UT)?\s*Code|Bill(?:ing)?\s+(?:To|Address)|"
    r"Ship(?:ping)?\s+(?:To|Address)|Sold\s+By|Phone|Mobile|E-?mail))\s*[:#]"
    r"|\b(?:GSTIN|PAN|CIN)(?:\s+No\.?)?\s*[:#-]\s*[A-Z0-9]")
_LABEL_FIELDS = ("_name", "_trade_name", "_address")


# --- an amount written in words ---------------------------------------------------------------
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i >= 2}
_SCALES = {"thousand": 1000, "lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000,
           "crore": 10000000, "crores": 10000000, "million": 1000000, "billion": 1000000000}
_CURRENCY = {"indian", "rupee", "rupees", "inr", "rs", "only", "s"}
_PAISE = {"paise", "paisa", "paisas"}


def _whole_number(words: list[str]) -> int | None:
    total = current = 0
    seen = False
    for word in words:
        if word == "and":
            continue
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
        else:
            return None                                   # a word that is not part of a number
        seen = True
    return total + current if seen else None


def amount_in_words(text: str):
    """'Indian Rupees Nine Lakh Eighty Six Thousand Nine Hundred Ninety Nine Only' -> 986999;
    'Sixty One Rupees And Ninety Five Paisa Only' -> 61.95. None when the text is not an amount
    in words (other words in it, or no number)."""
    from decimal import Decimal
    words = [w for w in re.split(r"[^a-z]+", str(text).lower()) if w]
    if not words:
        return None
    paise = 0
    if any(w in _PAISE for w in words):
        end = next(i for i, w in enumerate(words) if w in _PAISE)
        before = words[:end]
        # the paise are the number words between the last "and" (or the currency word) and "paise"
        cut = max((i for i, w in enumerate(before) if w in ("and",) or w in _CURRENCY), default=-1)
        paise_words, words = before[cut + 1:], before[:cut + 1]
        value = _whole_number(paise_words)
        if value is None or value >= 100:
            return None
        paise = value
    rupees = _whole_number([w for w in words if w not in _CURRENCY])
    if rupees is None:
        return None
    return Decimal(rupees) + Decimal(paise) / 100


_PLACE_FIELDS = ("place_of_supply", "place_of_origin")


def _cut_state(value: str) -> str | None:
    """The full state name when the value is only its first word(s): 'MADHYA' -> 'madhya pradesh'
    ('Uttar', 'WEST', 'Tamil', 'Jammu and' ...). None when the value is a whole state or not one."""
    name = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", value.lower().replace("&", " and "))).strip()
    name = re.sub(r"^(?:in )?", "", name)
    if not name or name in STATE_CODES:
        return None
    longer = [state for state in STATE_CODES if state.startswith(name + " ")]
    return min(longer, key=len) if longer else None


def _place_issues(fields: dict) -> list[tuple[str, str]]:
    """A place or state value that is only the first word(s) of a state name has been cut off at a
    space (a pattern that reads one word): 'MADHYA' for MADHYA PRADESH. Caught for every template."""
    issues = []
    for name, raw in fields.items():
        if not (name in _PLACE_FIELDS or name.endswith("_state")):
            continue
        value = _text(raw)
        full = _cut_state(value) if value else None
        if full:
            issues.append(("WARN", f"{name} {value!r} is only the start of a state name ({full.title()}): the "
                                   f"pattern probably reads one word; read the value up to the next column"))
    return issues


def _words_issue(fields: dict) -> list[tuple[str, str]]:
    """The amount in words must spell one of the document's printed amounts. A value cut short
    (a second line not read: "... Thousand Nine" for 9,86,999) or taken from another line fails."""
    from decimal import Decimal, InvalidOperation
    words = _text(fields.get("total_in_words"))
    if not words:
        return []
    spelled = amount_in_words(words)
    if spelled is None:
        return [("WARN", f"total_in_words {words[:80]!r} is not an amount in words (other text in it, or "
                         f"cut off in the middle): probably misread")]
    amounts = []
    for name, value in fields.items():
        if name == "amount" or name.startswith("amount_"):
            try:
                amounts.append(Decimal(str(value)).quantize(Decimal("0.01")))
            except (InvalidOperation, ValueError, TypeError):
                continue
    if amounts and spelled.quantize(Decimal("0.01")) not in amounts:
        return [("WARN", f"total_in_words says {spelled:,.2f} ({words[:80]!r}), but no amount of the "
                         f"document is {spelled:,.2f}: the words are probably cut off (a second line not "
                         f"read) or taken from somewhere else")]
    return []


def gstin_check_character(first14: str) -> str:
    total = 0
    for i, char in enumerate(first14):
        product = _CHARS.index(char) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARS[(36 - total % 36) % 36]


def valid_gstin(value: str) -> bool:
    """15 characters whose last one is the check character of the first 14 (special registrations
    such as TDS deductors or non-resident taxpayers have other letters inside, the same check)."""
    return bool(_GSTIN_LIKE.fullmatch(value)) and gstin_check_character(value[:14]) == value[14]


def state_code(value: str) -> str | None:
    """'Gujarat', 'GUJARAT', 'Gujarat(24)', 'IN-GJ', '24' -> '24'."""
    text = str(value).strip()
    found = re.search(r"\((\d{1,2})\)", text) or re.fullmatch(r"(\d{1,2})", text)
    if found:
        return found.group(1).zfill(2)
    iso = re.search(r"\bIN-([A-Z]{2})\b", text)
    if iso:
        return ISO_CODES.get(iso.group(1))
    name = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", text.lower().replace("&", " and "))).strip()
    if name in STATE_CODES:
        return STATE_CODES[name]
    # "Karnataka, India", "Bangalore, Karnataka": a state named inside the text, when only one is
    found = {code for state, code in STATE_CODES.items() if re.search(rf"\b{state}\b", name)}
    return found.pop() if len(found) == 1 else None


def _text(value) -> str | None:
    if isinstance(value, str) and value.strip() and not _PLACEHOLDER.match(value.strip()):
        return value.strip()
    return None


def text_issues(fields: dict) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = list(_words_issue(fields)) + _place_issues(fields)
    gstins = {}
    for name, raw in fields.items():
        value = _text(raw)
        if value is None:
            continue
        if name.endswith("_tax_id") and _GSTIN_LIKE.fullmatch(value):
            if not valid_gstin(value):
                issues.append(("WARN", f"{name} {value!r} is not a valid GSTIN (its check character does not "
                                       f"match): probably misread"))
            else:
                gstins[name[: -len("_tax_id")]] = value
        elif name.endswith("_pan") and not PAN.fullmatch(value):
            issues.append(("WARN", f"{name} {value!r} is not a PAN (5 letters, 4 digits, 1 letter): probably misread"))
        elif name.endswith("_company_reg") and re.match(r"[LU]\d", value) and not CIN.fullmatch(value):
            issues.append(("WARN", f"{name} {value!r} is not a complete CIN (21 characters): probably misread"))
        elif name.endswith("_fssai") and re.fullmatch(r"\d+", value) and not FSSAI.fullmatch(value):
            issues.append(("WARN", f"{name} {value!r} is not an FSSAI licence number (14 digits): probably misread"))
        if name.endswith(_LABEL_FIELDS):
            label = LABEL.search(value)
            if label:
                issues.append(("WARN", f"{name} contains the label {label.group(0)!r}: it has run into the next "
                                       f"field ({value[:80]!r})"))
    for party, gstin in gstins.items():
        pan = _text(fields.get(f"{party}_pan"))
        # a PAN of another kind (a partner's personal PAN on a firm's invoice) is not the GSTIN's
        if pan and PAN.fullmatch(pan) and PAN.fullmatch(gstin[2:12]) and pan != gstin[2:12] and pan[3] == gstin[5]:
            issues.append(("WARN", f"{party}_pan {pan} differs from the PAN inside {party}_tax_id {gstin} "
                                   f"({gstin[2:12]}): one of them is probably misread"))
        for key in (f"{party}_state_code", f"{party}_state"):
            printed = _text(fields.get(key))
            code = state_code(printed) if printed else None
            if code and code != gstin[:2]:
                issues.append(("WARN", f"{key} {printed!r} (state code {code}) differs from the state of "
                                       f"{party}_tax_id {gstin} ({gstin[:2]}): one of them is probably misread"))
                break
    return issues