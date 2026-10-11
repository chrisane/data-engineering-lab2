"""
Cash-up template versions: old and new layouts accepted side by side.

Builds real PDFs with the same values on template 1 (no version label)
and template 2 (version label, renamed and reordered lines) and proves
that both pass structure and data validation, extract to identical
canonical records, and that unknown or retired templates are reported
instead of guessed.
"""

import copy
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from check_coverage import assess_stage, index_results
from common import STRUCTURE_STAGE, load_source_registry
from document_extraction import TemplateError, extract_record
from staging_schema import staging_table
from validate_data import ReferenceData, validate_data
from validate_structure import expected_structure_checks, validate_source

SOURCE = "branch_cashup"
REGISTRY = {SOURCE: load_source_registry()[SOURCE]}

TEMPLATE_1 = [
    ("Shift ID", "SHIFT-000040"),
    ("Site Code", "BR004"),
    ("Business Date", "2026-09-24"),
    ("POS Terminal", "POS-BR004-01"),
    ("Cashier", "Test Cashier"),
    ("Opening Float", "N$ 750.00"),
    ("Cash Sales", "N$ 1 250.00"),
    ("Card Sales", "N$ 2,000.50"),
    ("Account Sales", "N$ 300.00"),
    ("Total Sales", "N$ 3,550.50"),
    ("Internal Expenses", "N$ 45.50"),
    ("Cash Removed", "N$ 1,200.00"),
    ("Expected Closing Cash", "N$ 754.50"),
    ("Actual Closing Cash", "N$ 754.50"),
    ("Variance", "N$ 0.00"),
    ("Cashier Signature", "Signed"),
    ("Manager Approval", "Approved"),
]

V2_LABELS = {
    "Site Code": "Branch Code",
    "POS Terminal": "Till Number",
    "Cash Sales": "Cash Takings",
    "Card Sales": "Card Takings",
    "Account Sales": "Account Takings",
    "Total Sales": "Total Takings",
    "Cash Removed": "Cash Banked",
}


def template_2(fields, version="2"):
    """Same values, template 2 labels, totals first."""

    values = dict(fields)
    order = ["Shift ID", "Site Code", "Business Date", "Total Sales", "Cash Sales", "Card Sales", "Account Sales"]
    order += [label for label in values if label not in order]

    return [("Template Version", version)] + [(V2_LABELS.get(label, label), values[label]) for label in order]


def write_pdf(path: Path, fields) -> Path:

    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    y = 800
    for label, value in fields:
        pdf.drawString(60, y, f"{label}:")
        y -= 16
        pdf.drawString(60, y, str(value))
        y -= 20
    pdf.save()

    return path


def file_name(fields) -> str:

    values = dict(fields)
    site = values.get("Site Code") or values.get("Branch Code")

    return f"{site}_{values['Shift ID']}_20260924_Cashup.pdf"


@pytest.fixture
def v1(tmp_path):
    (tmp_path / "v1").mkdir()
    return write_pdf(tmp_path / "v1" / file_name(TEMPLATE_1), TEMPLATE_1)


@pytest.fixture
def v2(tmp_path):
    (tmp_path / "v2").mkdir()
    return write_pdf(tmp_path / "v2" / file_name(TEMPLATE_1), template_2(TEMPLATE_1))


def extraction(registry=REGISTRY):

    return registry[SOURCE]["extraction"]


# ---------------------------------------------------------
# Extraction
# ---------------------------------------------------------


def test_both_templates_extract_to_the_same_canonical_record(v1, v2):

    headers_1, values_1 = extract_record(v1, extraction())
    headers_2, values_2 = extract_record(v2, extraction())

    assert headers_1 == headers_2

    record_1, record_2 = dict(zip(headers_1, values_1, strict=True)), dict(zip(headers_2, values_2, strict=True))

    assert record_1.pop("template_version") == "1"
    assert record_2.pop("template_version") == "2"
    assert record_1 == record_2
    assert record_2["cash_sales"] == 1250.00
    assert record_2["site_code"] == "BR004"
    assert record_2["pos_terminal"] == "POS-BR004-01"


def test_unregistered_template_is_refused_not_guessed(tmp_path):

    path = write_pdf(tmp_path / file_name(TEMPLATE_1), template_2(TEMPLATE_1, version="3"))

    with pytest.raises(TemplateError, match="'3' is not registered"):
        extract_record(path, extraction())


# ---------------------------------------------------------
# Structure validation and coverage
# ---------------------------------------------------------


@pytest.mark.parametrize("template", ["v1", "v2"])
def test_both_templates_pass_structure_validation_with_full_coverage(template, request):

    path = request.getfixturevalue(template)

    summary = validate_source(path, "T", SOURCE, REGISTRY)
    failures = [r for r in summary["results"] if r["status"] == "FAIL"]

    assert not failures, failures

    executed = {(r["rule"], r.get("field") or "") for r in summary["results"]}
    assert executed == expected_structure_checks(REGISTRY[SOURCE])

    version = next(r["actual"] for r in summary["results"] if r["rule"] == "TEMPLATE_VERSION")
    assert version == ("1" if template == "v1" else "2")


def test_coverage_is_the_same_for_old_and_new_templates(v1, v2):
    """Checks are named by field, so mixing layouts never looks like skipped checks."""

    results = []
    status_rows = []

    for path in (v1, v2):
        summary = validate_source(path, "T", SOURCE, REGISTRY)
        validation_id = f"VAL-{path.parent.name}"
        for result in summary["results"]:
            results.append(
                {
                    **result,
                    "field": result.get("field"),
                    "file": path.name + path.parent.name,
                    "validation_id": validation_id,
                }
            )
        status_rows.append(
            {
                "run_id": "T",
                "file": path.name + path.parent.name,
                "stage": STRUCTURE_STAGE,
                "status": "VALID",
                "validation_id": validation_id,
            }
        )

    index = index_results(results)

    for path in (v1, v2):
        assessment = assess_stage(
            SOURCE,
            path.name + path.parent.name,
            "T",
            STRUCTURE_STAGE,
            expected_structure_checks(REGISTRY[SOURCE]),
            status_rows,
            index,
        )
        assert assessment["coverage_status"] == "COMPLETE", assessment


def test_unknown_template_fails_structure_with_an_actionable_message(tmp_path):

    path = write_pdf(tmp_path / file_name(TEMPLATE_1), template_2(TEMPLATE_1, version="3"))

    summary = validate_source(path, "T", SOURCE, REGISTRY)
    failure = next(r for r in summary["results"] if r["status"] == "FAIL")

    assert failure["rule"] == "TEMPLATE_VERSION"
    assert failure["severity"] == "ERROR"
    assert "'3' is not registered" in failure["message"]
    assert "extraction.templates" in failure["recommended_action"]


def test_missing_label_on_new_template_names_the_field_and_template(tmp_path):

    fields = [(label, value) for label, value in template_2(TEMPLATE_1) if label != "Cash Takings"]
    path = write_pdf(tmp_path / file_name(TEMPLATE_1), fields)

    summary = validate_source(path, "T", SOURCE, REGISTRY)
    failure = next(r for r in summary["results"] if r["status"] == "FAIL")

    assert failure["rule"] == "REQUIRED_LABEL"
    assert failure["field"] == "cash_sales"
    assert "'Cash Takings'" in failure["message"] and "template 2" in failure["message"]


def test_retired_template_is_refused_after_its_cut_off(v1, tmp_path):

    registry = copy.deepcopy(REGISTRY)
    registry[SOURCE]["extraction"]["templates"]["versions"]["1"]["accepted_until"] = "2026-09-23"

    summary = validate_source(v1, "T", SOURCE, registry)
    failure = next(r for r in summary["results"] if r["status"] == "FAIL")

    assert failure["rule"] == "TEMPLATE_VERSION"
    assert "accepted until 2026-09-23" in failure["message"]

    # A template 1 document dated before the cut-off is still accepted.
    earlier = [(label, "2026-09-22" if label == "Business Date" else value) for label, value in TEMPLATE_1]
    (tmp_path / "early").mkdir()
    path = write_pdf(tmp_path / "early" / file_name(earlier), earlier)

    assert not [r for r in validate_source(path, "T", SOURCE, registry)["results"] if r["status"] == "FAIL"]


# ---------------------------------------------------------
# Data validation and staging
# ---------------------------------------------------------


def test_new_template_passes_the_same_business_rules(v2):

    summary = validate_data(v2, "T", SOURCE, REGISTRY, ReferenceData([], "T", REGISTRY))
    rules = {r["rule"] for r in summary["results"] if r["status"] == "FAIL"}

    # Only the site reference can fail here (no site master in this test).
    assert rules <= {"REFERENTIAL_INTEGRITY"}, rules
    assert any(r["rule"].startswith("CASHUP_") and r["status"] == "PASS" for r in summary["results"])


def test_staging_records_the_template_version():

    definition = staging_table(SOURCE, REGISTRY[SOURCE])
    column = next(c for c in definition.columns if c.name == "template_version")

    assert column.sql_type == "NVARCHAR(50)" and column.nullable
