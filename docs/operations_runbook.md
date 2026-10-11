# Operational runbook

## Daily operation

| What | How |
|---|---|
| Run everything | Double-click `run_pipeline.bat`, or `.\run_pipeline.bat --keep-data --skip-tests` unattended. |
| Schedule | `.\scripts\schedule_pipeline.ps1 -At "02:00"` registers a daily Windows task (`-Remove` deletes it). |
| Check health | `python scripts/health_check.py` (exit 0 OK, 1 warning, 2 critical; also writes `logs/health_latest.json`). |
| Why did a file fail? | `python scripts/trace_file.py <file, source or part of a name>` |
| Database state | `python scripts/migrate.py status` |

The status board stages, in order: data generation (asks first), ingestion, structural validation,
data validation, validation coverage, load eligibility, automated tests, database migrations,
metadata sync, warehouse orchestration, platform health.

Exit codes of the pipeline: `0` success, `1` completed with warnings (for example a quarantined
document, a partial load or a stale source), `2` failed or blocked, `130` cancelled.

## Monitoring queries (SQL Server)

```sql
SELECT * FROM audit.vw_PlatformHealth;                       -- one row per health check
SELECT TOP 20 * FROM audit.vw_PipelineExecutionHistory ORDER BY PipelineExecutionID DESC;
SELECT * FROM audit.vw_MappingExecutionHistory WHERE PipelineExecutionID = <id>;
SELECT * FROM audit.vw_ExecutionErrors WHERE PipelineExecutionID = <id> ORDER BY Severity;
SELECT * FROM audit.vw_LatestReconciliation WHERE CheckStatus = 'FAIL';
SELECT * FROM audit.vw_DataQualityBySource;
SELECT * FROM audit.vw_ValidationFailureDetail WHERE SourceName = '<source>';
SELECT * FROM audit.vw_SourceFreshness WHERE FreshnessStatus <> 'FRESH';
SELECT * FROM audit.vw_ProcessingDuration WHERE DurationStatus = 'SLOW';
SELECT * FROM audit.vw_StalledExecutions;
```

`audit.vw_ExecutionErrors` gives, for each problem: source, file, run, sheet, row, column, rule,
actual and expected values, business impact, recommended action and the steward to notify.

## Severity and what the platform does

| Situation | Severity | Platform response |
|---|---|---|
| File rejected at ingestion (wrong format, corrupt, empty) | WARNING (ERROR if critical source) | Quarantined; never loaded; newer rejection blocks loading the older version (stale). |
| File fails validation | WARNING | Quarantined with report; excluded by the gate; other files load (PARTIAL). |
| Critical source blocked (general ledger, sites, products, chart of accounts) | CRITICAL | Gate HOLDS everything; no SQL load. |
| Validation coverage incomplete | CRITICAL | SQL refused. |
| Automated tests fail | CRITICAL | SQL refused. |
| Mapping fails (data or system error) | ERROR | Mapping FAILED, pipeline stops, remaining mappings SKIPPED (recorded). |
| Transient SQL error (deadlock, timeout) | WARNING | Retried per mapping (`max_retries`, back-off); then ERROR. |
| Mandatory reconciliation control fails | CRITICAL / ERROR | Mapping FAILED, pipeline stops. |
| Warning control fails (for example freshness) | WARNING | Recorded; pipeline continues. |
| Run stopped without finishing | WARNING | Marked ABANDONED at the next run; no repair needed. |

## Recovery procedures

All loads are idempotent and transactional, so **re-running the pipeline is the standard recovery**.
Nothing is half-written: each staging file and each warehouse target is committed as a unit.

1. **A mapping failed.** Read `audit.vw_ExecutionErrors` for the run. Fix the cause (data: correct and
   resubmit the file; system: fix and redeploy). Re-run. Completed mappings re-run as no-ops.
2. **A source file is wrong.** Trace it, correct it at source, drop the corrected file into
   `data/incoming/`. It is ingested as a new version, validated and loaded; a correction becomes an
   update (or SCD2 version) with a change-log entry.
3. **The process was killed / the machine restarted.** Re-run. The stale execution is marked
   ABANDONED with an explanation; loads resume from the last committed state.
4. **Reconciliation failed.** Do not publish reports. `SELECT * FROM audit.vw_LatestReconciliation
   WHERE CheckStatus = 'FAIL'` shows observed vs expected and the action. Typical causes: missing master
   data (unknown members), a changed RAW file (hash lineage), a manual edit in the warehouse.
5. **A warehouse row was changed or deleted by hand.** Re-running the mapping's load restores it from
   the latest eligible staging batch (proved by the integration tests).
6. **A migration failed.** It was rolled back (one transaction per migration). Fix the script (it was
   never recorded as applied) and run `python scripts/migrate.py apply` again.
7. **A document must be withdrawn.** Add it under `withdrawn_files` in the registry with reason,
   date and approver, move it to `data/archive/`, re-run. It is refused at ingestion and marked
   WITHDRAWN at the gate; history is kept.

## Safe cancellation

Press `Ctrl+C` in the status board: `w` waits for the running step to finish (safest), `s` stops it
now, `c` continues. Generated files, RAW and quarantine copies and log migrations are written to a
temporary name and swapped in when complete; database work is transactional.

## Retention and archival

```powershell
python scripts/apply_retention.py            # report what would be archived
python scripts/apply_retention.py --apply    # archive to Parquet, verify, then remove
```

Staging keeps the newest `staging_batches_retained` runs per table (config/pipeline.yaml) and every
batch in use. Archives go to `data/archive/staging/` with a SHA-256 recorded in
`audit.RetentionAction`. Warehouse history, audit records and RAW evidence are never purged
automatically; RAW retention follows each source's `retention_years` and is a manual, approved task.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Named Pipes Provider: Could not open a connection` | Wrong `SQL_SERVER` in `.env` | Use `localhost` (or the exact instance name). |
| `MIGRATION ERROR: ... edited after being applied` | An applied migration file changed | Revert the edit; put the change in a new migration. |
| `Database ... has no migration history` | Database built before migration tracking | `python scripts/migrate.py baseline --through <last applied>` once. |
| `Configuration problems:` from metadata sync | Inconsistent `config/*.yaml` | The message lists each problem (unknown handler, missing lineage, cycle, unjustified tolerance...). |
| `Execution plan rejected` | Metadata references an unapproved handler or wrong target | Fix `config/pipeline.yaml`, re-sync. |
| Mapping BLOCKED, category ELIGIBILITY | Source file blocked, held or never ingested | `python scripts/trace_file.py <source>`. |
| `UNKNOWN_MEMBERS` control failed | Fact references a key missing from a dimension | Load/correct the master data, re-run. |
| `HASH_LINEAGE` control failed | RAW file changed after ingestion | Investigate; restore the original or re-ingest a new version. Never load a changed file. |
| SQL step BLOCKED on the status board | Gate held, coverage incomplete or tests failing | The step detail names the reason. |
