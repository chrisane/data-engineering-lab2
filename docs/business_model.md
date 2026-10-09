# Retail Data Platform v2 - Business Model

## Organisation
Nawa Retail Group is a fictional Namibian multi-site retailer used exclusively for data-engineering training.

The organisation consists of:
- Head Office
- Central Distribution Centre (DC)
- 12 retail branches
- Central procurement and finance functions
- Branch-level operational functions

## Core business domains
1. Sales and customer receipts
2. Inventory and stock movements
3. Procurement and suppliers
4. Distribution and branch replenishment
5. Supplier rebates
6. Accounts payable
7. Accounts receivable
8. Cash and banking
9. Operating expenses
10. Payroll summary postings
11. Fixed assets and depreciation
12. Local departmental/branch asset registers
13. General ledger and financial reporting

## Procurement model
Branches primarily replenish stocked merchandise from the DC. The DC monitors branch demand and its own inventory and purchases from suppliers. Products outside the DC's normal scope may be procured centrally/directly from suppliers.

Supplier agreements may specify a minimum monetary order value. Purchase orders below the supplier's applicable minimum are not valid.

Supplier rebates are earned according to purchasing thresholds and are tracked separately from ordinary purchase prices.

## Asset model
Large/capital assets are recorded in the ERP with a unique asset number, acquisition cost, acquisition date, useful life, accumulated depreciation and net book value.

Smaller/local assets are maintained by departments and branches in Excel-style registers. These deliberately create a less-governed source for later ingestion exercises.

## Accounting model
Operational transactions drive accounting entries. GL data is derived from source transactions rather than independently fabricated. Every journal contains a source document reference for lineage and reconciliation.

The synthetic platform is designed to support a complete trial balance, income statement and balance sheet.
