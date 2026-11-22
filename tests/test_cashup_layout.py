"""
Branch cash-up layout (Excel template exported to PDF).

Reads two PDFs exported by Excel itself from the template
(tests/test_data/branch_cashup_excel_*.pdf) and PDFs drawn by the data
generator's renderer, and proves that values are found by position
(label-value rows, table columns, wrapped headers, accounting numbers),
that both kinds of document extract to the same record, pass structure
and data validation, and that unknown or retired layouts are reported
instead of guessed.
"""

import copy
import shutil
from datetime import date
from pathlib import Path

import cashup_report
import pytest
from cashup_report import write_cashup_pdf

from common import load_source_registry
from document_extraction import TemplateError, extract_record
from staging_schema import staging_table
from validate_data import ReferenceData, validate_data
from validate_structure import expected_structure_checks, validate_source

SOURCE = "branch_cashup"
REGISTRY = {SOURCE: load_source_registry()[SOURCE]}
TEST_DATA = Path(__file__).parent / "test_data"

# The values on the Excel-exported fixtures (balanced; the shortage fixture
# is SHIFT-000002 with 674.94 counted, a variance of -150.00).
EXCEL_REPORT = {
    "Site Code": "BR001",
    "Shift Id": "SHIFT-000001",
    "Date": date(2026, 9, 21),
    "POS Terminal": "POS-BR001-01",
    "Cashier Name": "Stephanie Pena",
    "Opening Float": 750.00,
    "Cash Sales": 7394.94,
    "Card Sales": 1378.76,
    "Account Sales": 0.0,
    "Total Sales": 8773.70,
    "Internal Expenses": 120.00,
    "Cash Removed": 7200.00,
    "Expected Closing Cash": 824.94,
    "Actual Closing Cash": 824.94,
    "Variance": 0.0,
    "Cashier Signature": "Signed",
    "Manager Signature": "Approved",
}


def extraction(registry=REGISTRY):
    return registry[SOURCE]["extraction"]


def record(path: Path, registry=REGISTRY) -> dict:
    headers, values = extract_record(path, extraction(registry))
    return dict(zip(headers, values, strict=True))


def named(path: Path, folder: Path, report: dict) -> Path:
    """A copy of a document under the file name the email automation gives it."""

    folder.mkdir(exist_ok=True)
    target = folder / f"{report['Site Code']}_{report['Shift Id']}_{report['Date']:%Y%m%d}_Cashup.pdf"
    shutil.copyfile(path, target)
    return target


def generated(folder: Path, report: dict = EXCEL_REPORT, **options) -> Path:
    folder.mkdir(exist_ok=True)
    name = f"{report['Site Code']}_{report['Shift Id']}_{report['Date']:%Y%m%d}_Cashup.pdf"
    return write_cashup_pdf(folder / name, report, **options)


# ---------------------------------------------------------
# Extraction
# ---------------------------------------------------------


def test_excel_export_extracts_every_field():

    values = record(TEST_DATA / "branch_cashup_excel_balanced.pdf")

    assert values["site_code"] == "BR001"
    assert values["shift_id"] == "SHIFT-000001"
    assert values["business_date"] == date(2026, 9, 21)  # 21/09/2026, day first
    assert values["cashier"] == "Stephanie Pena"
    assert values["cash_sales"] == 7394.94 and values["card_sales"] == 1378.76
    assert values["account_sales"] == 0.0  # accounting zero printed as '-'
    assert values["expected_closing_cash"] == 824.94  # wrapped header 'Expected / Closing Cash'
    assert values["actual_closing_cash"] == 824.94  # wrapped header 'Actual Closing / Cash'
    assert values["variance"] == 0.0
    assert values["manager_approval"] == "Approved"
    assert values["template_version"] == "3"


def test_excel_export_negative_accounting_value():
    """Excel prints the minus sign at the left edge of the cell, apart from the number."""

    values = record(TEST_DATA / "branch_cashup_excel_shortage.pdf")

    assert values["actual_closing_cash"] == 674.94
    assert values["variance"] == -150.00


def test_generated_document_reads_the_same_as_the_excel_export(tmp_path):

    excel = record(named(TEST_DATA / "branch_cashup_excel_balanced.pdf", tmp_path / "excel", EXCEL_REPORT))
    drawn = record(generated(tmp_path / "drawn"))

    assert drawn == excel


def test_unregistered_template_is_refused_not_guessed(tmp_path):

    path = generated(tmp_path, extra_rows=[("Template Version", "9")])

    with pytest.raises(TemplateError, match="'9' is not registered"):
        extract_record(path, extraction())


# ---------------------------------------------------------
# Structure validation
# ---------------------------------------------------------


@pytest.mark.parametrize("kind", ["excel", "generated"])
def test_cashup_passes_structure_validation_with_full_coverage(kind, tmp_path):

    if kind == "excel":
        path = named(TEST_DATA / "branch_cashup_excel_balanced.pdf", tmp_path, EXCEL_REPORT)
    else:
        path = generated(tmp_path)

    results = validate_source(path, "T", SOURCE, REGISTRY)["results"]

    assert not [r for r in results if r["status"] == "FAIL"]
    assert {(r["rule"], r.get("field") or "") for r in results} == expected_structure_checks(REGISTRY[SOURCE])
    assert next(r["actual"] for r in results if r["rule"] == "TEMPLATE_VERSION") == "3"


def test_missing_label_names_the_field(tmp_path, monkeypatch):

    monkeypatch.setattr(cashup_report, "TAKINGS", [label for label in cashup_report.TAKINGS if label != "Cash Sales"])
    path = generated(tmp_path)

    failure = next(r for r in validate_source(path, "T", SOURCE, REGISTRY)["results"] if r["status"] == "FAIL")

    assert failure["rule"] == "REQUIRED_LABEL"
    assert failure["field"] == "cash_sales"
    assert "'Cash Sales'" in failure["message"]


def test_unknown_template_fails_structure_with_an_actionable_message(tmp_path):

    path = generated(tmp_path, extra_rows=[("Template Version", "9")])

    failure = next(r for r in validate_source(path, "T", SOURCE, REGISTRY)["results"] if r["status"] == "FAIL")

    assert failure["rule"] == "TEMPLATE_VERSION"
    assert "'9' is not registered" in failure["message"]
    assert "extraction.templates" in failure["recommended_action"]


def test_retired_template_is_refused_after_its_cut_off(tmp_path):

    registry = copy.deepcopy(REGISTRY)
    registry[SOURCE]["extraction"]["templates"]["versions"]["3"]["accepted_until"] = "2026-09-20"

    failure = next(
        r for r in validate_source(generated(tmp_path), "T", SOURCE, registry)["results"] if r["status"] == "FAIL"
    )

    assert failure["rule"] == "TEMPLATE_VERSION"
    assert "accepted until 2026-09-20" in failure["message"]

    earlier = dict(EXCEL_REPORT, Date=date(2026, 9, 19))
    path = generated(tmp_path / "early", earlier)

    assert not [r for r in validate_source(path, "T", SOURCE, registry)["results"] if r["status"] == "FAIL"]


# ---------------------------------------------------------
# Data validation and staging
# ---------------------------------------------------------


def test_cashup_passes_the_business_rules(tmp_path):

    summary = validate_data(generated(tmp_path), "T", SOURCE, REGISTRY, ReferenceData([], "T", REGISTRY))
    failed = {r["rule"] for r in summary["results"] if r["status"] == "FAIL"}

    # Only the site reference can fail here (no site master in this test).
    assert failed <= {"REFERENTIAL_INTEGRITY"}, failed
    assert {"CASHUP_TOTAL_SALES", "CASHUP_EXPECTED_CLOSING", "CASHUP_VARIANCE"} <= {
        r["rule"] for r in summary["results"] if r["status"] == "PASS"
    }


def test_shortage_passes_the_variance_rule(tmp_path):
    """Variance = actual - expected: a shortage is negative and consistent, so it is not an error."""

    shortage = dict(EXCEL_REPORT, **{"Actual Closing Cash": 674.94, "Variance": -150.00})
    summary = validate_data(generated(tmp_path, shortage), "T", SOURCE, REGISTRY, ReferenceData([], "T", REGISTRY))

    assert next(r for r in summary["results"] if r["rule"] == "CASHUP_VARIANCE")["status"] == "PASS"


def test_typed_unreadable_amount_is_reported(tmp_path):

    typed = dict(EXCEL_REPORT, **{"Card Sales": "1,2O4.5O"})
    summary = validate_data(generated(tmp_path, typed), "T", SOURCE, REGISTRY, ReferenceData([], "T", REGISTRY))
    failure = next(r for r in summary["results"] if r["status"] == "FAIL" and r["rule"] == "DATA_TYPE")

    assert failure["field"] == "card_sales"
    assert failure["actual"] == "1,2O4.5O"


def test_staging_records_the_template_version():

    definition = staging_table(SOURCE, REGISTRY[SOURCE])
    column = next(c for c in definition.columns if c.name == "template_version")

    assert column.sql_type == "NVARCHAR(50)" and column.nullable
