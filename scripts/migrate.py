"""
Apply and inspect versioned SQL migrations (sql/NNN_*.sql).

    python scripts/migrate.py status              # what is applied / pending
    python scripts/migrate.py apply               # apply pending migrations
    python scripts/migrate.py baseline --through 14
    python scripts/migrate.py apply --database RetailDataPlatform_Test

The target database defaults to SQL_DATABASE in .env.
Exit codes: 0 = up to date / success, 1 = pending or changed, 2 = error.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "warehouse"))

from db_connection import database_name  # noqa: E402
from migrations import MigrationError, apply, baseline, status  # noqa: E402


def main() -> int:

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("command", choices=["status", "apply", "baseline"])
    parser.add_argument("--database", help="Target database (default: SQL_DATABASE).")
    parser.add_argument("--through", type=int, help="baseline: last version already applied.")
    args = parser.parse_args()

    database = args.database or database_name()

    try:
        if args.command == "status":
            report = status(database)

            print(f"\nMIGRATIONS: {database}")

            for item in report:
                applied = f"{item['applied_at']:%Y-%m-%d %H:%M}" if item["applied_at"] else ""
                print(f"  {item['name']:<50} {item['state']:<9} {applied}")

            problems = [item for item in report if item["state"] in ("PENDING", "CHANGED")]

            print(
                f"\n{len(report) - len(problems)} applied, "
                f"{sum(i['state'] == 'PENDING' for i in report)} pending, "
                f"{sum(i['state'] == 'CHANGED' for i in report)} changed"
            )

            return 1 if problems else 0

        if args.command == "baseline":
            if args.through is None:
                parser.error("baseline requires --through")

            recorded = baseline(args.through, database)
            print(f"Baseline recorded {len(recorded)} migration(s) in {database}.")
            return 0

        done = apply(database)
        print(f"{len(done)} migration(s) applied to {database}." if done else f"{database} is up to date.")
        return 0

    except MigrationError as error:
        print(f"MIGRATION ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
