# Power BI semantic model

Power BI connects to the **`mart` schema only** (role `role_report_reader`). The views hide technical
columns and soft-deleted rows, so the warehouse can change underneath without breaking reports.

## Model

Import mode (data volumes are small); one model per subject area sharing conformed dimensions, or
one combined model:

| Area | Fact tables | Dimensions |
|---|---|---|
| Sales | `mart.FactSales`, `mart.FactSalesLine` | DimDate, DimSite, DimCustomer, DimProduct, DimPaymentMethod |
| Inventory | `mart.FactInventoryMovement`, `mart.FactInventorySnapshot` | DimDate, DimSite, DimProduct |
| Purchasing | `mart.FactPurchaseOrder`, `mart.FactPurchaseOrderLine`, `mart.FactGoodsReceipt`, `mart.FactSupplierInvoice`, `mart.FactSupplierRebate` | DimDate, DimSupplier, DimProduct, DimSite |
| Finance | `mart.FactGeneralLedger`, `mart.FactOperatingExpense` | DimDate, DimAccount, DimSite |
| Assets | `mart.FactAssetDepreciation` | DimDate, DimAsset, DimSite |
| Cash management | `mart.FactBranchCashup` | DimDate, DimSite |

### Relationships

All single-direction, many-to-one, from fact key to dimension key:

- `Fact*[...DateKey]` -> `DimDate[DateKey]` (mark `DimDate` as the date table on `Date`). Facts with
  two dates (`FactGoodsReceipt` receipt/order, `FactSupplierInvoice` invoice/due) have one active
  relationship (receipt / invoice date) and one inactive, used with `USERELATIONSHIP`.
- `SiteKey`, `CustomerKey`, `ProductKey`, `SupplierKey`, `AccountKey`, `AssetKey`,
  `PaymentMethodKey` -> the matching dimension key.
- SCD2 dimensions relate on the version key; filter `IsCurrent = TRUE` in slicers for "as is"
  attributes, or use the version attributes for "as loaded".
- Do not relate facts to each other. Combine facts through shared dimensions (for example cash-ups vs
  sales by `DimSite` and `DimDate`).

Hide all key columns, set `Sort by column` for month names (`MonthNumber`) and keep exception views
(`mart.vw_*`) as separate tables for exception pages.

### Measures

Explicit measures only (no implicit aggregation); see [powerbi/measures.dax](powerbi/measures.dax).
Format currency as `N$ #,##0.00`.

## Dashboards

| Dashboard | Content |
|---|---|
| Sales | Net sales, gross margin and %, by branch, category, payment method, month; top products |
| Finance | Income statement and balance sheet by month (`vw_IncomeStatement`, `vw_BalanceSheet`), trial balance, account reconciliation status, expense analysis, creditor ageing |
| Purchasing and suppliers | Spend by supplier, minimum-order compliance, three-way match status, lead times, rebate earned vs expected, reorder proposals vs minimum order value |
| Inventory | Stock on hand and value by site, replenishment status (reorder, excess, slow-moving), month-end positions |
| Assets | Register, depreciation schedule vs GL, local register exceptions, assets by site |
| Cash management | Cash-up reconciliation by branch and day, variances, missing and quarantined documents |
| Data quality and operations | `audit.vw_DataQualityBySource`, `vw_DataQualityByDimension`, `vw_PlatformHealth`, failed controls (steward role) |

## Incremental refresh

The large facts (`FactSalesLine`, `FactInventoryMovement`, `FactGeneralLedger`) can use incremental
refresh on the date key: create `RangeStart`/`RangeEnd` parameters and filter
`[SaleDateKey] >= Date.Year(RangeStart)*10000 + Date.Month(RangeStart)*100 + Date.Day(RangeStart)`
(and `<` for `RangeEnd`) so the filter folds to SQL. Refresh the last 2 months, keep 3 years.
Because sources are full extracts, schedule a periodic full refresh to pick up corrections to older
periods (the warehouse records them in `audit.FactChangeLog`).

## Security

- Report users get `role_report_reader`; personal data is masked by SQL Server for them.
- Row-level security for branch managers: a role filtering `DimSite[SiteID]` against a user-to-site
  mapping table (`USERPRINCIPALNAME()`), so each manager sees only their branch.
- Data stewards use a separate model or workspace with `role_data_steward` for data-quality pages.
