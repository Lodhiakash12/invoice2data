"""Builds the PDFs the region tests read (python tests/fixtures/make_region_fixtures.py).

staggered_block.pdf     Invoice To: | right column printed half a line lower, two characters
                        from the buyer's longest line (Shiprocket-style)
label_value_block.pdf   Name: / Address: value with lines hanging under the value, Place of
                        Supply: one character-width gap away, Pincode: label, an item table
                        with an indented description line below (Blinkit-style)
"""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent


def staggered(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 9)
    y = 760
    c.drawString(40, y, "Invoice To:")
    left = ["MEHTA TRADING COMPANY PRIVATE LIMITED", "Plot 9, Sector 21, Opposite Railway Station Road,",
            "Vashi, Navi Mumbai", "Maharashtra - 400703"]
    right = ["State Code: 27", "Place of Supply: Maharashtra", "GSTIN: 27AAAAA0000A1Z5", "Reverse Charge: No"]
    for i, text in enumerate(left):
        c.drawString(40, y - 14 - i * 14, text)
    for i, text in enumerate(right):
        c.drawString(262, y - 7 - i * 14, text)          # half a line lower, close to the left column
    c.drawString(40, y - 100, "HSN        Description                                   Total")
    c.drawString(40, y - 114, "998313     Freight charges                               100.00")
    c.drawString(98, y - 126, "including fuel surcharge")
    c.save()


def label_value(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 9)
    y = 760
    c.drawString(16, y, "Name")
    c.drawString(66, y, ":")
    c.drawString(78, y, "Riya Sharma")
    c.drawString(300, y, "Invoice Date: 13-09-2025")
    c.drawString(16, y - 13, "Address")
    c.drawString(66, y - 13, ":")
    c.drawString(78, y - 13, "Flat 12, Shanti Apartments, Near Old Water Tank Road")
    c.drawString(300, y - 13, "Place of Supply: Gujarat")
    c.drawString(78, y - 24, "Navrangpura, Ahmedabad, Gujarat, 380009")
    c.drawString(16, y - 38, "Pincode")
    c.drawString(66, y - 38, ":")
    c.drawString(78, y - 38, "380009")
    c.drawString(14, y - 52, "Sr. no   HSN Code      Item Description              Total")
    c.drawString(14, y - 66, "1        998549        Late night")
    c.drawString(107, y - 75, "convenience charge")                # a description line in the table
    c.save()


if __name__ == "__main__":
    staggered(HERE / "staggered_block.pdf")
    label_value(HERE / "label_value_block.pdf")
