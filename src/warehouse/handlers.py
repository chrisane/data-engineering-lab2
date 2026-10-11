"""
Approved execution handlers.

Metadata (meta.SourceTargetMapping / meta.ExecutionHandler) only ever
names a handler CODE. This module binds each code to exactly one
version-controlled implementation and the target it may write, so no
SQL or Python is ever taken from metadata.

    PYTHON            a function in this repository
    STORED_PROCEDURE  one fixed procedure, called with @PipelineExecutionID only
    RECONCILIATION    a control in reconciliation.py

The orchestrator refuses a mapping whose handler is not registered here,
not approved and active in metadata, or whose target does not match.
"""

import csv
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "src" / "validation"))

from db_connection import get_connection  # noqa: E402
from staging_loader import load_source  # noqa: E402


@dataclass
class HandlerResult:
    rows_read: int = 0
    rows_written: int = 0
    message: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Handler:
    code: str
    handler_type: str
    layers: frozenset[str]
    target: str | None = None  # fixed target table, or None = per source (staging)
    procedure: str | None = None
    run: Callable | None = None


class HandlerError(RuntimeError):
    """A handler failed; message is actionable."""


# ---------------------------------------------------------
# PYTHON HANDLERS
# ---------------------------------------------------------


def run_generic_staging(context) -> HandlerResult:
    """Stage every eligible file version of the mapping's source."""

    result = load_source(
        context.mapping["source"],
        context.decisions,
        context.registry,
        context.pipeline_id,
    )

    if not result.files:
        raise HandlerError(f"No file versions found for {context.mapping['source']}.")

    if result.failed:
        details = "; ".join(f"{f.file}: {f.message}" for f in result.failed[:3])
        raise HandlerError(f"{len(result.failed)} file(s) failed to stage: {details}")

    refused = [f for f in result.files if f.status == "SKIPPED" and f.message.startswith("Not eligible")]

    return HandlerResult(
        rows_read=result.rows_read,
        rows_written=result.rows_inserted,
        message=result.summary(),
        warnings=[f"{f.file}: {f.message}" for f in refused],
    )


def run_reference_accounts(context) -> HandlerResult:
    """
    Load config/chart_of_accounts.csv into stg.ChartOfAccounts.

    Reference data maintained under version control: the batch ID is
    derived from the file hash, so an unchanged file is never staged twice.
    The file is checked before loading (unique codes, required values,
    normal balance Debit/Credit).
    """

    path = ROOT / "config" / "chart_of_accounts.csv"
    digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    run_id = f"REF-{digest[:16]}"

    with open(path, newline="", encoding="utf-8-sig") as file:
        rows = [(number, row) for number, row in enumerate(csv.DictReader(file), start=2)]

    required = ["account_code", "account_name", "account_type", "financial_statement", "normal_balance"]
    problems = []
    seen = {}

    for number, row in rows:
        for column in required:
            if not (row.get(column) or "").strip():
                problems.append(f"row {number}: {column} is blank")
        code = (row.get("account_code") or "").strip()
        if code in seen:
            problems.append(f"row {number}: account_code {code} duplicates row {seen[code]}")
        seen[code] = number
        if row.get("normal_balance") not in ("Debit", "Credit"):
            problems.append(f"row {number}: normal_balance must be Debit or Credit")

    if problems:
        raise HandlerError(f"config/chart_of_accounts.csv is invalid: {'; '.join(problems[:5])}")

    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM stg.ChartOfAccounts WHERE IngestionRunID = ? AND SourceFileHash = ?",
            run_id,
            digest,
        )
        already = cursor.fetchone()[0]

        if already == 0:
            cursor.fast_executemany = True
            cursor.executemany(
                "INSERT INTO stg.ChartOfAccounts (account_code, account_name, account_type, "
                "financial_statement, normal_balance, IngestionRunID, SourceFile, SourceRowNumber, "
                "SourceFileHash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["account_code"].strip(),
                        r["account_name"].strip(),
                        r["account_type"].strip(),
                        r["financial_statement"].strip(),
                        r["normal_balance"].strip(),
                        run_id,
                        "config/chart_of_accounts.csv",
                        number,
                        digest,
                    )
                    for number, r in rows
                ],
            )
        elif already != len(rows):
            raise HandlerError(
                f"stg.ChartOfAccounts holds {already} rows of this version but the file has {len(rows)}."
            )

        # Record the reference batch so the warehouse procedure can find it.
        cursor.execute(
            "DELETE FROM audit.PipelineSourceBatch WHERE PipelineExecutionID = ? AND SourceName = ?",
            context.pipeline_id,
            "chart_of_accounts",
        )
        cursor.execute(
            "INSERT INTO audit.PipelineSourceBatch (PipelineExecutionID, SourceName, FileName, "
            "IngestionRunID, SourceFileHash, RawPath, Decision, Reason, IsCritical, ValidatedRowCount) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ELIGIBLE', ?, 1, ?)",
            context.pipeline_id,
            "chart_of_accounts",
            "chart_of_accounts.csv",
            run_id,
            digest,
            "config/chart_of_accounts.csv",
            "Version-controlled reference data; checked for required values, unique codes and normal balance.",
            len(rows),
        )
        cursor.execute(
            "INSERT INTO audit.LoadExecution (IngestionRunID, SourceName, SourceFile, SourceFileHash, "
            "LoadStatus, RowsRead, RowsInserted, RowsRejected, CompletedAt, ErrorMessage, "
            "PipelineExecutionID, StagingTable) VALUES (?, ?, ?, ?, ?, ?, ?, 0, SYSUTCDATETIME(), ?, ?, ?)",
            run_id,
            "chart_of_accounts",
            "config/chart_of_accounts.csv",
            digest,
            "COMPLETED" if already == 0 else "SKIPPED",
            len(rows),
            0 if already else len(rows),
            None if already == 0 else "Already staged; row count matches the file.",
            context.pipeline_id,
            "stg.ChartOfAccounts",
        )
        connection.commit()

    finally:
        connection.close()

    return HandlerResult(
        rows_read=len(rows),
        rows_written=0 if already else len(rows),
        message=(f"Chart of accounts {run_id}: {'already staged' if already else f'{len(rows)} rows staged'}"),
    )


def run_supplier_scd2(context) -> HandlerResult:
    """
    Existing supplier SCD2 procedure (dw.usp_LoadDimSupplier). Its own
    audit (audit.SupplierWarehouseExecution) is mirrored into
    audit.WarehouseExecution so it reconciles like every other target.
    """

    batch = context.single_batch("supplier_master")
    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM audit.LoadExecution WHERE IngestionRunID = ? AND SourceName = 'supplier_master' "
            "AND SourceFileHash = ? AND LoadStatus = 'COMPLETED'",
            batch["run_id"],
            batch["file_hash"],
        )
        if cursor.fetchone()[0] != 1:
            raise HandlerError(
                "The eligible supplier version lacks a unique completed staging load "
                "(audit.LoadExecution); the SCD2 procedure requires one."
            )
        connection.commit()

        cursor.execute("EXEC dw.usp_LoadDimSupplier @IngestionRunID = ?", batch["run_id"])
        while cursor.nextset():
            pass
        connection.commit()

        cursor.execute(
            "SELECT TOP 1 ExecutionID, Status, SourceRows, InsertedSuppliers, RevisedSuppliers, "
            "UnchangedSuppliers, ErrorMessage FROM audit.SupplierWarehouseExecution "
            "WHERE IngestionRunID = ? ORDER BY ExecutionID DESC",
            batch["run_id"],
        )
        row = cursor.fetchone()

        if row is None or row.Status not in ("COMPLETED", "SKIPPED"):
            raise HandlerError(f"Supplier SCD2 did not complete: {row.ErrorMessage if row else 'no audit row'}")

        cursor.execute(
            "INSERT INTO audit.WarehouseExecution (PipelineExecutionID, MappingCode, TargetTable, SourceName, "
            "IngestionRunID, SourceFileHash, ExecutionStatus, SourceRows, InsertedRows, UpdatedRows, "
            "UnchangedRows, CompletedAt) VALUES (?, ?, 'dw.DimSupplier', 'supplier_master', ?, ?, ?, ?, ?, ?, ?, "
            "SYSUTCDATETIME())",
            context.pipeline_id,
            context.mapping["code"],
            batch["run_id"],
            batch["file_hash"],
            row.Status,
            row.SourceRows,
            row.InsertedSuppliers,
            row.RevisedSuppliers,
            row.UnchangedSuppliers,
        )
        connection.commit()

        return HandlerResult(
            rows_read=row.SourceRows,
            rows_written=row.InsertedSuppliers + row.RevisedSuppliers,
            message=(
                f"Supplier SCD2 execution {row.ExecutionID}: {row.InsertedSuppliers} new, "
                f"{row.RevisedSuppliers} revised, {row.UnchangedSuppliers} unchanged"
            ),
        )

    finally:
        connection.close()


def run_procedure(context) -> HandlerResult:
    """Call the handler's fixed procedure with @PipelineExecutionID only."""

    handler = HANDLERS[context.mapping["handler"]]
    connection = get_connection()

    try:
        cursor = connection.cursor()

        if handler.code == "DIM_DATE":
            cursor.execute(f"EXEC {handler.procedure}")
        else:
            cursor.execute(f"EXEC {handler.procedure} @PipelineExecutionID = ?", context.pipeline_id)

        summary = None

        while True:
            if cursor.description:
                columns = [c[0] for c in cursor.description]
                rows = cursor.fetchall()
                if rows and "InsertedRows" in columns:
                    summary = dict(zip(columns, rows[0], strict=True))
            if not cursor.nextset():
                break

        connection.commit()

    finally:
        connection.close()

    if summary is None:
        return HandlerResult(message=f"{handler.procedure} completed.")

    written = (
        (summary.get("InsertedRows") or 0) + (summary.get("UpdatedRows") or 0) + (summary.get("SoftDeletedRows") or 0)
    )

    message = (
        f"{summary['SourceRows']} source rows: {summary['InsertedRows']} inserted, "
        f"{summary['UpdatedRows']} updated, {summary.get('SoftDeletedRows', 0)} soft-deleted"
    )

    warnings = []

    if summary.get("UnknownMemberRows"):
        warnings.append(f"{summary['UnknownMemberRows']} row(s) resolved to Unknown dimension members")

    return HandlerResult(summary["SourceRows"] or 0, written, message, warnings)


# ---------------------------------------------------------
# REGISTRY
# ---------------------------------------------------------


def _procedure(code: str, procedure: str, target: str) -> Handler:
    return Handler(code, "STORED_PROCEDURE", frozenset({"WAREHOUSE"}), target, procedure, run_procedure)


HANDLERS: dict[str, Handler] = {
    handler.code: handler
    for handler in [
        Handler("GENERIC_STAGING", "PYTHON", frozenset({"STAGING"}), None, None, run_generic_staging),
        Handler("SUPPLIER_STAGING", "PYTHON", frozenset({"STAGING"}), "stg.Supplier", None, run_generic_staging),
        Handler(
            "REFERENCE_ACCOUNTS", "PYTHON", frozenset({"STAGING"}), "stg.ChartOfAccounts", None, run_reference_accounts
        ),
        Handler("SUPPLIER_SCD2", "PYTHON", frozenset({"WAREHOUSE"}), "dw.DimSupplier", None, run_supplier_scd2),
        _procedure("DIM_DATE", "dw.usp_LoadDimDate", "dw.DimDate"),
        _procedure("DIM_SITE", "dw.usp_LoadDimSite", "dw.DimSite"),
        _procedure("DIM_DEPARTMENT", "dw.usp_LoadDimDepartment", "dw.DimDepartment"),
        _procedure("DIM_PRODUCT", "dw.usp_LoadDimProduct", "dw.DimProduct"),
        _procedure("DIM_CUSTOMER", "dw.usp_LoadDimCustomer", "dw.DimCustomer"),
        _procedure("DIM_ACCOUNT", "dw.usp_LoadDimAccount", "dw.DimAccount"),
        _procedure("DIM_ASSET", "dw.usp_LoadDimAsset", "dw.DimAsset"),
        _procedure("DIM_LOCAL_ASSET", "dw.usp_LoadDimLocalAsset", "dw.DimLocalAsset"),
        _procedure("FACT_SALES", "dw.usp_LoadFactSales", "dw.FactSales"),
        _procedure("FACT_SALES_LINE", "dw.usp_LoadFactSalesLine", "dw.FactSalesLine"),
        _procedure("FACT_INVENTORY_MOVEMENT", "dw.usp_LoadFactInventoryMovement", "dw.FactInventoryMovement"),
        _procedure("FACT_INVENTORY_SNAPSHOT", "dw.usp_BuildFactInventorySnapshot", "dw.FactInventorySnapshot"),
        _procedure("FACT_PURCHASE_ORDER", "dw.usp_LoadFactPurchaseOrder", "dw.FactPurchaseOrder"),
        _procedure("FACT_PURCHASE_ORDER_LINE", "dw.usp_LoadFactPurchaseOrderLine", "dw.FactPurchaseOrderLine"),
        _procedure("FACT_GOODS_RECEIPT", "dw.usp_LoadFactGoodsReceipt", "dw.FactGoodsReceipt"),
        _procedure("FACT_SUPPLIER_INVOICE", "dw.usp_LoadFactSupplierInvoice", "dw.FactSupplierInvoice"),
        _procedure("FACT_SUPPLIER_REBATE", "dw.usp_LoadFactSupplierRebate", "dw.FactSupplierRebate"),
        _procedure("FACT_GENERAL_LEDGER", "dw.usp_LoadFactGeneralLedger", "dw.FactGeneralLedger"),
        _procedure("FACT_OPERATING_EXPENSE", "dw.usp_LoadFactOperatingExpense", "dw.FactOperatingExpense"),
        _procedure("FACT_ASSET_DEPRECIATION", "dw.usp_BuildFactAssetDepreciation", "dw.FactAssetDepreciation"),
        _procedure("FACT_BRANCH_CASHUP", "dw.usp_LoadFactBranchCashup", "dw.FactBranchCashup"),
        _procedure("BRIDGE_PRODUCT_SUPPLIER", "dw.usp_LoadBridgeProductSupplier", "dw.BridgeProductSupplier"),
        Handler(
            "MART_REPLENISHMENT_PLAN",
            "STORED_PROCEDURE",
            frozenset({"MART"}),
            "mart.ReplenishmentPlan",
            "mart.usp_BuildReplenishmentPlan",
            run_procedure,
        ),
    ]
}

# Reconciliation handler codes; implementations live in reconciliation.py.
RECONCILIATION_HANDLERS = {
    "RECON_ROW_COUNT",
    "RECON_HASH_LINEAGE",
    "RECON_FRESHNESS",
    "RECON_WAREHOUSE_AUDIT",
    "RECON_KEY_COVERAGE",
    "RECON_MEASURE_TOTAL",
    "RECON_UNKNOWN_MEMBERS",
    "RECON_SCD2_INTEGRITY",
    "RECON_GL_BALANCE",
    "RECON_SUPPLIER",
}


def implementation_name(code: str) -> str:
    """Human-readable implementation reference recorded in meta.ExecutionHandler."""

    if code in RECONCILIATION_HANDLERS:
        return f"src/warehouse/reconciliation.py::{code}"

    handler = HANDLERS[code]

    if handler.procedure:
        return handler.procedure

    return f"src/warehouse/handlers.py::{handler.run.__name__}"
