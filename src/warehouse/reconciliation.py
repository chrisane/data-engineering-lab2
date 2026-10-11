"""
Reconciliation controls: post-load quality gates.

Each control type is version-controlled code with parameterised SQL.
Metadata (meta.ReconciliationControl) chooses which control runs after
which mapping, its severity, tolerance and parameters. Table and column
names from metadata are validated (identifier pattern + existence in the
database) and quoted before use, so metadata cannot inject SQL.

Every outcome is written to audit.ReconciliationResult. The orchestrator
decides what happens next from the severity:
    CRITICAL / ERROR (mandatory)  -> mapping FAILED, pipeline stops
    WARNING                       -> recorded, pipeline continues
"""

import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "src" / "validation"))

from common import read_manifest  # noqa: E402
from db_connection import get_connection  # noqa: E402
from staging_loader import sha256_file  # noqa: E402

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
NO_HASH = "0" * 64


@dataclass
class ControlOutcome:
    control_code: str
    status: str  # PASS / FAIL
    severity: str
    mandatory: bool
    details: str
    observed: float | int | None = None
    expected: float | int | None = None
    recommended_action: str | None = None

    @property
    def blocks(self) -> bool:
        return self.status == "FAIL" and self.mandatory and self.severity in ("CRITICAL", "ERROR")


class ControlConfigurationError(RuntimeError):
    pass


# ---------------------------------------------------------
# IDENTIFIER SAFETY
# ---------------------------------------------------------


def quote_table(cursor, qualified: str) -> str:

    parts = qualified.split(".")

    if len(parts) != 2 or not all(IDENTIFIER.match(p) for p in parts):
        raise ControlConfigurationError(f"Invalid table name in control parameters: {qualified!r}")

    cursor.execute("SELECT OBJECT_ID(?, 'U')", qualified)

    if cursor.fetchone()[0] is None:
        raise ControlConfigurationError(f"Table {qualified} referenced by a control does not exist.")

    return f"[{parts[0]}].[{parts[1]}]"


def quote_column(cursor, qualified_table: str, column: str) -> str:

    if not IDENTIFIER.match(column):
        raise ControlConfigurationError(f"Invalid column name in control parameters: {column!r}")

    cursor.execute("SELECT COL_LENGTH(?, ?)", qualified_table, column)

    if cursor.fetchone()[0] is None:
        raise ControlConfigurationError(f"Column {qualified_table}.{column} referenced by a control does not exist.")

    return f"[{column}]"


def within_tolerance(observed, expected, tolerance, tolerance_type) -> bool:

    if observed is None or expected is None:
        return False

    difference = abs(Decimal(str(observed)) - Decimal(str(expected)))
    tolerance = Decimal(str(tolerance or 0))

    if tolerance_type == "PERCENT":
        return difference <= abs(Decimal(str(expected))) * tolerance / 100

    return difference <= tolerance


# ---------------------------------------------------------
# BATCH HELPERS
# ---------------------------------------------------------


def eligible_batches(cursor, pipeline_id: int, source: str) -> list:

    cursor.execute(
        "SELECT FileName, IngestionRunID, SourceFileHash, RawPath, ValidatedRowCount "
        "FROM audit.PipelineSourceBatch WHERE PipelineExecutionID = ? AND SourceName = ? "
        "AND Decision = 'ELIGIBLE'",
        pipeline_id,
        source,
    )

    return cursor.fetchall()


def batch_filter(batches) -> tuple[str, list]:
    """WHERE fragment matching rows of any of the batches (parameterised)."""

    if not batches:
        return "1 = 0", []

    clauses = " OR ".join("(IngestionRunID = ? AND SourceFileHash = ?)" for _ in batches)
    params = [value for batch in batches for value in (batch.IngestionRunID, batch.SourceFileHash)]

    return f"({clauses})", params


# ---------------------------------------------------------
# CONTROLS
# ---------------------------------------------------------


def control_row_count(cursor, control, context) -> tuple:
    """Rows established by validation == rows in staging, per file version."""

    source = context.mapping["source"]
    table_name = context.mapping["target"]
    table = quote_table(cursor, table_name)
    batches = eligible_batches(cursor, context.pipeline_id, source)

    expected = observed = 0
    mismatches = []

    for batch in batches:
        cursor.execute(
            f"SELECT COUNT(*) FROM {table} WHERE IngestionRunID = ? AND SourceFileHash = ?",
            batch.IngestionRunID,
            batch.SourceFileHash,
        )
        staged = cursor.fetchone()[0]
        validated = batch.ValidatedRowCount

        expected += validated or 0
        observed += staged

        if validated is None or staged != validated:
            mismatches.append(f"{batch.FileName}: validated {validated}, staged {staged}")

    if not batches:
        return (
            "FAIL",
            0,
            None,
            f"No eligible batch of {source} recorded for this run.",
            "Check the load eligibility decision for the source.",
        )

    if mismatches:
        return (
            "FAIL",
            observed,
            expected,
            f"{len(mismatches)} file version(s) do not reconcile: {'; '.join(mismatches[:5])}",
            "Re-run staging for the file; if it persists, compare the RAW file with its validation.",
        )

    return (
        "PASS",
        observed,
        expected,
        f"{len(batches)} file version(s): {observed} staged rows equal {expected} validated rows.",
        None,
    )


def control_hash_lineage(cursor, control, context) -> tuple:
    """
    RAW evidence unchanged, manifest agrees with the gate, every staged
    row has file and row lineage, and every batch has a completed load audit.
    """

    source = context.mapping["source"]
    table = quote_table(cursor, context.mapping["target"])
    batches = eligible_batches(cursor, context.pipeline_id, source)
    manifest = {(row["run_id"], row["file_hash"]) for row in read_manifest() if row["status"] == "INGESTED"}
    problems = []

    for batch in batches:
        if batch.RawPath and not batch.RawPath.startswith("config/"):
            raw = (ROOT / batch.RawPath).resolve()

            if not raw.is_file():
                problems.append(f"{batch.FileName}: RAW file missing")
            elif sha256_file(raw) != batch.SourceFileHash:
                problems.append(f"{batch.FileName}: RAW file hash changed since ingestion")

            if (batch.IngestionRunID, batch.SourceFileHash) not in manifest:
                problems.append(f"{batch.FileName}: batch not found as INGESTED in the manifest")

        cursor.execute(
            f"SELECT COUNT(*) FROM {table} WHERE IngestionRunID = ? AND SourceFileHash = ? "
            f"AND (SourceFile IS NULL OR LTRIM(RTRIM(SourceFile)) = '' OR SourceRowNumber IS NULL)",
            batch.IngestionRunID,
            batch.SourceFileHash,
        )
        missing_lineage = cursor.fetchone()[0]

        if missing_lineage:
            problems.append(f"{batch.FileName}: {missing_lineage} staged row(s) lack file or row lineage")

        cursor.execute(
            "SELECT COUNT(*) FROM audit.LoadExecution WHERE IngestionRunID = ? AND SourceFileHash = ? "
            "AND SourceName = ? AND LoadStatus = 'COMPLETED'",
            batch.IngestionRunID,
            batch.SourceFileHash,
            source,
        )

        if cursor.fetchone()[0] != 1:
            problems.append(f"{batch.FileName}: no unique COMPLETED load audit for this version")

    if problems:
        return (
            "FAIL",
            len(problems),
            0,
            "; ".join(problems[:5]),
            "Investigate the RAW evidence and audit.LoadExecution; never load a changed or untraceable file.",
        )

    return "PASS", 0, 0, f"{len(batches)} version(s): hashes, manifest, lineage and load audit consistent.", None


def control_freshness(cursor, control, context) -> tuple:
    """Hours since new data last arrived vs the source's freshness threshold."""

    source = context.mapping["source"]

    cursor.execute("SELECT FreshnessThresholdHours FROM meta.DataSource WHERE SourceName = ?", source)
    row = cursor.fetchone()
    threshold = row[0] if row else None

    if threshold is None:
        return "PASS", None, None, "No freshness threshold applies (reference or generated data).", None

    ingested = [r["ingested_at"] for r in read_manifest() if r["source"] == source and r["status"] == "INGESTED"]

    if not ingested:
        return (
            "FAIL",
            None,
            threshold,
            "No data has ever been ingested for this source.",
            "Request the extract from the source owner.",
        )

    latest = max(datetime.fromisoformat(value) for value in ingested)
    age_hours = round((datetime.now() - latest).total_seconds() / 3600, 1)

    if age_hours <= threshold:
        return "PASS", age_hours, threshold, f"New data arrived {age_hours} h ago (threshold {threshold} h).", None

    return (
        "FAIL",
        age_hours,
        threshold,
        f"Latest new data arrived {age_hours} h ago ({latest:%Y-%m-%d %H:%M}); threshold is {threshold} h.",
        "Confirm with the data owner whether a new extract is due; reports may be out of date.",
    )


def latest_warehouse_execution(cursor, context):

    cursor.execute(
        "SELECT TOP 1 * FROM audit.WarehouseExecution WHERE PipelineExecutionID = ? AND MappingCode = ? "
        "ORDER BY WarehouseExecutionID DESC",
        context.pipeline_id,
        context.mapping["code"],
    )

    return cursor.fetchone()


def control_warehouse_audit(cursor, control, context) -> tuple:
    """The warehouse execution exists, succeeded and processed every staged row."""

    execution = latest_warehouse_execution(cursor, context)

    if execution is None:
        return (
            "FAIL",
            None,
            None,
            "No audit.WarehouseExecution row for this mapping in this run.",
            "The load procedure did not record its outcome; check the procedure.",
        )

    if execution.ExecutionStatus not in ("COMPLETED", "SKIPPED"):
        return (
            "FAIL",
            None,
            None,
            f"Warehouse execution status {execution.ExecutionStatus}: {execution.ErrorMessage}",
            "See the error message and audit.ExecutionError.",
        )

    if context.mapping["strategy"] == "FULL_REFRESH":
        return "PASS", execution.InsertedRows, None, f"Rebuilt {execution.InsertedRows} row(s).", None

    batches = eligible_batches(cursor, context.pipeline_id, context.mapping["source"])
    staging_table = context.staging_table_for(context.mapping["source"])
    table = quote_table(cursor, staging_table)
    where, params = batch_filter(batches)
    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", *params)
    staged = cursor.fetchone()[0]

    processed = execution.InsertedRows + execution.UpdatedRows + execution.UnchangedRows

    if execution.SourceRows != staged or processed != staged:
        return (
            "FAIL",
            processed,
            staged,
            (f"Warehouse read {execution.SourceRows} and accounted for {processed} row(s); staging holds {staged}."),
            "Rows were lost or duplicated between staging and the warehouse; investigate before reporting.",
        )

    return (
        "PASS",
        processed,
        staged,
        (
            f"{staged} staged row(s): {execution.InsertedRows} inserted, {execution.UpdatedRows} updated, "
            f"{execution.UnchangedRows} unchanged, {execution.SoftDeletedRows} soft-deleted."
        ),
        None,
    )


def control_key_coverage(cursor, control, context) -> tuple:
    """Every staged business key exists in the target (active / current)."""

    params = control["params"]
    staging_name, target_name = params["staging_table"], params["target_table"]
    staging = quote_table(cursor, staging_name)
    target = quote_table(cursor, target_name)
    staging_keys = [quote_column(cursor, staging_name, c) for c in params["staging_key"]]
    target_keys = [quote_column(cursor, target_name, c) for c in params["target_key"]]

    filters = {
        "active": "AND t.[IsDeletedInSource] = 0",
        "current": "AND t.[IsCurrent] = 1 AND t.[IsDeletedInSource] = 0",
        "all": "",
    }

    if params.get("target_filter", "active") not in filters:
        raise ControlConfigurationError(f"Unknown target_filter {params.get('target_filter')!r}")

    if len(target_keys) != len(staging_keys):
        raise ControlConfigurationError("staging_key and target_key must have the same number of columns.")

    join = " AND ".join(f"t.{tk} = s.{sk}" for tk, sk in zip(target_keys, staging_keys, strict=True))
    batches = eligible_batches(cursor, context.pipeline_id, context.mapping["source"])
    where, values = batch_filter(batches)

    # The batch filter applies to the staging rows (only s is in scope there).
    cursor.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join('s.' + k for k in staging_keys)} "
        f"FROM {staging} AS s WHERE {where} "
        f"AND NOT EXISTS (SELECT 1 FROM {target} AS t WHERE {join} "
        f"{filters[params.get('target_filter', 'active')]})) AS missing",
        *values,
    )
    missing = cursor.fetchone()[0]

    if missing:
        return (
            "FAIL",
            missing,
            0,
            f"{missing} staged business key(s) are missing from {target_name}.",
            f"Re-run the {context.mapping['code']} load and check its procedure for filtered rows.",
        )

    return "PASS", 0, 0, f"Every staged key of {staging_name} is present in {target_name}.", None


def control_measure_total(cursor, control, context) -> tuple:
    """Staged total of a measure == warehouse total of active rows (within tolerance)."""

    params = control["params"]
    staging_name, target_name = params["staging_table"], params["target_table"]
    staging = quote_table(cursor, staging_name)
    target = quote_table(cursor, target_name)
    staging_measure = quote_column(cursor, staging_name, params["staging_measure"])
    target_measure = quote_column(cursor, target_name, params["target_measure"])

    batches = eligible_batches(cursor, context.pipeline_id, context.mapping["source"])
    where, values = batch_filter(batches)

    cursor.execute(
        f"SELECT CONVERT(DECIMAL(38, 6), ISNULL(SUM({staging_measure}), 0)) FROM {staging} WHERE {where}", *values
    )
    expected = cursor.fetchone()[0]

    cursor.execute(
        f"SELECT CONVERT(DECIMAL(38, 6), ISNULL(SUM({target_measure}), 0)) FROM {target} WHERE [IsDeletedInSource] = 0"
    )
    observed = cursor.fetchone()[0]

    if within_tolerance(observed, expected, control.get("tolerance"), control.get("tolerance_type")):
        return (
            "PASS",
            float(observed),
            float(expected),
            f"{target_name}.{params['target_measure']} total {observed:,.2f} agrees with staging {expected:,.2f}.",
            None,
        )

    return (
        "FAIL",
        float(observed),
        float(expected),
        (
            f"{target_name}.{params['target_measure']} total {observed:,.2f} differs from staging "
            f"{expected:,.2f} by {observed - expected:,.2f} (tolerance {control.get('tolerance') or 0})."
        ),
        "Find the rows that differ (key coverage, soft deletes, conversion) before the data is reported.",
    )


def control_unknown_members(cursor, control, context) -> tuple:

    execution = latest_warehouse_execution(cursor, context)

    if execution is None:
        return "FAIL", None, 0, "No warehouse execution recorded for this mapping.", None

    tolerance = control.get("tolerance") or 0

    if execution.UnknownMemberRows <= tolerance:
        return "PASS", execution.UnknownMemberRows, 0, "Every dimension key resolved.", None

    return (
        "FAIL",
        execution.UnknownMemberRows,
        0,
        (
            f"{execution.UnknownMemberRows} row(s) of {context.mapping['target']} reference a dimension "
            f"member that does not exist (mapped to Unknown, -1)."
        ),
        "Load the missing master data (late-arriving dimension) or correct the source keys, then re-run.",
    )


def control_scd2_integrity(cursor, control, context) -> tuple:

    params = control["params"]
    target_name = params["target_table"]
    target = quote_table(cursor, target_name)
    key = quote_column(cursor, target_name, params["target_key"])

    cursor.execute(
        f"SELECT COUNT(*) FROM (SELECT {key} FROM {target} GROUP BY {key} "
        f"HAVING SUM(CASE WHEN [IsCurrent] = 1 THEN 1 ELSE 0 END) <> 1) AS x"
    )
    bad_current = cursor.fetchone()[0]

    cursor.execute(
        f"SELECT COUNT(*) FROM (SELECT {key}, [EffectiveFrom], LAG([EffectiveTo]) OVER "
        f"(PARTITION BY {key} ORDER BY [EffectiveFrom], [RevisionNumber]) AS PreviousTo, "
        f"ROW_NUMBER() OVER (PARTITION BY {key} ORDER BY [EffectiveFrom], [RevisionNumber]) AS rn "
        f"FROM {target}) AS h WHERE h.rn > 1 AND (h.PreviousTo IS NULL OR h.PreviousTo > h.[EffectiveFrom])"
    )
    overlaps = cursor.fetchone()[0]

    if bad_current or overlaps:
        return (
            "FAIL",
            bad_current + overlaps,
            0,
            f"{bad_current} key(s) without exactly one current version; {overlaps} overlapping period(s).",
            "History is corrupt; stop reporting from this dimension and repair it under change control.",
        )

    return "PASS", 0, 0, "One current version per key; no overlapping history.", None


def control_gl_balance(cursor, control, context) -> tuple:

    tolerance = Decimal(str(control.get("tolerance") or 0))

    cursor.execute(
        "SELECT COUNT(*) FROM (SELECT JournalID FROM dw.FactGeneralLedger WHERE IsDeletedInSource = 0 "
        "GROUP BY JournalID HAVING ABS(SUM(DebitAmount) - SUM(CreditAmount)) > ?) AS x",
        tolerance,
    )
    unbalanced = cursor.fetchone()[0]

    cursor.execute(
        "SELECT ISNULL(SUM(DebitAmount), 0), ISNULL(SUM(CreditAmount), 0) "
        "FROM dw.FactGeneralLedger WHERE IsDeletedInSource = 0"
    )
    debit, credit = cursor.fetchone()

    if unbalanced or abs(debit - credit) > tolerance:
        return (
            "FAIL",
            unbalanced,
            0,
            f"{unbalanced} unbalanced journal(s); ledger debits {debit:,.2f} vs credits {credit:,.2f}.",
            "Do not publish financial statements; trace the journals with scripts/trace_file.py general_ledger.",
        )

    return "PASS", 0, 0, f"All journals balance; ledger debits = credits = {debit:,.2f}.", None


def control_supplier(cursor, control, context) -> tuple:
    """The existing supplier reconciliation procedure (five checks)."""

    batch = context.single_batch("supplier_master")
    started = datetime.now(UTC).replace(tzinfo=None)

    cursor.execute(
        "EXEC audit.usp_ReconcileSupplier @IngestionRunID = ?, @SourceFileHash = ?, @FailOnError = 0",
        batch["run_id"],
        batch["file_hash"],
    )
    checks = cursor.fetchall()

    while cursor.nextset():
        pass

    # Link the procedure's own result rows to this pipeline run and control.
    cursor.execute(
        "UPDATE audit.ReconciliationResult SET PipelineExecutionID = ?, MappingID = ?, "
        "ControlCode = CONCAT(?, '.', CheckCode), Severity = ?, SourceName = 'supplier_master', "
        "TargetObject = 'dw.DimSupplier' WHERE PipelineExecutionID IS NULL AND IngestionRunID = ? "
        "AND SourceFileHash = ? AND CheckedAt >= ?",
        context.pipeline_id,
        context.mapping["id"],
        control["code"],
        control["severity"],
        batch["run_id"],
        batch["file_hash"],
        started,
    )

    failed = [c for c in checks if c.CheckStatus == "FAIL"]

    if failed:
        return (
            "FAIL",
            len(failed),
            0,
            "Failed supplier checks: " + ", ".join(c.CheckCode for c in failed),
            "See audit.ReconciliationResult for the supplier checks.",
        )

    return "PASS", 0, 0, f"All {len(checks)} supplier reconciliation checks passed.", None


CONTROLS = {
    "RECON_ROW_COUNT": control_row_count,
    "RECON_HASH_LINEAGE": control_hash_lineage,
    "RECON_FRESHNESS": control_freshness,
    "RECON_WAREHOUSE_AUDIT": control_warehouse_audit,
    "RECON_KEY_COVERAGE": control_key_coverage,
    "RECON_MEASURE_TOTAL": control_measure_total,
    "RECON_UNKNOWN_MEMBERS": control_unknown_members,
    "RECON_SCD2_INTEGRITY": control_scd2_integrity,
    "RECON_GL_BALANCE": control_gl_balance,
    "RECON_SUPPLIER": control_supplier,
}


# ---------------------------------------------------------
# RUNNER
# ---------------------------------------------------------


def run_control(control: dict, context) -> ControlOutcome:
    """Run one control and record its result."""

    implementation = CONTROLS.get(control["handler"])

    if implementation is None:
        raise ControlConfigurationError(f"Reconciliation handler {control['handler']} is not implemented.")

    connection = get_connection()

    try:
        cursor = connection.cursor()

        try:
            status, observed, expected, details, action = implementation(cursor, control, context)
        except ControlConfigurationError:
            raise
        except Exception as error:
            status, observed, expected = "FAIL", None, None
            details = f"Control could not run: {type(error).__name__}: {error}"
            action = "Fix the control or the data it reads; an unverified load is treated as failed."

        connection.commit()

        batch = context.first_batch(context.mapping["source"])
        is_count = isinstance(observed, int) and isinstance(expected, (int, type(None)))

        cursor.execute(
            "INSERT INTO audit.ReconciliationResult (IngestionRunID, SourceFileHash, CheckCode, CheckStatus, "
            "ObservedValue, ExpectedValue, Details, PipelineExecutionID, MappingID, ControlCode, Severity, "
            "SourceName, TargetObject, ObservedAmount, ExpectedAmount, Tolerance, RecommendedAction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch or {}).get("run_id") or "N/A",
            (batch or {}).get("file_hash") or NO_HASH,
            control["name"][:80],
            status,
            observed if is_count else None,
            expected if is_count else None,
            details[:1000],
            context.pipeline_id,
            context.mapping["id"],
            control["code"],
            control["severity"],
            context.mapping["source"],
            context.mapping["target"],
            None if is_count or observed is None else Decimal(str(observed)),
            None if is_count or expected is None else Decimal(str(expected)),
            control.get("tolerance"),
            action,
        )
        connection.commit()

    finally:
        connection.close()

    return ControlOutcome(
        control["code"], status, control["severity"], control["mandatory"], details, observed, expected, action
    )


def parse_parameters(raw: str | None) -> dict:

    return json.loads(raw) if raw else {}
