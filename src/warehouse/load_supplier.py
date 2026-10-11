"""
Supplier Excel -> SQL Server staging loader (standalone entry point).

Kept for backward compatibility: the supplier load is now performed by
the generic, contract-driven staging loader (staging_loader.py) with the
same safeguards - fresh load-eligibility decision, RAW location and
SHA-256 check, exclusive lock, idempotent skip, one transaction and a
terminal audit.LoadExecution row for every attempt.

Run from the project root:

    python src/warehouse/load_supplier.py

Exit codes: 0 = loaded or already staged, 1 = load failed, 2 = refused.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "src" / "validation"))

from common import load_registry_config  # noqa: E402
from staging_loader import current_decisions, load_source  # noqa: E402

SOURCE = "supplier_master"


def main() -> int:
    config = load_registry_config()
    result = load_source(SOURCE, current_decisions(config), config)

    print(result.summary())

    if not result.files:
        print(f"SQL LOAD REFUSED: no file version of {SOURCE} has been ingested.")
        return 2

    for file in result.files:
        print(f"{file.status}: {file.file} - {file.message} (audit LoadID={file.load_id})")

    if result.failed:
        return 1

    if all(f.status == "SKIPPED" and f.message.startswith("Not eligible") for f in result.files):
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
