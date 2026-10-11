"""
End-to-end integration tests against SQL Server.

Builds a throwaway database (RetailDataPlatform_IT) from the migrations,
synchronises the metadata and runs the metadata-driven pipeline on the
repository's validated data (data/raw + logs/), then checks schema,
lineage, idempotency, SCD2 history, fact corrections, reconciliation,
business rules, monitoring and security.

Run:   python -m pytest tests/integration -m integration -v
Needs: SQL Server reachable via .env, and the generate -> ingest ->
       validate -> eligibility steps run at least once.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

IT_DATABASE = "RetailDataPlatform_IT"
TECHNICAL_COLUMNS = {
    "IngestionRunID",
    "SourceFileHash",
    "SourceRowNumber",
    "RowHash",
    "IsDeletedInSource",
    "InsertedPipelineExecutionID",
    "UpdatedPipelineExecutionID",
    "InsertedAt",
    "UpdatedAt",
    "EffectiveFrom",
    "EffectiveTo",
    "IsCurrent",
    "RevisionNumber",
    "ChangeType",
    "RecordedAt",
    "RecordedBy",
    "PipelineExecutionID",
    "BuiltAt",
    "SourceFile",
}


@pytest.fixture(scope="session")
def database():

    try:
        from db_connection import get_connection

        get_connection("master").close()
    except Exception as error:
        pytest.skip(f"SQL Server not reachable: {error}")

    previous = os.environ.get("SQL_DATABASE")
    os.environ["SQL_DATABASE"] = IT_DATABASE

    from db_connection import get_connection
    from metadata_sync import plan, sync
    from migrations import apply

    master = get_connection("master", autocommit=True)
    master.cursor().execute(
        f"IF DB_ID('{IT_DATABASE}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{IT_DATABASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{IT_DATABASE}]; END"
    )
    master.close()

    apply(IT_DATABASE, verbose=False)

    connection = get_connection()
    sync(connection, plan())
    connection.close()

    yield get_connection

    if previous is None:
        os.environ.pop("SQL_DATABASE", None)
    else:
        os.environ["SQL_DATABASE"] = previous


@pytest.fixture(scope="session")
def first_run(database):

    from orchestrator import run_pipeline

    code = run_pipeline()
    cursor = database().cursor()
    pipeline_id = cursor.execute("SELECT MAX(PipelineExecutionID) FROM audit.PipelineExecution").fetchone()[0]

    return code, pipeline_id


def query(get_connection, sql, *params):

    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(sql, *params)
        return cursor.fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------
# Schema and metadata
# ---------------------------------------------------------


def test_every_migration_is_applied(database):

    from migrations import status

    assert {item["state"] for item in status(IT_DATABASE)} == {"APPLIED"}


def test_staging_tables_match_the_registry(database):

    from common import load_registry_config
    from staging_schema import all_staging_tables

    rows = query(
        database,
        """
        SELECT s.name + '.' + t.name, c.name, TYPE_NAME(c.user_type_id), c.max_length, c.precision, c.scale,
               c.is_nullable
        FROM sys.columns AS c JOIN sys.tables AS t ON t.object_id = c.object_id
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id WHERE s.name = 'stg'""",
    )
    live = {(r[0], r[1]): r for r in rows}

    for definition in all_staging_tables(load_registry_config()):
        for column in definition.columns:
            key = (definition.qualified_name, column.name)
            assert key in live, f"{key} missing from the database"
            if definition.qualified_name == "stg.Supplier":
                continue  # created before the registry-derived types (005)
            row = live[key]
            expected = column.sql_type.replace(" ", "").upper()
            actual = (
                f"NVARCHAR({row[3] // 2})"
                if row[2] == "nvarchar"
                else f"DECIMAL({row[4]},{row[5]})"
                if row[2] == "decimal"
                else row[2].upper()
            )
            assert actual == expected, f"{key}: {actual} != {expected}"
            assert bool(row[6]) == column.nullable, f"{key}: nullability"


def test_every_warehouse_column_has_lineage(database):

    columns = query(
        database,
        """
        SELECT s.name + '.' + t.name, c.name FROM sys.columns AS c
        JOIN sys.tables AS t ON t.object_id = c.object_id JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        WHERE s.name = 'dw'""",
    )
    documented = {(r[0], r[1]) for r in query(database, "SELECT TargetObject, TargetColumn FROM meta.ColumnLineage")}

    missing = [(t, c) for t, c in columns if c not in TECHNICAL_COLUMNS and (t, c) not in documented]

    assert not missing, f"Columns without lineage: {missing[:10]}"


# ---------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------


def test_first_run_completes_and_every_control_passes(database, first_run):

    code, pipeline_id = first_run
    statuses = dict(
        query(
            database,
            "SELECT ExecutionStatus, COUNT(*) FROM audit.MappingExecution "
            "WHERE PipelineExecutionID = ? GROUP BY ExecutionStatus",
            pipeline_id,
        )
    )
    failed_controls = query(
        database,
        "SELECT ControlCode, Details FROM audit.ReconciliationResult "
        "WHERE PipelineExecutionID = ? AND CheckStatus = 'FAIL' "
        "AND Severity IN ('CRITICAL', 'ERROR')",
        pipeline_id,
    )

    assert code in (0, 1)
    assert "FAILED" not in statuses
    assert statuses.get("COMPLETED", 0) >= 40
    assert failed_controls == []


def test_blocked_documents_are_not_loaded_and_are_reported(database, first_run):

    _, pipeline_id = first_run
    blocked = query(
        database,
        "SELECT FileName FROM audit.PipelineSourceBatch WHERE PipelineExecutionID = ? "
        "AND SourceName = 'branch_cashup' AND Decision = 'BLOCKED'",
        pipeline_id,
    )

    for (file_name,) in blocked:
        assert not query(database, "SELECT 1 FROM dw.FactBranchCashup WHERE SourceFile = ?", file_name)
        assert query(
            database,
            "SELECT 1 FROM audit.ExecutionError WHERE PipelineExecutionID = ? AND ErrorMessage LIKE ?",
            pipeline_id,
            f"{file_name}%",
        )


def test_reprocessing_unchanged_data_writes_nothing(database, first_run):

    from orchestrator import run_pipeline

    staged_before = query(database, "SELECT COUNT(*) FROM stg.GeneralLedger")[0][0]

    assert run_pipeline() in (0, 1)

    pipeline_id = query(database, "SELECT MAX(PipelineExecutionID) FROM audit.PipelineExecution")[0][0]
    changes = query(
        database,
        """
        SELECT ISNULL(SUM(w.InsertedRows + w.UpdatedRows + w.SoftDeletedRows), 0)
        FROM audit.WarehouseExecution AS w
        JOIN meta.SourceTargetMapping AS m ON m.MappingCode = w.MappingCode
        WHERE w.PipelineExecutionID = ? AND m.LoadStrategy <> 'FULL_REFRESH'""",
        pipeline_id,
    )[0][0]

    assert changes == 0
    assert query(database, "SELECT COUNT(*) FROM stg.GeneralLedger")[0][0] == staged_before
    assert query(database, "SELECT COUNT(*) FROM audit.FactChangeLog")[0][0] == 0


# ---------------------------------------------------------
# History and corrections (each in a rolled-back transaction)
# ---------------------------------------------------------


def new_test_batch(cursor, source, staging_table, columns, transform=None, drop_key=None):
    """
    Create a later batch of a source in staging (a corrected re-extract),
    and a pipeline execution that selects it. Returns the pipeline ID.
    """

    run_id, file_hash = "ING-29991231_000000", "f" * 64
    cursor.execute(
        "INSERT INTO audit.PipelineExecution (ExecutionStatus) OUTPUT INSERTED.PipelineExecutionID VALUES ('STARTED')"
    )
    pipeline_id = cursor.fetchone()[0]
    cursor.execute(f"SELECT TOP 1 IngestionRunID, SourceFileHash FROM {staging_table} ORDER BY StagingID DESC")
    latest_run, latest_hash = cursor.fetchone()
    column_list = ", ".join(f"[{c}]" for c in columns)
    cursor.execute(
        f"INSERT INTO {staging_table} ({column_list}, IngestionRunID, SourceFile, SourceRowNumber, SourceFileHash) "
        f"SELECT {column_list}, ?, 'test.xlsx', SourceRowNumber, ? FROM {staging_table} "
        f"WHERE IngestionRunID = ? AND SourceFileHash = ?",
        run_id,
        file_hash,
        latest_run,
        latest_hash,
    )
    if transform:
        cursor.execute(transform.format(table=staging_table), run_id)
    if drop_key:
        cursor.execute(drop_key.format(table=staging_table), run_id)
    cursor.execute(
        "INSERT INTO audit.PipelineSourceBatch (PipelineExecutionID, SourceName, FileName, IngestionRunID, "
        "SourceFileHash, Decision) VALUES (?, ?, 'test.xlsx', ?, ?, 'ELIGIBLE')",
        pipeline_id,
        source,
        run_id,
        file_hash,
    )
    return pipeline_id


def test_scd2_change_creates_a_new_version_and_keeps_history(database, first_run):

    connection = database()

    try:
        cursor = connection.cursor()
        pipeline_id = new_test_batch(
            cursor,
            "product_master",
            "stg.Product",
            ["product_id", "product_name", "category", "standard_cost", "selling_price", "dc_stocked"],
            transform="UPDATE {table} SET selling_price = selling_price + 10 "
            "WHERE IngestionRunID = ? AND product_id = 'P00001'",
        )
        cursor.execute("EXEC dw.usp_LoadDimProduct @PipelineExecutionID = ?", pipeline_id)
        while cursor.nextset():
            pass

        versions = cursor.execute(
            "SELECT RevisionNumber, IsCurrent, EffectiveTo, SellingPrice, ChangeType "
            "FROM dw.DimProduct WHERE ProductID = 'P00001' ORDER BY RevisionNumber"
        ).fetchall()
        assert [(v.RevisionNumber, bool(v.IsCurrent)) for v in versions] == [(1, False), (2, True)]
        assert versions[0].EffectiveTo is not None and versions[1].ChangeType == "UPDATE"
        assert versions[1].SellingPrice == versions[0].SellingPrice + 10

        bad = cursor.execute(
            "SELECT COUNT(*) FROM (SELECT ProductID FROM dw.DimProduct GROUP BY ProductID "
            "HAVING SUM(CAST(IsCurrent AS INT)) <> 1) AS x"
        ).fetchone()[0]
        assert bad == 0

    finally:
        connection.rollback()
        connection.close()


def test_fact_correction_and_removal_are_logged(database, first_run):

    connection = database()

    try:
        cursor = connection.cursor()
        pipeline_id = new_test_batch(
            cursor,
            "operating_expenses",
            "stg.OperatingExpense",
            ["expense_id", "expense_date", "site_id", "expense_type", "description", "amount"],
            transform="UPDATE {table} SET amount = amount + 1 WHERE IngestionRunID = ? AND expense_id = 'EXP000001'",
            drop_key="DELETE FROM {table} WHERE IngestionRunID = ? AND expense_id = 'EXP000002'",
        )
        cursor.execute("EXEC dw.usp_LoadFactOperatingExpense @PipelineExecutionID = ?", pipeline_id)
        while cursor.nextset():
            pass

        execution = cursor.execute(
            "SELECT UpdatedRows, SoftDeletedRows, WarehouseExecutionID FROM audit.WarehouseExecution "
            "WHERE PipelineExecutionID = ?",
            pipeline_id,
        ).fetchone()
        assert (execution.UpdatedRows, execution.SoftDeletedRows) == (1, 1)

        log = dict(
            cursor.execute(
                "SELECT BusinessKey, ChangeType FROM audit.FactChangeLog WHERE WarehouseExecutionID = ?",
                execution.WarehouseExecutionID,
            ).fetchall()
        )
        assert log == {"EXP000001": "UPDATE", "EXP000002": "SOFT_DELETE"}
        assert cursor.execute(
            "SELECT IsDeletedInSource FROM dw.FactOperatingExpense WHERE ExpenseID = 'EXP000002'"
        ).fetchone()[0]

    finally:
        connection.rollback()
        connection.close()


# ---------------------------------------------------------
# Reconciliation catches tampering; a re-run recovers
# ---------------------------------------------------------


def test_reconciliation_detects_missing_rows_and_rerun_recovers(database, first_run):

    from orchestrator import RunContext, load_plan
    from reconciliation import run_control

    connection = database()
    connection.cursor().execute("DELETE FROM dw.FactSales WHERE SaleID = (SELECT MIN(SaleID) FROM dw.FactSales)")
    connection.commit()
    connection.close()

    pipeline_id = query(
        database,
        "SELECT MAX(PipelineExecutionID) FROM audit.PipelineSourceBatch WHERE SourceName = 'sales_transactions'",
    )[0][0]
    plan, _ = load_plan()
    mapping = next(m for m in plan if m["code"] == "DW_FACT_SALES")
    context = RunContext(pipeline_id, {}, {}, [], mapping)
    outcomes = {
        c["name"]: run_control(c, context) for c in mapping["controls"] if c["name"] in ("KEY_COVERAGE", "TOTAL_AMOUNT")
    }

    assert outcomes["KEY_COVERAGE"].status == "FAIL" and outcomes["KEY_COVERAGE"].blocks
    assert outcomes["TOTAL_AMOUNT"].status == "FAIL"

    # Recovery: re-running the load restores the missing row (idempotent MERGE).
    connection = database()
    cursor = connection.cursor()
    cursor.execute("EXEC dw.usp_LoadFactSales @PipelineExecutionID = ?", pipeline_id)
    while cursor.nextset():
        pass
    connection.commit()
    connection.close()

    assert run_control(next(c for c in mapping["controls"] if c["name"] == "KEY_COVERAGE"), context).status == "PASS"


# ---------------------------------------------------------
# Business rules
# ---------------------------------------------------------


def test_financial_statements_balance(database, first_run):

    assert not query(
        database,
        "SELECT YearMonth FROM mart.vw_TrialBalance GROUP BY YearMonth "
        "HAVING ABS(SUM(ClosingBalanceDebitPositive)) > 0.01",
    )
    assert {r[0] for r in query(database, "SELECT DISTINCT Status FROM mart.vw_BalanceSheetCheck")} == {"BALANCED"}


def test_operational_subledgers_reconcile_to_the_ledger(database, first_run):

    differences = {
        r[0]
        for r in query(database, "SELECT DISTINCT Area FROM mart.vw_AccountReconciliation WHERE Status = 'DIFFERENCE'")
    }

    # Depreciation differs by design of the source data (documented finding).
    assert differences <= {"Depreciation (6300) vs asset depreciation schedule"}


def test_cashup_exceptions_are_detected(database, first_run):

    statuses = {
        (r[0], r[1].isoformat(), r[2])
        for r in query(
            database,
            "SELECT SiteID, BusinessDate, ReconciliationStatus FROM mart.vw_CashupReconciliation "
            "WHERE ReconciliationStatus <> 'BALANCED'",
        )
    }

    assert ("BR005", "2026-09-24", "MISSING_CASHUP") in statuses
    assert ("BR003", "2026-09-22", "CASH_SHORTAGE") in statuses
    assert ("BR007", "2026-09-25", "CASH_OVERAGE") in statuses
    assert ("BR012", "2026-09-27", "CASHUP_QUARANTINED") in statuses
    assert any(s[0] == "BR010" and s[1] == "2026-09-26" for s in statuses)


def test_purchase_orders_meet_minimum_order_values(database, first_run):

    assert {r[0] for r in query(database, "SELECT DISTINCT Status FROM mart.vw_PurchaseOrderCompliance")} == {
        "COMPLIANT"
    }


# ---------------------------------------------------------
# Monitoring and security
# ---------------------------------------------------------


def test_stalled_execution_is_abandoned(database, first_run):

    from orchestrator import abandon_stalled

    three_hours_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
    connection = database()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO audit.PipelineExecution (ExecutionStatus, StartedAt, HeartbeatAt) "
        "OUTPUT INSERTED.PipelineExecutionID VALUES ('STARTED', ?, ?)",
        three_hours_ago,
        three_hours_ago,
    )
    stale = cursor.fetchone()[0]
    connection.commit()
    connection.close()

    assert abandon_stalled(-1, 60) >= 1
    assert (
        query(database, "SELECT ExecutionStatus FROM audit.PipelineExecution WHERE PipelineExecutionID = ?", stale)[0][
            0
        ]
        == "ABANDONED"
    )


def test_live_run_is_never_abandoned_but_dead_run_is_at_once(database):
    """A run in a long load sends no heartbeat; its liveness lock proves it is alive."""

    from orchestrator import abandon_stalled, hold_run_lock, release_run_lock

    connection = database()
    cursor = connection.cursor()
    ids = []
    for _ in range(2):
        cursor.execute(
            "INSERT INTO audit.PipelineExecution (ExecutionStatus, StartedAt, HeartbeatAt) "
            "OUTPUT INSERTED.PipelineExecutionID VALUES ('STARTED', SYSUTCDATETIME(), SYSUTCDATETIME())"
        )
        ids.append(cursor.fetchone()[0])
    connection.commit()
    connection.close()

    live, dead = ids
    lock = hold_run_lock(live)

    try:
        abandon_stalled(-1, 0)  # threshold 0: silence alone never abandons
        status = dict(query(database, "SELECT PipelineExecutionID, ExecutionStatus FROM audit.PipelineExecution"))
        assert status[live] == "STARTED"
        assert status[dead] == "ABANDONED"  # no lock holder: process gone, no waiting period
    finally:
        release_run_lock(lock, live)

    abandon_stalled(-1, 60)
    assert (
        query(database, "SELECT ExecutionStatus FROM audit.PipelineExecution WHERE PipelineExecutionID = ?", live)[0][0]
        == "ABANDONED"
    )


def test_platform_health_view(database, first_run):

    checks = {r[0]: r[1] for r in query(database, "SELECT CheckName, HealthStatus FROM audit.vw_PlatformHealth")}

    assert {"LAST_PIPELINE_RUN", "STALLED_EXECUTIONS", "FAILED_CONTROLS_LAST_RUN", "STALE_SOURCES"} <= set(checks)


def test_personal_data_is_masked_for_report_readers(database, first_run):

    connection = database()

    try:
        cursor = connection.cursor()
        cursor.execute("IF USER_ID('it_report_reader') IS NULL CREATE USER it_report_reader WITHOUT LOGIN")
        cursor.execute("ALTER ROLE role_report_reader ADD MEMBER it_report_reader")
        connection.commit()

        real = cursor.execute(
            "SELECT TOP 1 CustomerName FROM mart.DimCustomer WHERE CustomerKey > 0 ORDER BY CustomerKey"
        ).fetchone()[0]
        cursor.execute("EXECUTE AS USER = 'it_report_reader'")
        masked = cursor.execute(
            "SELECT TOP 1 CustomerName FROM mart.DimCustomer WHERE CustomerKey > 0 ORDER BY CustomerKey"
        ).fetchone()[0]
        denied = None
        try:
            cursor.execute("SELECT TOP 1 * FROM stg.Customer")
        except Exception as error:
            denied = error
        cursor.execute("REVERT")

        assert masked != real and masked.endswith("xxxx")
        assert denied is not None, "report readers must not read staging"

    finally:
        connection.close()
