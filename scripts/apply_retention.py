"""
Apply the staging retention policy (archive, then remove old batches).

Each staging table keeps its newest `staging_batches_retained` batches
(config/pipeline.yaml) and every batch selected by the latest pipeline
run. Older batches are written to Parquet under data/archive/staging/,
the archive is re-read and verified, its SHA-256 recorded, and only then
are the rows deleted - all recorded in audit.RetentionAction.

The default is a DRY RUN that only reports. Deleting requires --apply
(an explicit, approved operator action). Warehouse history, audit
records and RAW evidence are never touched.

    python scripts/apply_retention.py            # report only
    python scripts/apply_retention.py --apply    # archive and remove
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src" / "validation"))
sys.path.insert(0, str(ROOT / "src" / "warehouse"))

from db_connection import get_connection  # noqa: E402
from metadata_sync import load_yaml  # noqa: E402

ARCHIVE_DIR = ROOT / "data" / "archive" / "staging"
TABLE = re.compile(r"^stg\.[A-Za-z]+$")


def retention_candidates(cursor, keep: int) -> list[tuple]:
    """(table, run_id, hash, rows) of batches older than the retained ones."""

    tables = [
        row[0]
        for row in cursor.execute(
            "SELECT DISTINCT StagingTable FROM meta.DataSource WHERE StagingTable IS NOT NULL"
        ).fetchall()
    ]

    protected = {
        (r[0], r[1])
        for r in cursor.execute(
            "SELECT IngestionRunID, SourceFileHash FROM audit.PipelineSourceBatch "
            "WHERE PipelineExecutionID = (SELECT MAX(PipelineExecutionID) FROM audit.PipelineSourceBatch)"
        ).fetchall()
    }

    candidates = []

    for table in tables:
        if not TABLE.match(table):
            raise ValueError(f"Unexpected staging table name {table!r}")

        batches = cursor.execute(
            f"SELECT IngestionRunID, SourceFileHash, COUNT(*) FROM {table} "
            f"GROUP BY IngestionRunID, SourceFileHash ORDER BY IngestionRunID DESC"
        ).fetchall()

        # Document sets hold one batch per document; retention counts runs.
        runs = sorted({b[0] for b in batches}, reverse=True)
        keep_runs = set(runs[:keep])

        for run_id, file_hash, rows in batches:
            if run_id not in keep_runs and (run_id, file_hash) not in protected:
                candidates.append((table, run_id, file_hash, rows))

    return candidates


def archive_and_delete(connection, table: str, run_id: str, file_hash: str) -> tuple[int, str, str]:

    cursor = connection.cursor()
    cursor.execute(f"SELECT * FROM {table} WHERE IngestionRunID = ? AND SourceFileHash = ?", run_id, file_hash)
    columns = [c[0] for c in cursor.description]
    rows = cursor.fetchall()

    data = {name: [str(row[i]) if row[i] is not None else None for row in rows] for i, name in enumerate(columns)}
    target = ARCHIVE_DIR / table.split(".")[1] / f"{run_id}_{file_hash[:12]}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(data), target)

    if pq.read_table(target).num_rows != len(rows):
        raise RuntimeError(f"Archive verification failed for {target}")

    checksum = hashlib.sha256(target.read_bytes()).hexdigest()

    cursor.execute(f"DELETE FROM {table} WHERE IngestionRunID = ? AND SourceFileHash = ?", run_id, file_hash)
    deleted = cursor.rowcount

    if deleted != len(rows):
        raise RuntimeError(f"Deleted {deleted} rows but archived {len(rows)}; rolled back.")

    relative = target.relative_to(ROOT).as_posix()
    cursor.execute(
        "INSERT INTO audit.RetentionAction (ObjectName, IngestionRunID, SourceFileHash, RowsArchived, RowsDeleted, "
        "ArchivePath, ArchiveChecksum, Reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        table,
        run_id,
        file_hash,
        len(rows),
        deleted,
        relative,
        checksum,
        "Staging batch older than the retained batches (pipeline.yaml staging_batches_retained).",
    )
    connection.commit()

    return deleted, relative, checksum


def main() -> int:

    parser = argparse.ArgumentParser(description="Archive and remove old staging batches.")
    parser.add_argument("--apply", action="store_true", help="Archive and delete (default: report only).")
    args = parser.parse_args()

    keep = int((load_yaml("pipeline.yaml").get("pipeline") or {}).get("staging_batches_retained", 3))
    connection = get_connection()

    try:
        candidates = retention_candidates(connection.cursor(), keep)

        print(f"Retention policy: keep the newest {keep} run(s) per staging table and every batch in use.")

        if not candidates:
            print("Nothing to archive.")
            return 0

        for table, run_id, file_hash, rows in candidates:
            if not args.apply:
                print(f"WOULD ARCHIVE {table} {run_id} ({rows} rows)")
                continue

            try:
                deleted, path, checksum = archive_and_delete(connection, table, run_id, file_hash)
                print(f"ARCHIVED {table} {run_id}: {deleted} rows -> {path} (sha256 {checksum[:12]})")
            except Exception as error:
                connection.rollback()
                print(f"FAILED {table} {run_id}: {error}")
                return 2

        if not args.apply:
            print(f"\nDry run: {len(candidates)} batch(es). Re-run with --apply to archive and remove them.")

        return 0

    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
