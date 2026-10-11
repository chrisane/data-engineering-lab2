"""Run the Supplier SCD2 warehouse transformation for the currently eligible version."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "validation"))

from common import FILE_STATUS_LOG_FILE, VALIDATION_LOG_FILE, load_registry_config, read_log, read_manifest
from db_connection import get_connection
from load_eligibility import decide


def main() -> int:
    decisions = decide(
        load_registry_config(),
        read_manifest(),
        read_log(FILE_STATUS_LOG_FILE),
        read_log(VALIDATION_LOG_FILE),
    )
    supplier = [d for d in decisions if d["source"] == "supplier_master"]
    if len(supplier) != 1:
        raise RuntimeError(f"Expected one Supplier decision; found {len(supplier)}")
    decision = supplier[0]
    if decision["decision"] != "ELIGIBLE":
        raise RuntimeError(f"WAREHOUSE REFUSED: {decision['decision']}: {decision.get('reason')}")
    run_id = decision["run_id"]
    digest = decision["file_hash"].lower()

    connection = get_connection()
    try:
        connection.autocommit = False
        cursor = connection.cursor()
        # Check that the exact eligible version has a completed staging load.
        cursor.execute(
            """
            SELECT COUNT(*) FROM audit.LoadExecution
            WHERE IngestionRunID=? AND SourceName='supplier_master'
              AND SourceFileHash=? AND LoadStatus='COMPLETED'
        """,
            run_id,
            digest,
        )
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("WAREHOUSE REFUSED: exact eligible version lacks a unique completed staging audit")
        cursor.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT SourceFileHash)
            FROM stg.Supplier WHERE IngestionRunID=? AND SourceFileHash=?
        """,
            run_id,
            digest,
        )
        staged, versions = cursor.fetchone()
        if staged == 0 or versions != 1:
            raise RuntimeError("WAREHOUSE REFUSED: no matching staged Supplier rows")
        connection.commit()
        cursor.execute("EXEC dw.usp_LoadDimSupplier @IngestionRunID=?", run_id)
        # Consume all result sets, if any, before querying audit.
        while cursor.nextset():
            pass
        connection.commit()
        cursor.execute(
            """
            SELECT TOP 1 ExecutionID, Status, SourceRows,
                   InsertedSuppliers, RevisedSuppliers, UnchangedSuppliers
            FROM audit.SupplierWarehouseExecution
            WHERE IngestionRunID=? ORDER BY ExecutionID DESC
        """,
            run_id,
        )
        result = cursor.fetchone()
        if result is None or result[1] not in ("COMPLETED", "SKIPPED"):
            raise RuntimeError("WAREHOUSE FAILED: no completed warehouse execution found")
        execution_id, status, source_rows, inserted, revised, unchanged = result
        if source_rows != staged or (inserted or 0) + (revised or 0) + (unchanged or 0) != staged:
            raise RuntimeError("WAREHOUSE FAILED: transformation counts do not reconcile")
        print(
            f"WAREHOUSE COMPLETED: run={run_id}; execution={execution_id}; "
            f"source={source_rows}, inserted={inserted}, revised={revised}, unchanged={unchanged}"
        )
        return 0
    except Exception as error:
        connection.rollback()
        print(f"WAREHOUSE FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
