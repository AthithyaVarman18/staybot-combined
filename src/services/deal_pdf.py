"""
One-page investment summary PDF for a saved deal.

It prints only stored numbers and the assumptions used, states where each key
number came from, and carries a plain disclaimer: this is a projection from
those inputs, not advice and not a promise of returns.
"""

from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#222222")
MUTED = colors.HexColor("#6a6a6a")
LINE = colors.HexColor("#dddddd")
GOOD = colors.HexColor("#0b7a2f")
BAD = colors.HexColor("#c13515")

_styles = getSampleStyleSheet()
TITLE = ParagraphStyle("t", parent=_styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK, spaceAfter=2)
SUB = ParagraphStyle("s", parent=_styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=MUTED, alignment=TA_CENTER)
H2 = ParagraphStyle("h", parent=_styles["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=INK, spaceBefore=12, spaceAfter=4)
BODY = ParagraphStyle("b", parent=_styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
SMALL = ParagraphStyle("sm", parent=BODY, fontSize=8, leading=11, textColor=MUTED)
BIG = ParagraphStyle("big", parent=BODY, fontName="Helvetica-Bold", fontSize=15, leading=18)


def dollars(value):
    return f"${float(value or 0):,.0f}"


def percent(value):
    return "—" if value is None else f"{float(value):.2f}%"


def table(rows, widths=(2.6 * inch, 1.5 * inch), bold_last=False):
    data = [[Paragraph(escape(str(k)), BODY), Paragraph(escape(str(v)), BODY)] for k, v in rows]
    t = Table(data, colWidths=widths)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
             ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if bold_last:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    return t


def property_photo(url, width=6.6 * inch):
    """The MLS main photo, when it downloads quickly. Never blocks the PDF."""
    if not url or not str(url).startswith("http"):
        return None
    try:
        import requests
        response = requests.get(url, timeout=6)
        if not response.ok or len(response.content) > 8_000_000:
            return None
        image = Image(BytesIO(response.content))
        ratio = image.imageHeight / image.imageWidth
        image.drawWidth, image.drawHeight = width, min(width * ratio, 3.1 * inch)
        image.hAlign = "CENTER"
        return image
    except Exception:
        return None


def render(deal: dict) -> bytes:
    stored = deal.get("results") or {}
    results = stored.get("results") or {}
    listing = stored.get("listing") or {}
    cash = stored.get("cash_needed") or {}
    loan = stored.get("loan") or {}
    costs = stored.get("costs_monthly") or {}
    inputs = deal.get("inputs") or {}
    sources = stored.get("sources") or {}
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f"Deal analysis {deal.get('label', '')}")
    address = listing.get("street_address") or deal.get("label") or "Property"
    where = ", ".join(filter(None, [listing.get("city"), listing.get("postal_code")]))
    facts = " · ".join(filter(None, [
        f"{listing['bedrooms']} bed" if listing.get("bedrooms") else None,
        f"{listing['bathrooms_full']} bath" if listing.get("bathrooms_full") else None,
        f"{int(listing['living_area']):,} sq ft" if listing.get("living_area") else None,
        f"built {listing['year_built']}" if listing.get("year_built") else None,
        f"MLS {listing['list_number']}" if listing.get("list_number") else None]))

    flow = float(results.get("cash_flow_monthly") or 0)
    photo = property_photo(listing.get("photo_url"))
    story = ([photo, Spacer(1, 8)] if photo else []) + [
        Paragraph("Rental investment analysis", TITLE),
        Paragraph(escape(f"{address}{' · ' + where if where else ''}"), SUB),
        Paragraph(escape(facts), SUB), Spacer(1, 10),
        Paragraph(f"{'+' if flow >= 0 else '-'}${abs(flow):,.0f} per month cash flow",
                  ParagraphStyle("flow", parent=BIG, textColor=GOOD if flow >= 0 else BAD)),
        Paragraph(f"Cap rate {percent(results.get('cap_rate_percent'))} · "
                  f"Cash-on-cash {percent(results.get('cash_on_cash_percent'))} · "
                  f"DSCR {results.get('dscr') if results.get('dscr') is not None else '—'} · "
                  f"Cash to buy {dollars(cash.get('total'))}", BODY),
        Paragraph("Purchase", H2),
        table([("Price", dollars(inputs.get("price"))),
               (f"Down payment ({inputs.get('down_payment_percent')}%)", dollars(cash.get("down_payment"))),
               (f"Closing costs ({inputs.get('closing_costs_percent')}%)", dollars(cash.get("closing_costs"))),
               ("Repairs before renting", dollars(cash.get("repairs"))),
               ("Total cash needed", dollars(cash.get("total")))], bold_last=True),
        Paragraph("Loan", H2),
        table([("Loan amount", dollars(loan.get("amount"))),
               (f"Rate / term", f"{inputs.get('interest_rate_percent')}% over {inputs.get('loan_years')} years"),
               ("Monthly payment", dollars(loan.get("monthly_payment")))]),
        Paragraph("Monthly money", H2),
        table([("Rent", dollars(inputs.get("monthly_rent"))),
               ("Property taxes", "-" + dollars(costs.get("taxes"))),
               ("Insurance", "-" + dollars(costs.get("insurance"))),
               ("HOA", "-" + dollars(costs.get("hoa"))),
               (f"Management ({inputs.get('management_percent')}%)", "-" + dollars(costs.get("management"))),
               (f"Maintenance ({inputs.get('maintenance_percent')}%)", "-" + dollars(costs.get("maintenance"))),
               (f"Empty periods ({inputs.get('vacancy_percent')}%)", "-" + dollars(costs.get("vacancy"))),
               ("Loan payment", "-" + dollars(loan.get("monthly_payment"))),
               ("Cash flow", ("+" if flow >= 0 else "-") + dollars(abs(flow)))], bold_last=True),
    ]
    flags = stored.get("flags") or []
    if flags:
        story.append(Paragraph("Watch out", H2))
        for flag in flags:
            story.append(Paragraph("• " + escape(flag.get("text", "")), BODY))
    story += [
        Paragraph("Where the numbers came from", H2),
        Paragraph(escape("; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sources.items())) or "Entered by staff.", SMALL),
        Paragraph(f"Break-even rent {dollars(results.get('breakeven_rent'))} — below this the property loses money each month.", SMALL),
        Spacer(1, 10),
        Paragraph("This is a projection calculated from the inputs above, not financial advice and not a promise of returns. "
                  "Taxes, insurance, HOA fees, rents and repair costs must be confirmed before making an offer. "
                  f"Prepared {datetime.now(timezone.utc).strftime('%B %d, %Y')} by Staybot for discussion with a licensed broker.", SMALL),
    ]
    doc.build(story)
    return buffer.getvalue()
