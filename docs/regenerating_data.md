# Regenerating the data from scratch

This procedure wipes every piece of data the platform holds and rebuilds it from newly generated
synthetic files. Use it to start over with a clean slate: after experiments, after changing the
generator, or to get a known starting point.

It removes **data only**. The database schema is rebuilt from the same version-controlled migrations
in `sql/`, so nothing about the schema, configuration or code changes.

## What gets wiped

The data lives in three places, and all three must be cleared **together**. They reference each other
by run ID and file hash, so clearing one but not the others leaves the platform inconsistent (for
example, the ingestion manifest would treat regenerated files with an unchanged hash as duplicates
and skip them).

| Location | Holds | Action |
|---|---|---|
| `data/incoming/` (including `cashups/`) | Generated source files | Delete |
| `data/raw/`, `data/quarantine/`, `data/processed/` | Ingested copies, quarantined files and reports, per run | Delete (keep `.gitkeep`) |
| `data/archive/staging/` | Staging batches archived by retention (only if retention has run) | Delete |
| `logs/` | Ingestion manifest, run history, validation results, eligibility, pipeline logs | Delete (keep `.gitkeep`) |
| SQL Server database `RetailDataPlatform` | Staging, warehouse, marts, audit and metadata | Drop, then rebuild from migrations |

The repository itself ships with no data: only the empty `data/` folders (`.gitkeep`) and `logs/`.
Everything listed above is created on your machine and is ignored by git. `config/`, `sql/`, `src/`
and `scripts/` are not touched.

## Before you start

- Run every command from the project root in PowerShell, with the virtual environment active:
  `.\.venv\Scripts\Activate.ps1`.
- `.env` must point at your SQL Server (see [developer_setup.md](developer_setup.md)). The commands
  below use `<SQL_SERVER>`: replace it with the `SQL_SERVER` value from your `.env`.
- Close anything connected to `RetailDataPlatform` (Power BI, SSMS query windows). Dropping the
  database disconnects them.
- If the daily schedule is registered, make sure it does not start during the reset (or remove it with
  `.\scripts\schedule_pipeline.ps1 -Remove` and register it again afterwards).

> **This cannot be undone.** Back up the database first if you might want the old data again
> (`BACKUP DATABASE [RetailDataPlatform] TO DISK = N'C:\Backups\RetailDataPlatform.bak'`).

## Step 1 (optional): choose new values

The generator is deterministic: `SEED` in `scripts/generate_all.py` fixes every value, so regenerating
with the same seed produces **identical** data. That is what you want for a reproducible reset.

For genuinely different data, change the seed (line 8) to any other integer:

```python
SEED = 20261122
```

The deliberate exceptions (missing cash-up, shortage, overage, misreported and unreadable cash-ups,
template rollout) are tied to branches and dates, not to the seed, so they are still planted. Counts
and amounts change, including the unplanted findings listed in [data_generation.md](data_generation.md).
Commit the new seed so everyone else regenerates the same data.

## Step 2: delete the file-based data

```powershell
Remove-Item -Recurse -Force data\incoming\* -ErrorAction SilentlyContinue
Get-ChildItem data\raw\*, data\quarantine\*, data\processed\* -Exclude .gitkeep | Remove-Item -Recurse -Force
Remove-Item -Recurse -Force data\archive\staging, data\.generating -ErrorAction SilentlyContinue
Get-ChildItem logs\* -Exclude .gitkeep | Remove-Item -Recurse -Force
```

Check that only the kept files remain:

```powershell
Get-ChildItem data, logs -Recurse | Select-Object -ExpandProperty FullName
```

This should list only the `data` subfolders (`archive`, `incoming`, `processed`, `quarantine`, `raw`)
and the `.gitkeep` files. If empty `ING-*` run folders remain under `data\raw` or `data\quarantine`,
remove them:

```powershell
Get-ChildItem data\raw, data\quarantine -Directory -Filter 'ING-*' | Remove-Item -Recurse -Force
```

## Step 3: drop the database

With `sqlcmd` (installed with the SQL Server client tools):

```powershell
sqlcmd -S <SQL_SERVER> -E -Q "IF DB_ID(N'RetailDataPlatform') IS NOT NULL BEGIN ALTER DATABASE [RetailDataPlatform] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [RetailDataPlatform]; END"
```

Or in SSMS: right-click the `RetailDataPlatform` database > **Delete**, tick **Close existing
connections**, OK.

(`-E` uses Windows authentication. With SQL authentication use `-U <user> -P <password>` instead.)

## Step 4: rebuild the empty database

Run these commands one at a time, in order, in the same PowerShell terminal. Wait for each to finish
before starting the next; if one shows an error, stop and fix it first, because each depends on the
one before. The `# ...` comments can be pasted too: PowerShell ignores everything after `#`.

```powershell
.\.venv\Scripts\Activate.ps1           # turns on the virtual environment: the prompt starts with (.venv)
python scripts/migrate.py apply        # creates the database and applies every migration in sql/
python scripts/sync_metadata.py        # loads config/ into the meta catalogue
python scripts/migrate.py status       # every migration should show as applied
```

| Command | What you should see |
|---|---|
| `Activate.ps1` | `(.venv)` at the start of the prompt. If PowerShell says *running scripts is disabled*, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again. |
| `migrate.py apply` | Each migration applied in turn, ending with all of them applied. |
| `sync_metadata.py` | A summary line, for example `21 sources, 131 data elements, 44 mappings, ...`. |
| `migrate.py status` | Every migration listed as applied. This command only checks; it changes nothing. |

The schema is now identical to before, with no data except the seeded reference rows (Unknown `-1`
members, payment methods and so on).

The pipeline in step 5 also runs the migrations and the metadata sync (its "database migrations" and
"metadata sync" stages), where they report nothing to do. Running them yourself first shows a wrong
connection or configuration straight away, instead of halfway through the pipeline.

## Step 5: generate the data and run the pipeline

In the same terminal:

```powershell
.\run_pipeline.bat --regenerate        # generates new data and runs the full pipeline
```

This opens the status board in a **separate window**, generates the data without asking, then ingests,
validates, runs the tests, loads the warehouse and runs the health check. The window stays open when
the run finishes so you can read the result. To stay in the current terminal instead:
`python scripts/run_pipeline.py --regenerate`.

At the end of the data generation stage the generator prints its self-check. Expect:

| Check | Expected |
|---|---|
| `files_created` | 18 |
| `gl_balanced` | True |
| `negative_stock_positions` | 0 |
| `duplicate_asset_ids` | 0 |
| `cashup_documents` | 83 (12 branches x 7 days, less the one deliberately missing) |

## Step 6: verify

The pipeline's exit code is shown on the status board:

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Completed with warnings. **Expected** on a fresh run: the deliberately unreadable BR012 cash-up is quarantined, and sources may show as stale because the data covers calendar 2026. |
| 2 | Failed or blocked: read the stage detail and `logs/pipeline_<timestamp>.log` |

Then:

```powershell
python scripts/health_check.py --summary
python scripts/trace_file.py BR012     # shows why the unreadable cash-up was quarantined
```

and in SQL Server:

```sql
SELECT TOP 1 * FROM audit.vw_PipelineExecutionHistory ORDER BY PipelineExecutionID DESC;
SELECT * FROM audit.vw_LatestReconciliation WHERE CheckStatus = 'FAIL';   -- expect no rows
SELECT COUNT(*) AS SalesRows FROM dw.FactSales;
SELECT * FROM mart.vw_CashupReconciliation WHERE ReconciliationStatus <> 'BALANCED';  -- the planted cash-up exceptions
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot drop database because it is currently in use` | Something still holds a connection; the `SET SINGLE_USER WITH ROLLBACK IMMEDIATE` in step 3 closes them. In SSMS, tick **Close existing connections**. |
| `sqlcmd` is not recognised | Use SSMS (step 3), or install the SQL Server command-line tools. |
| Ingestion reports files as duplicates | `logs/` was not cleared. Repeat steps 2-5. |
| Mapping fails with `HASH_LINEAGE` or eligibility errors | The database and the files are out of step (one was cleared, the other not). Repeat steps 2-5. |
| `Named Pipes Provider: Could not open a connection` | Wrong `SQL_SERVER` in `.env`. |

More in the [operations runbook](operations_runbook.md#troubleshooting).
