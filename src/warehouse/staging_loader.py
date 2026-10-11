"""
Generic, contract-driven loader: validated RAW file -> SQL staging.

Works for every registered source (Excel, CSV and PDF documents). For
each eligible file version:

    1. Refuse anything the load-eligibility gate did not mark ELIGIBLE.
    2. Check the RAW file is inside data/raw and its SHA-256 still
       matches the hash recorded at ingestion (evidence unchanged).
    3. Read it with the same reader used by validation (including PDF
       extraction), so exactly what was validated is loaded.
    4. Under an exclusive lock per staging table: if this version is
       already staged (same run, hash and row numbers) record SKIPPED;
       otherwise insert all rows and the COMPLETED audit row in one
       transaction. A partial earlier load is an error, never topped up.

Every attempt is recorded in audit.LoadExecution.

Usage:
    python src/warehouse/staging_loader.py --source site_master
    python src/warehouse/staging_loader.py --all
"""

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "src" / "validation"))

from common import (  # noqa: E402
    FILE_STATUS_LOG_FILE,
    VALIDATION_LOG_FILE,
    load_registry_config,
    read_log,
    read_manifest,
    read_table,
)
from db_connection import get_connection  # noqa: E402
from load_eligibility import decide  # noqa: E402
from staging_schema import StagingTable, staging_table  # noqa: E402

RAW_ROOT = (ROOT / "data" / "raw").resolve()


class StagingRefused(RuntimeError):
    """The file may not be loaded (not eligible, or evidence changed)."""


@dataclass
class FileLoadResult:
    file: str
    status: str  # COMPLETED, SKIPPED, FAILED
    rows_read: int = 0
    rows_inserted: int = 0
    load_id: int | None = None
    message: str = ""


@dataclass
class SourceLoadResult:
    source: str
    files: list[FileLoadResult] = field(default_factory=list)

    @property
    def rows_read(self) -> int:
        return sum(f.rows_read for f in self.files)

    @property
    def rows_inserted(self) -> int:
        return sum(f.rows_inserted for f in self.files)

    @property
    def failed(self) -> list[FileLoadResult]:
        return [f for f in self.files if f.status == "FAILED"]

    def summary(self) -> str:
        completed = sum(f.status == "COMPLETED" for f in self.files)
        skipped = sum(f.status == "SKIPPED" for f in self.files)
        return (
            f"{self.source}: {len(self.files)} file(s), {completed} loaded, "
            f"{skipped} already staged, {len(self.failed)} failed; "
            f"{self.rows_inserted} row(s) inserted of {self.rows_read} read"
        )


# ---------------------------------------------------------
# VALUE CONVERSION
# ---------------------------------------------------------


def convert(value, sql_type: str):
    """Convert a validated cell value to the staging column's type."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None

    upper = sql_type.upper()

    if upper == "BIT":
        return bool(value)

    if upper == "INT":
        return int(float(value))

    if upper.startswith("DECIMAL"):
        return Decimal(str(round(float(value), 6)))

    if upper == "DATE":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip()[:10])

    # Text: integers read from Excel as floats (1010.0) keep their code form.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def sha256_file(path: Path) -> str:

    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


# ---------------------------------------------------------
# READING
# ---------------------------------------------------------


def staging_rows(definition: StagingTable, source_config: dict, raw: Path) -> list[tuple]:
    """(row_number, converted values...) for every data row of the file."""

    headers, rows = read_table(
        raw,
        source_config.get("structure") or {},
        source_config.get("extraction"),
    )

    positions = {}

    for index, header in enumerate(headers):
        if header is not None and header not in positions:
            positions[header] = index

    missing = [c.name for c in definition.columns if c.name not in positions]

    if missing:
        raise StagingRefused(f"Columns missing from {raw.name}: {missing}")

    result = []

    for row_number, values in rows:
        converted = [row_number]

        for column in definition.columns:
            index = positions[column.name]
            raw_value = values[index] if index < len(values) else None

            try:
                converted.append(convert(raw_value, column.sql_type))
            except (TypeError, ValueError) as error:
                raise StagingRefused(
                    f"{raw.name} row {row_number}, column {column.name}: cannot convert "
                    f"{raw_value!r} to {column.sql_type} ({error}). The value passed "
                    f"validation, so the staging type or the contract needs review."
                ) from error

        result.append(tuple(converted))

    return result


def resolve_raw(decision: dict) -> Path:

    raw = Path(decision["raw_path"])

    if not raw.is_absolute():
        raw = ROOT / raw

    raw = raw.resolve()

    if not raw.is_relative_to(RAW_ROOT) or not raw.is_file():
        raise StagingRefused(f"RAW file missing or outside data/raw: {raw}")

    return raw


# ---------------------------------------------------------
# AUDIT
# ---------------------------------------------------------


def record_attempt(
    connection, decision, source, table, status, pipeline_id, rows_read=0, rows_inserted=0, error=None
) -> int:
    """Terminal audit row in its own committed transaction."""

    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO audit.LoadExecution
            (IngestionRunID, SourceName, SourceFile, SourceFileHash, LoadStatus,
             RowsRead, RowsInserted, RowsRejected, CompletedAt, ErrorMessage,
             PipelineExecutionID, StagingTable)
        OUTPUT INSERTED.LoadID
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, SYSUTCDATETIME(), ?, ?, ?)
        """,
        decision.get("run_id") or "UNKNOWN",
        source,
        decision.get("file") or "UNKNOWN",
        (decision.get("file_hash") or "0" * 64).lower(),
        status,
        rows_read,
        rows_inserted,
        str(error)[:4000] if error else None,
        pipeline_id,
        table,
    )
    load_id = cursor.fetchone()[0]
    connection.commit()

    return load_id


# ---------------------------------------------------------
# LOADING
# ---------------------------------------------------------


def load_file(decision: dict, source: str, source_config: dict, pipeline_id: int | None = None) -> FileLoadResult:

    definition = staging_table(source, source_config)
    table = definition.qualified_name
    file_name = decision.get("file") or "UNKNOWN"

    if decision.get("decision") != "ELIGIBLE":
        reason = decision.get("reason") or "Load not authorised by the eligibility gate."
        connection = get_connection()
        try:
            load_id = record_attempt(
                connection, decision, source, table, "SKIPPED", pipeline_id, error=f"Refused: {reason}"
            )
        finally:
            connection.close()
        return FileLoadResult(file_name, "SKIPPED", load_id=load_id, message=f"Not eligible: {reason}")

    raw = resolve_raw(decision)
    digest = sha256_file(raw)

    if digest.lower() != (decision.get("file_hash") or "").lower():
        raise StagingRefused(
            f"SHA-256 of {raw.name} no longer matches the ingested hash; the RAW evidence has changed since ingestion."
        )

    rows = staging_rows(definition, source_config, raw)

    if not rows:
        raise StagingRefused(f"Eligible file {raw.name} has no data rows.")

    run_id = decision["run_id"]
    columns = ", ".join(f"[{c}]" for c in definition.column_names)
    placeholders = ", ".join("?" for _ in range(len(definition.columns) + 4))
    lock_name = f"RetailDataPlatform:{table}:load"

    connection = get_connection()
    lock_acquired = False

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            DECLARE @result INT;
            EXEC @result = sys.sp_getapplock @Resource = ?, @LockMode = 'Exclusive',
                 @LockOwner = 'Session', @LockTimeout = 30000;
            SELECT @result;
            """,
            lock_name,
        )

        if cursor.fetchone()[0] < 0:
            raise RuntimeError(f"Could not acquire the staging lock for {table}.")

        lock_acquired = True

        cursor.execute(
            f"SELECT SourceRowNumber FROM {table} WHERE IngestionRunID = ? AND SourceFileHash = ?",
            run_id,
            digest,
        )
        staged = {record[0] for record in cursor.fetchall()}
        expected = {row[0] for row in rows}

        if staged:
            connection.rollback()

            if staged != expected:
                raise RuntimeError(
                    f"{table} already holds {len(staged)} row(s) of this version but the "
                    f"file has {len(expected)}; a partial load needs manual reconciliation."
                )

            load_id = record_attempt(
                connection,
                decision,
                source,
                table,
                "SKIPPED",
                pipeline_id,
                rows_read=len(rows),
                error="Already staged; row numbers match the source file.",
            )

            return FileLoadResult(file_name, "SKIPPED", len(rows), 0, load_id, "Already staged (idempotent skip).")

        cursor.execute(
            """
            INSERT INTO audit.LoadExecution
                (IngestionRunID, SourceName, SourceFile, SourceFileHash, LoadStatus,
                 RowsRead, PipelineExecutionID, StagingTable)
            OUTPUT INSERTED.LoadID
            VALUES (?, ?, ?, ?, 'STARTED', ?, ?, ?)
            """,
            run_id,
            source,
            file_name,
            digest,
            len(rows),
            pipeline_id,
            table,
        )
        load_id = cursor.fetchone()[0]

        cursor.fast_executemany = True
        cursor.executemany(
            f"INSERT INTO {table} ({columns}, IngestionRunID, SourceFile, SourceRowNumber, "
            f"SourceFileHash) VALUES ({placeholders})",
            [(*row[1:], run_id, file_name, row[0], digest) for row in rows],
        )
        cursor.fast_executemany = False

        cursor.execute(
            f"SELECT COUNT(*) FROM {table} WHERE IngestionRunID = ? AND SourceFileHash = ?",
            run_id,
            digest,
        )
        staged_count = cursor.fetchone()[0]

        if staged_count != len(rows):
            raise RuntimeError(f"Row reconciliation failed for {file_name}: source {len(rows)}, staged {staged_count}.")

        cursor.execute(
            "UPDATE audit.LoadExecution SET LoadStatus = 'COMPLETED', RowsInserted = ?, "
            "CompletedAt = SYSUTCDATETIME() WHERE LoadID = ?",
            staged_count,
            load_id,
        )

        connection.commit()

        return FileLoadResult(
            file_name, "COMPLETED", len(rows), staged_count, load_id, f"Staged {staged_count} row(s)."
        )

    except Exception as error:
        connection.rollback()

        try:
            load_id = record_attempt(
                connection, decision, source, table, "FAILED", pipeline_id, rows_read=len(rows), error=error
            )
        except Exception:
            load_id = None

        return FileLoadResult(file_name, "FAILED", len(rows), 0, load_id, str(error))

    finally:
        if lock_acquired:
            try:
                connection.cursor().execute(
                    "EXEC sys.sp_releaseapplock @Resource = ?, @LockOwner = 'Session'",
                    lock_name,
                )
            except Exception:  # noqa: S110 - closing the connection releases a session lock anyway  # nosec B110
                pass

        connection.close()


def load_source(
    source: str, decisions: list[dict], registry_config: dict, pipeline_id: int | None = None
) -> SourceLoadResult:
    """Load every eligible file version of a source (one, or many documents)."""

    source_config = registry_config["sources"][source]
    result = SourceLoadResult(source)

    for decision in decisions:
        if decision["source"] != source or decision.get("decision") == "WITHDRAWN":
            continue

        try:
            result.files.append(load_file(decision, source, source_config, pipeline_id))
        except StagingRefused as error:
            connection = get_connection()
            try:
                load_id = record_attempt(
                    connection,
                    decision,
                    source,
                    source_config.get("staging_table"),
                    "FAILED",
                    pipeline_id,
                    error=error,
                )
            finally:
                connection.close()
            result.files.append(
                FileLoadResult(decision.get("file") or "UNKNOWN", "FAILED", load_id=load_id, message=str(error))
            )

    return result


def current_decisions(registry_config: dict) -> list[dict]:
    """A fresh gate decision (never a possibly stale CSV row)."""

    return decide(
        registry_config,
        read_manifest(),
        read_log(FILE_STATUS_LOG_FILE),
        read_log(VALIDATION_LOG_FILE),
    )


def main() -> int:

    parser = argparse.ArgumentParser(description="Load validated files into SQL staging.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", help="Registered source name.")
    group.add_argument("--all", action="store_true", help="Every source with a staging table.")
    args = parser.parse_args()

    config = load_registry_config()
    decisions = current_decisions(config)

    sources = [s for s, c in config["sources"].items() if c.get("staging_table")] if args.all else [args.source]

    exit_code = 0

    for source in sources:
        if source not in config["sources"]:
            print(f"Unknown source {source}")
            return 2

        result = load_source(source, decisions, config)
        print(result.summary())

        for failed in result.failed:
            print(f"   FAILED {failed.file}: {failed.message}")

        if result.failed:
            exit_code = 1

        if not result.files:
            print(f"   No file versions found for {source}.")
            exit_code = max(exit_code, 2)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
