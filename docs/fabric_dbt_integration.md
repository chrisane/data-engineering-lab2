# Microsoft Fabric and dbt: evaluation and migration path

Status: **evaluation and design only.** The local SQL Server platform is complete; no Fabric trial or
dbt project is assumed or required.

## Recommended Fabric architecture

| Local component | Fabric equivalent | Notes |
|---|---|---|
| `data/incoming`, `data/raw`, `data/quarantine` | Lakehouse **Files** (bronze) | Keep the immutable-RAW and hash discipline; OneLake shortcuts for source drops. |
| Validation (Python) | Fabric **notebook** (same modules) or a Spark job | `src/validation` is plain Python; run it in a notebook with the registry from the repo. |
| Load gate, manifest, logs | Lakehouse **Delta tables** (`audit_*`) | Replace CSV logs with Delta tables; the gate logic is unchanged. |
| `stg` schema | Lakehouse Delta tables (silver) | Loaded by notebook from eligible files. |
| `dw` + `mart` | Fabric **Warehouse** (T-SQL) | Most procedures port directly; check unsupported features (see below). |
| Orchestrator | Fabric **Data Factory pipeline** calling notebooks/procedures, or the Python orchestrator in a notebook | Keep metadata-driven ordering; Data Factory can read the plan from the `meta` tables. |
| Power BI import model | **Direct Lake** semantic model on the Warehouse/Lakehouse | No refresh copies; the star schema is already Direct Lake friendly. |
| Git + CI | Fabric Git integration + GitHub Actions | Keep migrations and configuration in the same repo. |

### T-SQL differences to plan for (Fabric Warehouse)

Fabric Warehouse supports most of the T-SQL used here but, at the time of writing, not all of:
`MERGE` (supported in newer releases - verify), `IDENTITY` (use `IDENTITY` where available or
generated keys), `sp_getapplock`, dynamic data masking and some constraint enforcement (primary/foreign
keys are informational). The load procedures isolate these in a few places: replace application locks
with pipeline-level concurrency control, enforce keys through the reconciliation controls (already in
place), and implement masking with object-level security or the semantic model.

## Should dbt be introduced?

**Recommendation: not now; reconsider when moving to Fabric.**

dbt would add value for SQL-based transformations, documentation, tests and lineage. This platform
already has:

- versioned, checksummed migrations for DDL;
- hand-written, audited load procedures with idempotent MERGE, SCD2, soft deletes and change logs;
- reconciliation controls, data tests and lineage in metadata, generated documentation.

Introducing dbt today would duplicate transformation logic (procedures vs models) and split lineage
across two systems, which the requirements explicitly ask to avoid. dbt becomes the better choice if:

1. the warehouse moves to Fabric/Spark where stored procedures are less natural; or
2. the team prefers ELT models over procedural loads.

Migration path if adopted: convert each `dw.usp_Load*` into a dbt incremental model
(`unique_key` = business key, `merge` strategy) or a snapshot (SCD2); express reconciliation controls
as dbt tests (`unique`, `not_null`, `relationships`, custom tests for totals and balances); generate
`sources.yml` and column descriptions from `config/source_registry.yaml` and
`config/data_dictionary.yaml` (the metadata sync already holds everything needed); retire the
procedures only when the dbt models pass the same integration tests.
