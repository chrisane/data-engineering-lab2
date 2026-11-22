USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    018 - Fact tables. GRAIN is stated for every table.

    Transaction facts (UPSERT by business key) carry:
      - lineage: IngestionRunID, SourceFileHash, SourceRowNumber
      - RowHash: unchanged reloads write nothing
      - IsDeletedInSource: set when a record disappears from a later full
        extract (corrections and reversals are visible, never silently lost)
      - Inserted/UpdatedPipelineExecutionID

    Snapshot / derived facts (FULL_REFRESH) are rebuilt from other facts
    and record the PipelineExecutionID that built them.

    Facts of different grains are never joined directly; analysis across
    grains goes through conformed dimensions (see docs/warehouse_design.md).
*/

-- GRAIN: one row per sales transaction (sale_id).
IF OBJECT_ID(N'gold.FactSales', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactSales (
        SalesKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactSales PRIMARY KEY,
        SaleID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactSales_SaleID UNIQUE,
        SaleDateKey INT NOT NULL CONSTRAINT FK_FactSales_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactSales_Site REFERENCES gold.DimSite(SiteKey),
        CustomerKey INT NOT NULL CONSTRAINT FK_FactSales_Customer REFERENCES gold.DimCustomer(CustomerKey),
        PaymentMethodKey INT NOT NULL CONSTRAINT FK_FactSales_Payment REFERENCES gold.DimPaymentMethod(PaymentMethodKey),
        NetAmount DECIMAL(19, 2) NOT NULL,
        VATAmount DECIMAL(19, 2) NOT NULL,
        TotalAmount DECIMAL(19, 2) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactSales_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactSales_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
    CREATE INDEX IX_FactSales_DateSite ON gold.FactSales(SaleDateKey, SiteKey) INCLUDE (PaymentMethodKey, TotalAmount);
END;
GO

-- GRAIN: one row per sales line (sale_id, product_id).
IF OBJECT_ID(N'gold.FactSalesLine', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactSalesLine (
        SalesLineKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactSalesLine PRIMARY KEY,
        SaleID NVARCHAR(50) NOT NULL,
        ProductID NVARCHAR(50) NOT NULL,
        SaleDateKey INT NOT NULL CONSTRAINT FK_FactSalesLine_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactSalesLine_Site REFERENCES gold.DimSite(SiteKey),
        CustomerKey INT NOT NULL CONSTRAINT FK_FactSalesLine_Customer REFERENCES gold.DimCustomer(CustomerKey),
        ProductKey INT NOT NULL CONSTRAINT FK_FactSalesLine_Product REFERENCES gold.DimProduct(ProductKey),
        PaymentMethodKey INT NOT NULL CONSTRAINT FK_FactSalesLine_Payment REFERENCES gold.DimPaymentMethod(PaymentMethodKey),
        Quantity INT NOT NULL,
        UnitPrice DECIMAL(19, 4) NOT NULL,
        LineNetAmount DECIMAL(19, 2) NOT NULL,
        UnitCost DECIMAL(19, 4) NOT NULL,
        LineCostAmount DECIMAL(19, 2) NOT NULL,
        GrossMarginAmount DECIMAL(19, 2) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactSalesLine_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactSalesLine_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT UQ_FactSalesLine_Key UNIQUE (SaleID, ProductID)
    );
    CREATE INDEX IX_FactSalesLine_DateProduct ON gold.FactSalesLine(SaleDateKey, ProductKey, SiteKey);
END;
GO

-- GRAIN: one row per inventory movement (movement_id).
IF OBJECT_ID(N'gold.FactInventoryMovement', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactInventoryMovement (
        InventoryMovementKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactInventoryMovement PRIMARY KEY,
        MovementID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactInventoryMovement_ID UNIQUE,
        MovementDateKey INT NOT NULL CONSTRAINT FK_FactInventoryMovement_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactInventoryMovement_Site REFERENCES gold.DimSite(SiteKey),
        ProductKey INT NOT NULL CONSTRAINT FK_FactInventoryMovement_Product REFERENCES gold.DimProduct(ProductKey),
        MovementType NVARCHAR(30) NOT NULL,
        Direction CHAR(3) NOT NULL,             -- IN / OUT
        Quantity INT NOT NULL,                  -- signed: + into the site, - out of it
        UnitCost DECIMAL(19, 4) NOT NULL,
        MovementValue DECIMAL(19, 2) NOT NULL,  -- signed, Quantity x UnitCost
        SourceDocumentID NVARCHAR(50) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactInventoryMovement_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactInventoryMovement_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT CK_FactInventoryMovement_Direction CHECK (Direction IN ('IN', 'OUT'))
    );
    CREATE INDEX IX_FactInventoryMovement_SiteProduct ON gold.FactInventoryMovement(SiteKey, ProductKey, MovementDateKey) INCLUDE (Quantity, MovementValue);
END;
GO

-- GRAIN: one row per site, product and month end (closing stock position).
IF OBJECT_ID(N'gold.FactInventorySnapshot', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactInventorySnapshot (
        SnapshotDateKey INT NOT NULL CONSTRAINT FK_FactInventorySnapshot_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactInventorySnapshot_Site REFERENCES gold.DimSite(SiteKey),
        ProductKey INT NOT NULL CONSTRAINT FK_FactInventorySnapshot_Product REFERENCES gold.DimProduct(ProductKey),
        QuantityIn INT NOT NULL,
        QuantityOut INT NOT NULL,
        ClosingQuantity INT NOT NULL,
        ClosingValue DECIMAL(19, 2) NOT NULL,   -- ClosingQuantity x current standard cost
        PipelineExecutionID BIGINT NOT NULL,
        BuiltAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactInventorySnapshot_BuiltAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_FactInventorySnapshot PRIMARY KEY (SnapshotDateKey, SiteKey, ProductKey)
    );
END;
GO

-- GRAIN: one row per purchase order (po_id).
IF OBJECT_ID(N'gold.FactPurchaseOrder', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactPurchaseOrder (
        PurchaseOrderKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactPurchaseOrder PRIMARY KEY,
        POID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactPurchaseOrder_ID UNIQUE,
        OrderDateKey INT NOT NULL CONSTRAINT FK_FactPurchaseOrder_Date REFERENCES gold.DimDate(DateKey),
        SupplierKey BIGINT NOT NULL CONSTRAINT FK_FactPurchaseOrder_Supplier REFERENCES gold.DimSupplier(SupplierKey),
        DeliverySiteKey INT NOT NULL CONSTRAINT FK_FactPurchaseOrder_Site REFERENCES gold.DimSite(SiteKey),
        POStatus NVARCHAR(30) NOT NULL,
        POValue DECIMAL(19, 2) NOT NULL,
        SupplierMinimumOrderValue DECIMAL(19, 2) NOT NULL,
        MeetsMinimumOrderValue BIT NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactPurchaseOrder_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactPurchaseOrder_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- GRAIN: one row per purchase order line (po_id, product_id).
IF OBJECT_ID(N'gold.FactPurchaseOrderLine', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactPurchaseOrderLine (
        PurchaseOrderLineKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactPurchaseOrderLine PRIMARY KEY,
        POID NVARCHAR(50) NOT NULL,
        ProductID NVARCHAR(50) NOT NULL,
        OrderDateKey INT NOT NULL CONSTRAINT FK_FactPurchaseOrderLine_Date REFERENCES gold.DimDate(DateKey),
        SupplierKey BIGINT NOT NULL CONSTRAINT FK_FactPurchaseOrderLine_Supplier REFERENCES gold.DimSupplier(SupplierKey),
        ProductKey INT NOT NULL CONSTRAINT FK_FactPurchaseOrderLine_Product REFERENCES gold.DimProduct(ProductKey),
        DeliverySiteKey INT NOT NULL CONSTRAINT FK_FactPurchaseOrderLine_Site REFERENCES gold.DimSite(SiteKey),
        Quantity INT NOT NULL,
        UnitCost DECIMAL(19, 4) NOT NULL,
        LineValue DECIMAL(19, 2) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactPurchaseOrderLine_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactPurchaseOrderLine_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT UQ_FactPurchaseOrderLine_Key UNIQUE (POID, ProductID)
    );
END;
GO

-- GRAIN: one row per goods received note (grn_id).
IF OBJECT_ID(N'gold.FactGoodsReceipt', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactGoodsReceipt (
        GoodsReceiptKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactGoodsReceipt PRIMARY KEY,
        GRNID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactGoodsReceipt_ID UNIQUE,
        POID NVARCHAR(50) NOT NULL,
        ReceiptDateKey INT NOT NULL CONSTRAINT FK_FactGoodsReceipt_Date REFERENCES gold.DimDate(DateKey),
        OrderDateKey INT NOT NULL CONSTRAINT FK_FactGoodsReceipt_OrderDate REFERENCES gold.DimDate(DateKey),
        SupplierKey BIGINT NOT NULL CONSTRAINT FK_FactGoodsReceipt_Supplier REFERENCES gold.DimSupplier(SupplierKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactGoodsReceipt_Site REFERENCES gold.DimSite(SiteKey),
        LeadTimeDays INT NULL,                  -- receipt date - order date
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactGoodsReceipt_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactGoodsReceipt_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- GRAIN: one row per supplier invoice (invoice_id).
IF OBJECT_ID(N'gold.FactSupplierInvoice', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactSupplierInvoice (
        SupplierInvoiceKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactSupplierInvoice PRIMARY KEY,
        InvoiceID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactSupplierInvoice_ID UNIQUE,
        POID NVARCHAR(50) NOT NULL,
        InvoiceDateKey INT NOT NULL CONSTRAINT FK_FactSupplierInvoice_Date REFERENCES gold.DimDate(DateKey),
        DueDateKey INT NOT NULL CONSTRAINT FK_FactSupplierInvoice_DueDate REFERENCES gold.DimDate(DateKey),
        SupplierKey BIGINT NOT NULL CONSTRAINT FK_FactSupplierInvoice_Supplier REFERENCES gold.DimSupplier(SupplierKey),
        InvoiceStatus NVARCHAR(30) NOT NULL,
        NetAmount DECIMAL(19, 2) NOT NULL,
        VATAmount DECIMAL(19, 2) NOT NULL,
        TotalAmount DECIMAL(19, 2) NOT NULL,
        PaymentTermsDays INT NULL,
        OrderedValue DECIMAL(19, 2) NULL,       -- PO value, for invoice-to-order matching
        InvoiceToOrderVariance DECIMAL(19, 2) NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactSupplierInvoice_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactSupplierInvoice_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- GRAIN: one row per supplier per rebate period (supplier_id, period).
IF OBJECT_ID(N'gold.FactSupplierRebate', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactSupplierRebate (
        SupplierRebateKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactSupplierRebate PRIMARY KEY,
        SupplierID NVARCHAR(50) NOT NULL,
        RebatePeriod CHAR(7) NOT NULL,
        PeriodEndDateKey INT NOT NULL CONSTRAINT FK_FactSupplierRebate_Date REFERENCES gold.DimDate(DateKey),
        SupplierKey BIGINT NOT NULL CONSTRAINT FK_FactSupplierRebate_Supplier REFERENCES gold.DimSupplier(SupplierKey),
        QualifyingSpend DECIMAL(19, 2) NOT NULL,
        RebateRate DECIMAL(9, 6) NOT NULL,
        RebateAmount DECIMAL(19, 2) NOT NULL,
        ExpectedRebateRate DECIMAL(9, 6) NOT NULL,   -- from rebate thresholds (business rules)
        ExpectedRebateAmount DECIMAL(19, 2) NOT NULL,
        RebateVariance DECIMAL(19, 2) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactSupplierRebate_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactSupplierRebate_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT UQ_FactSupplierRebate_Key UNIQUE (SupplierID, RebatePeriod)
    );
END;
GO

-- GRAIN: one row per journal line (journal_id, line number within the journal).
IF OBJECT_ID(N'gold.FactGeneralLedger', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactGeneralLedger (
        GeneralLedgerKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactGeneralLedger PRIMARY KEY,
        JournalID NVARCHAR(50) NOT NULL,
        JournalLineNumber INT NOT NULL,
        PostingDateKey INT NOT NULL CONSTRAINT FK_FactGeneralLedger_Date REFERENCES gold.DimDate(DateKey),
        AccountKey INT NOT NULL CONSTRAINT FK_FactGeneralLedger_Account REFERENCES gold.DimAccount(AccountKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactGeneralLedger_Site REFERENCES gold.DimSite(SiteKey),
        SourceDocumentID NVARCHAR(50) NOT NULL,
        TransactionType NVARCHAR(50) NOT NULL,
        DebitAmount DECIMAL(19, 2) NOT NULL,
        CreditAmount DECIMAL(19, 2) NOT NULL,
        NetAmount DECIMAL(19, 2) NOT NULL,      -- debit - credit
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactGeneralLedger_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactGeneralLedger_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT UQ_FactGeneralLedger_Line UNIQUE (JournalID, JournalLineNumber),
        CONSTRAINT CK_FactGeneralLedger_Sides CHECK (DebitAmount >= 0 AND CreditAmount >= 0)
    );
    CREATE INDEX IX_FactGeneralLedger_AccountDate ON gold.FactGeneralLedger(AccountKey, PostingDateKey) INCLUDE (DebitAmount, CreditAmount, SiteKey);
END;
GO

-- GRAIN: one row per operating expense (expense_id).
IF OBJECT_ID(N'gold.FactOperatingExpense', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactOperatingExpense (
        OperatingExpenseKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactOperatingExpense PRIMARY KEY,
        ExpenseID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactOperatingExpense_ID UNIQUE,
        ExpenseDateKey INT NOT NULL CONSTRAINT FK_FactOperatingExpense_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactOperatingExpense_Site REFERENCES gold.DimSite(SiteKey),
        AccountKey INT NOT NULL CONSTRAINT FK_FactOperatingExpense_Account REFERENCES gold.DimAccount(AccountKey),
        ExpenseType NVARCHAR(30) NOT NULL,
        ExpenseDescription NVARCHAR(255) NULL,
        Amount DECIMAL(19, 2) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactOperatingExpense_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactOperatingExpense_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- GRAIN: one row per ERP asset per month in service during the reporting
-- year (straight-line depreciation schedule derived from DimAsset).
IF OBJECT_ID(N'gold.FactAssetDepreciation', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactAssetDepreciation (
        AssetKey INT NOT NULL CONSTRAINT FK_FactAssetDepreciation_Asset REFERENCES gold.DimAsset(AssetKey),
        PeriodEndDateKey INT NOT NULL CONSTRAINT FK_FactAssetDepreciation_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactAssetDepreciation_Site REFERENCES gold.DimSite(SiteKey),
        DepreciationAmount DECIMAL(19, 2) NOT NULL,
        AccumulatedDepreciation DECIMAL(19, 2) NOT NULL,
        NetBookValue DECIMAL(19, 2) NOT NULL,
        PipelineExecutionID BIGINT NOT NULL,
        BuiltAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactAssetDepreciation_BuiltAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_FactAssetDepreciation PRIMARY KEY (AssetKey, PeriodEndDateKey)
    );
END;
GO

-- GRAIN: one row per branch cash-up document (shift_id).
IF OBJECT_ID(N'gold.FactBranchCashup', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FactBranchCashup (
        BranchCashupKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactBranchCashup PRIMARY KEY,
        ShiftID NVARCHAR(50) NOT NULL CONSTRAINT UQ_FactBranchCashup_Shift UNIQUE,
        BusinessDateKey INT NOT NULL CONSTRAINT FK_FactBranchCashup_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FactBranchCashup_Site REFERENCES gold.DimSite(SiteKey),
        PosTerminal NVARCHAR(255) NULL,
        Cashier NVARCHAR(255) NULL,             -- CONFIDENTIAL: employee name
        OpeningFloat DECIMAL(19, 2) NOT NULL,
        CashSales DECIMAL(19, 2) NOT NULL,
        CardSales DECIMAL(19, 2) NOT NULL,
        AccountSales DECIMAL(19, 2) NOT NULL,
        TotalSales DECIMAL(19, 2) NOT NULL,
        InternalExpenses DECIMAL(19, 2) NOT NULL,
        CashRemoved DECIMAL(19, 2) NOT NULL,
        ExpectedClosingCash DECIMAL(19, 2) NOT NULL,
        ActualClosingCash DECIMAL(19, 2) NOT NULL,
        CashVariance DECIMAL(19, 2) NOT NULL,
        CashVarianceStatus NVARCHAR(20) NOT NULL,  -- BALANCED / SHORTAGE / OVERAGE (tolerance in meta.Setting)
        ManagerApproval NVARCHAR(50) NULL,
        SourceFile NVARCHAR(500) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_FactBranchCashup_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactBranchCashup_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO
