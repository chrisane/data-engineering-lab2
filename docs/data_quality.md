# Data quality checks

Every check the platform runs proves one of eight **quality dimensions**. The dimensions, and the
dimension of every check, are defined once in `config/pipeline.yaml` (`quality_dimensions`,
`validation_check_dimensions` and each reconciliation handler's `dimension`) and synchronised to
`meta.QualityDimension` and `meta.QualityCheckType`.

## The eight dimensions and where they are checked

Checks run at two points: **validation** of each file before it may load (bronze to silver), and
**reconciliation controls** after each load (silver, gold, presentation).

| Dimension | Question | Validation (before loading) | Reconciliation controls (after loading) |
|---|---|---|---|
| **Freshness** | Did new data arrive on time? | - | `RECON_FRESHNESS`: hours since new data vs the source's update frequency (every silver mapping) |
| **Volume** | Is the amount of data plausible? | `MINIMUM_ROWS`; empty files rejected at ingestion (`EMPTY_FILE`) | `RECON_VOLUME`: rows selected this run vs the median of the last 5 runs, expected 0.5-2.0x (every silver mapping) |
| **Schema** | Are the expected sheets, columns and types there? | Readability, required sheet, columns and labels, unexpected or duplicate columns, template version, data types; wrong format or content rejected at ingestion | `RECON_SCHEMA`: silver tables match their contract exactly (columns, types, nullability); gold and presentation tables have every column their lineage declares (every mapping) |
| **Completeness** | Is anything missing or lost? | `REQUIRED_VALUE` per field; validation coverage proves every configured check ran | `RECON_ROW_COUNT` (validated = staged), `RECON_WAREHOUSE_AUDIT` (every staged row processed), `RECON_KEY_COVERAGE` (every key reached gold), `RECON_EXPECTED_RECORDS` (every branch has sales and a cash-up for every day) |
| **Duplicate** | Does anything occur twice? | `UNIQUE` business keys; identical files skipped at ingestion (`SKIPPED_DUPLICATE`) | `RECON_DUPLICATE_KEYS`: business key unique among active (or current) rows, on every gold table with a key |
| **Accuracy** | Are values valid and correct? | Ranges, allowed values, patterns, valid dates; calculations and comparisons (business rules) | `RECON_MEASURE_TOTAL` (silver and gold totals agree), `RECON_CROSS_TOTAL` (feature-table label = sales) |
| **Consistency** | Does related data agree? | `REFERENTIAL_INTEGRITY`; group balances (journals) | `RECON_HASH_LINEAGE`, `RECON_UNKNOWN_MEMBERS`, `RECON_SCD2_INTEGRITY`, `RECON_GL_BALANCE`, `RECON_SUPPLIER` |
| **Anomaly** | Is a value out of line with its own history? | - | `RECON_ANOMALY`: robust z-score (median and MAD) of daily totals; daily sales per branch and cash variances |

The full list of checks per source and mapping, with their parameters, is generated in
[catalogue/quality_rules.md](catalogue/quality_rules.md), including a coverage matrix
(checks per mapping and dimension).

## Coverage is enforced

`python scripts/sync_metadata.py` (and the CI configuration check) rejects the configuration when:

- a reconciliation handler or validation check has no dimension;
- a **silver** mapping has no check for FRESHNESS, VOLUME, SCHEMA or COMPLETENESS;
- a **gold** or **presentation** mapping has no check for SCHEMA or COMPLETENESS;
- any of the eight dimensions is not covered by at least one configured check.

The layer requirements come from default controls (`default_controls` in `config/pipeline.yaml`), so a new
mapping is covered automatically. A mapping that genuinely cannot be checked for a required dimension
declares an exemption with a reason, for example the generated calendar:

```yaml
quality_exempt:
  COMPLETENESS: Generated calendar with no source to reconcile; the procedure inserts every date 2020-2030.
```

## Severity

| Check | Severity | Effect of a failure |
|---|---|---|
| Schema, row count, lineage, warehouse audit, key coverage, duplicate keys, ledger balance, supplier reconciliation | CRITICAL | Mapping fails, pipeline stops |
| Measure totals (most; a few are CRITICAL), cross totals, unknown members (most) | ERROR | Mapping fails, pipeline stops |
| Freshness, volume, expected records, anomaly | WARNING | Recorded and reported; the load stands (exit code 1) |

Volume, expected-record and anomaly checks are warnings on purpose: they say "this looks unusual",
not "this is wrong". Each failure is in `audit.vw_ExecutionErrors` with the recommended action.

## The new checks in detail

**`RECON_SCHEMA`.** Silver tables are compared with the contract-derived definition (`src/warehouse/staging_schema.py`):
every column with its type and nullability, and no unexpected columns. `silver.Supplier` predates the generated tables
and keeps its original types, declared with `structure.sql_types` in the registry. Gold and presentation tables must
contain every column listed in the mapping's lineage.

**`RECON_VOLUME`.** Sums the validated rows of the file versions selected for loading this run
(`audit.PipelineSourceBatch`) and compares them with the median of the previous runs (`history_runs`).
The first run establishes the baseline. A truncated or doubled extract shows up here before anyone reports on it.

**`RECON_EXPECTED_RECORDS`.** Builds every expected member and date between the first and last date loaded (for
example every branch x every trading day) and lists the combinations with no record. On the synthetic data it reports
the missing BR005 cash-up (2026-09-24) and the quarantined BR012 cash-up (2026-09-27).

**`RECON_ANOMALY`.** Totals the measure per group and date, then scores each point with the modified z-score
`0.6745 x |value - median| / MAD` against the group's own history (or against all groups when `baseline: pooled`;
when the MAD is 0 the mean absolute deviation is used). Points above `z_threshold` (3.5) are anomalies. Every anomaly
is stored in `audit.DataAnomaly`; the control fails when there are more than its tolerance accepts.

| Control | Measure | Tolerance | On the synthetic data |
|---|---|---|---|
| `GOLD_FACT_SALES.SALES_ANOMALY` | Daily takings per branch | 1% of branch-days (takings are right-skewed) | About 23 of 4,380 branch-days flagged for review; passes |
| `GOLD_FACT_BRANCH_CASHUP.CASH_VARIANCE_ANOMALY` | Cash variance per branch and day, pooled baseline | 0 | Flags the planted BR003 shortage (score 51.7) and BR007 overage (13.8) |

**`RECON_DUPLICATE_KEYS`.** Runs on every gold table with a business key, using the table's grain (for example
`JournalID + JournalLineNumber` for the ledger, the current version for SCD2 dimensions).

**`RECON_CROSS_TOTAL`.** Compares the totals of a measure in two warehouse tables; used to prove the ML feature table's
label accounts for every unit sold.

## Monitoring

```sql
SELECT * FROM audit.vw_QualityResultsByDimension;                 -- last run: checks run / passed / failed per dimension
SELECT * FROM audit.vw_QualityCheckCoverage WHERE CoverageStatus = 'NOT_COVERED';
SELECT * FROM audit.vw_DataAnomalies ORDER BY Score DESC;         -- anomalies flagged in the last run
SELECT * FROM audit.vw_DataQualityByDimension;                    -- validation failures per dimension
SELECT * FROM audit.vw_LatestReconciliation WHERE CheckStatus = 'FAIL';
```

## Adding a check

1. Pick the handler for the dimension (or add one in `src/warehouse/reconciliation.py`, register it in
   `RECONCILIATION_HANDLERS` in `src/warehouse/handlers.py` and under `handlers` in `config/pipeline.yaml` with its
   `dimension`).
2. Add the control to the mapping's `controls` in `config/pipeline.yaml` (name, handler, severity, params; a non-zero
   tolerance needs a `tolerance_reason`), or to `default_controls` for a whole layer.
3. `python scripts/sync_metadata.py --check`, `python scripts/generate_docs.py`, run the unit and integration tests.
