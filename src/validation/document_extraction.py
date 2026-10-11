"""
Structured field extraction from PDF documents (branch cash-ups).

A source's registry entry describes the fields to extract:

    extraction:
      fields:
        shift_id:      {label: Shift ID}
        business_date: {label: Business Date, type: date}
        cash_sales:    {label: Cash Sales, type: money}
      filename_fields: '^(?P<file_site_code>BR\\d{3})_(?P<file_shift_id>SHIFT-\\d{6})_...'

Document templates (versions of the layout)
    A source may register several templates that are accepted side by
    side, so branches can move to a new layout at different times:

    extraction:
      fields: ...                      # labels of the default template
      templates:
        version_label: Template Version
        date_field: business_date      # used for accepted_until
        versions:
          "1": {default: true}         # documents without a version label
          "2": {labels: {cash_sales: Cash Takings}}   # relabelled fields
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
    document_date = parse_date(found[label]) if label and label in found else None

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

    found = label_values(document_text(path))
    version = resolve_template(found, extraction)
    labels = field_labels(extraction, version)
    fields = extraction["fields"]

    headers = list(fields)
    values = []

    for name, spec in fields.items():
        label = labels[name]
        raw = found.get(label) if label else None
        parser = PARSERS[spec.get("type", "text")]

        values.append(None if raw is None else parser(raw))

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
