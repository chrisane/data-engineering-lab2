# Developer setup guide

## Prerequisites

- Windows 10/11, PowerShell, VS Code
- Python 3.14 (3.13+ works)
- SQL Server 2019 (Developer Edition) with Windows authentication, ODBC Driver 17 or 18
- Git

## 1. Environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
pre-commit install --hook-type pre-commit --hook-type pre-push    # optional local quality gate
```

Create `.env` in the project root (never committed - it is in `.gitignore`):

```ini
SQL_SERVER=localhost
SQL_DATABASE=RetailDataPlatform
SQL_DRIVER=ODBC Driver 17 for SQL Server
# Optional SQL authentication (CI); Windows authentication is used when unset:
# SQL_USERNAME=
# SQL_PASSWORD=
```

Check the connection: `python test_sql_connection.py`.

## 2. Database

```powershell
python scripts/migrate.py apply          # creates the database and applies every migration
python scripts/sync_metadata.py          # validates config/ and loads the meta catalogue
```

A database created before migration tracking existed is recorded once with
`python scripts/migrate.py baseline --through 14` (see [sql_migrations.md](sql_migrations.md)).

## 3. Data and pipeline

```powershell
.\run_pipeline.bat                       # everything, with the status board
```

or step by step:

```powershell
python scripts/generate_all.py
python src/ingestion/discover_files.py
python src/validation/validate_structure.py
python src/validation/validate_data.py
python src/validation/check_coverage.py
python src/validation/load_eligibility.py
python src/warehouse/orchestrator.py
python scripts/health_check.py
```

## 4. Tests

```powershell
python -m pytest tests                                   # unit, configuration, SQL standards (seconds)
python -m pytest tests/integration -m integration -v     # end to end on a throwaway database (~10 min)
ruff check . ; ruff format --check . ; bandit -q -c pyproject.toml -r src scripts
python scripts/generate_docs.py --check                  # catalogue docs up to date
```

The integration tests build `RetailDataPlatform_IT` from the migrations, run the pipeline twice on the
repository's validated data and drop nothing in your working database.

## 5. Adding a new source (worked procedure)

1. **Contract.** Add the source to `config/source_registry.yaml`: `format`, `path`, `staging_table`,
   `structure`, `data_rules`, `business_rules`, `governance` (owner, steward, classification, personal
   data, retention, frequency). PDFs also need `extraction`.
2. **Definitions.** Add every column to `config/data_dictionary.yaml`.
3. **Validate the contract.** `python -m pytest tests` (registry and configuration tests).
4. **Staging table.** `python scripts/generate_staging_ddl.py` prints the DDL; put the new table in a
   new migration `sql/NNN_create_staging_<name>.sql` (never edit an applied migration).
5. **Warehouse.** Add the dimension/fact table and its load procedure in a new migration, following an
   existing procedure (batch from `audit.usp_RequireSingleBatch`, lock, MERGE with row hash,
   `audit.usp_CompleteWarehouseExecution`, `audit.usp_RecordWarehouseFailure`).
6. **Handler.** Register the procedure in `src/warehouse/handlers.py` (code -> procedure -> target).
7. **Mapping.** Add the mapping to `config/pipeline.yaml`: handler (approved), dependencies,
   reconciliation controls, column lineage. Staging mappings are created automatically.
8. **Apply and verify.** `python scripts/migrate.py apply`, `python scripts/sync_metadata.py`,
   `python scripts/generate_docs.py`, run the unit and integration tests, then the pipeline.

No orchestrator change is needed: the plan, ordering, gating and reconciliation all come from metadata.

## 6. Conventions

- Python: ruff (lint + format, 120 columns); scripts are runnable from the project root.
- SQL: one migration per change, idempotent guards, `SET XACT_ABORT ON`, parameterised dynamic SQL
  only with validated identifiers, explicit column lists in presentation views (checked by
  `tests/test_sql_standards.py`).
- Naming: `stg.<Entity>` with source column names; `dw.Dim<Entity>` / `dw.Fact<Process>` in PascalCase;
  `mart.vw_<Purpose>`; handler codes `DIM_*`, `FACT_*`, `RECON_*`; mapping codes `STG_*`, `DW_*`.
