# CI/CD guide

## Pipeline (`.github/workflows/ci.yml`)

| Job | When | Checks |
|---|---|---|
| quality | every push to `main`, every pull request, manual | ruff lint, ruff format check, bandit static security analysis, pip-audit dependency vulnerabilities, configuration and dependency validation (`sync_metadata.py --check`), released migrations unchanged (PRs), unit / configuration / regression / negative / SQL-standards tests, generated docs up to date |
| integration | opt-in (see below), after quality | SQL Server 2019 container; generate -> ingest -> validate -> gate; integration tests on a fresh database: schema vs registry, lineage completeness, full pipeline twice (idempotency), SCD2 history, fact corrections, reconciliation tampering, business rules, monitoring, masking |

### Enabling the integration job

1. Repository **secret** `CI_SQL_SA_PASSWORD`: a strong password used only for the throwaway CI
   container (never a real credential).
2. Repository **variable** `RUN_SQL_INTEGRATION` = `true`.

The job uses SQL authentication through `SQL_USERNAME` / `SQL_PASSWORD` environment variables, which
`db_connection.py` prefers over Windows authentication. It has not been run in GitHub yet (the local
environment has no GitHub CLI); the same tests pass locally against SQL Server 2019.

## Branching and releases

- Work on a branch, open a pull request into `main`; the quality job must pass.
- Database changes ship as new migrations with the code that needs them; deployment is
  `python scripts/migrate.py apply` followed by `python scripts/sync_metadata.py`.
- Configuration changes (`config/`) are reviewed like code; the metadata sync records the checksum and
  git commit of what was deployed (`meta.MetadataSyncLog`).

## Local equivalent

```powershell
pre-commit install --hook-type pre-commit --hook-type pre-push
ruff check . ; ruff format --check . ; bandit -q -c pyproject.toml -r src scripts ; pip-audit -r requirements.txt
python scripts/sync_metadata.py --check ; python -m pytest tests
python -m pytest tests/integration -m integration -v
```
