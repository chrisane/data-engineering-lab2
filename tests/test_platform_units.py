"""
Unit tests for platform building blocks (no database needed).
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from document_extraction import Fragment, layout_values, parse_date, parse_money
from migrations import MigrationError, discover, retarget, split_batches
from orchestrator import RunContext, eligibility_block
from reconciliation import ControlConfigurationError, quote_table, within_tolerance
from staging_loader import convert
from staging_schema import sql_type, staging_table

# ---------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("N$ 31 475.00", 31475.00),
        ("N$ 6903.40", 6903.40),
        ("N$ 22,910.00", 22910.00),
        ("N$ -150.00", -150.00),
        ("N$ 0.00", 0.0),
        ("7,394.94", 7394.94),
        ("-", 0.0),
        ("- 150.00", -150.00),
    ],
)
def test_money_formats_used_by_branches_parse(raw, expected):

    assert parse_money(raw) == expected


def test_unreadable_amount_is_returned_as_text_so_validation_reports_it():

    assert parse_money("N$ 1,2O4.5O") == "N$ 1,2O4.5O"


def test_day_first_dates_parse_when_the_source_declares_them():

    assert parse_date("21/09/2026", ["%d/%m/%Y"]) == date(2026, 9, 21)
    assert parse_date("2026-09-21") == date(2026, 9, 21)
    assert parse_date("21/09/2026") == "21/09/2026"  # not declared: left for validation to report


def test_values_are_found_right_of_labels_and_below_table_headers():

    fragments = [
        Fragment(0, 52.8, 689.5, "Site Code"),
        Fragment(0, 148.2, 689.5, "BR001"),
        # Table: one header wraps over two lines; the variance sign is printed apart from the number.
        Fragment(0, 148.2, 521.8, "Cash Removed"),
        Fragment(0, 246.7, 537.4, "Expected"),
        Fragment(0, 246.7, 522.2, "Closing Cash"),
        Fragment(0, 323.1, 521.8, "Variance"),
        Fragment(0, 200.0, 506.6, "7,200.00"),
        Fragment(0, 290.0, 506.6, "824.94"),
        Fragment(0, 380.0, 506.6, "150.00"),
        Fragment(0, 323.1, 506.6, "-"),
    ]

    assert layout_values(fragments, ["Site Code", "Cash Removed", "Expected Closing Cash", "Variance", "Absent"]) == {
        "Site Code": "BR001",
        "Cash Removed": "7,200.00",
        "Expected Closing Cash": "824.94",
        "Variance": "- 150.00",
    }


# ---------------------------------------------------------
# Staging schema and conversion
# ---------------------------------------------------------

CONTRACT = {
    "staging_table": "silver.Example",
    "structure": {"required_columns": ["id", "qty", "price", "day", "flag", "note"], "sql_types": {}},
    "data_rules": {
        "required_values": ["id", "qty"],
        "unique": ["id"],
        "numeric": {"qty": {"integer": True}, "price": {"minimum": 0}},
        "dates": ["day"],
        "allowed_values": {"flag": [True, False]},
    },
}


def test_sql_types_follow_the_validation_contract():

    assert sql_type("id", CONTRACT) == "NVARCHAR(50)"
    assert sql_type("qty", CONTRACT) == "INT"
    assert sql_type("price", CONTRACT) == "DECIMAL(19, 6)"
    assert sql_type("day", CONTRACT) == "DATE"
    assert sql_type("flag", CONTRACT) == "BIT"
    assert sql_type("note", CONTRACT) == "NVARCHAR(255)"


def test_required_values_are_not_null_and_unique_key_is_business_key():

    table = staging_table("example", CONTRACT)
    nullability = {c.name: c.nullable for c in table.columns}

    assert nullability["id"] is False and nullability["qty"] is False and nullability["note"] is True
    assert table.business_key == ("id",)


def test_value_conversion():

    assert convert(1010.0, "NVARCHAR(20)") == "1010"
    assert convert(3.0, "INT") == 3
    assert convert(0.035, "DECIMAL(19, 6)") == Decimal("0.035")
    assert convert("2026-01-05", "DATE") == date(2026, 1, 5)
    assert convert("  ", "NVARCHAR(50)") is None


# ---------------------------------------------------------
# Migrations
# ---------------------------------------------------------


def test_migrations_are_numbered_without_gaps():

    migrations = discover()

    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))


def test_migration_numbering_gap_is_rejected(tmp_path):

    (tmp_path / "001_a.sql").write_text("SELECT 1")
    (tmp_path / "003_c.sql").write_text("SELECT 1")

    with pytest.raises(MigrationError, match="gap"):
        discover(tmp_path)


def test_batches_split_on_go_lines_only():

    sql = "SELECT 'GO' AS x;\nGO\nSELECT 2\n  go  \nSELECT 3"

    assert split_batches(sql) == ["SELECT 'GO' AS x;", "SELECT 2", "SELECT 3"]


def test_retarget_redirects_database_statements():

    sql = "USE RetailDataPlatform;\nIF DB_ID(N'RetailDataPlatform') IS NULL CREATE DATABASE RetailDataPlatform;"
    out = retarget(sql, "RetailDataPlatform_Test")

    assert "USE [RetailDataPlatform_Test]" in out
    assert "DB_ID(N'RetailDataPlatform_Test')" in out
    assert "CREATE DATABASE [RetailDataPlatform_Test]" in out


# ---------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------


def test_tolerance_absolute_and_percent():

    assert within_tolerance(100.004, 100, 0.01, "ABSOLUTE")
    assert not within_tolerance(100.02, 100, 0.01, "ABSOLUTE")
    assert within_tolerance(101, 100, 1, "PERCENT")
    assert not within_tolerance(102, 100, 1, "PERCENT")
    assert not within_tolerance(None, 100, 1, "ABSOLUTE")


@pytest.mark.parametrize("name", ["gold.Fact;DROP TABLE x", "gold", "gold.Fact Sales", "[gold].[x]"])
def test_unsafe_identifiers_are_rejected_before_any_sql(name):

    with pytest.raises(ControlConfigurationError):
        quote_table(None, name)


# ---------------------------------------------------------
# Orchestrator eligibility
# ---------------------------------------------------------

REGISTRY = {
    "sources": {
        "site_master": {"path": "site_master.xlsx", "staging_table": "silver.Site"},
        "branch_cashup": {"path": "cashups/*.pdf", "staging_table": "silver.BranchCashup"},
    }
}


def context(decisions):
    return RunContext(1, REGISTRY, {"reference_sources": {}}, decisions)


def test_single_file_source_must_be_eligible():

    mapping = {"layer": "SILVER", "source": "site_master"}

    assert eligibility_block(mapping, context([{"source": "site_master", "decision": "ELIGIBLE"}])) is None
    reason = eligibility_block(
        mapping, context([{"source": "site_master", "decision": "BLOCKED", "reason": "Failed data validation"}])
    )
    assert "BLOCKED" in reason and "Failed data validation" in reason
    assert "No file version" in eligibility_block(mapping, context([]))


def test_document_set_loads_when_any_document_is_eligible():

    mapping = {"layer": "SILVER", "source": "branch_cashup"}
    decisions = [
        {"source": "branch_cashup", "decision": "BLOCKED", "reason": "x"},
        {"source": "branch_cashup", "decision": "ELIGIBLE"},
    ]

    assert eligibility_block(mapping, context(decisions)) is None


def test_warehouse_mappings_are_gated_by_dependencies_not_eligibility():

    assert eligibility_block({"layer": "GOLD", "source": "site_master"}, context([])) is None


# ---------------------------------------------------------
# Generated staging migration is in step with the registry
# ---------------------------------------------------------


def test_staging_migration_creates_every_registry_staging_table():

    sql = (Path(__file__).resolve().parents[1] / "sql" / "016_create_staging_tables.sql").read_text(encoding="utf-8")
    registry = __import__("common").load_registry_config()

    for source, config in registry["sources"].items():
        if config.get("staging_table") and config["staging_table"] != "silver.Supplier":
            assert f"CREATE TABLE {config['staging_table']} (" in sql, source
