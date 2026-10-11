USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    023 - Presentation layer (mart schema) for Power BI and analysts.

    Star-schema views over the warehouse: friendly names, technical and
    lineage columns hidden, soft-deleted rows excluded. Power BI connects
    to these views only, so the warehouse can change underneath without
    breaking reports. Facts of different grains are separate tables that
    share conformed dimensions; they are related only through dimensions.

    SCD2 dimensions expose every version (facts point at the version
    current when they were loaded) plus IsCurrent for "as-is" reporting.
*/

-- ---------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------
CREATE OR ALTER VIEW mart.DimDate AS
SELECT DateKey, [Date], [Year], QuarterNumber, QuarterName, MonthNumber, MonthName, MonthShortName,
       YearMonth, DayOfMonth, DayOfWeekNumber, DayName, IsoWeek, IsWeekend, FiscalYear, FiscalPeriod,
       MonthStartDate, MonthEndDate, IsMonthEnd
FROM dw.DimDate;
GO

CREATE OR ALTER VIEW mart.DimSite AS
SELECT SiteKey, SiteID, SiteName, Town, SiteType, IsBranch, IsDistributionCentre, IsHeadOffice,
       IsDeletedInSource AS IsRetired
FROM dw.DimSite;
GO

CREATE OR ALTER VIEW mart.DimDepartment AS
SELECT DepartmentKey, DepartmentID, DepartmentName FROM dw.DimDepartment;
GO

CREATE OR ALTER VIEW mart.DimProduct AS
SELECT ProductKey, ProductID, ProductName, Category, StandardCost, SellingPrice, IsDCStocked, SupplyRoute,
       RevisionNumber, IsCurrent, EffectiveFrom, EffectiveTo, IsDeletedInSource AS IsDiscontinued
FROM dw.DimProduct;
GO

CREATE OR ALTER VIEW mart.DimCustomer AS
SELECT CustomerKey, CustomerID, CustomerName, CustomerType, Town, CreditLimit, HasCreditFacility,
       RevisionNumber, IsCurrent, EffectiveFrom, EffectiveTo
FROM dw.DimCustomer;
GO

CREATE OR ALTER VIEW mart.DimSupplier AS
SELECT SupplierKey, SupplierID, SupplierName, Location, MinimumOrderValueNAD, RebateRate, PaymentTermsDays,
       RevisionNumber, IsCurrent, EffectiveFrom, EffectiveTo
FROM dw.DimSupplier;
GO

CREATE OR ALTER VIEW mart.DimAccount AS
SELECT AccountKey, AccountCode, AccountName, AccountType, FinancialStatement, NormalBalance, NormalSign,
       StatementSection
FROM dw.DimAccount;
GO

CREATE OR ALTER VIEW mart.DimPaymentMethod AS
SELECT PaymentMethodKey, PaymentMethodCode, PaymentMethodName, IsCash, SettlementType FROM dw.DimPaymentMethod;
GO

CREATE OR ALTER VIEW mart.DimAsset AS
SELECT AssetKey, AssetID, SiteID, AssetClass, AcquisitionDate, AcquisitionCost, UsefulLifeYears,
       MonthlyDepreciation, DepreciationMethod, AssetStatus, FullyDepreciatedDate
FROM dw.DimAsset WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.DimLocalAsset AS
SELECT LocalAssetKey, LocalAssetID, SiteID, DepartmentID, AssetDescription, AssetCondition, AssignedTo,
       IsAssigned, RecordedDate
FROM dw.DimLocalAsset WHERE IsDeletedInSource = 0;
GO

-- ---------------------------------------------------------
-- Facts (active rows only)
-- ---------------------------------------------------------
CREATE OR ALTER VIEW mart.FactSales AS
SELECT SaleID, SaleDateKey, SiteKey, CustomerKey, PaymentMethodKey, NetAmount, VATAmount, TotalAmount
FROM dw.FactSales WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactSalesLine AS
SELECT SaleID, ProductID, SaleDateKey, SiteKey, CustomerKey, ProductKey, PaymentMethodKey, Quantity, UnitPrice,
       LineNetAmount, UnitCost, LineCostAmount, GrossMarginAmount
FROM dw.FactSalesLine WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactInventoryMovement AS
SELECT MovementID, MovementDateKey, SiteKey, ProductKey, MovementType, Direction, Quantity, UnitCost,
       MovementValue, SourceDocumentID
FROM dw.FactInventoryMovement WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactInventorySnapshot AS
SELECT SnapshotDateKey, SiteKey, ProductKey, QuantityIn, QuantityOut, ClosingQuantity, ClosingValue
FROM dw.FactInventorySnapshot;
GO

CREATE OR ALTER VIEW mart.FactPurchaseOrder AS
SELECT POID, OrderDateKey, SupplierKey, DeliverySiteKey, POStatus, POValue, SupplierMinimumOrderValue,
       MeetsMinimumOrderValue
FROM dw.FactPurchaseOrder WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactPurchaseOrderLine AS
SELECT POID, ProductID, OrderDateKey, SupplierKey, ProductKey, DeliverySiteKey, Quantity, UnitCost, LineValue
FROM dw.FactPurchaseOrderLine WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactGoodsReceipt AS
SELECT GRNID, POID, ReceiptDateKey, OrderDateKey, SupplierKey, SiteKey, LeadTimeDays
FROM dw.FactGoodsReceipt WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactSupplierInvoice AS
SELECT InvoiceID, POID, InvoiceDateKey, DueDateKey, SupplierKey, InvoiceStatus, NetAmount, VATAmount, TotalAmount,
       PaymentTermsDays, OrderedValue, InvoiceToOrderVariance
FROM dw.FactSupplierInvoice WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactSupplierRebate AS
SELECT SupplierID, RebatePeriod, PeriodEndDateKey, SupplierKey, QualifyingSpend, RebateRate, RebateAmount,
       ExpectedRebateRate, ExpectedRebateAmount, RebateVariance
FROM dw.FactSupplierRebate WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactGeneralLedger AS
SELECT JournalID, JournalLineNumber, PostingDateKey, AccountKey, SiteKey, SourceDocumentID, TransactionType,
       DebitAmount, CreditAmount, NetAmount
FROM dw.FactGeneralLedger WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactOperatingExpense AS
SELECT ExpenseID, ExpenseDateKey, SiteKey, AccountKey, ExpenseType, ExpenseDescription, Amount
FROM dw.FactOperatingExpense WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.FactAssetDepreciation AS
SELECT AssetKey, PeriodEndDateKey, SiteKey, DepreciationAmount, AccumulatedDepreciation, NetBookValue
FROM dw.FactAssetDepreciation;
GO

CREATE OR ALTER VIEW mart.FactBranchCashup AS
SELECT ShiftID, BusinessDateKey, SiteKey, PosTerminal, Cashier, OpeningFloat, CashSales, CardSales, AccountSales,
       TotalSales, InternalExpenses, CashRemoved, ExpectedClosingCash, ActualClosingCash, CashVariance,
       CashVarianceStatus, ManagerApproval
FROM dw.FactBranchCashup WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW mart.BridgeProductSupplier AS
SELECT ProductID, SupplierID, ProductKey, SupplierKey, SupplierUnitCost, IsPreferredSupplier
FROM dw.BridgeProductSupplier WHERE IsDeletedInSource = 0;
GO
