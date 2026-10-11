"""
Metadata-driven SQL orchestrator.

Runs after ingestion, validation, coverage and the load-eligibility gate.
What runs, in which order, with which approved handler and which
reconciliation controls, comes from the meta schema (synchronised from
config/pipeline.yaml by scripts/sync_metadata.py).

For each pipeline execution:
  1. Detect stalled executions (STARTED, no heartbeat) and mark them ABANDONED.
  2. Re-evaluate the load-eligibility gate and record the exact file version
     selected per source (audit.PipelineSourceBatch).
  3. Load validation outcomes for those versions into SQL (data-quality reporting).
  4. Build and validate the plan: approved, registered handlers; targets that
     match the handler; explicit dependencies without cycles.
  5. Execute mappings in dependency order. A mapping is BLOCKED (recorded, with
     the reason) when its source is not eligible or a dependency did not complete.
     Transient database errors are retried per the mapping's retry policy.
  6. After each mapping, run its reconciliation controls. A failed mandatory
     CRITICAL/ERROR control fails the mapping; WARNING controls are recorded.
  7. On a failure the pipeline stops; every remaining mapping is recorded SKIPPED.

Final status: COMPLETED, PARTIAL (some mappings blocked, none failed),
FAILED or CANCELLED. Exit codes: 0, 1, 2 and 130 respectively.

Every transition is committed to the audit schema as it happens, so a
crash or a forced stop leaves an accurate record (completed by stalled-run
detection on the next run).
"""

from __future__ import annotations

import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "src" / "validation"))

from common import (  # noqa: E402
    FILE_STATUS_LOG_FILE,
    VALIDATION_LOG_FILE,
    load_registry_config,
    read_log,
    read_manifest,
)
from db_connection import get_connection, is_transient  # noqa: E402
from handlers import HANDLERS, HandlerError  # noqa: E402
from load_eligibility import decide, is_document_set  # noqa: E402
from metadata_sync import config_checksum, load_yaml  # noqa: E402
from reconciliation import parse_parameters, run_control  # noqa: E402

# ---------------------------------------------------------
# AUDIT WRITES (each in its own committed transaction)
# ---------------------------------------------------------


def execute_audit(sql: str, *params, fetch: bool = False):

    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(sql, *params)
        value = cursor.fetchone()[0] if fetch else None
        connection.commit()
        return value
    finally:
        connection.close()


def record_error(
    pipeline_id,
    stage,
    severity,
    category,
    message,
    mapping=None,
    mapping_execution_id=None,
    source=None,
    source_file=None,
    run_id=None,
    rule=None,
    actual=None,
    expected=None,
    impact=None,
    action=None,
    technical=None,
    row_number=None,
    column=None,
    sheet=None,
):

    execute_audit(
        "INSERT INTO audit.ExecutionError (PipelineExecutionID, MappingExecutionID, MappingCode, Stage, Severity, "
        "ErrorCategory, SourceName, SourceFile, IngestionRunID, SheetName, RowNumber, ColumnName, RuleCode, "
        "ActualValue, ExpectedValue, BusinessImpact, RecommendedAction, ErrorMessage, TechnicalDetail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        pipeline_id,
        mapping_execution_id,
        (mapping or {}).get("code"),
        stage,
        severity,
        category,
        source,
        source_file,
        run_id,
        sheet,
        row_number,
        column,
        rule,
        None if actual is None else str(actual)[:1000],
        None if expected is None else str(expected)[:1000],
        impact,
        action,
        str(message)[:4000],
        None if technical is None else str(technical)[:4000],
    )


# ---------------------------------------------------------
# CONTEXT
# ---------------------------------------------------------


@dataclass
class RunContext:
    pipeline_id: int
    registry: dict
    pipeline_config: dict
    decisions: list[dict]
    mapping: dict = field(default_factory=dict)

    def source_decisions(self, source: str) -> list[dict]:
        return [d for d in self.decisions if d["source"] == source]

    def eligible(self, source: str) -> list[dict]:
        return [d for d in self.source_decisions(source) if d.get("decision") == "ELIGIBLE"]

    def single_batch(self, source: str) -> dict:
        eligible = self.eligible(source)
        if len(eligible) != 1:
            raise HandlerError(f"Expected exactly one eligible {source} file version; found {len(eligible)}.")
        return eligible[0]

    def first_batch(self, source: str) -> dict | None:
        eligible = self.eligible(source)
        return eligible[0] if eligible else None

    def staging_table_for(self, source: str) -> str:
        if source in self.registry["sources"]:
            return self.registry["sources"][source]["staging_table"]
        return self.pipeline_config["reference_sources"][source]["staging_table"]


# ---------------------------------------------------------
# PREPARATION
# ---------------------------------------------------------


def abandon_stalled(pipeline_id: int, stalled_after_minutes: int) -> int:
    """Mark executions that stopped reporting as ABANDONED (with a reason)."""

    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT PipelineExecutionID, StartedAt, HeartbeatAt FROM audit.PipelineExecution "
            "WHERE ExecutionStatus = 'STARTED' AND PipelineExecutionID <> ? "
            "AND DATEDIFF(MINUTE, COALESCE(HeartbeatAt, StartedAt), SYSUTCDATETIME()) >= ?",
            pipeline_id,
            stalled_after_minutes,
        )
        stalled = cursor.fetchall()

        for row in stalled:
            message = (
                f"No heartbeat for at least {stalled_after_minutes} minutes since "
                f"{(row.HeartbeatAt or row.StartedAt):%Y-%m-%d %H:%M} UTC; the process was stopped or crashed."
            )
            cursor.execute(
                "UPDATE audit.MappingExecution SET ExecutionStatus = 'ABANDONED', CompletedAt = SYSUTCDATETIME(), "
                "OutcomeMessage = ? WHERE PipelineExecutionID = ? AND ExecutionStatus = 'STARTED'",
                message,
                row.PipelineExecutionID,
            )
            cursor.execute(
                "UPDATE audit.PipelineExecution SET ExecutionStatus = 'ABANDONED', CompletedAt = SYSUTCDATETIME(), "
                "ErrorMessage = ? WHERE PipelineExecutionID = ?",
                message,
                row.PipelineExecutionID,
            )
            cursor.execute(
                "INSERT INTO audit.ExecutionError (PipelineExecutionID, Stage, Severity, ErrorCategory, ErrorMessage, "
                "BusinessImpact, RecommendedAction) VALUES (?, 'ORCHESTRATION', 'WARNING', 'ABANDONED', ?, ?, ?)",
                row.PipelineExecutionID,
                message,
                "Targets of the abandoned run may be partly refreshed; each load is transactional, "
                "so no target is half-written.",
                "No repair needed: re-run the pipeline. Loads are idempotent and resume from the last committed state.",
            )

        connection.commit()
        return len(stalled)

    finally:
        connection.close()


def as_datetime(value: str | None) -> datetime | None:
    """ISO timestamps from the CSV logs as datetime objects (for bulk inserts)."""

    return datetime.fromisoformat(value) if value else None


def validated_row_counts(decisions: list[dict]) -> dict:
    """rows_checked per (validation id, file) from the validation status log."""

    counts = {}

    for row in read_log(FILE_STATUS_LOG_FILE):
        if row.get("validation_id") and row.get("rows_checked") not in (None, ""):
            counts[(row["validation_id"], row["file"])] = int(float(row["rows_checked"]))

    return counts


def record_source_batches(pipeline_id: int, decisions: list[dict]) -> None:

    counts = validated_row_counts(decisions)
    connection = get_connection()

    try:
        cursor = connection.cursor()
        rows = []

        for decision in decisions:
            file_name = decision.get("file") or "(none)"
            validated = counts.get((decision.get("data_validation_id"), file_name)) or counts.get(
                (decision.get("structure_validation_id"), file_name)
            )

            rows.append(
                (
                    pipeline_id,
                    decision["source"],
                    file_name,
                    decision.get("run_id"),
                    decision.get("file_hash"),
                    decision.get("raw_path"),
                    decision["decision"],
                    (decision.get("reason") or "")[:2000],
                    1 if decision.get("critical") else 0,
                    validated,
                    decision.get("structure_validation_id"),
                    decision.get("data_validation_id"),
                    as_datetime(decision.get("ingested_at")),
                )
            )

        cursor.fast_executemany = True
        cursor.executemany(
            "INSERT INTO audit.PipelineSourceBatch (PipelineExecutionID, SourceName, FileName, IngestionRunID, "
            "SourceFileHash, RawPath, Decision, Reason, IsCritical, ValidatedRowCount, StructureValidationID, "
            "DataValidationID, IngestedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()

    finally:
        connection.close()


def load_validation_outcomes(pipeline_id: int, decisions: list[dict]) -> int:
    """
    Copy the validation outcome of every selected file version (and its
    failed checks) into SQL, once per validation ID, for data-quality
    reporting and actionable error queries.
    """

    wanted = {}

    for decision in decisions:
        for key in ("structure_validation_id", "data_validation_id"):
            if decision.get(key):
                wanted[(decision[key], decision.get("file"))] = decision

    if not wanted:
        return 0

    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DISTINCT ValidationID FROM audit.ValidationOutcome")
        loaded = {row[0] for row in cursor.fetchall()}

        outcomes = [
            row
            for row in read_log(FILE_STATUS_LOG_FILE)
            if (row.get("validation_id"), row["file"]) in wanted and row["validation_id"] not in loaded
        ]

        if not outcomes:
            return 0

        cursor.fast_executemany = True
        cursor.executemany(
            "INSERT INTO audit.ValidationOutcome (ValidationID, IngestionRunID, SourceName, FileName, Stage, "
            "ValidationStatus, RowsChecked, Errors, Warnings, QuarantinePath, DataSteward, ValidatedAt, "
            "FirstSeenPipelineExecutionID) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r["validation_id"],
                    r["run_id"],
                    r["source"],
                    r["file"],
                    r["stage"],
                    r["status"],
                    int(float(r["rows_checked"])) if r.get("rows_checked") else None,
                    int(r["errors"] or 0),
                    int(r["warnings"] or 0),
                    r.get("quarantine_path") or None,
                    r.get("data_steward") or None,
                    as_datetime(r["validated_at"]),
                    pipeline_id,
                )
                for r in outcomes
            ],
        )

        new_ids = {r["validation_id"] for r in outcomes}
        failures = [
            r
            for r in read_log(VALIDATION_LOG_FILE)
            if r.get("validation_id") in new_ids and r["status"] == "FAIL" and (r["validation_id"], r["file"]) in wanted
        ]

        if failures:
            cursor.executemany(
                "INSERT INTO audit.ValidationFailure (ValidationID, IngestionRunID, SourceName, FileName, SheetName, "
                "RowNumber, RecordKey, Stage, RuleCode, FieldName, Severity, ActualValue, ExpectedValue, Message, "
                "RecommendedAction, ErrorType, TechnicalError) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["validation_id"],
                        r["run_id"],
                        r["source"],
                        r["file"],
                        r.get("sheet") or None,
                        int(float(r["row_number"])) if r.get("row_number") else None,
                        r.get("record_key") or None,
                        r["stage"],
                        r["rule"],
                        r.get("field") or None,
                        r.get("severity") or "ERROR",
                        (r.get("actual") or None) and r["actual"][:1000],
                        (r.get("expected") or None) and r["expected"][:1000],
                        (r.get("message") or "")[:2000],
                        (r.get("recommended_action") or None),
                        r.get("error_type") or None,
                        (r.get("technical_error") or None),
                    )
                    for r in failures
                ],
            )

        connection.commit()
        return len(outcomes)

    finally:
        connection.close()


# ---------------------------------------------------------
# PLAN
# ---------------------------------------------------------

PLAN_SQL = """
SELECT m.MappingID, m.MappingCode, s.SourceName, m.TargetSchema, m.TargetTable, m.LoadLayer, m.LoadStrategy,
       m.ExecutionOrder, m.MaxRetries, m.RetryDelaySeconds,
       h.HandlerID, h.HandlerCode, h.HandlerType, h.IsApproved, h.IsActive, e.StopOnFailure
FROM meta.SourceTargetMapping AS m
JOIN meta.DataSource AS s ON s.SourceID = m.SourceID
LEFT JOIN meta.MappingExecution AS e ON e.MappingID = m.MappingID
LEFT JOIN meta.ExecutionHandler AS h ON h.HandlerID = e.HandlerID
WHERE m.IsEnabled = 1
"""

CONTROLS_SQL = """
SELECT c.ControlCode, c.MappingID, h.HandlerCode, h.IsApproved, h.IsActive, c.Severity, c.IsMandatory,
       c.Tolerance, c.ToleranceType, c.ParametersJson
FROM meta.ReconciliationControl AS c
JOIN meta.ExecutionHandler AS h ON h.HandlerID = c.HandlerID
WHERE c.IsEnabled = 1
"""


class PlanError(RuntimeError):
    pass


def load_plan() -> tuple[list[dict], dict[int, set[int]]]:

    connection = get_connection()

    try:
        cursor = connection.cursor()
        rows = cursor.execute(PLAN_SQL).fetchall()
        dependencies = [
            (int(a), int(b))
            for a, b in cursor.execute("SELECT MappingID, DependsOnMappingID FROM meta.MappingDependency").fetchall()
        ]
        control_rows = cursor.execute(CONTROLS_SQL).fetchall()
    finally:
        connection.close()

    if not rows:
        raise PlanError("No enabled mappings in meta.SourceTargetMapping. Run scripts/sync_metadata.py.")

    mappings, problems = {}, []

    for row in rows:
        label = row.MappingCode or f"mapping {row.MappingID}"
        target = f"{row.TargetSchema}.{row.TargetTable}"
        handler = HANDLERS.get(row.HandlerCode)

        if row.HandlerID is None or not row.IsApproved or not row.IsActive:
            problems.append(f"{label}: no approved, active execution handler.")
        elif handler is None:
            problems.append(f"{label}: handler {row.HandlerCode} is not in the approved handler registry.")
        elif row.HandlerType != handler.handler_type:
            problems.append(
                f"{label}: handler {row.HandlerCode} type mismatch ({row.HandlerType} vs {handler.handler_type})."
            )
        elif row.LoadLayer not in handler.layers or (handler.target and handler.target != target):
            problems.append(f"{label}: handler {row.HandlerCode} is not approved for {row.LoadLayer} {target}.")

        if row.StopOnFailure != 1:
            problems.append(f"{label}: StopOnFailure = 0 is not supported (failures must stop the pipeline).")

        mappings[int(row.MappingID)] = {
            "id": int(row.MappingID),
            "code": row.MappingCode,
            "source": row.SourceName,
            "target": target,
            "layer": row.LoadLayer,
            "strategy": row.LoadStrategy,
            "order": int(row.ExecutionOrder),
            "handler": row.HandlerCode,
            "handler_id": row.HandlerID,
            "max_retries": int(row.MaxRetries or 0),
            "retry_delay": int(row.RetryDelaySeconds or 0),
            "controls": [],
        }

    for control in control_rows:
        mapping = mappings.get(int(control.MappingID))

        if mapping is None:
            continue

        if not control.IsApproved or not control.IsActive:
            problems.append(
                f"Control {control.ControlCode}: reconciliation handler {control.HandlerCode} is not approved."
            )

        mapping["controls"].append(
            {
                "code": control.ControlCode,
                "name": control.ControlCode.split(".", 1)[-1],
                "handler": control.HandlerCode,
                "severity": control.Severity,
                "mandatory": bool(control.IsMandatory),
                "tolerance": float(control.Tolerance) if control.Tolerance is not None else None,
                "tolerance_type": control.ToleranceType,
                "params": parse_parameters(control.ParametersJson),
            }
        )

    prerequisites = {mapping_id: set() for mapping_id in mappings}

    for child, parent in dependencies:
        if child in mappings:
            if parent not in mappings:
                problems.append(f"{mappings[child]['code']} depends on disabled or missing mapping {parent}.")
            else:
                prerequisites[child].add(parent)

    if problems:
        raise PlanError("Execution plan rejected:\n  - " + "\n  - ".join(problems))

    ordered, remaining = [], set(mappings)

    while remaining:
        ready = sorted(
            (m for m in remaining if prerequisites[m] <= set(ordered)),
            key=lambda m: (mappings[m]["order"], mappings[m]["code"] or ""),
        )
        if not ready:
            cycle = ", ".join(sorted(mappings[m]["code"] or str(m) for m in remaining))
            raise PlanError(f"Circular mapping dependencies among: {cycle}")
        ordered.append(ready[0])
        remaining.remove(ready[0])

    return [mappings[m] for m in ordered], prerequisites


# ---------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------


@dataclass
class Outcome:
    status: str  # COMPLETED / FAILED / BLOCKED / SKIPPED / CANCELLED
    message: str
    category: str | None = None
    rows_read: int | None = None
    rows_written: int | None = None
    attempts: int = 1


def eligibility_block(mapping: dict, context: RunContext) -> str | None:
    """Reason a staging mapping may not run, or None."""

    source = mapping["source"]

    if mapping["layer"] != "STAGING" or source not in context.registry["sources"]:
        return None

    decisions = context.source_decisions(source)
    eligible = context.eligible(source)

    if not decisions:
        return f"No file version of {source} has been ingested."

    if is_document_set(context.registry["sources"][source]):
        return None if eligible else f"No eligible {source} document: {decisions[0].get('reason')}"

    if len(eligible) != 1:
        decision = decisions[0]
        return f"{source} is {decision['decision']}: {decision.get('reason')}"

    return None


def run_handler(mapping: dict, context: RunContext, mapping_execution_id: int) -> Outcome:
    """Run with retries for transient database errors only."""

    handler = HANDLERS[mapping["handler"]]
    attempts = 0

    while True:
        attempts += 1

        try:
            result = handler.run(context)

            for warning in result.warnings:
                record_error(
                    context.pipeline_id,
                    mapping["layer"],
                    "WARNING",
                    "DATA",
                    warning,
                    mapping,
                    mapping_execution_id,
                    source=mapping["source"],
                    impact="The affected file version was not loaded; the target reflects the rest.",
                    action="Trace the file (scripts/trace_file.py) and resubmit a corrected version.",
                )

            return Outcome("COMPLETED", result.message, None, result.rows_read, result.rows_written, attempts)

        except Exception as error:
            if is_transient(error) and attempts <= mapping["max_retries"]:
                delay = mapping["retry_delay"] * attempts
                record_error(
                    context.pipeline_id,
                    mapping["layer"],
                    "WARNING",
                    "TRANSIENT",
                    f"Attempt {attempts} failed with a transient error; retrying in {delay}s.",
                    mapping,
                    mapping_execution_id,
                    source=mapping["source"],
                    technical=error,
                )
                time.sleep(delay)
                continue

            category = "TRANSIENT" if is_transient(error) else "DATA" if isinstance(error, HandlerError) else "SYSTEM"

            return Outcome("FAILED", f"{type(error).__name__}: {error}", category, attempts=attempts)


def run_controls(mapping: dict, context: RunContext, mapping_execution_id: int) -> Outcome | None:
    """None if no blocking failure; otherwise a FAILED outcome."""

    blocking = []

    for control in mapping["controls"]:
        try:
            outcome = run_control(control, context)
        except Exception as error:
            record_error(
                context.pipeline_id,
                "RECONCILIATION",
                "CRITICAL",
                "CONFIGURATION",
                f"Control {control['code']} could not run: {error}",
                mapping,
                mapping_execution_id,
                source=mapping["source"],
                rule=control["code"],
                action="Fix the control definition in config/pipeline.yaml and re-sync metadata.",
                technical=traceback.format_exc(limit=3),
            )
            blocking.append(control["code"])
            continue

        if outcome.status == "FAIL":
            batch = context.first_batch(mapping["source"]) or {}

            record_error(
                context.pipeline_id,
                "RECONCILIATION",
                outcome.severity,
                "RECONCILIATION",
                f"{control['code']}: {outcome.details}",
                mapping,
                mapping_execution_id,
                source=mapping["source"],
                source_file=batch.get("file"),
                run_id=batch.get("run_id"),
                rule=control["code"],
                actual=outcome.observed,
                expected=outcome.expected,
                impact=(
                    f"{mapping['target']} cannot be trusted for reporting until this is resolved."
                    if outcome.blocks
                    else f"{mapping['target']} was loaded; the issue needs attention but does not make it unsafe."
                ),
                action=outcome.recommended_action,
            )

            if outcome.blocks:
                blocking.append(control["code"])

    if blocking:
        return Outcome("FAILED", f"Reconciliation failed: {', '.join(blocking)}", "RECONCILIATION")

    return None


def start_mapping(pipeline_id: int, mapping: dict, status: str = "STARTED", message: str | None = None) -> int:

    return execute_audit(
        "INSERT INTO audit.MappingExecution (PipelineExecutionID, MappingID, HandlerID, ExecutionStatus, "
        "OutcomeMessage, CompletedAt) OUTPUT INSERTED.MappingExecutionID "
        "VALUES (?, ?, ?, ?, ?, IIF(? = 'STARTED', NULL, SYSUTCDATETIME()))",
        pipeline_id,
        mapping["id"],
        mapping["handler_id"],
        status,
        message,
        status,
        fetch=True,
    )


def finish_mapping(mapping_execution_id: int, outcome: Outcome) -> None:

    execute_audit(
        "UPDATE audit.MappingExecution SET ExecutionStatus = ?, CompletedAt = SYSUTCDATETIME(), OutcomeMessage = ?, "
        "AttemptCount = ?, RowsRead = ?, RowsWritten = ?, ErrorCategory = ? WHERE MappingExecutionID = ?",
        outcome.status,
        outcome.message[:2000],
        outcome.attempts,
        outcome.rows_read,
        outcome.rows_written,
        outcome.category,
        mapping_execution_id,
    )


def heartbeat(pipeline_id: int) -> None:

    execute_audit(
        "UPDATE audit.PipelineExecution SET HeartbeatAt = SYSUTCDATETIME() WHERE PipelineExecutionID = ?", pipeline_id
    )


ACTIONS = {
    "DATA": "Trace the source file (python scripts/trace_file.py <file>), correct it and resubmit.",
    "SYSTEM": "Check the technical detail; fix the code or database object and re-run (loads are idempotent).",
    "TRANSIENT": "Retries were exhausted; check SQL Server availability and re-run.",
    "RECONCILIATION": "Review audit.ReconciliationResult for this run; do not publish reports from the target.",
}


def run_pipeline() -> int:

    pipeline_id = None
    current = None  # (mapping, mapping_execution_id)
    results = Counter()

    try:
        pipeline_config = load_yaml("pipeline.yaml")
        settings = pipeline_config.get("pipeline") or {}

        pipeline_id = execute_audit(
            "INSERT INTO audit.PipelineExecution (PipelineName, ExecutionStatus, HeartbeatAt, ConfigChecksum) "
            "OUTPUT INSERTED.PipelineExecutionID VALUES (?, 'STARTED', SYSUTCDATETIME(), ?)",
            settings.get("name", "RetailDataPlatform"),
            config_checksum(),
            fetch=True,
        )
        print(f"PIPELINE EXECUTION ID: {pipeline_id}", flush=True)

        abandoned = abandon_stalled(pipeline_id, int(settings.get("stalled_after_minutes", 60)))
        if abandoned:
            print(f"Marked {abandoned} stalled execution(s) ABANDONED.", flush=True)

        registry = load_registry_config()
        decisions = decide(registry, read_manifest(), read_log(FILE_STATUS_LOG_FILE), read_log(VALIDATION_LOG_FILE))
        record_source_batches(pipeline_id, decisions)

        decision_counts = Counter(d["decision"] for d in decisions)
        print(f"GATE: {dict(decision_counts)}", flush=True)

        loaded = load_validation_outcomes(pipeline_id, decisions)
        if loaded:
            print(f"Recorded {loaded} validation outcome(s) in audit.ValidationOutcome.", flush=True)

        plan, prerequisites = load_plan()
        execute_audit(
            "UPDATE audit.PipelineExecution SET MappingsPlanned = ? WHERE PipelineExecutionID = ?",
            len(plan),
            pipeline_id,
        )
        print(f"METADATA PLAN: {len(plan)} approved mapping(s)", flush=True)

        context = RunContext(pipeline_id, registry, pipeline_config, decisions)
        status_by_id = {}
        stopped_by = None

        for mapping in plan:
            heartbeat(pipeline_id)
            context.mapping = mapping

            if stopped_by:
                outcome = Outcome("SKIPPED", f"Not run: pipeline stopped after {stopped_by} failed.", "DEPENDENCY")
                start_mapping(pipeline_id, mapping, "SKIPPED", outcome.message)
                status_by_id[mapping["id"]] = "SKIPPED"
                results["SKIPPED"] += 1
                continue

            not_done = sorted(
                (status_by_id.get(p), p) for p in prerequisites[mapping["id"]] if status_by_id.get(p) != "COMPLETED"
            )
            reason = None

            if not_done:
                names = {m["id"]: m["code"] for m in plan}
                reason = "Dependency not completed: " + ", ".join(f"{names[p]} ({s})" for s, p in not_done)
                category = "DEPENDENCY"
            else:
                reason = eligibility_block(mapping, context)
                category = "ELIGIBILITY"

            if reason:
                execution_id = start_mapping(pipeline_id, mapping, "BLOCKED", reason[:2000])
                execute_audit(
                    "UPDATE audit.MappingExecution SET ErrorCategory = ? WHERE MappingExecutionID = ?",
                    category,
                    execution_id,
                )
                record_error(
                    pipeline_id,
                    mapping["layer"],
                    "WARNING",
                    category,
                    reason,
                    mapping,
                    execution_id,
                    source=mapping["source"],
                    impact=f"{mapping['target']} was not refreshed in this run; it still holds the last loaded data.",
                    action=(
                        "Resolve the upstream issue (see the dependency's error)."
                        if category == "DEPENDENCY"
                        else "Trace the source file (python scripts/trace_file.py <source>) and resubmit."
                    ),
                )
                status_by_id[mapping["id"]] = "BLOCKED"
                results["BLOCKED"] += 1
                print(f"BLOCKED   {mapping['code']}: {reason[:160]}", flush=True)
                continue

            execution_id = start_mapping(pipeline_id, mapping)
            current = (mapping, execution_id)
            print(f"RUNNING   {mapping['code']} ({mapping['handler']} -> {mapping['target']})", flush=True)

            outcome = run_handler(mapping, context, execution_id)

            if outcome.status == "COMPLETED":
                control_failure = run_controls(mapping, context, execution_id)
                if control_failure:
                    control_failure.rows_read, control_failure.rows_written = outcome.rows_read, outcome.rows_written
                    outcome = control_failure

            if outcome.status == "FAILED":
                batch = context.first_batch(mapping["source"]) or {}
                record_error(
                    pipeline_id,
                    mapping["layer"],
                    "ERROR" if outcome.category != "RECONCILIATION" else "CRITICAL",
                    outcome.category or "SYSTEM",
                    f"{mapping['code']} failed: {outcome.message}",
                    mapping,
                    execution_id,
                    source=mapping["source"],
                    source_file=batch.get("file"),
                    run_id=batch.get("run_id"),
                    impact=f"{mapping['target']} and everything that depends on it were not refreshed.",
                    action=ACTIONS.get(outcome.category or "SYSTEM"),
                )
                stopped_by = mapping["code"]

            finish_mapping(execution_id, outcome)
            current = None
            status_by_id[mapping["id"]] = outcome.status
            results[outcome.status] += 1
            print(f"{outcome.status:<9} {mapping['code']}: {outcome.message[:200]}", flush=True)

        status = "FAILED" if results["FAILED"] else "PARTIAL" if results["BLOCKED"] else "COMPLETED"
        message = (
            None
            if status == "COMPLETED"
            else (f"Stopped after {stopped_by} failed." if stopped_by else f"{results['BLOCKED']} mapping(s) blocked.")
        )

        execute_audit(
            "UPDATE audit.PipelineExecution SET ExecutionStatus = ?, CompletedAt = SYSUTCDATETIME(), ErrorMessage = ?, "
            "MappingsCompleted = ?, MappingsFailed = ?, MappingsBlocked = ? WHERE PipelineExecutionID = ?",
            status,
            message,
            results["COMPLETED"],
            results["FAILED"],
            results["BLOCKED"] + results["SKIPPED"],
            pipeline_id,
        )

        print(
            f"SQL ORCHESTRATION {status}: {results['COMPLETED']} completed, {results['BLOCKED']} blocked, "
            f"{results['FAILED']} failed, {results['SKIPPED']} skipped; execution={pipeline_id}",
            flush=True,
        )

        return {"COMPLETED": 0, "PARTIAL": 1}.get(status, 2)

    except BaseException as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        status = "CANCELLED" if cancelled else "FAILED"
        message = "Cancelled by the operator." if cancelled else f"{type(error).__name__}: {error}"

        if current is not None:
            try:
                finish_mapping(current[1], Outcome(status, message, "CANCELLED" if cancelled else "SYSTEM"))
            except Exception as audit_error:
                print(f"MAPPING AUDIT UPDATE FAILED: {audit_error}", file=sys.stderr, flush=True)

        if pipeline_id is not None:
            try:
                execute_audit(
                    "UPDATE audit.PipelineExecution SET ExecutionStatus = ?, CompletedAt = SYSUTCDATETIME(), "
                    "ErrorMessage = ? WHERE PipelineExecutionID = ?",
                    status,
                    message[:2000],
                    pipeline_id,
                )
                if not cancelled:
                    record_error(
                        pipeline_id,
                        "ORCHESTRATION",
                        "CRITICAL",
                        "SYSTEM",
                        message,
                        (current or ({},))[0] or None,
                        impact="The run stopped before completing; nothing after this point was refreshed.",
                        action="Fix the cause shown in TechnicalDetail and re-run; loads are idempotent.",
                        technical=traceback.format_exc(limit=5),
                    )
            except Exception as audit_error:
                print(f"PIPELINE AUDIT UPDATE FAILED: {audit_error}", file=sys.stderr, flush=True)

        print(f"SQL ORCHESTRATION {status}: {message}", file=sys.stderr, flush=True)

        return 130 if cancelled else 2


def main() -> int:

    return run_pipeline()


if __name__ == "__main__":
    sys.exit(main())
