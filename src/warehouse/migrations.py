"""
Versioned, repeatable SQL migrations.

Every script in sql/ named NNN_description.sql is a migration. Applied
migrations are recorded in meta.SchemaMigration with a SHA-256 checksum,
so each script runs exactly once per database and any later edit to an
applied script is detected (schema drift).

Rules
    - Migrations run in version order; gaps or duplicate numbers are errors.
    - Each migration runs in one transaction (except database creation),
      so a failed migration leaves no partial changes.
    - Batches are split on lines containing only GO, as in SSMS.
    - `USE RetailDataPlatform` and the database-creation statements are
      redirected to the target database, so the same scripts build
      throwaway test databases.

Usage (see scripts/migrate.py):
    python scripts/migrate.py status
    python scripts/migrate.py apply
    python scripts/migrate.py baseline --through 14
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from db_connection import DEFAULT_DATABASE, database_name, get_connection

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = PROJECT_ROOT / "sql"

MIGRATION_PATTERN = re.compile(r"^(\d{3})_[a-z0-9_.]+\.sql$")
GO_PATTERN = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)

# Scripts that create the database cannot run inside a transaction.
NON_TRANSACTIONAL_PATTERN = re.compile(r"\bCREATE\s+DATABASE\b", re.IGNORECASE)

TRACKING_TABLE_SQL = """
IF SCHEMA_ID(N'meta') IS NULL EXEC(N'CREATE SCHEMA meta');
IF OBJECT_ID(N'meta.SchemaMigration', N'U') IS NULL
BEGIN
    CREATE TABLE meta.SchemaMigration (
        Version INT NOT NULL CONSTRAINT PK_SchemaMigration PRIMARY KEY,
        ScriptName NVARCHAR(260) NOT NULL,
        Checksum CHAR(64) NOT NULL,
        AppliedAt DATETIME2(7) NOT NULL
            CONSTRAINT DF_SchemaMigration_AppliedAt DEFAULT SYSUTCDATETIME(),
        AppliedBy NVARCHAR(128) NOT NULL
            CONSTRAINT DF_SchemaMigration_AppliedBy DEFAULT SUSER_SNAME(),
        DurationMs INT NULL,
        IsBaseline BIT NOT NULL CONSTRAINT DF_SchemaMigration_IsBaseline DEFAULT 0
    );
END;
"""


@dataclass
class Migration:
    version: int
    path: Path
    checksum: str

    @property
    def name(self) -> str:
        return self.path.name


class MigrationError(RuntimeError):
    pass


# ---------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------


def file_checksum(path: Path) -> str:
    """Checksum of the script with line endings normalised (CRLF == LF)."""

    text = path.read_bytes().replace(b"\r\n", b"\n")

    return hashlib.sha256(text).hexdigest()


def discover(sql_dir: Path = SQL_DIR) -> list[Migration]:

    migrations = []
    seen = {}

    for path in sorted(sql_dir.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)

        if not match:
            raise MigrationError(
                f"{path.name} does not follow the NNN_description.sql naming "
                f"convention (lower case, digits and underscores)."
            )

        version = int(match.group(1))

        if version in seen:
            raise MigrationError(f"Duplicate migration version {version:03d}: {seen[version]} and {path.name}")

        seen[version] = path.name
        migrations.append(Migration(version, path, file_checksum(path)))

    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(f"Migration numbering has a gap: expected {expected:03d}, found {migration.name}")

    return migrations


def split_batches(sql: str) -> list[str]:

    return [batch.strip() for batch in GO_PATTERN.split(sql) if batch.strip()]


def retarget(sql: str, database: str) -> str:
    """Point database-specific statements at the target database."""

    if database == DEFAULT_DATABASE:
        return sql

    quoted = f"[{database}]"

    sql = re.sub(r"\bUSE\s+\[?RetailDataPlatform\]?", f"USE {quoted}", sql, flags=re.IGNORECASE)
    sql = re.sub(r"DB_ID\(N?'RetailDataPlatform'\)", f"DB_ID(N'{database}')", sql)
    sql = re.sub(
        r"\bCREATE\s+DATABASE\s+\[?RetailDataPlatform\]?",
        f"CREATE DATABASE {quoted}",
        sql,
        flags=re.IGNORECASE,
    )

    return sql


# ---------------------------------------------------------
# DATABASE STATE
# ---------------------------------------------------------


def ensure_database(database: str) -> None:

    connection = get_connection("master", autocommit=True)

    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DB_ID(?)", database)

        if cursor.fetchone()[0] is None:
            cursor.execute(f"CREATE DATABASE [{database}]")

    finally:
        connection.close()


def applied_migrations(connection) -> dict[int, dict]:

    cursor = connection.cursor()
    cursor.execute(TRACKING_TABLE_SQL)
    connection.commit()

    cursor.execute("SELECT Version, ScriptName, Checksum, AppliedAt, IsBaseline FROM meta.SchemaMigration")

    return {
        row.Version: {
            "name": row.ScriptName,
            "checksum": row.Checksum,
            "applied_at": row.AppliedAt,
            "baseline": bool(row.IsBaseline),
        }
        for row in cursor.fetchall()
    }


def status(database: str | None = None) -> list[dict]:
    """Each migration with its state: APPLIED, BASELINE, PENDING or CHANGED."""

    database = database or database_name()
    ensure_database(database)

    connection = get_connection(database)

    try:
        applied = applied_migrations(connection)
    finally:
        connection.close()

    report = []

    for migration in discover():
        record = applied.get(migration.version)

        if record is None:
            state = "PENDING"
        elif record["checksum"] != migration.checksum:
            state = "CHANGED"
        else:
            state = "BASELINE" if record["baseline"] else "APPLIED"

        report.append(
            {
                "version": migration.version,
                "name": migration.name,
                "state": state,
                "applied_at": record["applied_at"] if record else None,
            }
        )

    return report


def database_has_existing_objects(connection) -> bool:

    cursor = connection.cursor()
    cursor.execute("SELECT OBJECT_ID(N'meta.DataSource', N'U')")

    return cursor.fetchone()[0] is not None


# ---------------------------------------------------------
# APPLY
# ---------------------------------------------------------


def apply(database: str | None = None, verbose: bool = True) -> list[str]:
    """
    Apply pending migrations in order. Stops at the first failure,
    which is rolled back. Refuses to run if an applied script changed.
    """

    database = database or database_name()
    ensure_database(database)

    migrations = discover()
    connection = get_connection(database)

    try:
        applied = applied_migrations(connection)

        changed = [m.name for m in migrations if m.version in applied and applied[m.version]["checksum"] != m.checksum]

        if changed:
            raise MigrationError(
                f"Applied migration(s) were edited after being applied: {changed}. "
                f"Revert the edit and put the change in a new migration."
            )

        if not applied and database_has_existing_objects(connection):
            raise MigrationError(
                f"Database {database} already contains platform objects but has no "
                f"migration history. Record the existing state first with: "
                f"python scripts/migrate.py baseline --through <last applied version>"
            )

        done = []

        for migration in migrations:
            if migration.version in applied:
                continue

            if verbose:
                print(f"Applying {migration.name} ...", flush=True)

            run_migration(connection, migration, database)
            done.append(migration.name)

        return done

    finally:
        connection.close()


def run_migration(connection, migration: Migration, database: str) -> None:

    sql = retarget(migration.path.read_text(encoding="utf-8-sig"), database)
    batches = split_batches(sql)
    transactional = not NON_TRANSACTIONAL_PATTERN.search(sql)

    started = datetime.now()
    cursor = connection.cursor()

    connection.autocommit = not transactional

    try:
        for number, batch in enumerate(batches, start=1):
            try:
                cursor.execute(batch)

                # Drain result sets (verification SELECTs) so the next
                # batch can run on the same connection.
                while cursor.nextset():
                    pass

            except Exception as error:
                raise MigrationError(
                    f"{migration.name} failed in batch {number} of {len(batches)}: "
                    f"{error}\n--- batch starts ---\n{batch[:600]}"
                ) from error

        # Scripts may switch database with USE; record in the target.
        cursor.execute(f"USE [{database}]")

        duration = int((datetime.now() - started).total_seconds() * 1000)

        cursor.execute(
            "INSERT INTO meta.SchemaMigration (Version, ScriptName, Checksum, DurationMs) VALUES (?, ?, ?, ?)",
            migration.version,
            migration.name,
            migration.checksum,
            duration,
        )

        if transactional:
            connection.commit()

    except Exception:
        if transactional:
            connection.rollback()

        raise

    finally:
        connection.autocommit = False


def baseline(through: int, database: str | None = None) -> list[str]:
    """
    Record migrations 1..through as already applied, without running them.
    Use once on a database built before migration tracking existed.
    """

    database = database or database_name()
    connection = get_connection(database)

    try:
        applied = applied_migrations(connection)
        cursor = connection.cursor()
        recorded = []

        for migration in discover():
            if migration.version > through or migration.version in applied:
                continue

            cursor.execute(
                "INSERT INTO meta.SchemaMigration "
                "(Version, ScriptName, Checksum, DurationMs, IsBaseline) "
                "VALUES (?, ?, ?, NULL, 1)",
                migration.version,
                migration.name,
                migration.checksum,
            )
            recorded.append(migration.name)

        connection.commit()

        return recorded

    finally:
        connection.close()
