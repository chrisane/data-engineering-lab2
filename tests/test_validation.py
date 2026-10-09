from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

from validate_data import ReferenceData, compile_formula, validate_data
from validate_structure import validate_source


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = PROJECT_ROOT / "tests" / "test_data"

HEADERS = ["po_id", "supplier_id", "po_date", "po_value", "vat", "total", "status"]

REGISTRY = {
    "purchase_orders": {
        "format": "xlsx",
        "path": "purchase_orders.xlsx",
        "structure": {
            "sheet": "Sheet1",
            "required_columns": HEADERS,
        },
        "data_rules": {
            "required_values": ["po_id", "supplier_id"],
            "unique": ["po_id"],
            "dates": ["po_date"],
            "numeric": {"po_value": {"minimum": 0}},
            "allowed_values": {"status": ["APPROVED", "CANCELLED"]},
            "patterns": {"po_id": r"^PO\d{6}$"},
        },
        "business_rules": {
            "calculations": [
                {"field": "total", "formula": "po_value + vat", "tolerance": 0.01},
            ],
            "comparisons": [
                {"name": "MAX_ORDER", "field": "po_value", "operator": "<=", "value": 100000},
            ],
        },
    },
}

VALID_ROW = ["PO000001", "SUP0001", datetime(2026, 1, 5), 1000, 150, 1150, "APPROVED"]


def make_workbook(path: Path, rows: list[list], headers=HEADERS, sheet="Sheet1") -> Path:

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)

    for row in rows:
        worksheet.append(row)

    workbook.save(path)

    return path


def run_data_validation(file: Path) -> dict:

    return validate_data(
        file=file,
        run_id="TEST",
        source="purchase_orders",
        source_registry=REGISTRY,
        reference_data=ReferenceData([], "TEST", REGISTRY),
    )


def failures(validation: dict) -> list[tuple]:

    return sorted(
        (result["rule"], result["field"], result["row_number"])
        for result in validation["results"]
        if result["status"] == "FAIL"
    )


# ---------------------------------------------------------
# STRUCTURE
# ---------------------------------------------------------

def test_corrupted_workbook_fails_readability():

    validation = validate_source(
        TEST_DATA / "corrupted_supplier_master.xlsx",
        "TEST",
        "purchase_orders",
        REGISTRY,
    )

    assert validation["status"] == "STRUCTURE_INVALID"
    assert validation["failed_stage"] == "WORKBOOK_READABLE"


def test_missing_sheet_and_missing_column(tmp_path):

    wrong_sheet = make_workbook(tmp_path / "a.xlsx", [VALID_ROW], sheet="Data")
    missing_column = make_workbook(
        tmp_path / "b.xlsx", [VALID_ROW[:-1]], headers=HEADERS[:-1]
    )

    assert validate_source(wrong_sheet, "T", "purchase_orders", REGISTRY)[
        "failed_stage"
    ] == "REQUIRED_SHEET"

    result = validate_source(missing_column, "T", "purchase_orders", REGISTRY)
    assert result["failed_stage"] == "REQUIRED_COLUMN"


def test_empty_workbook_fails_minimum_rows(tmp_path):

    file = make_workbook(tmp_path / "po.xlsx", [])

    result = validate_source(file, "T", "purchase_orders", REGISTRY)

    assert result["failed_stage"] == "MINIMUM_ROWS"


# ---------------------------------------------------------
# DATA AND BUSINESS RULES
# ---------------------------------------------------------

def test_clean_file_passes(tmp_path):

    file = make_workbook(tmp_path / "po.xlsx", [VALID_ROW])

    validation = run_data_validation(file)

    assert validation["status"] == "DATA_VALID"
    assert failures(validation) == []


def test_each_rule_reports_the_exact_row_and_field(tmp_path):

    rows = [
        VALID_ROW,                                                              # row 2
        ["PO000002", None, datetime(2026, 1, 5), 1000, 150, 1150, "APPROVED"],  # row 3 blank supplier
        ["PO000001", "SUP0001", datetime(2026, 1, 5), 1000, 150, 1150, "APPROVED"],  # row 4 duplicate
        ["PO000004", "SUP0001", "not a date", -5, 0, -5, "APPROVED"],           # row 5 date, range
        ["PO000005", "SUP0001", datetime(2026, 1, 5), "abc", 0, 0, "OPEN"],     # row 6 type, status
        ["P-6", "SUP0001", datetime(2026, 1, 5), 1000, 150, 9999, "APPROVED"],  # row 7 pattern, calc
        ["PO000008", "SUP0001", datetime(2026, 1, 5), 200000, 0, 200000, "APPROVED"],  # row 8 max
    ]

    file = make_workbook(tmp_path / "po.xlsx", rows)

    validation = run_data_validation(file)

    assert validation["status"] == "DATA_INVALID"
    assert failures(validation) == sorted([
        ("REQUIRED_VALUE", "supplier_id", 3),
        ("UNIQUE", "po_id", 4),
        ("VALID_DATE", "po_date", 5),
        ("NUMERIC_RANGE", "po_value", 5),
        ("DATA_TYPE", "po_value", 6),
        ("ALLOWED_VALUE", "status", 6),
        ("PATTERN", "po_id", 7),
        ("CALCULATION", "total", 7),
        ("MAX_ORDER", "po_value", 8),
    ])

    duplicate = next(r for r in validation["results"] if r["rule"] == "UNIQUE")
    assert duplicate["record_key"] == "po_id=PO000001"
    assert "first seen in row 2" in duplicate["message"]


def test_date_stored_as_text_is_only_a_warning(tmp_path):

    row = list(VALID_ROW)
    row[2] = "2026-01-05"

    validation = run_data_validation(make_workbook(tmp_path / "po.xlsx", [row]))

    assert validation["status"] == "DATA_VALID_WITH_WARNINGS"
    assert failures(validation) == [("DATE_STORED_AS_TEXT", "po_date", 2)]


def test_failures_are_capped_but_counted(tmp_path):

    rows = [[f"PO{n:06d}", None, None, 1, 0, 1, "APPROVED"] for n in range(10)]

    validation = validate_data(
        file=make_workbook(tmp_path / "po.xlsx", rows),
        run_id="TEST",
        source="purchase_orders",
        source_registry=REGISTRY,
        reference_data=ReferenceData([], "TEST", REGISTRY),
        max_failures_per_rule=3,
    )

    required = [
        r for r in validation["results"]
        if r["rule"] == "REQUIRED_VALUE" and r["status"] == "FAIL"
    ]

    # 3 individual rows plus one summary line.
    assert len(required) == 4
    assert validation["errors"] == 10


def test_group_balance_flags_unbalanced_journal(tmp_path):

    registry = {
        "general_ledger": {
            "format": "xlsx",
            "structure": {"sheet": "Sheet1"},
            "business_rules": {
                "group_balance": [
                    {"name": "JOURNAL_BALANCE", "group_by": "journal_id",
                     "debit": "debit", "credit": "credit"},
                ],
            },
        },
    }

    file = make_workbook(
        tmp_path / "gl.xlsx",
        [["J1", 100, 0], ["J1", 0, 100], ["J2", 50, 0]],
        headers=["journal_id", "debit", "credit"],
    )

    validation = validate_data(
        file, "TEST", "general_ledger", registry,
        ReferenceData([], "TEST", registry),
    )

    assert failures(validation) == sorted([
        ("JOURNAL_BALANCE", "journal_id", 4),
        ("JOURNAL_BALANCE_FILE_TOTAL", None, None),
    ])


@pytest.mark.parametrize("formula", [
    "__import__('os')",
    "net_amount.real",
    "net_amount ** 2",
    "'text'",
])
def test_unsafe_formulas_are_rejected(formula):

    with pytest.raises(ValueError):
        compile_formula(formula)
