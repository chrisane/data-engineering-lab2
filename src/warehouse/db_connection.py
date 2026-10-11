"""
SQL Server connection helper.

Settings come from .env (never from code or the repository):

    SQL_SERVER=localhost
    SQL_DATABASE=RetailDataPlatform
    SQL_DRIVER=ODBC Driver 17 for SQL Server
    # Optional - SQL authentication (e.g. CI). Windows authentication
    # is used when SQL_USERNAME is not set.
    SQL_USERNAME=
    SQL_PASSWORD=

Environment variables already set in the process (for example by the
test suite or CI) take precedence over .env.
"""

import os
import time
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_DATABASE = "RetailDataPlatform"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"

# SQLSTATEs / native errors that are worth retrying: deadlock victim,
# lock timeout, query timeout and dropped connections.
TRANSIENT_SQLSTATES = {"40001", "HYT00", "HYT01", "08S01", "08001"}
TRANSIENT_NATIVE_ERRORS = {1205, 1222, -2}


def database_name() -> str:

    return os.getenv("SQL_DATABASE") or DEFAULT_DATABASE


def connection_string(database: str | None = None) -> str:

    server = os.getenv("SQL_SERVER")
    driver = os.getenv("SQL_DRIVER") or DEFAULT_DRIVER

    if not server:
        raise ValueError("SQL_SERVER is not configured in .env")

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database or database_name()}",
        "Encrypt=no",
        "TrustServerCertificate=yes",
    ]

    username = os.getenv("SQL_USERNAME")

    if username:
        parts += [f"UID={username}", f"PWD={os.getenv('SQL_PASSWORD', '')}"]
    else:
        parts.append("Trusted_Connection=yes")

    return ";".join(parts) + ";"


def get_connection(database: str | None = None, autocommit: bool = False):
    """
    Open a connection. Retries a few times if the server is briefly
    unreachable, so a restarting service does not fail a whole run.
    """

    last_error = None

    for attempt in range(3):
        try:
            connection = pyodbc.connect(connection_string(database), timeout=10)
            connection.autocommit = autocommit
            return connection

        except pyodbc.Error as error:
            last_error = error
            if not is_transient(error):
                raise
            time.sleep(2 * (attempt + 1))

    raise last_error


def is_transient(error: Exception) -> bool:
    """True for errors a retry may resolve (deadlocks, timeouts, network)."""

    if not isinstance(error, pyodbc.Error) or not error.args:
        return False

    sqlstate = str(error.args[0])
    message = str(error.args[-1])

    if sqlstate in TRANSIENT_SQLSTATES:
        return True

    return any(f"({code})" in message for code in TRANSIENT_NATIVE_ERRORS)


if __name__ == "__main__":
    with get_connection() as connection:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT
                @@SERVERNAME AS ServerName,
                DB_NAME() AS DatabaseName,
                SYSTEM_USER AS ConnectedUser
        """)

        row = cursor.fetchone()

        print("\nSQL CONNECTION SUCCESSFUL")
        print(f"Server:   {row.ServerName}")
        print(f"Database: {row.DatabaseName}")
        print(f"User:     {row.ConnectedUser}")
