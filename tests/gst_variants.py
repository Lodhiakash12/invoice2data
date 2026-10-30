"""The other GST kind of a layout, made from a real sample: an IGST invoice becomes the CGST + SGST
print of the same layout (and the other way round), every other column kept where it was. Used by
the tests to prove that a template reads both kinds before a real document of the second kind
arrives."""
import re
from decimal import Decimal


def half(amount: str):
    v = Decimal(amount.replace(',', ''))
    a = (v / 2).quantize(Decimal('0.01'), rounding='ROUND_DOWN'); b = v - a
    fmt = (lambda x: f"{x:,.2f}") if ',' in amount or '.' in amount else (lambda x: f"{x:.0f}")
    return fmt(a), fmt(b)

def rate_half(r: str):
    v = Decimal(r) / 2
    return (f"{v:.2f}" if '.' in r and len(r.split('.')[1]) == 2 else f"{v.normalize()}")


def split_col(t, old, new_a, new_b, header_hint=r"Taxable|Gross"):
    """One tax column becomes two (IGST -> CGST + SGST), every other column keeps its place: the
    header word is widened and each row's value under it becomes two values; text further right
    moves by the same width on every line."""
    lines = t.split("\n")
    c = None
    sep = "    "
    w = len(new_a) + len(sep) + len(new_b) - len(old)
    out = []
    for line in lines:
        if c is None:
            m = re.search(rf"\b{old}\b", line)
            if m and re.search(header_hint, line):
                c = m.start()
                out.append(line[:c] + new_a + sep + new_b + line[m.end():])
                continue
            out.append(line); continue
        tok = None
        for m in re.finditer(r"-?[\d,]+\.\d{1,3}", line):
            if m.start() <= c + len(old) + 2 and m.end() >= c - 1:
                tok = m
        if tok is not None:
            a, b = half(tok.group())
            L = len(tok.group())
            out.append(line[:tok.start()] + a.rjust(L) + b.rjust(w) + line[tok.end():])
        elif len(line) > c:
            out.append(line[:c] + " " * w + line[c:])
        else:
            out.append(line)
    return "\n".join(out)


def merge_cols(t, a_head, b_head, new):
    """Two tax columns become one (SGST + CGST -> IGST) without moving any other column."""
    lines = t.split("\n"); out = []; ca = cb = None
    for line in lines:
        if ca is None:
            ma, mb = re.search(rf"\b{a_head}\b", line), re.search(rf"\b{b_head}\b", line)
            if ma and mb:
                ca, cb = (ma.start(), ma.end()), (mb.start(), mb.end())
                line = line[:ca[0]] + new.ljust(ca[1] - ca[0]) + line[ca[1]:cb[0]] + " " * (cb[1] - cb[0]) + line[cb[1]:]
            out.append(line); continue
        toks = [m for m in re.finditer(r"₹?[\d,]+\.\d{2}", line)]
        ta = next((m for m in toks if m.start() <= ca[1] + 3 and m.end() >= ca[0] - 3), None)
        tb = next((m for m in toks if m.start() <= cb[1] + 3 and m.end() >= cb[0] - 3), None)
        if ta and tb:
            cur = "₹" if ta.group().startswith("₹") else ""
            total = Decimal(ta.group().strip("₹").replace(",", "")) + Decimal(tb.group().strip("₹").replace(",", ""))
            line = (line[:ta.start()] + (cur + f"{total:.2f}").rjust(len(ta.group())) + line[ta.end():tb.start()]
                    + " " * len(tb.group()) + line[tb.end():])
        line = re.sub(r"^(\s+)CGST (\d+(?:\.\d+)?) %$", lambda m: f"{m.group(1)}IGST {Decimal(m.group(2))*2} %", line)
        if re.fullmatch(r"\s+SGST \d+(?:\.\d+)? %", line):
            continue
        out.append(line)
    return "\n".join(out)


CONVERT = {
 'flipkart_platform_fee_invoice.yml': lambda t: re.sub(r"(?m)^(\s+)IGST: ([\d.]+) %$",
     lambda m: f"{m.group(1)}CGST: {Decimal(m.group(2))/2} %\n{m.group(1)}SGST/UTGST: {Decimal(m.group(2))/2} %",
     split_col(t, "IGST", "CGST", "SGST")),
 'flipkart_bill_of_supply.yml': lambda t: merge_cols(t, "SGST", "CGST", "IGST"),
 'shiprocket_services_invoice.yml': lambda t: re.sub(r"(?m)^(\s+Total\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\s+)([\d,]+\.\d{2})\s*$",
     lambda m: f"{m.group(1)}{half(m.group(2))[0]}   {half(m.group(2))[1]}",
     re.sub(r"(\s)(\d+)%(\s+)([\d,]+\.\d{2})\s*$", lambda m: f"{m.group(1)}{int(m.group(2))//2}%{m.group(3)}{half(m.group(4))[0]}   {int(m.group(2))//2}%   {half(m.group(4))[1]}", t, flags=re.M)),
 'flipkart_seller_own_invoice.yml': lambda t: re.sub(r"\|\s*IGST:\s*([\d.]+)%", lambda m: f"| CGST: {Decimal(m.group(1))/2:.2f}% | SGST: {Decimal(m.group(1))/2:.2f}%",
     re.sub(r"(?m)^(\s+\d+\s+-?[\d,]+\.\d{2}\s+-?[\d,.]+\s+-?[\d,]+\.\d{2}\s+)([\d,]+\.\d{2})(\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}\s*)$",
     lambda m: f"{m.group(1)}{half(m.group(2))[0]}   {half(m.group(2))[1]}{m.group(3)}", t)),

 'shiprocket_charges_invoice.yml': lambda t: re.sub(r"(?m)^(\s*)([\d.]+)%\s+IGST(\s+)([\d,]+\.\d{2})",
     lambda m: f"{m.group(1)}{rate_half(m.group(2))}% CGST{m.group(3)}{half(m.group(4))[0]}\n\n{m.group(1)}{rate_half(m.group(2))}% SGST{m.group(3)}{half(m.group(4))[1]}", t),
 'shiprocket_credit_note.yml': lambda t: re.sub(r"(?m)^(\s*)([\d.]+)%\s+IGST(\s+)Rs\.\s*([\d,]+\.\d{2})",
     lambda m: f"{m.group(1)}{rate_half(m.group(2))}% CGST{m.group(3)}Rs. {half(m.group(4))[0]}\n\n{m.group(1)}{rate_half(m.group(2))}% SGST{m.group(3)}Rs. {half(m.group(4))[1]}", t),
 'ishwam_sales_invoice.yml': lambda t: re.sub(r"(?m)^(\s*)OUTPUT IGST (\d+)%(\s+)([\d,]+\.\d{2})",
     lambda m: f"{m.group(1)}OUTPUT CGST {rate_half(m.group(2))}%{m.group(3)}{half(m.group(4))[0]}\n{m.group(1)}OUTPUT SGST {rate_half(m.group(2))}%{m.group(3)}{half(m.group(4))[1]}", t),
 'mtalkz_tax_invoice.yml': lambda t: re.sub(r"(\s+)IGST(\d+) \((\d+)%\)(\s+)([\d,]+\.\d{2})",
     lambda m: f"{m.group(1)}CGST{int(m.group(2))//2} ({int(m.group(3))//2}%){m.group(4)}{half(m.group(5))[0]}\n{m.group(1)}SGST{int(m.group(2))//2} ({int(m.group(3))//2}%){m.group(4)}{half(m.group(5))[1]}",
     re.sub(r"(?m)^(\s*\d+\s+\S.*?\s{2,}[\d,]+\s+[\d,]*\.\d+\s+)(\d+)%(\s+)([\d,]+\.\d{2})(\s+[\d,]+\.\d{2}\s*)$",
     lambda m: f"{m.group(1)}{int(m.group(2))//2}%{m.group(3)}{half(m.group(4))[0]}   {int(m.group(2))//2}%   {half(m.group(4))[1]}{m.group(5)}", t)),
}



# --- the second pass (2026-10-29): Meesho, Zomato platform fee, Blinkit, Flipkart grocery, Vodafone

def _fmt_like(sample: str, value: Decimal) -> str:
    places = len(sample.split(".")[1]) if "." in sample else 0
    text = f"{value:,.{places}f}" if "," in sample else f"{value:.{places}f}"
    return text


def _meesho_tax(m, same_line: bool):
    rate, amount = Decimal(m.group(1)), Decimal(m.group(2).replace(",", ""))
    a = (amount / 2).quantize(Decimal("0.01"), rounding="ROUND_DOWN"); b = amount - a
    r = f"{rate / 2:.1f}"
    if same_line:
        return f"CGST @{r}% :Rs.{a:.2f}  SGST @{r}% :Rs.{b:.2f}"
    return f"CGST @{r}% :Rs.{a:.2f}", f"SGST @{r}% :Rs.{b:.2f}"


def meesho_same_line(t):
    return re.sub(r"IGST @([\d.]+)% :Rs\.([\d,]+\.\d{2})", lambda m: _meesho_tax(m, True), t)


def meesho_next_line(t):
    """CGST on the item's line, SGST below it in the Taxes column: beside the wrapped description
    when there is one, else on a line of its own."""
    lines, out, pending = t.split("\n"), [], None
    for line in lines:
        if pending is not None:
            col, text = pending
            pending = None
            if re.match(r"^\s+\S", line) and len(line.rstrip()) < col - 2:   # a description line
                out.append(line.rstrip().ljust(col) + text)
                continue
            out.append(" " * col + text)
        m = re.search(r"IGST @([\d.]+)% :Rs\.([\d,]+\.\d{2})", line)
        if m:
            cgst, sgst = _meesho_tax(m, False)
            line = line[:m.start()] + cgst.ljust(len(m.group())) + line[m.end():]
            pending = (m.start(), sgst)
        out.append(line)
    if pending is not None:
        out.append(" " * pending[0] + pending[1])
    return "\n".join(out)


def merge_rate_pairs(t, header_old, header_new):
    """CGST (%) CGST (INR) SGST (%) SGST (INR) -> IGST (%) IGST (INR): on item rows the two equal
    rates and two amounts become one doubled rate and the summed amount; on the Total row the two
    tax totals become one."""
    out = []
    for line in t.split("\n"):
        if re.search(header_old, line):
            out.append(re.sub(header_old, header_new, line)); continue
        m = re.search(r"(?<=\s)(\d+(?:\.\d+)?)\s+(\d[\d,]*\.\d+)\s+\1\s+(\d[\d,]*\.\d+)(?=\s)", line)
        if m and not line.lstrip().startswith("Total"):
            rate = Decimal(m.group(1)) * 2
            total = Decimal(m.group(2).replace(",", "")) + Decimal(m.group(3).replace(",", ""))
            out.append(line[:m.start()] + f"{_fmt_like(m.group(1), rate)}   {_fmt_like(m.group(2), total)}" + line[m.end():])
            continue
        m = re.match(r"^(Total\s+.*?)(\d[\d,]*\.\d+)(\s+)(\d[\d,]*\.\d+)(\s+\d[\d,]*\.\d+\s*)$", line)
        if m:
            total = Decimal(m.group(2).replace(",", "")) + Decimal(m.group(4).replace(",", ""))
            out.append(m.group(1) + _fmt_like(m.group(2), total) + m.group(5)); continue
        out.append(line)
    return "\n".join(out)


def grocery_igst(t):
    t = re.sub(r"(Taxable\s+)SGST(\s+)CGST", lambda m: m.group(1) + "IGST", t)
    return re.sub(r"(?m)((?:^\s+\d+(?:\.\d{1,2})?|Grand Total)\s+[\d,]+\.\d{2}\s+)([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$",
                  lambda m: m.group(1) + f"{Decimal(m.group(2).replace(',', '')) + Decimal(m.group(3).replace(',', '')):.2f}", t)


def vodafone_igst(t):
    m = re.search(r"(?m)^(\s*)State GST @ ([\d.]+)%(\s+)([\d,]+\.\d{2})\s*\n\s*Central GST @ ([\d.]+)%\s+([\d,]+\.\d{2})\s*$", t)
    if not m:
        return t
    rate = Decimal(m.group(2)) + Decimal(m.group(5))
    total = Decimal(m.group(4).replace(",", "")) + Decimal(m.group(6).replace(",", ""))
    head = f"{m.group(1)}Integrated GST @ {rate:.2f}%"
    width = len(m.group(1)) + len(f"State GST @ {m.group(2)}%") + len(m.group(3)) + len(m.group(4))
    return t[:m.start()] + head + f"{total:.2f}".rjust(width - len(head)) + t[m.end():]


CONVERT.update({
 'meesho_tax_invoice.yml': meesho_next_line,
 'meesho_credit_note.yml': meesho_next_line,
 'zomato_platform_fee_invoice.yml': lambda t: merge_cols(t, "CGST", "SGST", "IGST"),
 'blinkit_goods_invoice.yml': lambda t: merge_rate_pairs(t, r"CGST \(%\)\s+CGST \(INR\)\s+SGST \(%\)\s+SGST \(INR\)", "IGST (%)   IGST (INR)"),
 'blinkit_fee_invoice.yml': lambda t: merge_rate_pairs(t, r"CGST \(%\)\s+CGST \(INR\)\s+SGST \(%\)\s+SGST \(INR\)", "IGST (%) IGST (INR)"),
 'flipkart_grocery_invoice.yml': grocery_igst,
 'vodafone_idea_postpaid_bill.yml': vodafone_igst,
})


def zomato_restaurant_igst(t):
    """CGST (Rate) CGST (INR) SGST (Rate) SGST (INR) -> IGST (Rate) IGST (INR); the totals rows
    (Item(s) Total, Total Value) get one tax total."""
    out = []
    for line in t.split("\n"):
        line = re.sub(r"CGST \(Rate\)(\s+)CGST \(INR\)\s+SGST \(Rate\)\s+SGST \(INR\)", r"IGST (Rate)\1IGST (INR)", line)
        m = re.search(r"(?<=\s)(\d+(?:\.\d+)?)%\s+(\d[\d,]*(?:\.\d+)?)\s+\1%\s+(\d[\d,]*(?:\.\d+)?)(?=\s)", line)
        if m:
            rate = Decimal(m.group(1)) * 2
            total = Decimal(m.group(2).replace(",", "")) + Decimal(m.group(3).replace(",", ""))
            line = line[:m.start()] + f"{_fmt_like(m.group(1), rate)}%   {_fmt_like(m.group(2), total)}" + line[m.end():]
        elif re.match(r"^\s*(?:Item\(s\) Total|Total Value)\s", line):
            m = re.search(r"(\d[\d,]*(?:\.\d+)?)(\s+)(\d[\d,]*(?:\.\d+)?)(\s+\d[\d,]*(?:\.\d+)?\s*)$", line)
            if m:
                total = Decimal(m.group(1).replace(",", "")) + Decimal(m.group(3).replace(",", ""))
                line = line[:m.start()] + _fmt_like(m.group(1), total) + m.group(4)
        out.append(line)
    return "\n".join(out)


CONVERT['zomato_restaurant_invoice.yml'] = zomato_restaurant_igst


def myntra_cgst(t):
    """Myntra prints empty CGST and SGST/UGST columns on an IGST invoice: the same document from a
    seller in the buyer's state has the tax under those two headings and "9.0% CGST, 9.0% SGST"."""
    lines = t.split("\n")
    head = next(i for i, l in enumerate(lines) if re.search(r"\bCGST\b", l) and re.search(r"\bIGST\b", l))
    c_col, i_col = re.search(r"\bCGST\b", lines[head]).start(), re.search(r"\bIGST\b", lines[head]).start()
    s_col = re.search(r"\bSGST/", "\n".join(lines[:head])).start() - len("\n".join(lines[:head - 1])) - 1
    out = []
    for line in lines:
        m = re.search(r"Rs (\d[\d,]*\.\d{2})", line[i_col - 6:i_col + 12]) if len(line) > i_col else None
        if m and line.lstrip()[:1].isdigit() or (m and line.startswith("TOTAL")):
            start = i_col - 6 + m.start()
            a, b = half(m.group(1))
            line = line[:start] + " " * len(m.group()) + line[start + len(m.group()):]
            for col, text in ((c_col, f"Rs {a}"), (s_col, f"Rs {b}")):
                line = line[:col] + text + line[col + len(text):]
        line = re.sub(r"(\d+(?:\.\d+)?)% IGST", lambda r: f"{Decimal(r.group(1)) / 2}% CGST, {Decimal(r.group(1)) / 2}% SGST", line)
        out.append(line)
    return "\n".join(out)


CONVERT['myntra_tax_invoice.yml'] = myntra_cgst

CONVERT['myntra_seller_tax_invoice.yml'] = myntra_cgst
