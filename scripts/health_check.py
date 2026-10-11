"""
Platform health check (alert-ready).

Reads audit.vw_PlatformHealth and prints each check. Exit codes are
designed for schedulers and monitoring tools:

    0 = all OK, 1 = at least one WARNING, 2 = at least one CRITICAL

The result is also written to logs/health_latest.json, which an alerting
tool (or a scheduled task sending e-mail) can watch.

    python scripts/health_check.py
    python scripts/health_check.py --summary     # one-line summary last (status board)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src" / "warehouse"))

from db_connection import get_connection  # noqa: E402


def main() -> int:

    parser = argparse.ArgumentParser(description="Platform health check.")
    parser.add_argument("--summary", action="store_true", help="Print a one-line summary last.")
    args = parser.parse_args()

    try:
        connection = get_connection()
        try:
            rows = (
                connection.cursor()
                .execute("SELECT CheckName, HealthStatus, Detail FROM audit.vw_PlatformHealth ORDER BY CheckName")
                .fetchall()
            )
        finally:
            connection.close()
    except Exception as error:
        print(f"HEALTH CHECK FAILED: cannot query the database ({error})")
        return 2

    checks = [{"check": r.CheckName, "status": r.HealthStatus, "detail": r.Detail} for r in rows]

    for check in checks:
        print(f"{check['status']:<9} {check['check']:<32} {check['detail']}")

    worst = (
        2
        if any(c["status"] == "CRITICAL" for c in checks)
        else 1
        if any(c["status"] == "WARNING" for c in checks)
        else 0
    )

    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "health_latest.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "status": ["OK", "WARNING", "CRITICAL"][worst],
                "checks": checks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.summary:
        problems = [c for c in checks if c["status"] != "OK"]
        print(
            f"{['OK', 'WARNING', 'CRITICAL'][worst]}: "
            + ("; ".join(f"{c['check']} {c['status']}" for c in problems) if problems else "all checks OK")
        )

    return worst


if __name__ == "__main__":
    sys.exit(main())
