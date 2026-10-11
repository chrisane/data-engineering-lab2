USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    024 - Retail business-rule and analysis views (mart schema).

    Parameters come from meta.Setting (config/business_rules.yaml):
      reporting.as_at_date, cashup.*, replenishment.*, purchasing.*
    Every view states its grain. Exception views return one row per
    exception with a Status and the recommended action, so they can drive
    Power BI exception pages and alerts directly.
*/

-- =========================================================
-- FINANCE
-- =========================================================

-- GRAIN: one row per account per month (every account with postings, every
-- month with postings). Closing balance is cumulative over all history
-- (debit positive), so the balances of a month always sum to zero;
-- YearToDateMovement restarts each fiscal year (income-statement view).
CREATE OR ALTER VIEW mart.vw_TrialBalance AS
WITH Movement AS (
    SELECT d.YearMonth, g.AccountKey,
           SUM(g.DebitAmount) AS Debit, SUM(g.CreditAmount) AS Credit, SUM(g.NetAmount) AS NetMovement
    FROM dw.FactGeneralLedger AS g
    JOIN dw.DimDate AS d ON d.DateKey = g.PostingDateKey
    WHERE g.IsDeletedInSource = 0
    GROUP BY d.YearMonth, g.AccountKey
), Periods AS (
    SELECT DISTINCT d.FiscalYear, d.FiscalPeriod, d.YearMonth, d.MonthEndDate
    FROM dw.DimDate AS d WHERE d.YearMonth IN (SELECT YearMonth FROM Movement)
), Grid AS (
    SELECT p.FiscalYear, p.FiscalPeriod, p.YearMonth, p.MonthEndDate, a.AccountKey,
           ISNULL(m.Debit, 0) AS Debit, ISNULL(m.Credit, 0) AS Credit, ISNULL(m.NetMovement, 0) AS NetMovement
    FROM Periods AS p
    CROSS JOIN (SELECT DISTINCT AccountKey FROM Movement) AS a
    LEFT JOIN Movement AS m ON m.YearMonth = p.YearMonth AND m.AccountKey = a.AccountKey
)
SELECT g.FiscalYear, g.FiscalPeriod, g.YearMonth, g.MonthEndDate,
       a.AccountCode, a.AccountName, a.AccountType, a.StatementSection, a.FinancialStatement, a.NormalBalance,
       g.Debit, g.Credit, g.NetMovement,
       SUM(g.NetMovement) OVER (PARTITION BY g.AccountKey, g.FiscalYear ORDER BY g.MonthEndDate ROWS UNBOUNDED PRECEDING)
           AS YearToDateMovement,
       SUM(g.NetMovement) OVER (PARTITION BY g.AccountKey ORDER BY g.MonthEndDate ROWS UNBOUNDED PRECEDING)
           AS ClosingBalanceDebitPositive
FROM Grid AS g
JOIN dw.DimAccount AS a ON a.AccountKey = g.AccountKey;
GO

-- GRAIN: one row per month, site and account for income-statement accounts.
-- Revenue shown positive (credit-normal); expenses positive (debit-normal).
CREATE OR ALTER VIEW mart.vw_IncomeStatement AS
SELECT d.FiscalYear, d.FiscalPeriod, d.YearMonth, s.SiteID, s.SiteName,
       a.StatementSection, a.AccountType, a.AccountCode, a.AccountName,
       SUM(IIF(a.StatementSection = N'Revenue', -g.NetAmount, g.NetAmount)) AS Amount,
       -- Credits increase profit, debits reduce it.
       SUM(-g.NetAmount) AS ProfitContribution
FROM dw.FactGeneralLedger AS g
JOIN dw.DimAccount AS a ON a.AccountKey = g.AccountKey
JOIN dw.DimDate AS d ON d.DateKey = g.PostingDateKey
JOIN dw.DimSite AS s ON s.SiteKey = g.SiteKey
WHERE g.IsDeletedInSource = 0 AND a.FinancialStatement = N'Income Statement'
GROUP BY d.FiscalYear, d.FiscalPeriod, d.YearMonth, s.SiteID, s.SiteName,
         a.StatementSection, a.AccountType, a.AccountCode, a.AccountName;
GO

-- GRAIN: one row per month end and balance-sheet line. Balance-sheet
-- accounts accumulate over all history; income-statement accounts give
-- current-year earnings (this fiscal year) and prior years' earnings not
-- yet closed to retained earnings, so Assets = Liabilities + Equity.
CREATE OR ALTER VIEW mart.vw_BalanceSheet AS
WITH Lines AS (
    SELECT tb.MonthEndDate, tb.FiscalYear, tb.StatementSection, tb.FinancialStatement, tb.AccountCode,
           tb.AccountName, tb.ClosingBalanceDebitPositive, tb.YearToDateMovement
    FROM mart.vw_TrialBalance AS tb
)
SELECT MonthEndDate, StatementSection AS Section, AccountCode, AccountName,
       IIF(StatementSection = N'Assets', ClosingBalanceDebitPositive, -ClosingBalanceDebitPositive) AS Balance
FROM Lines WHERE FinancialStatement = N'Balance Sheet'
UNION ALL
SELECT MonthEndDate, N'Equity', N'CYE', N'Current year earnings', -SUM(YearToDateMovement)
FROM Lines WHERE FinancialStatement = N'Income Statement'
GROUP BY MonthEndDate
UNION ALL
SELECT MonthEndDate, N'Equity', N'PYE', N'Prior years earnings (not yet closed)',
       -SUM(ClosingBalanceDebitPositive - YearToDateMovement)
FROM Lines WHERE FinancialStatement = N'Income Statement'
GROUP BY MonthEndDate;
GO

-- GRAIN: one row per month end. Accounting-equation check.
CREATE OR ALTER VIEW mart.vw_BalanceSheetCheck AS
SELECT MonthEndDate,
       SUM(IIF(Section = N'Assets', Balance, 0)) AS TotalAssets,
       SUM(IIF(Section = N'Liabilities', Balance, 0)) AS TotalLiabilities,
       SUM(IIF(Section = N'Equity', Balance, 0)) AS TotalEquity,
       SUM(IIF(Section = N'Assets', Balance, -Balance)) AS Difference,
       IIF(ABS(SUM(IIF(Section = N'Assets', Balance, -Balance))) <= 0.01, 'BALANCED', 'OUT_OF_BALANCE') AS Status
FROM mart.vw_BalanceSheet
GROUP BY MonthEndDate;
GO

-- GRAIN: one row per reconciliation area and month. GL control accounts
-- against the operational sub-ledgers they are derived from.
CREATE OR ALTER VIEW mart.vw_AccountReconciliation AS
WITH GL AS (
    SELECT d.YearMonth, a.AccountCode, g.TransactionType, SUM(g.DebitAmount) AS Debit, SUM(g.CreditAmount) AS Credit
    FROM dw.FactGeneralLedger AS g
    JOIN dw.DimAccount AS a ON a.AccountKey = g.AccountKey
    JOIN dw.DimDate AS d ON d.DateKey = g.PostingDateKey
    WHERE g.IsDeletedInSource = 0
    GROUP BY d.YearMonth, a.AccountCode, g.TransactionType
), Pairs AS (
    -- Sales revenue (4000) vs sales transactions net
    SELECT N'Sales revenue (4000) vs sales' AS Area, gl.YearMonth, SUM(gl.Credit) AS GLAmount,
           (SELECT SUM(f.NetAmount) FROM dw.FactSales AS f JOIN dw.DimDate AS d ON d.DateKey = f.SaleDateKey
            WHERE f.IsDeletedInSource = 0 AND d.YearMonth = gl.YearMonth) AS SubledgerAmount
    FROM GL AS gl WHERE gl.AccountCode = N'4000' GROUP BY gl.YearMonth
    UNION ALL
    SELECT N'Output VAT (2030) vs sales VAT', gl.YearMonth, SUM(gl.Credit),
           (SELECT SUM(f.VATAmount) FROM dw.FactSales AS f JOIN dw.DimDate AS d ON d.DateKey = f.SaleDateKey
            WHERE f.IsDeletedInSource = 0 AND d.YearMonth = gl.YearMonth)
    FROM GL AS gl WHERE gl.AccountCode = N'2030' GROUP BY gl.YearMonth
    UNION ALL
    SELECT N'Cost of sales (5000) vs sales line cost', gl.YearMonth, SUM(gl.Debit),
           (SELECT SUM(f.LineCostAmount) FROM dw.FactSalesLine AS f JOIN dw.DimDate AS d ON d.DateKey = f.SaleDateKey
            WHERE f.IsDeletedInSource = 0 AND d.YearMonth = gl.YearMonth)
    FROM GL AS gl WHERE gl.AccountCode = N'5000' GROUP BY gl.YearMonth
    UNION ALL
    SELECT N'Trade payables (2000) vs supplier invoices', gl.YearMonth, SUM(gl.Credit),
           (SELECT SUM(f.TotalAmount) FROM dw.FactSupplierInvoice AS f JOIN dw.DimDate AS d ON d.DateKey = f.InvoiceDateKey
            WHERE f.IsDeletedInSource = 0 AND d.YearMonth = gl.YearMonth)
    FROM GL AS gl WHERE gl.AccountCode = N'2000' AND gl.TransactionType <> N'OPENING' GROUP BY gl.YearMonth
    UNION ALL
    SELECT N'Operating expenses vs expense register', gl.YearMonth, SUM(gl.Debit),
           (SELECT SUM(f.Amount) FROM dw.FactOperatingExpense AS f JOIN dw.DimDate AS d ON d.DateKey = f.ExpenseDateKey
            WHERE f.IsDeletedInSource = 0 AND d.YearMonth = gl.YearMonth)
    FROM GL AS gl
    WHERE gl.TransactionType IN (SELECT DISTINCT ExpenseType FROM dw.FactOperatingExpense) AND gl.Debit > 0
    GROUP BY gl.YearMonth
    UNION ALL
    SELECT N'Depreciation (6300) vs asset depreciation schedule', gl.YearMonth, SUM(gl.Debit),
           (SELECT SUM(f.DepreciationAmount) FROM dw.FactAssetDepreciation AS f
            JOIN dw.DimDate AS d ON d.DateKey = f.PeriodEndDateKey
            WHERE d.FiscalYear = (SELECT MAX(x.FiscalYear) FROM dw.DimDate AS x WHERE x.YearMonth = gl.YearMonth))
    FROM GL AS gl WHERE gl.AccountCode = N'6300' GROUP BY gl.YearMonth
    UNION ALL
    SELECT N'Rebate income (4200) vs supplier rebates', gl.YearMonth, SUM(gl.Credit),
           (SELECT SUM(f.RebateAmount) FROM dw.FactSupplierRebate AS f JOIN dw.DimDate AS d ON d.DateKey = f.PeriodEndDateKey
            WHERE f.IsDeletedInSource = 0 AND d.YearMonth = gl.YearMonth)
    FROM GL AS gl WHERE gl.AccountCode = N'4200' GROUP BY gl.YearMonth
)
SELECT Area, YearMonth, GLAmount, ISNULL(SubledgerAmount, 0) AS SubledgerAmount,
       GLAmount - ISNULL(SubledgerAmount, 0) AS Difference,
       CASE WHEN ABS(GLAmount - ISNULL(SubledgerAmount, 0)) <= 1.00 THEN 'RECONCILED'
            ELSE 'DIFFERENCE' END AS Status,
       IIF(ABS(GLAmount - ISNULL(SubledgerAmount, 0)) <= 1.00, NULL,
           N'Investigate postings vs the operational source (scripts/trace_file.py general_ledger).') AS RecommendedAction
FROM Pairs;
GO

-- GRAIN: one row per open supplier invoice, aged at the reporting as-at date.
CREATE OR ALTER VIEW mart.vw_CreditorsAgeing AS
WITH AsAt AS (
    SELECT COALESCE((SELECT TRY_CONVERT(DATE, SettingValue) FROM meta.Setting WHERE SettingKey = 'reporting.as_at_date'),
                    CONVERT(DATE, SYSDATETIME())) AS AsAtDate
)
SELECT i.InvoiceID, i.POID, s.SupplierID, s.SupplierName, inv.[Date] AS InvoiceDate, due.[Date] AS DueDate,
       i.TotalAmount, i.InvoiceStatus,
       DATEDIFF(DAY, due.[Date], a.AsAtDate) AS DaysOverdue,
       CASE WHEN DATEDIFF(DAY, due.[Date], a.AsAtDate) <= 0 THEN N'Not yet due'
            WHEN DATEDIFF(DAY, due.[Date], a.AsAtDate) <= 30 THEN N'1-30 days'
            WHEN DATEDIFF(DAY, due.[Date], a.AsAtDate) <= 60 THEN N'31-60 days'
            WHEN DATEDIFF(DAY, due.[Date], a.AsAtDate) <= 90 THEN N'61-90 days'
            ELSE N'Over 90 days' END AS AgeingBucket,
       a.AsAtDate
FROM dw.FactSupplierInvoice AS i
JOIN dw.DimSupplier AS s ON s.SupplierKey = i.SupplierKey
JOIN dw.DimDate AS inv ON inv.DateKey = i.InvoiceDateKey
JOIN dw.DimDate AS due ON due.DateKey = i.DueDateKey
CROSS JOIN AsAt AS a
WHERE i.IsDeletedInSource = 0 AND i.InvoiceStatus IN (N'OPEN', N'PARTIALLY_PAID') AND inv.[Date] <= a.AsAtDate;
GO

-- GRAIN: one row per account customer. Credit sales to date against the
-- credit limit. ASSUMPTION: no customer-receipt source exists, so the
-- exposure is cumulative credit sales (documented limitation).
CREATE OR ALTER VIEW mart.vw_DebtorsCreditExposure AS
SELECT c.CustomerID, c.CustomerName, c.CustomerType, c.CreditLimit,
       COUNT(*) AS CreditSales, SUM(f.TotalAmount) AS CreditSalesValue,
       IIF(ISNULL(c.CreditLimit, 0) = 0, 'NO_CREDIT_FACILITY',
           IIF(SUM(f.TotalAmount) > c.CreditLimit, 'OVER_LIMIT', 'WITHIN_LIMIT')) AS CreditStatus
FROM dw.FactSales AS f
JOIN dw.DimPaymentMethod AS pm ON pm.PaymentMethodKey = f.PaymentMethodKey AND pm.PaymentMethodCode = N'CREDIT'
JOIN dw.DimCustomer AS c ON c.CustomerKey = f.CustomerKey
WHERE f.IsDeletedInSource = 0
GROUP BY c.CustomerID, c.CustomerName, c.CustomerType, c.CreditLimit;
GO

-- GRAIN: one row per month, branch and product category.
CREATE OR ALTER VIEW mart.vw_SalesProfitability AS
SELECT d.FiscalYear, d.YearMonth, s.SiteID, s.SiteName, p.Category,
       SUM(f.Quantity) AS UnitsSold, SUM(f.LineNetAmount) AS NetSales, SUM(f.LineCostAmount) AS CostOfSales,
       SUM(f.GrossMarginAmount) AS GrossMargin,
       CONVERT(DECIMAL(9, 4), SUM(f.GrossMarginAmount) / NULLIF(SUM(f.LineNetAmount), 0)) AS GrossMarginPercent
FROM dw.FactSalesLine AS f
JOIN dw.DimDate AS d ON d.DateKey = f.SaleDateKey
JOIN dw.DimSite AS s ON s.SiteKey = f.SiteKey
JOIN dw.DimProduct AS p ON p.ProductKey = f.ProductKey
WHERE f.IsDeletedInSource = 0
GROUP BY d.FiscalYear, d.YearMonth, s.SiteID, s.SiteName, p.Category;
GO

-- GRAIN: one row per month, site and expense type.
CREATE OR ALTER VIEW mart.vw_ExpenseAnalysis AS
SELECT d.FiscalYear, d.YearMonth, s.SiteID, s.SiteName, s.SiteType, e.ExpenseType, a.AccountCode, a.AccountName,
       COUNT(*) AS Expenses, SUM(e.Amount) AS Amount
FROM dw.FactOperatingExpense AS e
JOIN dw.DimDate AS d ON d.DateKey = e.ExpenseDateKey
JOIN dw.DimSite AS s ON s.SiteKey = e.SiteKey
JOIN dw.DimAccount AS a ON a.AccountKey = e.AccountKey
WHERE e.IsDeletedInSource = 0
GROUP BY d.FiscalYear, d.YearMonth, s.SiteID, s.SiteName, s.SiteType, e.ExpenseType, a.AccountCode, a.AccountName;
GO

-- =========================================================
-- PURCHASING AND SUPPLIERS
-- =========================================================

-- GRAIN: one row per purchase order. Minimum order value in NAD.
CREATE OR ALTER VIEW mart.vw_PurchaseOrderCompliance AS
SELECT po.POID, d.[Date] AS OrderDate, s.SupplierID, s.SupplierName, po.POValue, po.SupplierMinimumOrderValue,
       po.POValue - po.SupplierMinimumOrderValue AS HeadroomAboveMinimum,
       IIF(po.MeetsMinimumOrderValue = 1, 'COMPLIANT', 'BELOW_MINIMUM') AS Status
FROM dw.FactPurchaseOrder AS po
JOIN dw.DimSupplier AS s ON s.SupplierKey = po.SupplierKey
JOIN dw.DimDate AS d ON d.DateKey = po.OrderDateKey
WHERE po.IsDeletedInSource = 0;
GO

-- GRAIN: one row per purchase order: ordered vs received vs invoiced.
-- Goods receipts carry no quantities, so receipt is matched at document
-- level; invoices are matched on value (tolerance purchasing.invoice_match_tolerance_nad).
CREATE OR ALTER VIEW mart.vw_ThreeWayMatch AS
WITH Inv AS (
    SELECT POID, COUNT(*) AS Invoices, SUM(NetAmount) AS InvoicedNet, SUM(TotalAmount) AS InvoicedTotal
    FROM dw.FactSupplierInvoice WHERE IsDeletedInSource = 0 GROUP BY POID
), Grn AS (
    SELECT POID, COUNT(*) AS Receipts, MIN(ReceiptDateKey) AS FirstReceiptDateKey, MAX(LeadTimeDays) AS LeadTimeDays
    FROM dw.FactGoodsReceipt WHERE IsDeletedInSource = 0 GROUP BY POID
)
SELECT po.POID, s.SupplierID, s.SupplierName, po.POValue AS OrderedNet,
       ISNULL(g.Receipts, 0) AS Receipts, g.LeadTimeDays,
       ISNULL(i.Invoices, 0) AS Invoices, i.InvoicedNet, i.InvoicedNet - po.POValue AS InvoiceVariance,
       CASE WHEN g.Receipts IS NULL AND i.Invoices IS NOT NULL THEN 'INVOICED_NOT_RECEIVED'
            WHEN g.Receipts IS NULL THEN 'NOT_RECEIVED'
            WHEN i.Invoices IS NULL THEN 'RECEIVED_NOT_INVOICED'
            WHEN ABS(i.InvoicedNet - po.POValue) > meta.fn_SettingDecimal('purchasing.invoice_match_tolerance_nad', 1)
                THEN 'AMOUNT_MISMATCH'
            ELSE 'MATCHED' END AS MatchStatus
FROM dw.FactPurchaseOrder AS po
JOIN dw.DimSupplier AS s ON s.SupplierKey = po.SupplierKey
LEFT JOIN Grn AS g ON g.POID = po.POID
LEFT JOIN Inv AS i ON i.POID = po.POID
WHERE po.IsDeletedInSource = 0;
GO

-- GRAIN: one row per supplier.
CREATE OR ALTER VIEW mart.vw_SupplierPerformance AS
SELECT s.SupplierID, s.SupplierName, s.MinimumOrderValueNAD, s.PaymentTermsDays,
       COUNT(DISTINCT m.POID) AS PurchaseOrders,
       SUM(m.OrderedNet) AS PurchaseValue,
       AVG(CONVERT(DECIMAL(9, 2), m.LeadTimeDays)) AS AvgLeadTimeDays,
       MAX(m.LeadTimeDays) AS MaxLeadTimeDays,
       CONVERT(DECIMAL(5, 2), 100.0 * SUM(IIF(m.MatchStatus = 'MATCHED', 1, 0)) / NULLIF(COUNT(*), 0)) AS MatchRatePercent,
       (SELECT SUM(r.RebateAmount) FROM dw.FactSupplierRebate AS r
        WHERE r.SupplierID = s.SupplierID AND r.IsDeletedInSource = 0) AS RebatesEarned,
       (SELECT SUM(r.RebateVariance) FROM dw.FactSupplierRebate AS r
        WHERE r.SupplierID = s.SupplierID AND r.IsDeletedInSource = 0) AS RebateVariance
FROM dw.DimSupplier AS s
LEFT JOIN mart.vw_ThreeWayMatch AS m ON m.SupplierID = s.SupplierID
WHERE s.IsCurrent = 1 AND s.SupplierKey <> -1
GROUP BY s.SupplierID, s.SupplierName, s.MinimumOrderValueNAD, s.PaymentTermsDays;
GO

-- GRAIN: one row per supplier per quarter: rebate eligibility and gap to the next threshold.
CREATE OR ALTER VIEW mart.vw_RebateEligibility AS
SELECT r.SupplierID, s.SupplierName, r.RebatePeriod, r.QualifyingSpend, r.RebateRate AS RecordedRate,
       r.ExpectedRebateRate, r.RebateAmount, r.ExpectedRebateAmount, r.RebateVariance,
       nt.ThresholdNAD AS NextThresholdNAD, nt.RebateRate AS NextThresholdRate,
       nt.ThresholdNAD - r.QualifyingSpend AS SpendToNextThreshold,
       IIF(ABS(r.RebateVariance) <= 0.01, 'CORRECT', 'INCORRECT') AS Status
FROM dw.FactSupplierRebate AS r
JOIN dw.DimSupplier AS s ON s.SupplierKey = r.SupplierKey
OUTER APPLY (SELECT TOP 1 t.ThresholdNAD, t.RebateRate FROM meta.RebateThreshold AS t
             WHERE t.ThresholdNAD > r.QualifyingSpend ORDER BY t.ThresholdNAD) AS nt
WHERE r.IsDeletedInSource = 0;
GO

-- GRAIN: one row per supplier per month.
CREATE OR ALTER VIEW mart.vw_PurchasingFrequency AS
SELECT s.SupplierID, s.SupplierName, d.YearMonth, COUNT(*) AS PurchaseOrders, SUM(po.POValue) AS PurchaseValue,
       AVG(po.POValue) AS AverageOrderValue
FROM dw.FactPurchaseOrder AS po
JOIN dw.DimSupplier AS s ON s.SupplierKey = po.SupplierKey
JOIN dw.DimDate AS d ON d.DateKey = po.OrderDateKey
WHERE po.IsDeletedInSource = 0
GROUP BY s.SupplierID, s.SupplierName, d.YearMonth;
GO

-- =========================================================
-- INVENTORY AND REPLENISHMENT
-- =========================================================

-- GRAIN: one row per site and product: stock position at the as-at date.
CREATE OR ALTER VIEW mart.vw_StockOnHand AS
WITH AsAt AS (
    SELECT dw.fn_DateKey(COALESCE((SELECT TRY_CONVERT(DATE, SettingValue) FROM meta.Setting
                                   WHERE SettingKey = 'reporting.as_at_date'), CONVERT(DATE, SYSDATETIME()))) AS AsAtKey
)
SELECT si.SiteID, si.SiteName, si.SiteType, p.ProductID, p.ProductName, p.Category,
       SUM(m.Quantity) AS QuantityOnHand,
       SUM(m.Quantity) * cur.StandardCost AS StockValue,
       MAX(IIF(m.Quantity < 0, m.MovementDateKey, NULL)) AS LastOutflowDateKey,
       MAX(IIF(m.Quantity > 0, m.MovementDateKey, NULL)) AS LastInflowDateKey
FROM dw.FactInventoryMovement AS m
CROSS JOIN AsAt AS a
JOIN dw.DimSite AS si ON si.SiteKey = m.SiteKey
JOIN dw.DimProduct AS p ON p.ProductKey = m.ProductKey
JOIN dw.DimProduct AS cur ON cur.ProductID = p.ProductID AND cur.IsCurrent = 1
WHERE m.IsDeletedInSource = 0 AND m.MovementDateKey <= a.AsAtKey
GROUP BY si.SiteID, si.SiteName, si.SiteType, p.ProductID, p.ProductName, p.Category, cur.StandardCost;
GO

/*
    GRAIN: one row per site and product.
    Demand: average daily outflow (sales at branches, transfers out at the
    DC) over replenishment.demand_window_days to the as-at date.
    Lead time: DC - average goods-receipt lead time of the product's
    suppliers; branches - replenishment.default_lead_time_days (weekly DC run).
    Reorder point = daily demand x (lead time + safety_stock_days).
*/
CREATE OR ALTER VIEW mart.vw_ReplenishmentPlan AS
WITH Params AS (
    SELECT COALESCE((SELECT TRY_CONVERT(DATE, SettingValue) FROM meta.Setting WHERE SettingKey = 'reporting.as_at_date'),
                    CONVERT(DATE, SYSDATETIME())) AS AsAtDate,
           meta.fn_SettingDecimal('replenishment.demand_window_days', 90) AS WindowDays,
           meta.fn_SettingDecimal('replenishment.safety_stock_days', 7) AS SafetyDays,
           meta.fn_SettingDecimal('replenishment.default_lead_time_days', 7) AS DefaultLeadDays,
           meta.fn_SettingDecimal('replenishment.excess_cover_days', 60) AS ExcessCoverDays,
           meta.fn_SettingDecimal('replenishment.slow_moving_days', 45) AS SlowMovingDays
), Demand AS (
    SELECT si.SiteID, p.ProductID,
           SUM(-m.Quantity) / MAX(pr.WindowDays) AS AvgDailyDemand
    FROM dw.FactInventoryMovement AS m
    CROSS JOIN Params AS pr
    JOIN dw.DimDate AS d ON d.DateKey = m.MovementDateKey
    JOIN dw.DimSite AS si ON si.SiteKey = m.SiteKey
    JOIN dw.DimProduct AS p ON p.ProductKey = m.ProductKey
    WHERE m.IsDeletedInSource = 0 AND m.Quantity < 0 AND m.MovementType IN (N'SALE', N'TRANSFER_OUT')
      AND d.[Date] > DATEADD(DAY, -CONVERT(INT, pr.WindowDays), pr.AsAtDate) AND d.[Date] <= pr.AsAtDate
    GROUP BY si.SiteID, p.ProductID
), SupplierLeadTime AS (
    SELECT l.ProductID, AVG(CONVERT(DECIMAL(9, 2), g.LeadTimeDays)) AS AvgLeadTimeDays
    FROM dw.FactPurchaseOrderLine AS l
    JOIN dw.FactGoodsReceipt AS g ON g.POID = l.POID AND g.IsDeletedInSource = 0
    WHERE l.IsDeletedInSource = 0
    GROUP BY l.ProductID
)
SELECT s.SiteID, s.SiteName, s.SiteType, s.ProductID, s.ProductName, s.Category, s.QuantityOnHand, s.StockValue,
       CONVERT(DECIMAL(19, 3), ISNULL(dm.AvgDailyDemand, 0)) AS AvgDailyDemand,
       CONVERT(DECIMAL(9, 2), IIF(s.SiteType = N'DISTRIBUTION_CENTRE', ISNULL(lt.AvgLeadTimeDays, pr.DefaultLeadDays),
                                  pr.DefaultLeadDays)) AS LeadTimeDays,
       CONVERT(DECIMAL(19, 1), ISNULL(dm.AvgDailyDemand, 0) *
           (IIF(s.SiteType = N'DISTRIBUTION_CENTRE', ISNULL(lt.AvgLeadTimeDays, pr.DefaultLeadDays), pr.DefaultLeadDays)
            + pr.SafetyDays)) AS ReorderPoint,
       CONVERT(DECIMAL(19, 1), s.QuantityOnHand / NULLIF(dm.AvgDailyDemand, 0)) AS DaysOfCover,
       DATEDIFF(DAY, lo.[Date], pr.AsAtDate) AS DaysSinceLastOutflow,
       CASE WHEN s.QuantityOnHand < 0 THEN 'NEGATIVE_STOCK'
            WHEN ISNULL(dm.AvgDailyDemand, 0) > 0 AND s.QuantityOnHand <
                 dm.AvgDailyDemand * (IIF(s.SiteType = N'DISTRIBUTION_CENTRE', ISNULL(lt.AvgLeadTimeDays, pr.DefaultLeadDays),
                                          pr.DefaultLeadDays) + pr.SafetyDays) THEN 'REORDER'
            WHEN s.QuantityOnHand > 0 AND (lo.[Date] IS NULL OR DATEDIFF(DAY, lo.[Date], pr.AsAtDate) > pr.SlowMovingDays)
                 THEN 'SLOW_MOVING'
            WHEN dm.AvgDailyDemand > 0 AND s.QuantityOnHand / dm.AvgDailyDemand > pr.ExcessCoverDays THEN 'EXCESS'
            ELSE 'OK' END AS StockStatus
FROM mart.vw_StockOnHand AS s
CROSS JOIN Params AS pr
LEFT JOIN Demand AS dm ON dm.SiteID = s.SiteID AND dm.ProductID = s.ProductID
LEFT JOIN SupplierLeadTime AS lt ON lt.ProductID = s.ProductID
LEFT JOIN dw.DimDate AS lo ON lo.DateKey = s.LastOutflowDateKey
WHERE s.SiteType IN (N'BRANCH', N'DISTRIBUTION_CENTRE');
GO

/*
    GRAIN: one row per supplier: what the DC would order now from its
    preferred suppliers (products below reorder point topped up to cover
    lead time + safety + one review period), valued in NAD and checked
    against the supplier's minimum order value.
*/
CREATE OR ALTER VIEW mart.vw_SupplierReorderProposal AS
WITH Needs AS (
    SELECT r.ProductID, r.ProductName,
           CEILING(r.AvgDailyDemand * (r.LeadTimeDays + meta.fn_SettingDecimal('replenishment.safety_stock_days', 7)
                   + meta.fn_SettingDecimal('replenishment.default_lead_time_days', 7)) - r.QuantityOnHand) AS SuggestedQuantity
    FROM mart.vw_ReplenishmentPlan AS r
    WHERE r.SiteType = N'DISTRIBUTION_CENTRE' AND r.StockStatus IN ('REORDER', 'NEGATIVE_STOCK')
)
SELECT s.SupplierID, s.SupplierName, s.MinimumOrderValueNAD,
       COUNT(*) AS ProductsToOrder,
       SUM(n.SuggestedQuantity) AS UnitsToOrder,
       SUM(n.SuggestedQuantity * b.SupplierUnitCost) AS ProposedOrderValue,
       IIF(SUM(n.SuggestedQuantity * b.SupplierUnitCost) >= s.MinimumOrderValueNAD, 'MEETS_MINIMUM',
           'BELOW_MINIMUM_CONSOLIDATE') AS MinimumOrderStatus,
       IIF(SUM(n.SuggestedQuantity * b.SupplierUnitCost) >= s.MinimumOrderValueNAD, 0,
           s.MinimumOrderValueNAD - SUM(n.SuggestedQuantity * b.SupplierUnitCost)) AS ValueShortOfMinimum
FROM Needs AS n
JOIN dw.BridgeProductSupplier AS b ON b.ProductID = n.ProductID AND b.IsPreferredSupplier = 1 AND b.IsDeletedInSource = 0
JOIN dw.DimSupplier AS s ON s.SupplierID = b.SupplierID AND s.IsCurrent = 1
WHERE n.SuggestedQuantity > 0
GROUP BY s.SupplierID, s.SupplierName, s.MinimumOrderValueNAD;
GO

-- =========================================================
-- ASSETS
-- =========================================================

-- GRAIN: one row per ERP asset, at the as-at date.
CREATE OR ALTER VIEW mart.vw_AssetRegister AS
WITH Latest AS (
    SELECT AssetKey, MAX(PeriodEndDateKey) AS LastPeriodKey FROM dw.FactAssetDepreciation GROUP BY AssetKey
)
SELECT a.AssetID, a.SiteID, si.SiteName, a.AssetClass, a.AcquisitionDate, a.AcquisitionCost, a.UsefulLifeYears,
       a.MonthlyDepreciation, ISNULL(f.AccumulatedDepreciation, 0) AS ScheduleAccumulatedDepreciation,
       ISNULL(f.NetBookValue, a.AcquisitionCost) AS ScheduleNetBookValue,
       a.SourceAccumulatedDepreciation, a.SourceNetBookValue,
       ISNULL(f.NetBookValue, a.AcquisitionCost) - a.SourceNetBookValue AS NetBookValueDifference,
       IIF(ABS(ISNULL(f.NetBookValue, a.AcquisitionCost) - ISNULL(a.SourceNetBookValue, 0)) <= 1, 'AGREES', 'DIFFERS')
           AS RegisterStatus,
       a.AssetStatus, a.FullyDepreciatedDate
FROM dw.DimAsset AS a
LEFT JOIN Latest AS l ON l.AssetKey = a.AssetKey
LEFT JOIN dw.FactAssetDepreciation AS f ON f.AssetKey = l.AssetKey AND f.PeriodEndDateKey = l.LastPeriodKey
LEFT JOIN dw.DimSite AS si ON si.SiteID = a.SiteID
WHERE a.AssetKey <> -1 AND a.IsDeletedInSource = 0;
GO

-- GRAIN: one row per ERP asset and fiscal year covered by the ledger:
-- depreciation per the schedule vs the depreciation journal posted for the
-- asset (source_document_id = asset_id).
CREATE OR ALTER VIEW mart.vw_AssetDepreciationReconciliation AS
WITH Schedule AS (
    SELECT a.AssetID, d.FiscalYear, SUM(f.DepreciationAmount) AS ScheduleDepreciation
    FROM dw.FactAssetDepreciation AS f
    JOIN dw.DimAsset AS a ON a.AssetKey = f.AssetKey
    JOIN dw.DimDate AS d ON d.DateKey = f.PeriodEndDateKey
    GROUP BY a.AssetID, d.FiscalYear
), Posted AS (
    SELECT g.SourceDocumentID AS AssetID, d.FiscalYear, SUM(g.DebitAmount) AS PostedDepreciation
    FROM dw.FactGeneralLedger AS g
    JOIN dw.DimDate AS d ON d.DateKey = g.PostingDateKey
    WHERE g.IsDeletedInSource = 0 AND g.TransactionType = N'DEPRECIATION'
    GROUP BY g.SourceDocumentID, d.FiscalYear
), LedgerYears AS (
    SELECT DISTINCT FiscalYear FROM Posted
), Keys AS (
    SELECT a.AssetID, y.FiscalYear
    FROM dw.DimAsset AS a CROSS JOIN LedgerYears AS y
    WHERE a.AssetKey <> -1 AND a.IsDeletedInSource = 0
    UNION
    SELECT AssetID, FiscalYear FROM Posted
)
SELECT k.AssetID, a.AssetClass, a.AcquisitionDate, a.FullyDepreciatedDate, k.FiscalYear,
       ISNULL(s.ScheduleDepreciation, 0) AS ScheduleDepreciation, ISNULL(p.PostedDepreciation, 0) AS PostedDepreciation,
       ISNULL(p.PostedDepreciation, 0) - ISNULL(s.ScheduleDepreciation, 0) AS Difference,
       CASE WHEN ABS(ISNULL(p.PostedDepreciation, 0) - ISNULL(s.ScheduleDepreciation, 0)) <= 1 THEN 'RECONCILED'
            WHEN a.AssetID IS NULL THEN 'POSTED_FOR_UNKNOWN_ASSET'
            WHEN p.PostedDepreciation IS NULL THEN 'NOT_POSTED'
            WHEN s.ScheduleDepreciation IS NULL AND a.FullyDepreciatedDate < DATEFROMPARTS(k.FiscalYear, 1, 1)
                THEN 'POSTED_AFTER_FULLY_DEPRECIATED'
            WHEN s.ScheduleDepreciation IS NULL THEN 'POSTED_WITHOUT_SCHEDULE'
            ELSE 'DIFFERENCE' END AS Status,
       CASE WHEN ABS(ISNULL(p.PostedDepreciation, 0) - ISNULL(s.ScheduleDepreciation, 0)) <= 1 THEN NULL
            WHEN s.ScheduleDepreciation IS NULL AND a.FullyDepreciatedDate < DATEFROMPARTS(k.FiscalYear, 1, 1)
                THEN N'Reverse the depreciation journal: the asset was already fully depreciated.'
            ELSE N'Agree the depreciation start convention (month of / month after acquisition) and correct the journal.'
       END AS RecommendedAction
FROM Keys AS k
LEFT JOIN dw.DimAsset AS a ON a.AssetID = k.AssetID AND a.IsDeletedInSource = 0
LEFT JOIN Schedule AS s ON s.AssetID = k.AssetID AND s.FiscalYear = k.FiscalYear
LEFT JOIN Posted AS p ON p.AssetID = k.AssetID AND p.FiscalYear = k.FiscalYear;
GO

-- GRAIN: one row per local-register exception.
CREATE OR ALTER VIEW mart.vw_LocalAssetExceptions AS
SELECT la.LocalAssetID, la.SiteID, la.DepartmentID, la.AssetDescription, la.AssetCondition, la.RecordedDate,
       'UNASSIGNED' AS ExceptionType, N'Assign a responsible employee in the branch register.' AS RecommendedAction
FROM dw.DimLocalAsset AS la WHERE la.LocalAssetKey <> -1 AND la.IsDeletedInSource = 0 AND la.IsAssigned = 0
UNION ALL
SELECT la.LocalAssetID, la.SiteID, la.DepartmentID, la.AssetDescription, la.AssetCondition, la.RecordedDate,
       'NEEDS_REPAIR', N'Schedule repair or write-off.'
FROM dw.DimLocalAsset AS la WHERE la.IsDeletedInSource = 0 AND la.AssetCondition = N'Needs Repair'
UNION ALL
SELECT la.LocalAssetID, la.SiteID, la.DepartmentID, la.AssetDescription, la.AssetCondition, la.RecordedDate,
       'POSSIBLE_DUPLICATE', N'Verify physically; the same item appears more than once in the register.'
FROM dw.DimLocalAsset AS la
WHERE la.IsDeletedInSource = 0 AND la.LocalAssetKey <> -1 AND EXISTS (
    SELECT 1 FROM dw.DimLocalAsset AS o
    WHERE o.LocalAssetID <> la.LocalAssetID AND o.IsDeletedInSource = 0 AND o.SiteID = la.SiteID
      AND o.DepartmentID = la.DepartmentID AND o.AssetDescription = la.AssetDescription
      AND ISNULL(o.AssignedTo, N'') = ISNULL(la.AssignedTo, N'') AND o.RecordedDate = la.RecordedDate)
UNION ALL
SELECT la.LocalAssetID, la.SiteID, la.DepartmentID, la.AssetDescription, la.AssetCondition, la.RecordedDate,
       'UNKNOWN_SITE_OR_DEPARTMENT', N'Correct the site or department code in the branch register.'
FROM dw.DimLocalAsset AS la
WHERE la.IsDeletedInSource = 0 AND la.LocalAssetKey <> -1
  AND (NOT EXISTS (SELECT 1 FROM dw.DimSite AS s WHERE s.SiteID = la.SiteID AND s.IsDeletedInSource = 0)
       OR NOT EXISTS (SELECT 1 FROM dw.DimDepartment AS d WHERE d.DepartmentID = la.DepartmentID AND d.IsDeletedInSource = 0));
GO

-- GRAIN: one row per site: ERP capital assets vs local register items.
CREATE OR ALTER VIEW mart.vw_AssetsBySite AS
SELECT s.SiteID, s.SiteName, s.SiteType,
       (SELECT COUNT(*) FROM dw.DimAsset AS a WHERE a.SiteID = s.SiteID AND a.IsDeletedInSource = 0) AS ErpAssets,
       (SELECT SUM(a.AcquisitionCost) FROM dw.DimAsset AS a WHERE a.SiteID = s.SiteID AND a.IsDeletedInSource = 0) AS ErpAssetCost,
       (SELECT COUNT(*) FROM dw.DimLocalAsset AS l WHERE l.SiteID = s.SiteID AND l.IsDeletedInSource = 0) AS LocalRegisterItems,
       (SELECT COUNT(*) FROM dw.DimLocalAsset AS l WHERE l.SiteID = s.SiteID AND l.IsDeletedInSource = 0 AND l.IsAssigned = 0)
           AS UnassignedLocalItems
FROM dw.DimSite AS s WHERE s.SiteKey <> -1;
GO

-- =========================================================
-- BRANCH CASH-UPS
-- =========================================================
/*
    GRAIN: one row per branch and business date in the cash-up window
    (the dates covered by any cash-up document). Declared takings per
    payment method vs POS sales (incl. VAT), cash variance, and missing or
    quarantined documents (from the latest load-gate decisions).
*/
CREATE OR ALTER VIEW mart.vw_CashupReconciliation AS
WITH Window_ AS (
    SELECT MIN(BusinessDateKey) AS FromKey, MAX(BusinessDateKey) AS ToKey FROM dw.FactBranchCashup
    WHERE IsDeletedInSource = 0
), BranchDays AS (
    SELECT s.SiteKey, s.SiteID, s.SiteName, d.DateKey, d.[Date]
    FROM dw.DimSite AS s
    CROSS JOIN Window_ AS w
    JOIN dw.DimDate AS d ON d.DateKey BETWEEN w.FromKey AND w.ToKey
    WHERE s.IsBranch = 1 AND s.IsDeletedInSource = 0
), Pos AS (
    SELECT f.SiteKey, f.SaleDateKey,
           SUM(IIF(pm.PaymentMethodCode = N'CASH', f.TotalAmount, 0)) AS PosCash,
           SUM(IIF(pm.PaymentMethodCode = N'CARD', f.TotalAmount, 0)) AS PosCard,
           SUM(IIF(pm.PaymentMethodCode = N'CREDIT', f.TotalAmount, 0)) AS PosAccount
    FROM dw.FactSales AS f JOIN dw.DimPaymentMethod AS pm ON pm.PaymentMethodKey = f.PaymentMethodKey
    WHERE f.IsDeletedInSource = 0
    GROUP BY f.SiteKey, f.SaleDateKey
), LatestGate AS (
    SELECT b.FileName, b.Decision, b.Reason
    FROM audit.PipelineSourceBatch AS b
    WHERE b.SourceName = N'branch_cashup'
      AND b.PipelineExecutionID = (SELECT MAX(PipelineExecutionID) FROM audit.PipelineSourceBatch
                                   WHERE SourceName = N'branch_cashup')
), NotLoaded AS (
    -- File names: <SiteID>_SHIFT-nnnnnn_<YYYYMMDD>_Cashup.pdf
    SELECT LEFT(FileName, CHARINDEX('_', FileName) - 1) AS SiteID,
           TRY_CONVERT(INT, SUBSTRING(FileName, CHARINDEX('_', FileName, CHARINDEX('_', FileName) + 1) + 1, 8)) AS DateKey,
           Decision, Reason
    FROM LatestGate WHERE Decision IN ('BLOCKED', 'HELD') AND FileName LIKE '%[_]Cashup.pdf'
)
SELECT bd.SiteID, bd.SiteName, bd.[Date] AS BusinessDate, c.ShiftID,
       ISNULL(p.PosCash, 0) AS PosCashSales, c.CashSales AS DeclaredCashSales,
       c.CashSales - ISNULL(p.PosCash, 0) AS CashSalesDifference,
       ISNULL(p.PosCard, 0) AS PosCardSales, c.CardSales AS DeclaredCardSales,
       c.CardSales - ISNULL(p.PosCard, 0) AS CardSalesDifference,
       ISNULL(p.PosAccount, 0) AS PosAccountSales, c.AccountSales AS DeclaredAccountSales,
       c.AccountSales - ISNULL(p.PosAccount, 0) AS AccountSalesDifference,
       c.CashVariance, c.CashVarianceStatus,
       CASE WHEN c.ShiftID IS NULL AND nl.Decision IS NOT NULL THEN 'CASHUP_QUARANTINED'
            WHEN c.ShiftID IS NULL THEN 'MISSING_CASHUP'
            WHEN ABS(c.CashSales - ISNULL(p.PosCash, 0)) > meta.fn_SettingDecimal('cashup.pos_tolerance_nad', 1)
              OR ABS(c.CardSales - ISNULL(p.PosCard, 0)) > meta.fn_SettingDecimal('cashup.pos_tolerance_nad', 1)
              OR ABS(c.AccountSales - ISNULL(p.PosAccount, 0)) > meta.fn_SettingDecimal('cashup.pos_tolerance_nad', 1)
                THEN 'POS_MISMATCH'
            WHEN c.CashVarianceStatus <> N'BALANCED' THEN 'CASH_' + c.CashVarianceStatus
            ELSE 'BALANCED' END AS ReconciliationStatus,
       CASE WHEN c.ShiftID IS NULL AND nl.Decision IS NOT NULL
                THEN CONCAT(N'Document submitted but not loaded: ', nl.Reason)
            WHEN c.ShiftID IS NULL THEN N'No cash-up received: obtain the document from the branch manager.'
            WHEN ABS(c.CashSales - ISNULL(p.PosCash, 0)) > meta.fn_SettingDecimal('cashup.pos_tolerance_nad', 1)
                THEN N'Declared cash takings differ from POS cash sales: recount and investigate.'
            WHEN c.CashVarianceStatus = N'SHORTAGE' THEN N'Cash shortage: investigate with the cashier and record the outcome.'
            WHEN c.CashVarianceStatus = N'OVERAGE' THEN N'Cash overage: check for unrecorded sales or float errors.'
            ELSE NULL END AS RecommendedAction
FROM BranchDays AS bd
LEFT JOIN dw.FactBranchCashup AS c
       ON c.SiteKey = bd.SiteKey AND c.BusinessDateKey = bd.DateKey AND c.IsDeletedInSource = 0
LEFT JOIN Pos AS p ON p.SiteKey = bd.SiteKey AND p.SaleDateKey = bd.DateKey
LEFT JOIN NotLoaded AS nl ON nl.SiteID = bd.SiteID AND nl.DateKey = bd.DateKey;
GO
