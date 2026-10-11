# Implementation report

Date: 2026-10-11. Environment: Windows 11, Python 3.14.7, SQL Server 2019 Developer (RTM 15.0.2000.5),
ODBC Driver 17, 7.5 GB RAM.

This report distinguishes what was **implemented and tested**, what is **implemented but not yet
exercised in its target environment**, and what is **designed or documented only**.

## Test results

| Suite | Result | Command |
|---|---|---|
| Unit, configuration, negative, regression, SQL standards (no database) | **332 passed** | `python -m pytest tests` |
| Integration on a fresh SQL Server database (`RetailDataPlatform_IT`) | **16 passed** (15 in the full 21-minute run; the lineage test failed on `dw.DimPaymentMethod`, was fixed and re-run with the schema tests: 3 passed) | `python -m pytest tests/integration -m integration -v` |
| Lint / format | ruff: no findings; 51 files formatted | `ruff check .` / `ruff format --check .` |
| Static security | bandit: no issues (3 reviewed `except: pass` cleanups annotated) | `bandit -q -c pyproject.toml -r src scripts` |
| Dependency vulnerabilities | pip-audit: no known vulnerabilities | `pip-audit -r requirements.txt` |
| Configuration validation | 21 sources, 131 data elements, 44 mappings, 67 dependencies, 129 controls, 280 rules, 373 lineage entries | `python scripts/sync_metadata.py --check` |
| Real database end-to-end | see "Production-database run" below | `run_pipeline.bat --keep-data` |

Integration tests prove: every migration applies to an empty server; staging matches the registry
(columns, types, nullability); every warehouse column has lineage; the first run completes with every
control passing; blocked documents are excluded and reported; re-running writes nothing (idempotent);
an SCD2 change creates a version and keeps history; fact corrections and removals are logged; a
deleted warehouse row is detected by reconciliation and restored by a re-run; trial balance and balance
sheet balance; six of seven ledger-to-subledger areas reconcile (depreciation differs by design of the
source); all planted cash-up exceptions are caught; purchase orders meet minimum order values; stalled
runs are abandoned; report readers see masked personal data and cannot read staging.

## Phase checklist

| Phase | Item | Status |
|---|---|---|
| A | Supplier reconciliation as a mandatory post-load gate | Done (`DW_DIM_SUPPLIER.SUPPLIER_RECONCILIATION`, CRITICAL) |
| A | Approved reconciliation handlers, configurable severity and tolerance | Done (10 control types, 129 controls; non-zero tolerances must be justified) |
| A | Mandatory failures fail the pipeline | Done (tested) |
| A | Source-to-staging row counts, hash and version lineage, staged-vs-transformed | Done (ROW_COUNT, HASH_LINEAGE, WAREHOUSE_AUDIT, KEY_COVERAGE, MEASURE_TOTAL) |
| A | Freshness, stalled / abandoned executions | Done (FRESHNESS control, `vw_SourceFreshness`, abandonment at run start: dead runs detected by a released liveness lock, live long-running runs never abandoned) |
| A | Monitoring views, actionable errors, retries, safe restart, recorded skips | Done (`audit.vw_*`, `audit.ExecutionError`, retry policy, idempotent loads) |
| B | Metadata for all sources: mappings, handlers, schemas, column mappings, rules, ownership, dependencies, strategies, schedule/frequency, retention, classification, lineage, versioning | Done (config -> `meta` sync with checksum and git commit) |
| C | All 19 sources staged, warehoused, reconciled, tested; Excel and PDF | Done |
| C | Generator extended deliberately (transfers, opening stock, consistent cash-ups) | Done and documented; legacy cash-ups withdrawn, not deleted |
| D | Star schema: 9 dimensions + bridge, 13 facts with declared grains, SCD1/SCD2, unknown members, corrections | Done |
| E | Inventory and replenishment, suppliers and purchasing, finance, assets, cash-ups | Done as mart views (see limitations) |
| F | DQ dimensions, severity, actionable reports, data dictionary, catalogue, end-to-end lineage | Done |
| G | Single command, dependency-aware, incremental/idempotent, transactional, retry, cancellation, scheduling, history, alert-ready health, retention | Done (scheduling script provided, not registered on this machine) |
| H | Lint, format, static analysis, SQL standards, unit/integration/DQ/reconciliation/SCD2/negative/regression tests, config and dependency validation, security checks, migration controls, CI | Done locally; GitHub Actions configured but **not yet run in GitHub** |
| I | Fabric, dbt, marts, semantic model, measures, dashboards, incremental refresh, security | Marts done; semantic model, measures, dashboards, RLS and incremental refresh **designed and documented, no .pbix built**; Fabric and dbt **evaluated** |
| J | Documentation set | Done (see README "Documentation") |

## Production-database run

Run on the real `RetailDataPlatform` database (2026-10-11, pipeline execution 4), after migrations
015-027 were applied to the database baselined at 014.

| Check | Result |
|---|---|
| Migrations | 27 recorded in `meta.SchemaMigration` (latest 027), checksums verified |
| Metadata sync | 24 inserted, 1,230 updated, 0 disabled |
| Load gate | 100 eligible, 1 blocked (BR012 unreadable cash-up), 111 withdrawn (superseded/legacy files) |
| Orchestration | **44 mappings completed, 0 blocked, 0 failed, 0 skipped** |
| Reconciliation controls | **134 results, all PASS** (95 critical, 19 error, 20 warning) |
| Balance sheet | every month-end `BALANCED` (difference 0.00) |
| Cash-ups | all planted exceptions detected: BR003 22 Sep shortage -150.00, BR005 24 Sep missing, BR007 25 Sep overage +40.00, BR010 26 Sep POS mismatch (over-reported), BR012 27 Sep quarantined |
| Replenishment plan | 1,365 site/product rows: 28 REORDER, 19 EXCESS, 2 SLOW_MOVING, 1,316 OK; DC has no REORDER lines, so the supplier reorder proposal is empty for this data set |
| Full pipeline exit code | 1 (WARNING) by design: the blocked cash-up is a warning, not a failure |

**Performance fix (027).** `mart.vw_ReplenishmentPlan` and `mart.vw_SupplierReorderProposal` took about
200 s per query through stacked views. Migration 027 materialises the plan in `mart.ReplenishmentPlan`,
built once per run by `mart.usp_BuildReplenishmentPlan` (mapping `MART_REPLENISHMENT_PLAN`, handler
`MART_REPLENISHMENT_PLAN`, full refresh, `WAREHOUSE_AUDIT` control, column lineage). The views keep their
names and columns and now return in 0.13 s and 0.04 s.

## Cash-up template versions (2026-10-11)

Branch cash-ups can arrive on several registered layouts at once (`extraction.templates` in the
registry), so branches can switch templates at different times. Each document's template is detected
from its `Template Version` label (no label = the default template 1). An unregistered version, or a
template used after its `accepted_until` date, is blocked with an actionable message. Fields keep their
canonical names, so staging and the warehouse are unaffected; `stg.BranchCashup.template_version`
(migration 028) records the layout used.

Tested: 10 unit tests (`tests/test_cashup_templates.py`) on real PDFs, covering both templates to the
same record, structure, coverage and business rules, unknown version, missing renamed label and retired
template. End to end on `RetailDataPlatform`: 31 template 2 documents (BR001-BR008 from 24 Sep) and 52
template 1 documents loaded, all planted cash-up exceptions still detected (BR007's overage is on
template 2), and every reconciliation control passed. Introducing templates renamed the label checks
(now by field), so the 52 template 1 documents held from the previous run had to be re-validated once
by hand. Since then validation records a contract fingerprint and re-validates held versions
automatically when a contract changes (see "Incremental validation" below).

## Incremental validation and staging (2026-10-11)

A run with nothing new took ~4 minutes re-reading unchanged workbooks (validation ~3 min, staging
~6 min before its fix). Validation now covers every held file version (as the gate does), but re-reads a
file only if it is new, its contract / the validation settings / the validation code changed (fingerprint
`contract_checksum` in `logs/validation_file_status.csv`), or its last validation crashed; `--force`
re-validates everything. Staging skips a version whose COMPLETED load and staged row count are recorded,
without parsing it. Tested: 13 unit tests (`tests/test_validation_reuse.py`) and two consecutive runs
on `RetailDataPlatform` with nothing new ingested:

| Step | Before | Run 1 (no fingerprints yet: everything held re-validated once) | Run 2 |
|---|---|---|---|
| Structural validation | 82 s (latest batch only) | 133 s, 101 held files across 2 runs | **6 s**, 101 reused |
| Data validation | 95 s | 134 s (BR012 still invalid) | **3 s**, 101 reused |
| Warehouse orchestration | 31 s (after the staging fix; 77+ min before) | 30 s | 36 s |

Gate (100 eligible, 1 blocked), coverage and health were identical in both runs.

## Known limitations and open items

1. **Power BI**: no `.pbix` / PBIP project is included; the model, relationships, DAX measures, RLS and
   refresh design are documented for building in Power BI Desktop.
2. **CI in GitHub**: the workflow has not run in GitHub yet (no GitHub CLI in this environment). The
   integration job is opt-in and needs a secret and a variable (see docs/cicd.md).
3. **Scheduling**: `scripts/schedule_pipeline.ps1` is provided but was not registered.
4. **Source data gaps**: no customer receipts or supplier payments (debtors are cumulative credit sales;
   all invoices are open), no quantities on goods receipts (document-level three-way match), no employee
   master.
5. **Retention periods** are draft assumptions to be confirmed against Namibian legal and tax rules.
6. **Performance**: on this 7.5 GB machine SQL Server waits for memory grants on the 122k-row ledger and
   139k-row inventory loads (a full first load took ~20 minutes in the integration run, ~3.5 minutes when
   memory was free). `mart.vw_BalanceSheetCheck` is correct but takes ~200 s on the full ledger here;
   it is a candidate for the same materialisation as 027. Acceptable for the lab; production should size
   memory and consider columnstore indexes on the large facts.
7. **SCD2 effective dates** are load time (sources have no effective dates).
8. **Depreciation**: the generated ledger over-depreciates 10 fully depreciated assets and uses a
   different start convention for 35 assets; the platform reports this rather than correcting it.
