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
| Branch cash-up PDFs generated from the sales data (one per branch per day, 21-27 Sept 2026) | The previous 111 PDFs used site codes S001-S004 (not in `site_master`) and amounts ~30x the sales; they could not be reconciled. They were **withdrawn** (registry `withdrawn_files`) and archived to `data/archive/legacy_cashups/`; their RAW copies and history are kept. |
| Generator self-check `negative_stock_positions` and `cashup_documents` | Proves the inventory and document generation. |

Generation writes every file to a staging folder first and moves them into place when all are
complete, so a cancelled run never leaves half-written files.

## Deliberate exceptions (what the platform must catch)

| Exception | Where | Caught by |
|---|---|---|
| Missing cash-up | BR005, 2026-09-24 | `mart.vw_CashupReconciliation` -> `MISSING_CASHUP` |
| Cash shortage N$150 | BR003, 2026-09-22 | `CashVarianceStatus = SHORTAGE` -> `CASH_SHORTAGE` |
| Cash overage N$40 | BR007, 2026-09-25 | `CASH_OVERAGE` |
| Cash sales misreported by N$500 | BR010, 2026-09-26 (over-reported because the branch had no cash sales that day) | Cash-up vs POS -> `POS_MISMATCH` |
| Unreadable amount (`N$ 1,2O4.5O`, letter O) | BR012, 2026-09-27 | Data validation `DATA_TYPE` -> quarantined, `CASHUP_QUARANTINED` |
| Inconsistent number formats (`N$ 31 475.00`, `N$ 6903.40`, `N$ 22,910.00`) | Random cash-ups | Parsed correctly by the extraction (no false failures) |
| Two cash-up templates at once: BR001-BR008 switch to template 2 (version label, renamed and reordered lines) from 2026-09-24; BR009-BR012 stay on template 1 | 31 template 2 documents, 52 template 1 | Both accepted; identical canonical fields; `stg.BranchCashup.template_version` records the layout (BR007's overage is on template 2) |

## Findings the platform surfaced in the generated ledger (not deliberately planted)

| Finding | Detail | Where reported |
|---|---|---|
| Depreciation posted after assets were fully depreciated | 10 assets received a full year of 2026 depreciation (N$1.29m) although fully depreciated earlier | `mart.vw_AssetDepreciationReconciliation` -> `POSTED_AFTER_FULLY_DEPRECIATED` |
| Depreciation start convention differs between GL and register | 35 assets acquired or fully depreciated during 2026 differ by a month or more | same view -> `DIFFERENCE` |
| Opening balances as ten single-line journals | Fixed earlier (one `OPEN-2026` journal) | `LEDGER_BALANCE` control |

These are left in the data on purpose: they demonstrate reconciliation between the asset register and
the general ledger.
