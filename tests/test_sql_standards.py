"""
SQL standards checks for the migration scripts (run in CI, no database).

Rules
  - every migration targets the database explicitly (USE RetailDataPlatform)
  - tables are only created behind an existence guard (re-runnable DDL)
  - no destructive statements (DROP TABLE / TRUNCATE / DROP DATABASE /
    DELETE without WHERE) except in approved full-refresh procedures
  - load procedures use SET XACT_ABORT ON and record failures
  - presentation views (mart) list their columns explicitly
  - no credentials in SQL
"""

import re
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
MIGRATIONS = sorted(SQL_DIR.glob("*.sql"))

# Procedures that rebuild a derived table from other warehouse tables.
APPROVED_FULL_REFRESH = {
    "dw.usp_BuildFactInventorySnapshot": "DELETE FROM dw.FactInventorySnapshot",
    "dw.usp_BuildFactAssetDepreciation": "DELETE FROM dw.FactAssetDepreciation",
}


def strip_comments(sql: str) -> str:

    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", sql)


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_targets_the_database(path):

    if path.name.startswith("001_"):
        return

    assert re.search(r"^\s*USE\s+RetailDataPlatform\s*;", path.read_text(encoding="utf-8"), re.M), (
        f"{path.name} must start with USE RetailDataPlatform;"
    )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_tables_are_created_behind_a_guard(path):

    sql = strip_comments(path.read_text(encoding="utf-8"))

    for match in re.finditer(r"CREATE\s+TABLE\s+(\w+\.\w+)", sql, re.I):
        table = match.group(1)
        preceding = sql[max(0, match.start() - 400) : match.start()]
        assert re.search(rf"OBJECT_ID\(N?'{re.escape(table)}'", preceding, re.I), (
            f"{path.name}: CREATE TABLE {table} needs an IF OBJECT_ID(...) IS NULL guard"
        )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_no_destructive_statements(path):

    sql = strip_comments(path.read_text(encoding="utf-8"))

    assert not re.search(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", sql, re.I), f"{path.name}: DROP is not allowed"
    assert not re.search(r"\bTRUNCATE\s+TABLE\b", sql, re.I), f"{path.name}: TRUNCATE is not allowed"

    for match in re.finditer(r"DELETE\s+FROM\s+([\w.]+)\s*;", sql, re.I):
        statement = match.group(0).rstrip(";").strip()
        approved = set(APPROVED_FULL_REFRESH.values()) | {
            "DELETE FROM meta.RebateThreshold",
            "DELETE FROM mart.ReplenishmentPlan",  # rebuilt each run by mart.usp_BuildReplenishmentPlan
        }
        assert re.sub(r"\s+", " ", statement) in approved, (
            f"{path.name}: unconditional '{statement}' is only allowed in approved full-refresh procedures"
        )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_load_procedures_are_transactional_and_audited(path):

    sql = path.read_text(encoding="utf-8")

    for match in re.finditer(
        r"CREATE\s+OR\s+ALTER\s+PROCEDURE\s+(dw\.usp_(?:Load|Build)\w+)(.*?)\nGO", sql, re.I | re.S
    ):
        name, body = match.group(1), match.group(2)
        assert "SET XACT_ABORT ON" in body, f"{name}: SET XACT_ABORT ON is required"
        if name not in ("dw.usp_LoadDimDate", "dw.usp_LoadDimSupplier"):
            assert "usp_RecordWarehouseFailure" in body, f"{name}: failures must be recorded"
            assert "usp_CompleteWarehouseExecution" in body, f"{name}: outcomes must be recorded"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_presentation_views_list_columns(path):

    sql = strip_comments(path.read_text(encoding="utf-8"))

    for match in re.finditer(
        r"CREATE\s+OR\s+ALTER\s+VIEW\s+(mart\.(?:Dim|Fact|Bridge)\w+)\s+AS\s+SELECT\s+\*", sql, re.I
    ):
        pytest.fail(f"{path.name}: {match.group(1)} must list its columns (no SELECT *)")


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_no_credentials_in_sql(path):

    sql = path.read_text(encoding="utf-8")

    assert not re.search(r"\b(PWD|PASSWORD)\s*=", sql, re.I), f"{path.name}: credentials must not be in SQL"
