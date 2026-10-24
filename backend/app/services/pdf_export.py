"""PDF report generation with ReportLab."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import Meeting
from app.services.diarization import speaker_labelled_text

_DEJAVU = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
)
ACCENT = colors.HexColor("#1f4fd8")


def _fonts() -> tuple[str, str]:
    """DejaVu (wider Unicode coverage) when installed, otherwise built-in Helvetica."""
    regular, bold = _DEJAVU
    if regular.exists() and bold.exists():
        if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))
        return "DejaVuSans", "DejaVuSans-Bold"
    return "Helvetica", "Helvetica-Bold"


def _text(value: str | None) -> str:
    return escape(value or "").replace("\n", "<br/>")


def build_meeting_pdf(meeting: Meeting, include_transcript: bool = False) -> bytes:
    regular, bold = _fonts()
    base = getSampleStyleSheet()
    title = ParagraphStyle(
        "T", parent=base["Title"], fontName=bold, fontSize=20, leading=25, alignment=0, spaceAfter=4
    )
    meta = ParagraphStyle(
        "M", parent=base["Normal"], fontName=regular, fontSize=9, leading=13, textColor=colors.HexColor("#555555")
    )
    h2 = ParagraphStyle(
        "H2", parent=base["Heading2"], fontName=bold, fontSize=13, spaceBefore=14, spaceAfter=6, textColor=ACCENT
    )
    body = ParagraphStyle("B", parent=base["BodyText"], fontName=regular, fontSize=10, leading=14)
    bullet = ParagraphStyle("BL", parent=body, leftIndent=14, bulletIndent=2, spaceAfter=2)
    cell = ParagraphStyle("C", parent=body, fontSize=9, leading=12)
    head = ParagraphStyle("CH", parent=cell, fontName=bold, textColor=colors.white)

    story: list = [Paragraph(_text(meeting.title), title)]
    meta_bits = [
        f"Date: {meeting.created_at.strftime('%d %b %Y, %H:%M UTC')}",
        f"Recording: {meeting.original_filename}",
    ]
    if meeting.language:
        meta_bits.append(f"Language: {meeting.language}")
    story.append(Paragraph(_text("   |   ".join(meta_bits)), meta))
    people = [p.name or p.email for p in meeting.participants if p.name or p.email]
    if people:
        story.append(Paragraph(_text("Participants: " + ", ".join(people)), meta))

    summary = meeting.summary
    if summary is None:
        story.append(Paragraph("Summary", h2))
        story.append(Paragraph("A summary has not been generated for this meeting yet.", body))
    else:
        story.append(Paragraph("Overview", h2))
        story.append(Paragraph(_text(summary.overview), body))
        if summary.sentiment:
            story.append(Paragraph(_text(f"Overall sentiment: {summary.sentiment}"), meta))
        for heading, items in (
            ("Key discussion points", summary.key_points),
            ("Decisions", summary.decisions),
            ("Risks", summary.risks),
        ):
            story.append(Paragraph(heading, h2))
            if items:
                story.extend(Paragraph(_text(i), bullet, bulletText="\u2022") for i in items)
            else:
                story.append(Paragraph("None recorded.", body))

    story.append(Paragraph("Action items", h2))
    if meeting.action_items:
        rows = [[Paragraph(h, head) for h in ("Task", "Owner", "Due", "Status")]]
        for a in meeting.action_items:
            rows.append(
                [
                    Paragraph(_text(a.task), cell),
                    Paragraph(_text(a.owner or "-"), cell),
                    Paragraph(_text(a.due_date or "-"), cell),
                    Paragraph("Done" if a.completed else "Open", cell),
                ]
            )
        table = Table(rows, colWidths=[88 * mm, 32 * mm, 30 * mm, 20 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d1da")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f9")]),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)
    else:
        story.append(Paragraph("None recorded.", body))

    if include_transcript and meeting.transcript:
        story.append(Spacer(1, 8))
        story.append(Paragraph("Transcript", h2))
        segments = meeting.transcript.segments or []
        if any(seg.get("speaker") for seg in segments):  # speaker identification was enabled
            for line in speaker_labelled_text(segments).splitlines():
                story.append(Paragraph(_text(line), body))
        else:
            story.append(Paragraph(_text(meeting.transcript.text), body))

    buffer = BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=meeting.title, author="AI Meeting Assistant",
    ).build(story)
    return buffer.getvalue()
