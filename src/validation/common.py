"""
Shared helpers for the validation scripts: paths, registry and
manifest access, table reading, result records, logging and
quarantine.
"""

import csv
import shutil

from datetime import datetime
from pathlib import Path

import openpyxl
import yaml


# ---------------------------------------------------------
# PROJECT PATHS
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = PROJECT_ROOT / "config"
RAW_DIR = PROJECT_ROOT / "data" / "raw"
QUARANTINE_DIR = PROJECT_ROOT / "data" / "quarantine"

SOURCE_REGISTRY_FILE = CONFIG_DIR / "source_registry.yaml"

MANIFEST_FILE = PROJECT_ROOT / "logs" / "ingestion_manifest.csv"
VALIDATION_LOG_FILE = PROJECT_ROOT / "logs" / "validation_results.csv"
FILE_STATUS_LOG_FILE = PROJECT_ROOT / "logs" / "validation_file_status.csv"

DEFAULT_MAX_FAILURES_PER_RULE = 500


# ---------------------------------------------------------
# LOG SCHEMAS
# ---------------------------------------------------------

RESULT_FIELDS = [
    "run_id",
    "source",
    "file",
    "sheet",
    "row_number",
    "record_key",
    "stage",
    "rule",
    "field",
    "status",
    "severity",
    "actual",
    "expected",
    "error_type",
    "technical_error",
    "message",
    "recommended_action",
    "validated_at",
]

# Column layout of validation_results.csv before the sheet,
# row_number and record_key columns were added (no header row).
LEGACY_RESULT_FIELDS = [
    "run_id",
    "source",
    "file",
    "stage",
    "rule",
    "field",
    "status",
    "severity",
    "actual",
    "expected",
    "error_type",
    "technical_error",
    "message",
    "recommended_action",
    "validated_at",
]

FILE_STATUS_FIELDS = [
    "run_id",
    "source",
    "file",
    "stage",
    "status",
    "rows_checked",
    "errors",
    "warnings",
    "quarantine_path",
    "validated_at",
]


# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

def load_registry_config() -> dict:

    with open(
        SOURCE_REGISTRY_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        return yaml.safe_load(file)


def load_source_registry() -> dict:

    return load_registry_config()["sources"]


def load_settings() -> dict:

    return load_registry_config().get("settings") or {}


# ---------------------------------------------------------
# MANIFEST
# ---------------------------------------------------------

def read_manifest() -> list[dict]:

    if not MANIFEST_FILE.exists():
        return []

    with open(
        MANIFEST_FILE,
        "r",
        newline="",
        encoding="utf-8",
    ) as manifest:
        return list(csv.DictReader(manifest))


def latest_ingestion_run(manifest: list[dict]) -> str | None:
    """
    The most recent run that actually ingested files. Runs that only
    found duplicates have nothing new to validate.
    """

    run_ids = [
        row["run_id"]
        for row in manifest
        if row["status"] == "INGESTED"
    ]

    return max(run_ids) if run_ids else None


def ingested_files_for_run(
    manifest: list[dict],
    run_id: str,
    source: str | None = None,
) -> list[dict]:

    return [
        row
        for row in manifest
        if row["run_id"] == run_id
        and row["status"] == "INGESTED"
        and (source is None or row["source"] == source)
    ]


def raw_file_path(manifest_row: dict) -> Path:

    # Older manifest rows were written with Windows separators.
    return PROJECT_ROOT / manifest_row["raw_path"].replace("\\", "/")


def latest_raw_file_for_source(
    manifest: list[dict],
    source: str,
    up_to_run_id: str | None = None,
) -> Path | None:
    """
    Most recent RAW copy of a source, ingested in or before the given
    run. Used to resolve references to other sources (for example
    supplier_master when validating purchase_orders).
    """

    candidates = [
        row
        for row in manifest
        if row["source"] == source
        and row["status"] == "INGESTED"
        and (up_to_run_id is None or row["run_id"] <= up_to_run_id)
    ]

    if not candidates:
        return None

    latest = max(candidates, key=lambda row: row["run_id"])

    return raw_file_path(latest)


# ---------------------------------------------------------
# TABLE READING
# ---------------------------------------------------------

def is_blank(value) -> bool:

    return value is None or (
        isinstance(value, str)
        and not value.strip()
    )


def read_table(
    file: Path,
    structure: dict,
) -> tuple[list, list[tuple[int, tuple]]]:
    """
    Read a tabular source (xlsx or csv).

    Returns (headers, rows) where rows is a list of
    (source_row_number, values). Row numbers match what a user sees
    in Excel, so error reports point to the exact row. Completely
    blank rows are skipped.
    """

    suffix = file.suffix.lower()

    if suffix == ".xlsx":

        workbook = openpyxl.load_workbook(
            file,
            read_only=True,
            data_only=True,
        )

        try:

            worksheet = workbook[structure["sheet"]]

            rows = worksheet.iter_rows(values_only=True)

            headers = [
                header.strip() if isinstance(header, str) else header
                for header in next(rows, ())
            ]

            data = [
                (row_number, row)
                for row_number, row in enumerate(rows, start=2)
                if not all(is_blank(value) for value in row)
            ]

        finally:
            workbook.close()

        return headers, data

    if suffix == ".csv":

        with open(
            file,
            "r",
            newline="",
            encoding="utf-8-sig",
        ) as csv_file:

            reader = csv.reader(csv_file)

            headers = [header.strip() for header in next(reader, [])]

            data = [
                (
                    row_number,
                    tuple(None if is_blank(value) else value for value in row),
                )
                for row_number, row in enumerate(reader, start=2)
                if not all(is_blank(value) for value in row)
            ]

        return headers, data

    raise ValueError(f"Unsupported tabular format: {file.suffix}")


# ---------------------------------------------------------
# RESULT RECORDS
# ---------------------------------------------------------

def make_result(
    *,
    run_id: str,
    source: str,
    file: Path,
    stage: str,
    rule: str,
    status: str,
    severity: str | None = None,
    **details,
) -> dict:

    return {
        "run_id": run_id,
        "source": source,
        "file": file.name,
        "stage": stage,
        "rule": rule,
        "status": status,
        "severity": severity,
        **details,
        "validated_at": datetime.now().isoformat(timespec="seconds"),
    }


def count_by_severity(results: list[dict]) -> tuple[int, int]:

    errors = sum(
        1 for result in results
        if result["status"] == "FAIL"
        and result.get("severity") == "ERROR"
    )

    warnings = sum(
        1 for result in results
        if result["status"] == "FAIL"
        and result.get("severity") == "WARNING"
    )

    return errors, warnings


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

def ensure_csv_schema(
    path: Path,
    fieldnames: list[str],
    legacy_fieldnames: list[str] | None = None,
) -> None:
    """
    Migrate an existing log to the current column layout, keeping
    every existing row. Handles logs written without a header row
    by mapping them onto legacy_fieldnames.
    """

    if not path.exists() or path.stat().st_size == 0:
        return

    with open(path, "r", newline="", encoding="utf-8") as log_file:
        rows = list(csv.reader(log_file))

    header = rows[0]

    if header == fieldnames:
        return

    if header and header[0] == "run_id":
        old_fields, data = header, rows[1:]

    elif legacy_fieldnames and len(header) == len(legacy_fieldnames):
        old_fields, data = legacy_fieldnames, rows

    else:
        raise ValueError(
            f"Cannot migrate {path}: unrecognised column layout."
        )

    with open(path, "w", newline="", encoding="utf-8") as log_file:

        writer = csv.DictWriter(
            log_file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()

        for row in data:
            writer.writerow(dict(zip(old_fields, row)))


def append_csv_rows(
    path: Path,
    fieldnames: list[str],
    rows: list[dict],
    legacy_fieldnames: list[str] | None = None,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ensure_csv_schema(path, fieldnames, legacy_fieldnames)

    file_exists = path.exists() and path.stat().st_size > 0

    with open(
        path,
        "a",
        newline="",
        encoding="utf-8",
    ) as log_file:

        writer = csv.DictWriter(
            log_file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        if not file_exists:
            writer.writeheader()

        for row in rows:
            writer.writerow(
                {field: row.get(field) for field in fieldnames}
            )


def write_validation_results(results: list[dict]) -> None:

    append_csv_rows(
        VALIDATION_LOG_FILE,
        RESULT_FIELDS,
        results,
        legacy_fieldnames=LEGACY_RESULT_FIELDS,
    )


def write_file_status(
    run_id: str,
    source: str,
    file: Path,
    stage: str,
    status: str,
    results: list[dict],
    rows_checked: int | None = None,
    quarantine_path: Path | None = None,
) -> None:

    errors, warnings = count_by_severity(results)

    append_csv_rows(
        FILE_STATUS_LOG_FILE,
        FILE_STATUS_FIELDS,
        [{
            "run_id": run_id,
            "source": source,
            "file": file.name,
            "stage": stage,
            "status": status,
            "rows_checked": rows_checked,
            "errors": errors,
            "warnings": warnings,
            "quarantine_path": (
                quarantine_path.relative_to(PROJECT_ROOT).as_posix()
                if quarantine_path else None
            ),
            "validated_at": datetime.now().isoformat(timespec="seconds"),
        }],
    )


# ---------------------------------------------------------
# QUARANTINE
# ---------------------------------------------------------

def quarantine_file(
    file: Path,
    run_id: str,
    results: list[dict],
) -> Path:
    """
    Copy a failed file to data/quarantine/<run_id>/ and write an
    exception report next to it. The RAW copy is left untouched
    as the original evidence.
    """

    run_raw_dir = RAW_DIR / run_id

    relative_path = (
        file.relative_to(run_raw_dir)
        if file.is_relative_to(run_raw_dir)
        else Path(file.name)
    )

    destination = QUARANTINE_DIR / run_id / relative_path

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.copy2(file, destination)

    failures = [
        result
        for result in results
        if result["status"] == "FAIL"
    ]

    report_path = destination.with_name(destination.name + ".errors.csv")

    # Rewrite (not append) so a re-validation replaces the report.
    report_path.unlink(missing_ok=True)

    append_csv_rows(report_path, RESULT_FIELDS, failures)

    return destination


# ---------------------------------------------------------
# TARGET SELECTION
# ---------------------------------------------------------

def select_targets(
    run_id: str | None,
    source: str | None,
    file: str | None,
    source_registry: dict,
) -> tuple[str, list[tuple[str, Path]], bool]:
    """
    Decide which files to validate.

    Returns (run_id, [(source, path)], is_adhoc). An ad-hoc run
    (--file) validates a file outside the ingestion pipeline, for
    example a controlled test file, and is never quarantined.
    """

    if file:

        if not source:
            raise SystemExit("--file requires --source.")

        if source not in source_registry:
            raise SystemExit(f"Unknown source '{source}'.")

        adhoc_run_id = run_id or (
            "ADHOC-" + datetime.now().strftime("%Y%m%d_%H%M%S")
        )

        return adhoc_run_id, [(source, Path(file).resolve())], True

    manifest = read_manifest()

    run_id = run_id or latest_ingestion_run(manifest)

    if run_id is None:
        raise SystemExit(
            "No ingested files found. "
            "Run src/ingestion/discover_files.py first."
        )

    rows = ingested_files_for_run(manifest, run_id, source)

    if not rows:
        raise SystemExit(
            f"No ingested files found for run {run_id}"
            + (f" and source {source}." if source else ".")
        )

    return run_id, [(row["source"], raw_file_path(row)) for row in rows], False
