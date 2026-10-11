# SQL migration guide

Database changes are deployed only through numbered migrations in `sql/` and the migration runner.

## Runner

```powershell
python scripts/migrate.py status                         # APPLIED / BASELINE / PENDING / CHANGED per script
python scripts/migrate.py apply                          # apply pending scripts in order
python scripts/migrate.py apply --database <name>        # any database (tests use RetailDataPlatform_IT)
python scripts/migrate.py baseline --through 14          # record an existing database once
```

- Applied scripts are recorded in `meta.SchemaMigration` with a SHA-256 checksum (line endings
  normalised), who applied them, when and how long they took.
- Each migration runs in **one transaction**; a failure rolls back the whole script.
- Batches are split on `GO` lines as in SSMS.
- `USE RetailDataPlatform` and database creation are redirected to the target database, so the same
  scripts build throwaway databases for testing.
- `apply` refuses to run if an applied script was edited (drift) or if the database has objects but no
  history (baseline first).

## Rules

1. Name scripts `NNN_short_description.sql`; numbers are consecutive (gaps are rejected).
2. **Never edit an applied migration.** Put the change in a new script. CI rejects pull requests that
   modify, rename or delete an existing migration.
3. Make DDL re-runnable: `IF OBJECT_ID(...) IS NULL` before `CREATE TABLE`, `COL_LENGTH` before
   `ADD`, `CREATE OR ALTER` for views and procedures (checked by tests).
4. No destructive statements (`DROP TABLE`, `TRUNCATE`, unconditional `DELETE`) except the approved
   full-refresh procedures. Removing data is an operator action (`scripts/apply_retention.py`).
5. Seed reference rows idempotently (`IF NOT EXISTS` / `MERGE`), never by hand.
6. Start every script with `USE RetailDataPlatform;` and `SET XACT_ABORT ON;`.

## History

| Scripts | Content |
|---|---|
| 001-014 | Schemas, metadata, supplier staging, SCD2, change audit, mappings, handlers, operational audit, supplier reconciliation (baselined on the original database). 002, 006 and 012 were made re-runnable and 012 now seeds the supplier handler/mapping rows that had been created by hand, before the baseline was recorded. |
| 015 | Governance attributes, mapping codes and retry policy, reconciliation controls, richer audit (heartbeat, abandoned runs, source batches, actionable errors, warehouse executions, fact change log), catalogue tables. |
| 016 | Staging tables for every source (generated from the registry). |
| 017-020 | Dimensions (with Unknown members and calendar), facts, load framework, all load procedures. |
| 021 | Data-quality result tables and monitoring views. |
| 022 | Product-supplier bridge. |
| 023-024 | Presentation star schema and business-rule views. |
| 025 | Roles and dynamic data masking. |
| 026 | Retention audit. |
| 027 | Materialised replenishment plan: `mart.ReplenishmentPlan` table and `mart.usp_BuildReplenishmentPlan` (mapping `MART_REPLENISHMENT_PLAN`); `vw_ReplenishmentPlan` and `vw_SupplierReorderProposal` read the table (same names and columns, ~200 s -> <1 s). |
| 028 | `stg.BranchCashup.template_version` (nullable): which registered cash-up template each document was read with. |
