# Security and access control

## Credentials

- Connection settings live in `.env` (git-ignored) or the process environment. No credential is stored
  in code, SQL or configuration; tests reject `PWD=` / `PASSWORD=` in migrations.
- Local use is Windows authentication. CI uses SQL authentication with a secret
  (`CI_SQL_SA_PASSWORD`) for a throwaway container only.

## Database roles (migration 025)

| Role | Access | Personal data |
|---|---|---|
| `role_report_reader` | `SELECT` on `mart` only | masked |
| `role_data_steward` | `mart`, `meta`, `audit` (catalogue, data quality, errors) | `UNMASK` |
| `role_data_engineer` | `mart`, `meta`, `audit`, `stg`, `dw`; `EXECUTE` on load procedures | `UNMASK` |

Grant roles to Windows groups, never to individuals:
`ALTER ROLE role_report_reader ADD MEMBER [DOMAIN\Retail-Analysts];`

## Personal data

Classified in the registry (`governance.personal_data_fields`) and the data dictionary:

| Data | Classification | Protection |
|---|---|---|
| Customer name and town (retail customers are individuals) | RESTRICTED | Dynamic data masking on `dw.DimCustomer`; report readers see `Xxxxx` |
| Employee assigned to a branch asset | CONFIDENTIAL | Masked on `dw.DimLocalAsset.AssignedTo` |
| Cashier name and signature on cash-ups | CONFIDENTIAL | Masked on `dw.FactBranchCashup.Cashier`; the signature is not loaded to the warehouse |

Masking is a presentation control: privileged users (`db_owner`, `sysadmin`) still read the data, and
staging (`stg`) is readable by engineers only. The integration tests prove a report reader sees masked
names and cannot read staging. For production, add Transparent Data Encryption and, if required,
Always Encrypted for RESTRICTED columns; in Power BI, apply row-level security (see the semantic model
guide).

## Integrity and evidence

- RAW files are immutable; their SHA-256 is re-checked before staging and by the `HASH_LINEAGE`
  control; a changed file is never loaded.
- Metadata cannot execute code: handlers are bound to implementations in version control; control
  parameters naming tables or columns are validated against the database and quoted.
- All dynamic SQL is parameterised for values.
- Every load, block, failure, reconciliation and retention action is recorded in `audit`.

## Retention

`governance.retention_years` per source (draft assumptions, to be confirmed against Namibian tax and
data-protection requirements). Staging retention is automated with archive-before-delete
(`scripts/apply_retention.py`); RAW and warehouse history are retained.
