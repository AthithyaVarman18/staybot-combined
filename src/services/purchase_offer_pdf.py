"""Non-contract purchase-offer review PDF.

This module deliberately does NOT render a North Carolina purchase contract.
It renders a plain-language summary for investor review only.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#222222")
MUTED = colors.HexColor("#666666")
LINE = colors.HexColor("#dddddd")
NOTICE_BG = colors.HexColor("#fff5d9")
NOTICE_TEXT = colors.HexColor("#6a4a00")

_styles = getSampleStyleSheet()
TITLE = ParagraphStyle("purchase_title", parent=_styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=INK, spaceAfter=4)
SUB = ParagraphStyle("purchase_sub", parent=_styles["Normal"], fontSize=9.5, leading=13, textColor=MUTED, alignment=TA_CENTER)
H2 = ParagraphStyle("purchase_h2", parent=_styles["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=INK, spaceBefore=12, spaceAfter=5)
BODY = ParagraphStyle("purchase_body", parent=_styles["Normal"], fontSize=10, leading=14, textColor=INK)
SMALL = ParagraphStyle("purchase_small", parent=BODY, fontSize=8.5, leading=11.5, textColor=MUTED)
NOTICE = ParagraphStyle("purchase_notice", parent=BODY, fontName="Helvetica-Bold", fontSize=10, leading=14, textColor=NOTICE_TEXT)
REVIEW_TITLE = "Summary for review — not a contract"
CELL = ParagraphStyle("purchase_cell", parent=BODY, fontSize=9.5, leading=12.5)
LABEL = ParagraphStyle("purchase_label", parent=CELL, fontName="Helvetica-Bold", textColor=MUTED)


def txt(value):
    return escape(str(value)) if value not in (None, "") else "Not provided"


def money(value, currency="USD"):
    if value in (None, ""):
        return "Not provided"
    try:
        return f"{currency} {Decimal(str(value)):,.2f}"
    except Exception:
        return txt(value)


def nice_date(value):
    if not value:
        return "Not provided"
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%B %d, %Y").replace(" 0", " ")
    except ValueError:
        return txt(value)


def nice_dt(value):
    if not value:
        return "Not provided"
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%B %d, %Y at %H:%M UTC").replace(" 0", " ")
    except ValueError:
        return txt(value)


def grid(rows):
    table = Table([[Paragraph(a, LABEL), Paragraph(b, CELL)] for a, b in rows], colWidths=(2.2 * inch, 4.3 * inch))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def render(summary: dict, *, version: int, generated_at: str | None = None) -> bytes:
    """Render a plain review summary only; never a contract."""
    terms = summary.get("terms") or {}
    currency = terms.get("currency") or "USD"
    buffer = BytesIO()

    def decorate(canvas, doc):
        canvas.saveState()
        width, height = LETTER
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"{summary.get('reference', 'Offer')} · Summary version {version}")
        canvas.drawRightString(width - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.75 * inch,
        title=f"Summary for review - {summary.get('reference', 'Purchase offer')} v{version}",
        author="Staybot", subject="Purchase offer review summary",
    )

    property_info = summary.get("property") or {}
    investor = summary.get("investor") or {}
    handoff = summary.get("handoff") or {}
    story = [
        Paragraph(REVIEW_TITLE, TITLE),
        Paragraph(
            f"Offer reference {txt(summary.get('reference'))} · Version {version} · Generated {nice_dt(generated_at)}",
            SUB,
        ),
        Spacer(1, 8),
        Table([[Paragraph(
            "This document is a plain-language summary for review. It is not a purchase contract and does not replace the broker's or attorney's approved contract form. The contract must be prepared and handled through the authorized broker/attorney process.",
            NOTICE,
        )]], colWidths=(6.5 * inch), style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NOTICE_BG),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#e4bf62")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ])),
        Paragraph("Property", H2),
        grid([
            ("Property", txt(property_info.get("title"))),
            ("Property ID / reference", txt(property_info.get("id")) + (f" / {txt(property_info.get('ref'))}" if property_info.get("ref") else "")),
            ("Address", txt(property_info.get("address") or property_info.get("location"))),
        ]),
        Paragraph("Investor", H2),
        grid([
            ("Name", txt(investor.get("name"))),
            ("Email", txt(investor.get("email"))),
            ("WhatsApp", txt(investor.get("phone"))),
        ]),
        Paragraph("Offer terms", H2),
        grid([
            ("Offer price", money(terms.get("price"), currency)),
            ("Earnest money", money(terms.get("earnest_money"), currency)),
            ("Due diligence fee", money(terms.get("due_diligence_fee"), currency)),
            ("Due diligence period", txt(terms.get("due_diligence_period_days")) + (" days" if terms.get("due_diligence_period_days") is not None else "")),
            ("Due diligence end", nice_date(terms.get("due_diligence_end"))),
            ("Closing date", nice_date(terms.get("closing_date"))),
            ("Financing contingency", txt(terms.get("financing_contingency"))),
            ("What is included", txt(terms.get("included"))),
            ("Financing deadline", nice_date(terms.get("financing_deadline"))),
            ("Offer expires", nice_dt(terms.get("expires_at"))),
        ]),
        Paragraph("Broker platform handoff", H2),
        grid([
            ("Platform", txt(handoff.get("platform"))),
            ("Status", txt(handoff.get("status"))),
            ("Link", txt(handoff.get("link"))),
        ]),
        Spacer(1, 8),
        Paragraph(
            "Investor approval confirms review of this summary only. It is not an electronic signature and is not acceptance of a purchase contract.",
            SMALL,
        ),
    ]

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()
