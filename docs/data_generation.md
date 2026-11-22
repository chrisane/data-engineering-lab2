# Synthetic data

`scripts/generate_all.py` produces deterministic data (seed 20261004) for calendar 2026: master data,
sales, purchasing, inventory, finance, assets and branch cash-up PDFs. Re-running produces identical
data (PDFs are byte-identical; Excel files embed a save timestamp, so they get a new hash and are
ingested as a new, unchanged version).

## Changes made for Phase C/E (2026-10-10)

| Change | Why |
|---|---|
| Weekly stock transfers DC -> branches (`TRANSFER_OUT` / `TRANSFER_IN`, shared `TRF` document), the first one including a buffer of three weeks' average sales | Without transfers every branch's stock went negative (sales with no inbound stock). Replenishment analysis needs realistic flows. |
| Opening stock at the DC (`ADJUSTMENT`, document `OPENING-STOCK-2026`) | So DC stock never goes negative; sized from the cumulative purchase/transfer flow plus 10%. |
| Branch cash-up PDFs generated from the sales data (one per branch per day, 21-27 Sept 2026) | The previous 111 PDFs used site codes S001-S004 (not in `site_master`) and amounts ~30x the sales; they could not be reconciled. They were **withdrawn** (registry `withdrawn_files`) and archived to `data/archive/legacy_cashups/`. The archive and withdrawal entry were removed on 2026-11-22 when the repository was reset to a clean baseline (see [regenerating_data.md](regenerating_data.md)). |
| Generator self-check `negative_stock_positions` and `cashup_documents` | Proves the inventory and document generation. |

## Cash-up layout (2026-11-22)

Cash-ups are drawn in the layout of the branches' Excel template
([templates/branch_cashup_template.xlsx](templates/branch_cashup_template.xlsx)) as Excel exports it to
PDF: site, shift, date, terminal and cashier as label-value rows; takings and the cash count as two
tables (two headers wrap over two lines); signatures. Numbers use Excel's accounting format (a dash for
zero, the minus sign at the left of the cell) and the date is day first (`21/09/2026`). The renderer is
`scripts/cashup_report.py`; the extraction reads values by position, so the generated documents and a
real Excel export read identically (proved by `tests/test_cashup_layout.py` against PDFs exported by
Excel). This layout is template 3; templates 1 (label lines) and 2 (relabelled lines) were retired.

Generation writes every file to a staging folder first and moves them into place when all are
complete, so a cancelled run never leaves half-written files.

## Deliberate exceptions (what the platform must catch)

| Exception | Where | Caught by |
|---|---|---|
| Missing cash-up | BR005, 2026-09-24 | `presentation.vw_CashupReconciliation` -> `MISSING_CASHUP`; control `GOLD_FACT_BRANCH_CASHUP.EXPECTED_RECORDS` |
| Cash shortage N$150 | BR003, 2026-09-22 | `CashVarianceStatus = SHORTAGE` -> `CASH_SHORTAGE`; anomaly in `audit.DataAnomaly` (`CASH_VARIANCE_ANOMALY`) |
| Cash overage N$40 | BR007, 2026-09-25 | `CASH_OVERAGE`; anomaly in `audit.DataAnomaly` |
| Cash sales misreported by N$500 | BR010, 2026-09-26 (over-reported because the branch had no cash sales that day) | Cash-up vs POS -> `POS_MISMATCH` |
| Unreadable amount (`1,2O4.5O` typed over the number format, letter O) | BR012, 2026-09-27 | Data validation `DATA_TYPE` -> quarantined, `CASHUP_QUARANTINED`; also reported by `EXPECTED_RECORDS` |
| Accounting formats: zero printed as `-`, negative variance printed as `-   150.00` | Every cash-up with no account sales; BR003's shortage | Parsed correctly by the extraction (no false failures) |

## Findings the platform surfaced in the generated ledger (not deliberately planted)

| Finding | Detail | Where reported |
|---|---|---|
| Depreciation posted after assets were fully depreciated | 10 assets received a full year of 2026 depreciation (N$1.29m) although fully depreciated earlier | `presentation.vw_AssetDepreciationReconciliation` -> `POSTED_AFTER_FULLY_DEPRECIATED` |
| Depreciation start convention differs between GL and register | 35 assets acquired or fully depreciated during 2026 differ by a month or more | same view -> `DIFFERENCE` |
| Opening balances as ten single-line journals | Fixed earlier (one `OPEN-2026` journal) | `LEDGER_BALANCE` control |

These are left in the data on purpose: they demonstrate reconciliation between the asset register and
the general ledger.
