import os
import pyodbc

from pathlib import Path
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def get_connection():

    server = os.getenv("SQL_SERVER")
    database = os.getenv(
        "SQL_DATABASE",
        "RetailDataPlatform"
    )
    driver = os.getenv(
        "SQL_DRIVER",
        "ODBC Driver 18 for SQL Server"
    )

    if not server:
        raise ValueError(
            "SQL_SERVER is not configured in .env"
        )

    connection_string = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "Trusted_Connection=yes;"
        "Encrypt=no;"
    )

    return pyodbc.connect(
        connection_string,
        timeout=10
    )


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