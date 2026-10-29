
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["TOKENHARBOR_API_KEY"],
    base_url="https://tokenharbor.ai/v1",
)

prompt = """
Understand the entire context and create a YAML template file for this document.

Document Type: Order Summary and Receipt

ORDER ID
10000076062608

ORDER DATE
4th Sep, 2026

CUSTOMER NAME
User TICKETING PLATFORM ENTITY

ORBGEN TECHNOLOGIES PRIVATE LIMITED

MOVIE THEATRE
Rajhans Cinemas, Nikol, Ahmedabad
2nd Floor, Pavillion Mall, New Nikol, Ahmedabad,
Gujarat 382350, India
SCREEN 1 · ROYAL - I3

MOVIE & SHOWTIME
Mirzapur: The Movie (A · Hindi · 2D)
FRI, 2026-09-04 · 10:00 AM - 01:17 PM

PARTICULARS | QUANTITY | TOTAL (INR)
Movie Ticket ROYAL - I3 | 1 | ₹99.00
Booking Fee | 1 | ₹22.42
Total | | ₹121.42
Includes Tax | | ₹3.42
PAID VIA | PhonePe UPI | ₹121.42
Payment Date: 4th Sep 2026, 01:03 AM

TERMS & CONDITIONS
1. A tax invoice for the movie ticket and 3D glass will be issued by the respective movie theater.
2. Orbgen Technologies Private Limited acts as an Electronic Commerce Operator under Section 9(5) of the CGST Act, 2017.
3. Applicable booking fees will be invoiced according to applicable laws.
4. Customer support: feedback@district.in.
5. Never share sensitive banking details or UPI PINs.
6. This is a computer-generated document.

Generate a valid invoice2data YAML template that matches this document.
Include:
- A suitable template name.
- Reliable keywords and exclusion keywords, if appropriate.
- Correct regular expressions for extracting the order ID, order date,
  customer name, theater, movie, showtime, ticket amount, booking fee,
  total amount, included tax, payment method, and payment date.
- Appropriate invoice2data field definitions and types.
- A structure compatible with invoice2data's YAML template format.

Ensure all regular expressions match the supplied text.
Avoid overly restrictive matching rules.
Return only the complete YAML template, without Markdown fences or explanations.
"""

res = client.chat.completions.create(
    model="deepseek-v4.1-flash:free",
    messages=[
        {
            "role": "user",
            "content": prompt,
        }
    ],
)

print(res.choices[0].message.content)
