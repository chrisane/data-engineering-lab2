"""
Branch cash-up report PDF in the layout of the Excel template
(docs/templates/branch_cashup_template.xlsx), as branches export it.

Positions, fills and number formats follow Excel's PDF export of the
template, so the synthetic documents exercise the same extraction as a
real one: label-value rows, two tables with wrapped headers, accounting
numbers (a dash for zero, the minus sign at the left of the cell), and
the date in day-first regional format.

    write_cashup_pdf(path, report)

`report` maps the template's labels to values: numbers for the money
fields (a string is drawn as typed text, left aligned, like a value a
branch typed over the number format), a date for "Date", text for the
rest. Extra key-value rows (for example a "Template Version") can be
added under the header block with `extra_rows`.
"""

from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# Column edges of the template (points), A..F, and the right edge of F.
COLUMNS = [50.4, 145.5, 243.9, 320.4, 399.9, 472.1, 534.5]
ROW = 15.25
PAD = 2.8

TITLE_BLUE = (0.29, 0.56, 0.85)
LABEL_GREY = (0.85, 0.85, 0.85)
HEADER_BLUE = (0.75, 0.89, 0.96)
VALUE_GREY = (0.95, 0.95, 0.95)
BORDER = (0.6, 0.6, 0.6)

HEADER_ROWS = ["Site Code", "Shift Id", "Date", "POS Terminal", "Cashier Name"]
TAKINGS = ["Opening Float", "Cash Sales", "Card Sales", "Account Sales", "Total Sales"]
COUNT = ["Internal Expenses", "Cash Removed", "Expected Closing Cash", "Actual Closing Cash", "Variance"]
WRAPPED = {"Expected Closing Cash": ("Expected", "Closing Cash"), "Actual Closing Cash": ("Actual Closing", "Cash")}
SIGNATURES = ["Cashier Signature", "Manager Signature"]


def cell(pdf, column: int, baseline: float, fill, height: float = ROW, span: int = 1) -> tuple[float, float]:
    """Draw a bordered cell whose text baseline is `baseline`; return its left and right edge."""

    left, right = COLUMNS[column], COLUMNS[column + span]
    pdf.setFillColorRGB(*fill)
    pdf.setStrokeColorRGB(*BORDER)
    pdf.rect(left, baseline - 4, right - left, height, stroke=1, fill=1)
    pdf.setFillColorRGB(0, 0, 0)

    return left, right


def accounting(pdf, left: float, right: float, baseline: float, value) -> None:
    """A number in Excel's accounting format; typed text is drawn as it was typed."""

    pdf.setFont("Helvetica", 10)

    if isinstance(value, str):
        pdf.drawString(left + PAD, baseline, value)
    elif round(value, 2) == 0:
        pdf.drawRightString(right - 16, baseline, "-")
    elif value < 0:
        pdf.drawString(left + PAD, baseline, "-")
        pdf.drawRightString(right - PAD, baseline, f"{-value:,.2f}")
    else:
        pdf.drawRightString(right - PAD, baseline, f"{value:,.2f}")


def table(pdf, labels: list[str], header_baseline: float, report: dict) -> None:

    tall = any(label in WRAPPED for label in labels)
    height = ROW * 2 if tall else ROW

    for offset, label in enumerate(labels):
        left, _ = cell(pdf, offset + 1, header_baseline, HEADER_BLUE, height)
        pdf.setFont("Helvetica-BoldOblique", 9)
        if label in WRAPPED:
            top, bottom = WRAPPED[label]
            pdf.drawString(left + PAD, header_baseline + ROW, top)
            pdf.drawString(left + PAD, header_baseline, bottom)
        else:
            pdf.drawString(left + PAD, header_baseline, label)

    for offset, label in enumerate(labels):
        left, right = cell(pdf, offset + 1, header_baseline - ROW, VALUE_GREY)
        accounting(pdf, left, right, header_baseline - ROW, report[label])
        cell(pdf, offset + 1, header_baseline - 2 * ROW, VALUE_GREY)


def write_cashup_pdf(path: Path, report: dict, extra_rows: list[tuple[str, str]] = ()) -> Path:

    # invariant=1 fixes the embedded timestamps, so regenerating an
    # unchanged document produces identical bytes (and the same hash).
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    pdf.setTitle("Branch Cash Up Report")

    left, right = cell(pdf, 0, 716.2, TITLE_BLUE, 20.5, span=6)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString((left + right) / 2, 718.2, "Branch Cash Up Report")
    pdf.setFillColorRGB(0, 0, 0)

    baseline = 689.5
    for label, value in [(label, report[label]) for label in HEADER_ROWS] + list(extra_rows):
        cell(pdf, 0, baseline, LABEL_GREY)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(COLUMNS[0] + PAD, baseline, label)
        value_left, value_right = cell(pdf, 1, baseline, (1, 1, 1))
        pdf.setFont("Helvetica", 10)
        if isinstance(value, date):
            pdf.drawRightString(value_right - PAD, baseline, value.strftime("%d/%m/%Y"))
        else:
            pdf.drawString(value_left + PAD, baseline, str(value))
        baseline -= ROW

    table(pdf, TAKINGS, baseline - 15.2, report)
    table(pdf, COUNT, baseline - 91.5, report)

    baseline -= 152.4
    for label in SIGNATURES:
        cell(pdf, 0, baseline, LABEL_GREY)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(COLUMNS[0] + PAD, baseline, label)
        value_left, value_right = cell(pdf, 1, baseline, (1, 1, 1))
        pdf.setFont("Helvetica-Oblique", 10)
        pdf.drawCentredString((value_left + value_right) / 2, baseline, str(report[label]))
        baseline -= ROW

    pdf.setFont("Helvetica-Oblique", 8)
    pdf.drawString(COLUMNS[0], 40, "Synthetic training document - not real business data")
    pdf.save()

    return path
