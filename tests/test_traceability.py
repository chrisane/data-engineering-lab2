"""
Tests for coverage, load eligibility and failure traceability.
"""

from datetime import datetime
from pathlib import Path

import openpyxl

import discover_files

from check_coverage import assess_file, assess_stage, index_results
from common import (
    DATA_STAGE,
    STRUCTURE_STAGE,
    describe_exception,
    exception_result,
    failure_summary,
)
from load_eligibility import decide
from validate_data import ReferenceData, expected_data_checks, validate_data
from validate_structure import expected_structure_checks, validate_source


# ---------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------

HEADERS = ["po_id", "supplier_id", "po_date", "po_value", "vat", "total", "status"]

SOURCE = {
    "format": "xlsx",
    "path": "purchase_orders.xlsx",
    "structure": {"sheet": "Sheet1", "required_columns": HEADERS},
    "data_rules": {
        "required_values": ["po_id", "supplier_id"],
        "unique": ["po_id", ["po_id", "supplier_id"]],
        "dates": ["po_date"],
        "numeric": {"po_value": {"minimum": 0}, "vat": {}},
        "allowed_values": {"status": ["APPROVED"]},
        "patterns": {"po_id": r"^PO\d{6}$"},
    },
    "business_rules": {
        "calculations": [{"field": "total", "formula": "po_value + vat"}],
        "comparisons": [
            {"name": "MAX_ORDER", "field": "po_value", "operator": "<=", "value": 100000},
        ],
        "group_balance": [
            {"group_by": "po_id", "debit": "po_value", "credit": "po_value"},
        ],
    },
}

REGISTRY = {"purchase_orders": SOURCE}


def make_workbook(path: Path, rows: list[list]) -> Path:

    workbook = openpyxl.Workbook()
    workbook.active.title = "Sheet1"
    workbook.active.append(HEADERS)

    for row in rows:
        workbook.active.append(row)

    workbook.save(path)

    return path


def status_row(stage, status, validation_id="VAL-1", run_id="ING-1", file="po.xlsx", **extra):

    return {
        "run_id": run_id, "validation_id": validation_id, "file": file,
        "stage": stage, "status": status, "errors": "0", "warnings": "0",
        **extra,
    }


def result_rows(checks, validation_id="VAL-1", file="po.xlsx", status="PASS"):

    return [
        {"validation_id": validation_id, "file": file, "rule": rule,
         "field": field, "status": status, "severity": None}
        for rule, field in checks
    ]


# ---------------------------------------------------------
# EXPECTED CHECKS MATCH WHAT VALIDATION REALLY RUNS
# ---------------------------------------------------------

def test_expected_checks_match_a_real_validation(tmp_path):
    """
    Guards against the expected-check lists drifting from the rule
    functions: a clean file must produce exactly the expected checks.
    """

    file = make_workbook(
        tmp_path / "po.xlsx",
        [["PO000001", "SUP0001", datetime(2026, 1, 5), 1000, 150, 1150, "APPROVED"]],
    )

    structure = validate_source(file, "T", "purchase_orders", REGISTRY)
    data = validate_data(file, "T", "purchase_orders", REGISTRY, ReferenceData([], "T", REGISTRY))

    executed_structure = {(r["rule"], r.get("field") or "") for r in structure["results"]}
    executed_data = {(r["rule"], r.get("field") or "") for r in data["results"]}

    assert expected_structure_checks(SOURCE) <= executed_structure
    assert expected_data_checks(SOURCE) == executed_data


# ---------------------------------------------------------
# COVERAGE
# ---------------------------------------------------------

def test_coverage_complete_when_every_check_ran():

    expected = expected_data_checks(SOURCE)
    rows = result_rows(expected)

    assessment = assess_stage(
        "purchase_orders", "po.xlsx", "ING-1", DATA_STAGE, expected,
        [status_row(DATA_STAGE, "DATA_VALID")], index_results(rows),
    )

    assert assessment["coverage_status"] == "COMPLETE"


def test_coverage_incomplete_names_the_checks_that_did_not_run():

    expected = expected_data_checks(SOURCE)
    rows = result_rows(expected - {("PATTERN", "po_id")})

    assessment = assess_stage(
        "purchase_orders", "po.xlsx", "ING-1", DATA_STAGE, expected,
        [status_row(DATA_STAGE, "DATA_VALID")], index_results(rows),
    )

    assert assessment["coverage_status"] == "INCOMPLETE"
    assert assessment["missing_checks"] == "PATTERN(po_id)"


def test_coverage_missing_and_unverifiable():

    expected = expected_data_checks(SOURCE)

    missing = assess_stage(
        "purchase_orders", "po.xlsx", "ING-1", DATA_STAGE, expected, [], {},
    )
    legacy = assess_stage(
        "purchase_orders", "po.xlsx", "ING-1", DATA_STAGE, expected,
        [status_row(DATA_STAGE, "DATA_VALID", validation_id="")], {},
    )

    assert missing["coverage_status"] == "MISSING"
    assert legacy["coverage_status"] == "UNVERIFIABLE"


def test_coverage_accepts_validation_that_stopped_at_a_recorded_failure():

    rows = result_rows([("WORKBOOK_READABLE", "")], status="FAIL")

    assessment = assess_stage(
        "purchase_orders", "po.xlsx", "ING-1", STRUCTURE_STAGE,
        expected_structure_checks(SOURCE),
        [status_row(STRUCTURE_STAGE, "STRUCTURE_INVALID")], index_results(rows),
    )

    assert assessment["coverage_status"] == "STOPPED_AT_FAILURE"


def test_sources_without_data_rules_are_reported_as_structure_only():

    pdf_source = {"format": "pdf", "path": "cashups/*.pdf", "structure": {}}
    rows = result_rows([("PDF_READABLE", "")], file="a.pdf")

    assessments = assess_file(
        "branch_cashup", "a.pdf", "ING-1", pdf_source,
        [status_row(STRUCTURE_STAGE, "STRUCTURE_VALID", file="a.pdf")],
        index_results(rows),
    )

    assert [a["coverage_status"] for a in assessments] == ["COMPLETE", "NOT_APPLICABLE"]


# ---------------------------------------------------------
# LOAD ELIGIBILITY
# ---------------------------------------------------------

LIGHT_SOURCE = {
    "format": "xlsx",
    "path": None,  # set per source below
    "structure": {"sheet": "Sheet1", "required_columns": ["id"]},
    "data_rules": {"required_values": ["id"]},
}


def light_registry(*names, critical=()):

    return {
        "sources": {
            name: {**LIGHT_SOURCE, "path": f"{name}.xlsx"} for name in names
        },
        "load_policy": {
            "eligible_structure_statuses": ["STRUCTURE_VALID"],
            "eligible_data_statuses": ["DATA_VALID", "DATA_VALID_WITH_WARNINGS"],
            "critical_sources": list(critical),
        },
    }


def ingested(source, run_id="ING-1", status="INGESTED", message=None):

    return {
        "run_id": run_id, "source": source, "file_name": f"{source}.xlsx",
        "file_hash": "abc", "ingested_at": "2026-10-09", "status": status,
        "raw_path": f"data/raw/{run_id}/{source}.xlsx", "message": message,
    }


def validated(source, data_status="DATA_VALID", run_id="ING-1", failures=()):
    """Status and result rows for a complete validation of a source."""

    file = f"{source}.xlsx"
    structure_id, data_id = f"S-{source}-{run_id}", f"D-{source}-{run_id}"

    statuses = [
        status_row(STRUCTURE_STAGE, "STRUCTURE_VALID", structure_id, run_id, file),
        status_row(DATA_STAGE, data_status, data_id, run_id, file,
                   errors=str(len(failures))),
    ]

    results = result_rows(expected_structure_checks(LIGHT_SOURCE), structure_id, file)
    results += result_rows(expected_data_checks(LIGHT_SOURCE), data_id, file)

    for row_number in failures:
        results.append({
            "validation_id": data_id, "file": file, "rule": "REQUIRED_VALUE",
            "field": "id", "status": "FAIL", "severity": "ERROR",
            "row_number": str(row_number), "message": "Required field 'id' is blank.",
        })

    return statuses, results


def decisions_by_source(decisions):

    return {d["source"]: d for d in decisions}


def test_valid_sources_are_eligible():

    statuses, results = validated("sites")

    decisions = decide(light_registry("sites"), [ingested("sites")], statuses, results)

    assert decisions[0]["decision"] == "ELIGIBLE"


def test_failed_source_is_blocked_with_the_exact_reason():

    statuses, results = validated("sites", "DATA_INVALID", failures=[7, 9])

    decision = decide(light_registry("sites"), [ingested("sites")], statuses, results)[0]

    assert decision["decision"] == "BLOCKED"
    assert "REQUIRED_VALUE on id x2 (first at row 7)" in decision["reason"]


def test_blocked_critical_source_holds_everything_else():

    good_status, good_results = validated("products")
    bad_status, bad_results = validated("ledger", "DATA_INVALID", failures=[3])

    decisions = decisions_by_source(decide(
        light_registry("products", "ledger", critical=["ledger"]),
        [ingested("products"), ingested("ledger")],
        good_status + bad_status,
        good_results + bad_results,
    ))

    assert decisions["ledger"]["decision"] == "BLOCKED"
    assert decisions["products"]["decision"] == "HELD"
    assert "ledger" in decisions["products"]["reason"]


def test_newer_rejected_submission_blocks_the_older_valid_version():

    statuses, results = validated("sites", run_id="ING-1")

    manifest = [
        ingested("sites", "ING-1"),
        ingested("sites", "ING-2", status="INVALID_CONTENT",
                 message="File content is not a valid .xlsx file."),
    ]

    decision = decide(light_registry("sites"), manifest, statuses, results)[0]

    assert decision["decision"] == "BLOCKED"
    assert "newer submission (run ING-2) was rejected" in decision["reason"]
    assert "stale" in decision["reason"]


def test_never_ingested_source_explains_the_ingestion_rejection():

    manifest = [ingested("sites", status="EMPTY_FILE", message="File is empty (0 bytes).")]

    decision = decide(light_registry("sites"), manifest, [], [])[0]

    assert decision["decision"] == "BLOCKED"
    assert "EMPTY_FILE: File is empty (0 bytes)." in decision["reason"]


def test_incomplete_validation_blocks_an_otherwise_valid_file():

    statuses, results = validated("sites")

    # Drop the data-rule results: the check "passed" without running.
    results = [row for row in results if not row["validation_id"].startswith("D-")]

    decision = decide(light_registry("sites"), [ingested("sites")], statuses, results)[0]

    assert decision["decision"] == "BLOCKED"
    assert "Validation incomplete" in decision["reason"]


# ---------------------------------------------------------
# CRASH DIAGNOSTICS
# ---------------------------------------------------------

def raise_inside_project():
    raise ValueError("bad cell")


def test_exceptions_record_message_and_code_location():

    try:
        raise_inside_project()
    except ValueError as error:
        description = describe_exception(error)
        result = exception_result("ING-1", "sites", Path("sites.xlsx"), DATA_STAGE, error)

    assert description.startswith("bad cell (at tests/test_traceability.py:")
    assert "in raise_inside_project" in description
    assert result["rule"] == "VALIDATION_EXCEPTION"
    assert result["error_type"] == "ValueError"


def test_failure_summary_counts_and_locates_errors():

    rows = [
        {"status": "FAIL", "severity": "ERROR", "rule": "UNIQUE", "field": "id", "row_number": "4"},
        {"status": "FAIL", "severity": "ERROR", "rule": "UNIQUE", "field": "id", "row_number": "9"},
        {"status": "FAIL", "severity": "WARNING", "rule": "PATTERN", "field": "id", "row_number": "2"},
    ]

    assert failure_summary(rows) == "UNIQUE on id x2 (first at row 4)"


# ---------------------------------------------------------
# INGESTION TRACEABILITY
# ---------------------------------------------------------

def test_ingestion_errors_and_ignored_files_are_recorded(tmp_path, monkeypatch):

    incoming = tmp_path / "incoming"
    incoming.mkdir()

    monkeypatch.setattr(discover_files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(discover_files, "INCOMING_DIR", incoming)
    monkeypatch.setattr(discover_files, "MANIFEST_FILE", tmp_path / "manifest.csv")

    registry = {"sites": {"format": "xlsx", "path": "sites.xlsx"}}

    ignored = incoming / "notes.xlsx"
    ignored.write_bytes(b"PK\x03\x04")

    outcome = discover_files.process_file(ignored, "ING-1", registry, ["notes.xlsx"], {})
    assert outcome["status"] == "IGNORED"

    try:
        raise PermissionError("file is locked by another program")
    except PermissionError as error:
        failed = discover_files.record_ingestion_error(
            incoming / "sites.xlsx", "ING-1", registry, error,
        )

    assert failed["status"] == "INGESTION_ERROR"
    assert failed["source"] == "sites"
    assert "PermissionError: file is locked by another program" in failed["message"]

    manifest = (tmp_path / "manifest.csv").read_text(encoding="utf-8")
    assert "IGNORED" in manifest and "INGESTION_ERROR" in manifest
