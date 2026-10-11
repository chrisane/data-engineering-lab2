"""
Structured field extraction from PDF documents (branch cash-ups).

A source's registry entry describes the fields to extract:

    extraction:
      fields:
        shift_id:      {label: Shift ID}
        business_date: {label: Business Date, type: date}
        cash_sales:    {label: Cash Sales, type: money}
      filename_fields: '^(?P<file_site_code>BR\\d{3})_(?P<file_shift_id>SHIFT-\\d{6})_...'

Each document becomes one record (row number 1). Values are parsed by
type; a value that cannot be parsed is returned unchanged as text, so
the normal validation rules (numeric, dates) report it with the actual
value instead of the extraction failing silently. Fields captured from
the file name (filename_fields) are added as extra columns so content
can be checked against the name.

The same function is used by validation and by the staging loader, so
what is validated is exactly what is loaded.
"""

import re
from datetime import date
from pathlib import Path

from pypdf import PdfReader

MONEY_PATTERN = re.compile(r"^-?\d+(\.\d{1,2})?$")


def parse_money(raw: str):
    """
    'N$ 31 475.00', 'N$ 6903.40', 'N$ 22,910.00', 'N$ -150.00' -> float.
    Anything else is returned unchanged (and fails numeric validation).
    """

    text = raw.strip()
    cleaned = re.sub(r"^N\$\s*", "", text)
    cleaned = cleaned.replace(",", "").replace(" ", "").replace(" ", "")

    if MONEY_PATTERN.match(cleaned):
        return float(cleaned)

    return text


def parse_date(raw: str):

    text = raw.strip()

    try:
        return date.fromisoformat(text)
    except ValueError:
        return text


PARSERS = {
    "money": parse_money,
    "date": parse_date,
    "text": lambda raw: raw.strip() or None,
}


def document_text(path: Path) -> str:

    reader = PdfReader(path)

    return "\n".join(page.extract_text() or "" for page in reader.pages)


def label_values(text: str) -> dict[str, str]:
    """
    Map 'Label:' lines to the value on the same line or the next line
    (both layouts occur in exported PDFs).
    """

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    values = {}

    for index, line in enumerate(lines):
        if ":" not in line:
            continue

        label, _, rest = line.partition(":")

        if rest.strip():
            values.setdefault(label.strip(), rest.strip())
        elif index + 1 < len(lines) and not lines[index + 1].endswith(":"):
            values.setdefault(label.strip(), lines[index + 1])

    return values


def extract_record(path: Path, extraction: dict) -> tuple[list[str], tuple]:
    """
    Return (headers, values) for one document. Missing labels give None
    (reported by required-value rules).
    """

    found = label_values(document_text(path))
    fields = extraction["fields"]

    headers = list(fields)
    values = []

    for spec in fields.values():
        raw = found.get(spec["label"])
        parser = PARSERS[spec.get("type", "text")]

        values.append(None if raw is None else parser(raw))

    pattern = extraction.get("filename_fields")

    if pattern:
        match = re.match(pattern, path.name)
        groups = match.groupdict() if match else {}

        for group in re.compile(pattern).groupindex:
            headers.append(group)
            values.append(groups.get(group))

    return headers, tuple(values)
