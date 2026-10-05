"""
Lease agreement PDF, rendered only from a stored agreement-version snapshot.

Nothing here invents terms: every value comes from the snapshot, and every
clause comes from the configured template recorded in that snapshot.
Drafts carry a DRAFT watermark; demo templates carry a DEMO notice on every
page. Signature lines are left blank: approvals are listed as approvals,
never as signatures.
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
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#222222")
MUTED = colors.HexColor("#6a6a6a")
LINE = colors.HexColor("#dddddd")
ACCENT = colors.HexColor("#e61e4d")

SCHEDULES = {"monthly": "Monthly", "twice_monthly": "Twice a month", "weekly": "Weekly"}
FREQUENCIES = {"monthly": "per month", "one_time": "one time", "yearly": "per year"}

_styles = getSampleStyleSheet()
TITLE = ParagraphStyle("title", parent=_styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=INK, spaceAfter=4)
SUB = ParagraphStyle("sub", parent=_styles["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=MUTED, alignment=TA_CENTER)
H2 = ParagraphStyle("h2", parent=_styles["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=INK, spaceBefore=12, spaceAfter=5)
BODY = ParagraphStyle("body", parent=_styles["Normal"], fontName="Helvetica", fontSize=10, leading=14, textColor=INK)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.5, leading=11.5, textColor=MUTED)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=9.5, leading=12.5)
CELL_LABEL = ParagraphStyle("cell_label", parent=CELL, fontName="Helvetica-Bold", textColor=MUTED)
NOTICE = ParagraphStyle("notice", parent=BODY, fontName="Helvetica-Bold", fontSize=9.5, leading=13, textColor=colors.HexColor("#8a1c1c"))


def text(value):
    return escape(str(value)) if value not in (None, "") else "Not provided"


def money(amount, currency):
    if amount in (None, ""):
        return "Not provided"
    return f"{currency} {Decimal(str(amount)):,.2f}"


def nice_date(value):
    if not value:
        return "Not provided"
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%B %d, %Y").replace(" 0", " ")
    except ValueError:
        return text(value)


def nice_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%B %d, %Y at %H:%M UTC").replace(" 0", " ")
    except ValueError:
        return text(value)


def grid(rows, widths=(1.9 * inch, 4.6 * inch)):
    table = Table([[Paragraph(label, CELL_LABEL), Paragraph(value, CELL)] for label, value in rows], colWidths=widths)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def clause_text(body):
    return "<br/>".join(escape(line) for line in str(body).splitlines())


def render(snapshot: dict, *, version: int, content_hash: str, final: bool, approvals: list | None = None,
           generated_at: str | None = None) -> bytes:
    """approvals: [{"party", "name", "decision", "at", "channel"}] for the rendered version."""

    terms = snapshot["terms"]
    currency = terms.get("currency") or "USD"
    template = snapshot["template"]
    is_demo = template.get("status") != "approved"
    reference = snapshot["reference"]
    buffer = BytesIO()

    def decorate(canvas, doc):
        canvas.saveState()
        width, height = LETTER
        if not final:
            canvas.setFont("Helvetica-Bold", 96)
            canvas.setFillColor(colors.Color(0.85, 0.1, 0.2, alpha=0.12))
            canvas.translate(width / 2, height / 2)
            canvas.rotate(40)
            canvas.drawCentredString(0, -30, "DRAFT")
            canvas.rotate(-40)
            canvas.translate(-width / 2, -height / 2)
        if is_demo:
            canvas.setFillColor(colors.HexColor("#8a1c1c"))
            canvas.setFont("Helvetica-Bold", 8.5)
            canvas.drawCentredString(width / 2, height - 0.45 * inch,
                                     "DEMO TEMPLATE - FOR TESTING ONLY - NOT A VALID OR LEGALLY REVIEWED LEASE")
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 8)
        label = "FINAL" if final else "DRAFT - NOT FOR SIGNATURE"
        canvas.drawString(0.75 * inch, 0.5 * inch, f"{reference} · Agreement version {version} · {label}")
        canvas.drawRightString(width - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.drawString(0.75 * inch, 0.34 * inch, f"Content fingerprint (SHA-256): {content_hash}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=LETTER, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.8 * inch,
                            title=f"Residential Lease Agreement {reference} v{version}",
                            author="Staybot onboarding", subject="Residential lease agreement")
    story = [
        Paragraph("Residential Lease Agreement", TITLE),
        Paragraph(f"Agreement reference {escape(reference)} · Version {version} · "
                  f"{'Final' if final else 'Draft for review'} · Generated {escape(nice_time(generated_at)) if generated_at else ''}", SUB),
        Spacer(1, 8),
    ]
    if is_demo:
        story += [Paragraph("This document uses a DEMO template for testing. It is not a production-ready or legally "
                            "reviewed lease and must not be used with real tenants or owners.", NOTICE), Spacer(1, 4)]
    if not final:
        story += [Paragraph("DRAFT: shared so both parties can review the terms. It is not final and has not been signed.", NOTICE)]

    prop, tenant, owner = snapshot["property"], snapshot["tenant"], snapshot["owner"]
    story += [
        Paragraph("1. Parties", H2),
        grid([
            ("Tenant", text(tenant.get("full_name"))),
            ("Tenant contact", f"WhatsApp {text(tenant.get('whatsapp'))}" + (f" · {text(tenant['email'])}" if tenant.get("email") else "")),
            ("Owner", text(owner.get("full_name"))),
            ("Owner contact", f"WhatsApp {text(owner.get('whatsapp'))}" + (f" · {text(owner['email'])}" if owner.get("email") else "")),
        ]),
        Paragraph("2. Premises", H2),
        grid([
            ("Property", text(prop.get("title"))),
            ("Property ID", text(prop.get("id")) + (f" · ref {text(prop['ref'])}" if prop.get("ref") else "")),
            ("Unit", text(prop.get("unit"))),
            ("Address", text(prop.get("address"))),
        ]),
        Paragraph("3. Lease term", H2),
        grid([
            ("Lease start", nice_date(terms.get("lease_start"))),
            ("Lease end", nice_date(terms.get("lease_end"))),
            ("Move-in date", nice_date(terms.get("move_in_date"))),
        ]),
        Paragraph("4. Rent, deposit and payments", H2),
        grid([
            ("Monthly rent", money(terms.get("rent"), currency)),
            ("Security deposit", money(terms.get("deposit"), currency)),
            ("Currency", text(currency)),
            ("Payment schedule", text(SCHEDULES.get(terms.get("payment_schedule"), terms.get("payment_schedule")))),
            ("Rent due", f"Day {text(terms.get('rent_due_day'))} of each period" if terms.get("rent_due_day") else "Not provided"),
        ]),
    ]

    charges = terms.get("charges") or []
    story.append(Paragraph("5. Agreed charges", H2))
    if charges:
        rows = [[Paragraph("Charge", CELL_LABEL), Paragraph("Amount", CELL_LABEL), Paragraph("Frequency", CELL_LABEL)]]
        rows += [[Paragraph(text(c.get("label")), CELL), Paragraph(money(c.get("amount"), currency), CELL),
                  Paragraph(text(FREQUENCIES.get(c.get("frequency"), c.get("frequency"))), CELL)] for c in charges]
        table = Table(rows, colWidths=(3.3 * inch, 1.6 * inch, 1.6 * inch), repeatRows=1)
        table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story.append(table)
    else:
        story.append(Paragraph("No additional charges were agreed.", BODY))

    occupants = terms.get("occupants") or []
    story += [
        Paragraph("6. Occupants and home details", H2),
        grid([
            ("Occupants", "<br/>".join(escape(o) for o in occupants) if occupants else "Not provided"),
            ("Pets", text(terms.get("pets"))),
            ("Utilities", text(terms.get("utilities"))),
        ]),
        Paragraph("7. Additional agreed terms", H2),
    ]
    extra = terms.get("additional_terms") or []
    story += [Paragraph(f"{i}. {escape(t)}", BODY) for i, t in enumerate(extra, 1)] or [Paragraph("None.", BODY)]

    story.append(Paragraph("8. Template clauses", H2))
    story.append(Paragraph(f"From the template “{escape(template.get('name', ''))}”, version {template.get('template_version')}"
                           f"{' (DEMO, not approved for real use)' if is_demo else ', approved for use'}.", SMALL))
    story.append(Spacer(1, 6))
    for i, clause in enumerate(template.get("clauses") or [], 1):
        story.append(KeepTogether([Paragraph(f"8.{i} {escape(clause.get('title', ''))}", CELL_LABEL),
                                   Paragraph(clause_text(clause.get("body", "")), BODY), Spacer(1, 6)]))

    story.append(Paragraph("9. Approval record", H2))
    if final and approvals:
        rows = [(f"{a['party'].title()} approval",
                 f"{escape(a['name'])} approved agreement version {version} on {escape(nice_time(a['at']))} "
                 f"via {'secure WhatsApp link' if a.get('channel') == 'secure_link' else escape(a.get('channel', ''))}.")
                for a in approvals]
        story.append(grid(rows))
        story.append(Spacer(1, 4))
        story.append(Paragraph("Approvals confirm each party agreed to this exact version. An approval is not a signature.", SMALL))
    else:
        story.append(Paragraph("Pending. Both the tenant and the owner must approve this exact version before it can be finalized.", BODY))

    signature_rows = []
    for role, person in (("Tenant", tenant), ("Owner", owner)):
        signature_rows.append(KeepTogether([
            Spacer(1, 14),
            Paragraph(f"{role}: {text(person.get('full_name'))}", CELL_LABEL),
            Spacer(1, 22),
            Table([["", ""]], colWidths=(3.6 * inch, 2.2 * inch),
                  style=TableStyle([("LINEABOVE", (0, 0), (0, 0), 0.8, INK), ("LINEABOVE", (1, 0), (1, 0), 0.8, INK)])),
            Table([[Paragraph("Signature", SMALL), Paragraph("Date", SMALL)]], colWidths=(3.6 * inch, 2.2 * inch)),
        ]))
    story += [Paragraph("10. Signatures", H2),
              Paragraph("No electronic signatures were collected by Staybot. Sign by hand below if a signed copy is required.", SMALL)]
    story += signature_rows

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()
