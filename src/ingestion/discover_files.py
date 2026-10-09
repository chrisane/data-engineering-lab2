import csv
import hashlib
import shutil
import traceback

from datetime import datetime
from pathlib import Path

import yaml


# ---------------------------------------------------------
# PROJECT PATHS
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

INCOMING_DIR = PROJECT_ROOT / "data" / "incoming"
RAW_DIR = PROJECT_ROOT / "data" / "raw"
QUARANTINE_DIR = PROJECT_ROOT / "data" / "quarantine"

MANIFEST_FILE = PROJECT_ROOT / "logs" / "ingestion_manifest.csv"
RUN_LOG_FILE = PROJECT_ROOT / "logs" / "ingestion_runs.csv"

SOURCE_REGISTRY_FILE = (
    PROJECT_ROOT / "config" / "source_registry.yaml"
)

SUPPORTED_EXTENSIONS = {".xlsx", ".csv", ".pdf"}

# Leading bytes ("magic numbers") that identify genuine files.
# A renamed or corrupted file fails this check even when its
# extension looks correct.
FILE_SIGNATURES = {
    "xlsx": b"PK\x03\x04",
    "pdf": b"%PDF",
}

MANIFEST_FIELDS = [
    "run_id",
    "source",
    "file_name",
    "file_hash",
    "ingested_at",
    "raw_path",
    "status",
    "quarantine_path",
    "message",
]

RUN_LOG_FIELDS = [
    "run_id",
    "started_at",
    "finished_at",
    "discovered",
    "ingested",
    "duplicates",
    "ignored",
    "unregistered",
    "rejected",
    "run_status",
]

# Statuses that mean the file was refused and quarantined.
REJECTED_STATUSES = {
    "INVALID_FORMAT",
    "INVALID_CONTENT",
    "EMPTY_FILE",
    "INGESTION_ERROR",
}


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


# ---------------------------------------------------------
# MANIFEST AND RUN LOG
# ---------------------------------------------------------

def upgrade_manifest_header() -> None:
    """
    Earlier versions of the manifest had fewer columns.
    New columns are only ever appended, so rewriting the header
    line is enough: csv.DictReader fills the missing trailing
    values of older rows with None.
    """

    if not MANIFEST_FILE.exists():
        return

    lines = MANIFEST_FILE.read_text(encoding="utf-8").splitlines(
        keepends=True,
    )

    if not lines:
        return

    current_header = lines[0].strip().split(",")

    if current_header == MANIFEST_FIELDS:
        return

    if current_header != MANIFEST_FIELDS[:len(current_header)]:
        raise ValueError(
            f"Unrecognised manifest header in {MANIFEST_FILE}: "
            f"{current_header}"
        )

    lines[0] = ",".join(MANIFEST_FIELDS) + "\n"

    # Write a complete copy, then swap it in, so an interrupted
    # upgrade can never leave a truncated manifest.
    temporary = MANIFEST_FILE.with_name(MANIFEST_FILE.name + ".partial")
    temporary.write_text("".join(lines), encoding="utf-8")
    temporary.replace(MANIFEST_FILE)


def append_csv_row(
    path: Path,
    fieldnames: list[str],
    row: dict,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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
        )

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)


def write_manifest_record(
    run_id: str,
    source: str,
    file: Path,
    file_hash: str | None,
    status: str,
    raw_path: Path | None = None,
    quarantine_path: Path | None = None,
    message: str | None = None,
) -> None:

    append_csv_row(
        MANIFEST_FILE,
        MANIFEST_FIELDS,
        {
            "run_id": run_id,
            "source": source,
            "file_name": file.relative_to(INCOMING_DIR).as_posix(),
            "file_hash": file_hash,
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
            "raw_path": (
                raw_path.relative_to(PROJECT_ROOT).as_posix()
                if raw_path else None
            ),
            "status": status,
            "quarantine_path": (
                quarantine_path.relative_to(PROJECT_ROOT).as_posix()
                if quarantine_path else None
            ),
            "message": message,
        },
    )


def load_ingested_hashes() -> dict[str, str]:
    """
    Return {file_hash: "run_id/file_name"} for every file that has
    already been ingested, so duplicates can be detected without
    re-reading the manifest for each file.
    """

    ingested = {}

    if not MANIFEST_FILE.exists():
        return ingested

    with open(
        MANIFEST_FILE,
        "r",
        newline="",
        encoding="utf-8",
    ) as manifest:

        for row in csv.DictReader(manifest):

            if row["status"] == "INGESTED":
                ingested[row["file_hash"]] = (
                    f"{row['run_id']}/{row['file_name']}"
                )

    return ingested


# ---------------------------------------------------------
# FILE CHECKS
# ---------------------------------------------------------

def create_run_id() -> str:

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return f"ING-{timestamp}"


def calculate_file_hash(file: Path) -> str:

    sha256 = hashlib.sha256()

    with open(file, "rb") as file_handle:

        while chunk := file_handle.read(8192):
            sha256.update(chunk)

    return sha256.hexdigest()


def identify_source(
    file: Path,
    source_registry: dict,
) -> str | None:

    relative_path = file.relative_to(INCOMING_DIR)

    for source_name, source_config in source_registry.items():

        if relative_path.full_match(source_config["path"]):
            return source_name

    return None


def is_ignored(
    file: Path,
    ignored_patterns: list[str],
) -> bool:

    relative_path = file.relative_to(INCOMING_DIR)

    return any(
        relative_path.full_match(pattern)
        for pattern in ignored_patterns
    )


def validate_file_format(
    file: Path,
    source_name: str,
    source_registry: dict,
) -> bool:

    expected_format = source_registry[source_name]["format"]

    actual_format = file.suffix.lower().lstrip(".")

    return actual_format == expected_format.lower()


def verify_file_content(
    file: Path,
    expected_format: str,
) -> tuple[bool, str | None]:
    """
    Check that the file content matches its declared format.
    Returns (is_valid, reason).
    """

    with open(file, "rb") as file_handle:
        head = file_handle.read(65536)

    expected_format = expected_format.lower()

    if expected_format in FILE_SIGNATURES:

        signature = FILE_SIGNATURES[expected_format]

        if head.startswith(signature):
            return True, None

        return False, (
            f"File content is not a valid .{expected_format} file "
            f"(first bytes: {head[:8]!r})."
        )

    if expected_format == "csv":

        if b"\x00" in head:
            return False, "CSV file contains binary content."

        try:
            head.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            # A multi-byte character may be cut at the 64 KB
            # boundary; only fail if the error is before that.
            if error.start < len(head) - 4:
                return False, f"CSV file is not UTF-8 encoded: {error}."

        return True, None

    return True, None


# ---------------------------------------------------------
# FILE PLACEMENT
# ---------------------------------------------------------

def copy_preserving_path(
    file: Path,
    target_directory: Path,
) -> Path:

    destination = target_directory / file.relative_to(INCOMING_DIR)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Copy under a temporary name and rename when complete, so an
    # interrupted copy never looks like a finished file.
    temporary = destination.with_name(destination.name + ".partial")

    shutil.copy2(
        file,
        temporary,
    )

    temporary.replace(destination)

    return destination


# ---------------------------------------------------------
# INGESTION
# ---------------------------------------------------------

def process_file(
    file: Path,
    run_id: str,
    source_registry: dict,
    ignored_patterns: list[str],
    ingested_hashes: dict[str, str],
) -> dict:
    """
    Classify, verify and (if valid and new) copy one incoming file
    to RAW. Every outcome is written to the manifest.
    """

    if is_ignored(file, ignored_patterns):

        message = "File is listed under ignored_files in the source registry."

        # Recorded so "why was this file not ingested?" always has an answer.
        write_manifest_record(
            run_id, "IGNORED", file, None,
            status="IGNORED",
            message=message,
        )

        return {
            "source": "IGNORED",
            "status": "IGNORED",
            "file_hash": None,
            "message": message,
        }

    source = identify_source(file, source_registry)

    if source is None:

        file_hash = calculate_file_hash(file)

        message = (
            "No source in the registry matches this file name. "
            "Register the source or rename the file."
        )

        write_manifest_record(
            run_id, "UNKNOWN", file, file_hash,
            status="UNREGISTERED",
            message=message,
        )

        return {
            "source": "UNKNOWN",
            "status": "UNREGISTERED",
            "file_hash": file_hash,
            "message": message,
        }

    file_hash = calculate_file_hash(file)
    expected_format = source_registry[source]["format"]

    status = None
    message = None

    if file.stat().st_size == 0:

        status = "EMPTY_FILE"
        message = "File is empty (0 bytes)."

    elif not validate_file_format(file, source, source_registry):

        status = "INVALID_FORMAT"
        message = (
            f"Expected a .{expected_format} file, "
            f"received '{file.suffix}'."
        )

    else:

        content_valid, reason = verify_file_content(
            file,
            expected_format,
        )

        if not content_valid:
            status = "INVALID_CONTENT"
            message = reason

    if status in REJECTED_STATUSES:

        steward = (source_registry[source].get("governance") or {}).get("data_steward")

        if steward:
            message += f" Notify: {steward}."

        quarantine_path = copy_preserving_path(
            file,
            QUARANTINE_DIR / run_id,
        )

        write_manifest_record(
            run_id, source, file, file_hash,
            status=status,
            quarantine_path=quarantine_path,
            message=message,
        )

        return {
            "source": source,
            "status": status,
            "file_hash": file_hash,
            "message": message,
        }

    if file_hash in ingested_hashes:

        message = (
            f"Identical content already ingested as "
            f"{ingested_hashes[file_hash]}."
        )

        write_manifest_record(
            run_id, source, file, file_hash,
            status="SKIPPED_DUPLICATE",
            message=message,
        )

        return {
            "source": source,
            "status": "SKIPPED_DUPLICATE",
            "file_hash": file_hash,
            "message": message,
        }

    raw_path = copy_preserving_path(
        file,
        RAW_DIR / run_id,
    )

    write_manifest_record(
        run_id, source, file, file_hash,
        status="INGESTED",
        raw_path=raw_path,
    )

    ingested_hashes[file_hash] = (
        f"{run_id}/{file.relative_to(INCOMING_DIR).as_posix()}"
    )

    return {
        "source": source,
        "status": "INGESTED",
        "file_hash": file_hash,
        "message": None,
    }


def describe_exception(error: Exception) -> str:
    """The error message plus the line of this script that raised it."""

    frames = [
        frame for frame in traceback.extract_tb(error.__traceback__)
        if Path(frame.filename).resolve() == Path(__file__).resolve()
    ]

    location = (
        f" (at {Path(__file__).name}:{frames[-1].lineno} in {frames[-1].name})"
        if frames else ""
    )

    return f"{type(error).__name__}: {error}{location}"


def record_ingestion_error(
    file: Path,
    run_id: str,
    source_registry: dict,
    error: Exception,
) -> dict:
    """
    An unexpected error stopped this file (for example, it was locked
    by another program). Log the cause and location in the manifest.
    """

    try:
        source = identify_source(file, source_registry) or "UNKNOWN"
    except Exception:
        source = "UNKNOWN"

    message = (
        f"Unexpected error while ingesting the file: "
        f"{describe_exception(error)}. The file was not ingested; "
        f"fix the cause and re-run ingestion."
    )

    write_manifest_record(
        run_id, source, file, None,
        status="INGESTION_ERROR",
        message=message,
    )

    return {
        "source": source,
        "status": "INGESTION_ERROR",
        "file_hash": None,
        "message": message,
    }


def discover_files() -> list[Path]:

    return sorted(
        file
        for file in INCOMING_DIR.rglob("*")
        if file.is_file()
        and file.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def determine_run_status(run_stats: dict) -> str:

    if run_stats["rejected"] > 0:
        return "COMPLETED_WITH_ERRORS"

    if run_stats["unregistered"] > 0:
        return "COMPLETED_WITH_WARNINGS"

    return "COMPLETED"


def run_ingestion() -> dict:

    started_at = datetime.now().isoformat(timespec="seconds")

    registry_config = load_registry_config()
    source_registry = registry_config["sources"]
    ignored_patterns = registry_config.get("ignored_files") or []

    upgrade_manifest_header()

    ingested_hashes = load_ingested_hashes()

    files = discover_files()

    run_id = create_run_id()

    print(f"\nINGESTION RUN: {run_id}")
    print("\nDISCOVERY RESULTS:")

    run_stats = {
        "discovered": len(files),
        "ingested": 0,
        "duplicates": 0,
        "ignored": 0,
        "unregistered": 0,
        "rejected": 0,
    }

    status_counters = {
        "INGESTED": "ingested",
        "SKIPPED_DUPLICATE": "duplicates",
        "IGNORED": "ignored",
        "UNREGISTERED": "unregistered",
    }

    for file in files:

        try:

            outcome = process_file(
                file,
                run_id,
                source_registry,
                ignored_patterns,
                ingested_hashes,
            )

        except Exception as error:

            # Record the failure against this file and carry on with
            # the rest of the batch.
            outcome = record_ingestion_error(file, run_id, source_registry, error)

        status = outcome["status"]

        run_stats[status_counters.get(status, "rejected")] += 1

        file_hash = outcome["file_hash"]

        print(
            f"{file.relative_to(INCOMING_DIR).as_posix()}"
            f" -> {outcome['source']}"
            f" -> {status}"
            f" -> {file_hash[:12] if file_hash else '-'}"
        )

        if status in REJECTED_STATUSES:
            print(f"   {outcome['message']}")

    run_status = determine_run_status(run_stats)

    append_csv_row(
        RUN_LOG_FILE,
        RUN_LOG_FIELDS,
        {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            **run_stats,
            "run_status": run_status,
        },
    )

    print("\nRUN SUMMARY")
    print("--------------------------------------------")
    print(f"Run ID:         {run_id}")
    print(f"Discovered:     {run_stats['discovered']}")
    print(f"Ingested:       {run_stats['ingested']}")
    print(f"Duplicates:     {run_stats['duplicates']}")
    print(f"Ignored:        {run_stats['ignored']}")
    print(f"Unregistered:   {run_stats['unregistered']}")
    print(f"Rejected:       {run_stats['rejected']}")
    print(f"Run status:     {run_status}")

    if run_stats["ingested"]:
        print(f"RAW folder:     {(RAW_DIR / run_id).relative_to(PROJECT_ROOT)}")

    return {
        "run_id": run_id,
        "run_status": run_status,
        **run_stats,
    }


if __name__ == "__main__":

    run_ingestion()
