"""
Structured field extraction from PDF documents (branch cash-ups).

A source's registry entry describes the fields to extract by label:

    extraction:
      fields:
        shift_id:      {label: Shift Id}
        business_date: {label: Date, type: date}
        cash_sales:    {label: Cash Sales, type: money}
      date_formats: ['%d/%m/%Y']       # accepted besides ISO dates
      filename_fields: '^(?P<file_site_code>BR\\d{3})_(?P<file_shift_id>SHIFT-\\d{6})_...'

Reading the layout
    Cash-ups are filled in on a spreadsheet template and exported to PDF
    (docs/templates/branch_cashup_template.xlsx), so every cell is a
    separately positioned piece of text. Values are found by position,
    not by text order:

      - a label with no other label on its line is a key-value row: the
        value is the text to its right on the same line;
      - a label sharing its line with other labels is a table header: the
        value is the text below it, in the first row underneath, between
        this header and the next one to its right (its column).

    A header may wrap over several lines of its cell ("Expected / Closing
    Cash"); its pieces are joined. The pieces of one cell are joined left
    to right, so an accounting format's separated sign ("-   150.00")
    reads as -150.00, and a lone "-" (accounting zero) as 0.

Document templates (versions of the layout)
    A source may register several templates that are accepted side by
    side, so branches can move to a new layout at different times:

    extraction:
      fields: ...                      # labels of the default template
      templates:
        version_label: Template Version
        date_field: business_date      # used for accepted_until
        versions:
          "3": {default: true}         # documents without a version label
          "4": {labels: {cash_sales: Cash Takings}}   # relabelled fields
          # optional per version: accepted_until (last business date accepted),
          # not_captured (fields this layout does not have -> NULL)

    The template is detected per document from its version label. An
    unregistered version, or a retired template, is reported (never
    guessed). Every field keeps its canonical name, so staging and the
    warehouse do not change when a label does; the detected version is
    added to the record as template_version.

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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

MONEY_PATTERN = re.compile(r"^-?\d+(\.\d{1,2})?$")


def parse_money(raw: str):
    """
    '7,394.94', '- 150.00' (accounting sign), '-' (accounting zero),
    'N$ 31 475.00' -> float. Anything else is returned unchanged (and
    fails numeric validation).
    """

    text = raw.strip()
    cleaned = re.sub(r"^N\$\s*", "", text)
    cleaned = cleaned.replace(",", "").replace(" ", "").replace(" ", "")

    if cleaned == "-":
        return 0.0

    if MONEY_PATTERN.match(cleaned):
        return float(cleaned)

    return text


def parse_date(raw: str, formats: list[str] | tuple = ()):
    """ISO dates, or any of the source's date_formats (for example '%d/%m/%Y')."""

    text = raw.strip()

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    for pattern in formats:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue

    return text


def parse_value(raw: str, kind: str, extraction: dict):

    if kind == "money":
        return parse_money(raw)

    if kind == "date":
        return parse_date(raw, extraction.get("date_formats") or ())

    return raw.strip() or None


# ---------------------------------------------------------
# LAYOUT READING
# ---------------------------------------------------------

SAME_LINE = 2.5  # points: pieces on one line differ by less than this
WRAP_GAP = 20.0  # points: furthest apart two lines of one wrapped cell are
ROW_BELOW = 45.0  # points: furthest a table value lies below its header
COLUMN_TOLERANCE = 3.0  # points: a column starts this far left of its header


@dataclass(frozen=True)
class Fragment:
    page: int
    x: float
    y: float
    text: str


def document_fragments(path: Path) -> list[Fragment]:
    """Every piece of text in the document with its position (PDF points, origin bottom left)."""

    reader = PdfReader(path)
    fragments = []

    for page_number, page in enumerate(reader.pages):

        def visit(text, cm, tm, font, size, page_number=page_number):
            text = text.strip()
            if text:
                x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
                y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
                fragments.append(Fragment(page_number, x, y, text))

        page.extract_text(visitor_text=visit)

    return fragments


def normalise(text: str) -> str:

    return " ".join(text.split()).rstrip(":").strip().casefold()


def find_labels(fragments: list[Fragment], labels: list[str]) -> dict[str, list[int]]:
    """
    Fragments that make up each label found: one piece, or the lines of
    a wrapped cell (same column, each line just below the previous).
    """

    matches: dict[str, list[int]] = {}
    used: set[int] = set()

    for label in labels:
        key = normalise(label)
        index = next((i for i, f in enumerate(fragments) if i not in used and normalise(f.text) == key), None)
        if index is not None:
            matches[label] = [index]
            used.add(index)

    for label in labels:
        if label in matches:
            continue

        key = normalise(label)

        for start, fragment in enumerate(fragments):
            if start in used or not key.startswith(normalise(fragment.text) + " "):
                continue

            chain, text, current = [start], normalise(fragment.text), fragment

            while text != key:
                below = [
                    (i, f)
                    for i, f in enumerate(fragments)
                    if i not in used
                    and i not in chain
                    and f.page == current.page
                    and abs(f.x - current.x) <= COLUMN_TOLERANCE
                    and 0 < current.y - f.y <= WRAP_GAP
                ]
                if not below:
                    break
                index, current = max(below, key=lambda item: item[1].y)
                candidate = f"{text} {normalise(current.text)}"
                if not (key == candidate or key.startswith(candidate + " ")):
                    break
                chain.append(index)
                text = candidate

            if text == key:
                matches[label] = chain
                used.update(chain)
                break

    return matches


def joined(pieces: list[Fragment]) -> str:

    return " ".join(f.text for f in sorted(pieces, key=lambda f: f.x))


def layout_values(fragments: list[Fragment], labels: list[str]) -> dict[str, str]:
    """
    Value of every label found among the fragments (an empty string when
    the label is there but its value is blank). Labels not found are absent.
    """

    matches = find_labels(fragments, labels)
    label_pieces = {index for chain in matches.values() for index in chain}
    values = {}

    for label, chain in matches.items():
        first, last = fragments[chain[0]], fragments[chain[-1]]
        others = [
            fragments[other_chain[0]]
            for other, other_chain in matches.items()
            if other != label
            and fragments[other_chain[-1]].page == last.page
            and abs(fragments[other_chain[-1]].y - last.y) <= SAME_LINE
        ]
        free = [f for i, f in enumerate(fragments) if i not in label_pieces and f.page == last.page]

        if not others:
            # Key-value row: the value is to the right on the same line.
            values[label] = joined([f for f in free if abs(f.y - last.y) <= SAME_LINE and f.x > first.x])
            continue

        # Table header: the value is below, in this header's column.
        right = [f.x for f in others if f.x > first.x]
        start = first.x - COLUMN_TOLERANCE
        end = min(right) - COLUMN_TOLERANCE if right else float("inf")
        below = [f for f in free if start <= f.x < end and SAME_LINE < last.y - f.y <= ROW_BELOW]
        row = max((f.y for f in below), default=None)
        values[label] = joined([f for f in below if abs(f.y - row) <= SAME_LINE]) if below else ""

    return values


def document_values(path: Path, labels: list[str]) -> dict[str, str]:
    """Value of every label found in a PDF document (see layout_values)."""

    return layout_values(document_fragments(path), labels)


class TemplateError(ValueError):
    """The document's template cannot be accepted (unknown or retired)."""


TEMPLATE_VERSION_COLUMN = "template_version"


def template_versions(extraction: dict | None) -> dict[str, dict]:
    """Registered templates by version (empty when the source has none)."""

    versions = ((extraction or {}).get("templates") or {}).get("versions") or {}

    return {str(version): spec or {} for version, spec in versions.items()}


def field_labels(extraction: dict, version: str | None = None) -> dict[str, str | None]:
    """
    Label of every field in a template; None for a field the template
    does not capture. Without a version, the default labels.
    """

    labels = {name: spec["label"] for name, spec in extraction["fields"].items()}

    if version is not None:
        template = template_versions(extraction)[version]
        labels.update(template.get("labels") or {})
        labels.update(dict.fromkeys(template.get("not_captured") or []))

    return labels


def candidate_labels(extraction: dict | None, required: list[str] | tuple = ()) -> list[str]:
    """Every label a document of this source may carry: all templates' labels and the version label."""

    labels = list(required)

    if extraction:
        labels += [label for label in field_labels(extraction).values() if label]
        for version in template_versions(extraction):
            labels += [label for label in field_labels(extraction, version).values() if label]
        if template_versions(extraction):
            labels.append(extraction["templates"].get("version_label", "Template Version"))

    return list(dict.fromkeys(labels))


def detect_template(found: dict[str, str], extraction: dict) -> str | None:
    """
    Template version of a document: the value of its version label, or
    the default template when the label is absent. None when the source
    registers no templates. Raises TemplateError for a version that is
    not registered.
    """

    versions = template_versions(extraction)

    if not versions:
        return None

    version_label = extraction["templates"].get("version_label", "Template Version")
    declared = found.get(version_label)

    if declared is None:
        defaults = [version for version, spec in versions.items() if spec.get("default")]

        if not defaults:
            raise TemplateError(f"The document has no '{version_label}' label and no default template is registered.")

        return defaults[0]

    declared = declared.strip()

    if declared not in versions:
        raise TemplateError(
            f"Template version '{declared}' is not registered for this source "
            f"(accepted: {', '.join(sorted(versions))})."
        )

    return declared


def check_template_retired(found: dict[str, str], extraction: dict, version: str) -> None:
    """
    Raise TemplateError when the document is dated after its template's
    accepted_until date (the business retired that layout).
    """

    accepted_until = template_versions(extraction)[version].get("accepted_until")

    if not accepted_until:
        return

    if isinstance(accepted_until, str):
        accepted_until = date.fromisoformat(accepted_until)

    date_field = extraction["templates"].get("date_field")
    label = field_labels(extraction, version).get(date_field) if date_field else None
    formats = extraction.get("date_formats") or ()
    document_date = parse_date(found[label], formats) if label and label in found else None

    if isinstance(document_date, date) and document_date > accepted_until:
        raise TemplateError(
            f"Template {version} was accepted until {accepted_until.isoformat()}; "
            f"this document is dated {document_date.isoformat()}."
        )


def resolve_template(found: dict[str, str], extraction: dict) -> str | None:
    """Detect the template and confirm it is still accepted."""

    version = detect_template(found, extraction)

    if version is not None:
        check_template_retired(found, extraction, version)

    return version


def extract_record(path: Path, extraction: dict) -> tuple[list[str], tuple]:
    """
    Return (headers, values) for one document. Missing labels give None
    (reported by required-value rules). Raises TemplateError when the
    document's template is not accepted (structure validation reports
    this before extraction is used).
    """

    found = document_values(path, candidate_labels(extraction))
    version = resolve_template(found, extraction)
    labels = field_labels(extraction, version)
    fields = extraction["fields"]

    headers = list(fields)
    values = []

    for name, spec in fields.items():
        label = labels[name]
        raw = found.get(label) if label else None

        values.append(parse_value(raw, spec.get("type", "text"), extraction) if raw else None)

    if version is not None:
        headers.append(TEMPLATE_VERSION_COLUMN)
        values.append(version)

    pattern = extraction.get("filename_fields")

    if pattern:
        match = re.match(pattern, path.name)
        groups = match.groupdict() if match else {}

        for group in re.compile(pattern).groupindex:
            headers.append(group)
            values.append(groups.get(group))

    return headers, tuple(values)
