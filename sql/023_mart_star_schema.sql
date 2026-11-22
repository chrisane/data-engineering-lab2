USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    023 - Presentation layer (presentation schema) for Power BI and analysts.

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
CREATE OR ALTER VIEW presentation.DimDate AS
SELECT DateKey, [Date], [Year], QuarterNumber, QuarterName, MonthNumber, MonthName, MonthShortName,
       YearMonth, DayOfMonth, DayOfWeekNumber, DayName, IsoWeek, IsWeekend, FiscalYear, FiscalPeriod,
       MonthStartDate, MonthEndDate, IsMonthEnd
FROM gold.DimDate;
GO

CREATE OR ALTER VIEW presentation.DimSite AS
SELECT SiteKey, SiteID, SiteName, Town, SiteType, IsBranch, IsDistributionCentre, IsHeadOffice,
       IsDeletedInSource AS IsRetired
FROM gold.DimSite;
GO

CREATE OR ALTER VIEW presentation.DimDepartment AS
SELECT DepartmentKey, DepartmentID, DepartmentName FROM gold.DimDepartment;
GO

CREATE OR ALTER VIEW presentation.DimProduct AS
SELECT ProductKey, ProductID, ProductName, Category, StandardCost, SellingPrice, IsDCStocked, SupplyRoute,
       RevisionNumber, IsCurrent, EffectiveFrom, EffectiveTo, IsDeletedInSource AS IsDiscontinued
FROM gold.DimProduct;
GO

CREATE OR ALTER VIEW presentation.DimCustomer AS
SELECT CustomerKey, CustomerID, CustomerName, CustomerType, Town, CreditLimit, HasCreditFacility,
       RevisionNumber, IsCurrent, EffectiveFrom, EffectiveTo
FROM gold.DimCustomer;
GO

CREATE OR ALTER VIEW presentation.DimSupplier AS
SELECT SupplierKey, SupplierID, SupplierName, Location, MinimumOrderValueNAD, RebateRate, PaymentTermsDays,
       RevisionNumber, IsCurrent, EffectiveFrom, EffectiveTo
FROM gold.DimSupplier;
GO

CREATE OR ALTER VIEW presentation.DimAccount AS
SELECT AccountKey, AccountCode, AccountName, AccountType, FinancialStatement, NormalBalance, NormalSign,
       StatementSection
FROM gold.DimAccount;
GO

CREATE OR ALTER VIEW presentation.DimPaymentMethod AS
SELECT PaymentMethodKey, PaymentMethodCode, PaymentMethodName, IsCash, SettlementType FROM gold.DimPaymentMethod;
GO

CREATE OR ALTER VIEW presentation.DimAsset AS
SELECT AssetKey, AssetID, SiteID, AssetClass, AcquisitionDate, AcquisitionCost, UsefulLifeYears,
       MonthlyDepreciation, DepreciationMethod, AssetStatus, FullyDepreciatedDate
FROM gold.DimAsset WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.DimLocalAsset AS
SELECT LocalAssetKey, LocalAssetID, SiteID, DepartmentID, AssetDescription, AssetCondition, AssignedTo,
       IsAssigned, RecordedDate
FROM gold.DimLocalAsset WHERE IsDeletedInSource = 0;
GO

-- ---------------------------------------------------------
-- Facts (active rows only)
-- ---------------------------------------------------------
CREATE OR ALTER VIEW presentation.FactSales AS
SELECT SaleID, SaleDateKey, SiteKey, CustomerKey, PaymentMethodKey, NetAmount, VATAmount, TotalAmount
FROM gold.FactSales WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactSalesLine AS
SELECT SaleID, ProductID, SaleDateKey, SiteKey, CustomerKey, ProductKey, PaymentMethodKey, Quantity, UnitPrice,
       LineNetAmount, UnitCost, LineCostAmount, GrossMarginAmount
FROM gold.FactSalesLine WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactInventoryMovement AS
SELECT MovementID, MovementDateKey, SiteKey, ProductKey, MovementType, Direction, Quantity, UnitCost,
       MovementValue, SourceDocumentID
FROM gold.FactInventoryMovement WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactInventorySnapshot AS
SELECT SnapshotDateKey, SiteKey, ProductKey, QuantityIn, QuantityOut, ClosingQuantity, ClosingValue
FROM gold.FactInventorySnapshot;
GO

CREATE OR ALTER VIEW presentation.FactPurchaseOrder AS
SELECT POID, OrderDateKey, SupplierKey, DeliverySiteKey, POStatus, POValue, SupplierMinimumOrderValue,
       MeetsMinimumOrderValue
FROM gold.FactPurchaseOrder WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactPurchaseOrderLine AS
SELECT POID, ProductID, OrderDateKey, SupplierKey, ProductKey, DeliverySiteKey, Quantity, UnitCost, LineValue
FROM gold.FactPurchaseOrderLine WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactGoodsReceipt AS
SELECT GRNID, POID, ReceiptDateKey, OrderDateKey, SupplierKey, SiteKey, LeadTimeDays
FROM gold.FactGoodsReceipt WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactSupplierInvoice AS
SELECT InvoiceID, POID, InvoiceDateKey, DueDateKey, SupplierKey, InvoiceStatus, NetAmount, VATAmount, TotalAmount,
       PaymentTermsDays, OrderedValue, InvoiceToOrderVariance
FROM gold.FactSupplierInvoice WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactSupplierRebate AS
SELECT SupplierID, RebatePeriod, PeriodEndDateKey, SupplierKey, QualifyingSpend, RebateRate, RebateAmount,
       ExpectedRebateRate, ExpectedRebateAmount, RebateVariance
FROM gold.FactSupplierRebate WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactGeneralLedger AS
SELECT JournalID, JournalLineNumber, PostingDateKey, AccountKey, SiteKey, SourceDocumentID, TransactionType,
       DebitAmount, CreditAmount, NetAmount
FROM gold.FactGeneralLedger WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactOperatingExpense AS
SELECT ExpenseID, ExpenseDateKey, SiteKey, AccountKey, ExpenseType, ExpenseDescription, Amount
FROM gold.FactOperatingExpense WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.FactAssetDepreciation AS
SELECT AssetKey, PeriodEndDateKey, SiteKey, DepreciationAmount, AccumulatedDepreciation, NetBookValue
FROM gold.FactAssetDepreciation;
GO

CREATE OR ALTER VIEW presentation.FactBranchCashup AS
SELECT ShiftID, BusinessDateKey, SiteKey, PosTerminal, Cashier, OpeningFloat, CashSales, CardSales, AccountSales,
       TotalSales, InternalExpenses, CashRemoved, ExpectedClosingCash, ActualClosingCash, CashVariance,
       CashVarianceStatus, ManagerApproval
FROM gold.FactBranchCashup WHERE IsDeletedInSource = 0;
GO

CREATE OR ALTER VIEW presentation.BridgeProductSupplier AS
SELECT ProductID, SupplierID, ProductKey, SupplierKey, SupplierUnitCost, IsPreferredSupplier
FROM gold.BridgeProductSupplier WHERE IsDeletedInSource = 0;
GO
