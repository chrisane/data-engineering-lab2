"""
Validate the configuration and synchronise it into the SQL metadata
catalogue (meta schema).

    python scripts/sync_metadata.py              # validate + sync
    python scripts/sync_metadata.py --check      # validate only (no database; used by CI)

Exit codes: 0 = success, 1 = configuration problems, 2 = database error.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "warehouse"))

from metadata_sync import ConfigurationError, plan, sync  # noqa: E402


def main() -> int:

    parser = argparse.ArgumentParser(description="Synchronise configuration into the meta schema.")
    parser.add_argument("--check", action="store_true", help="Validate the configuration only.")
    parser.add_argument("--database", help="Target database (default: SQL_DATABASE).")
    args = parser.parse_args()

    try:
        catalogue = plan()
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 1

    print(
        f"Configuration valid: {len(catalogue.sources)} sources, {len(catalogue.elements)} data elements, "
        f"{len(catalogue.mappings)} mappings, {len(catalogue.dependencies)} dependencies, "
        f"{len(catalogue.controls)} reconciliation controls, {len(catalogue.rules)} business rules, "
        f"{len(catalogue.lineage)} lineage entries."
    )

    if args.check:
        return 0

    from db_connection import get_connection

    try:
        connection = get_connection(args.database)
        try:
            counts = sync(connection, catalogue)
        finally:
            connection.close()
    except Exception as error:
        print(f"METADATA SYNC FAILED: {error}", file=sys.stderr)
        return 2

    print(
        f"Metadata synchronised: {counts['inserted']} inserted, {counts['updated']} updated, "
        f"{counts['disabled']} disabled (checksum {catalogue.checksum[:12]})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
