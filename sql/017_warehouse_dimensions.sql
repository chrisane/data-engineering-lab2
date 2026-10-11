USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    017 - Conformed dimensions.

    SCD strategy
      DimDate          generated calendar, 2020-01-01 to 2030-12-31
      DimSite          SCD1  (corrections overwrite; site attributes rarely change)
      DimDepartment    SCD1
      DimAccount       SCD1  (chart of accounts reference data from config/)
      DimPaymentMethod static reference data (seeded below)
      DimProduct       SCD2  (cost and price history matters for margin analysis)
      DimCustomer      SCD2  (customer type and credit limit history)
      DimSupplier      SCD2  (existing, 008-010)
      DimAsset         SCD1  (as-at attributes; depreciation history is a fact)
      DimLocalAsset    SCD1

    Every dimension has an Unknown member with key -1, so a fact row can
    always be loaded; unknown-member counts are reconciled and reported.

    SCD2 effective dates are system (load) time, as for DimSupplier:
    they record when the warehouse learned of a change, not when the
    business changed it (the sources carry no effective dates).
*/

-- ---------------------------------------------------------
-- DimDate
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimDate', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimDate (
        DateKey INT NOT NULL CONSTRAINT PK_DimDate PRIMARY KEY,
        [Date] DATE NOT NULL CONSTRAINT UQ_DimDate_Date UNIQUE,
        [Year] SMALLINT NOT NULL,
        QuarterNumber TINYINT NOT NULL,
        QuarterName CHAR(2) NOT NULL,
        MonthNumber TINYINT NOT NULL,
        MonthName NVARCHAR(10) NOT NULL,
        MonthShortName CHAR(3) NOT NULL,
        YearMonth CHAR(7) NOT NULL,
        DayOfMonth TINYINT NOT NULL,
        DayOfWeekNumber TINYINT NOT NULL,     -- 1 = Monday
        DayName NVARCHAR(10) NOT NULL,
        IsoWeek TINYINT NOT NULL,
        IsWeekend BIT NOT NULL,
        FiscalYear SMALLINT NOT NULL,
        FiscalPeriod TINYINT NOT NULL,
        MonthStartDate DATE NOT NULL,
        MonthEndDate DATE NOT NULL,
        IsMonthEnd BIT NOT NULL
    );
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadDimDate
    @StartDate DATE = '2020-01-01',
    @EndDate DATE = '2030-12-31'
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    SET DATEFIRST 1;

    DECLARE @FiscalStartMonth INT = COALESCE(
        (SELECT TRY_CONVERT(INT, SettingValue) FROM meta.Setting
         WHERE SettingKey = 'company.financial_year_start_month'), 1);

    IF NOT EXISTS (SELECT 1 FROM dw.DimDate WHERE DateKey = -1)
        INSERT dw.DimDate VALUES (-1, '1900-01-01', 1900, 1, 'Q1', 1, N'Unknown', 'UNK', '1900-01',
                                  1, 1, N'Unknown', 1, 0, 1900, 1, '1900-01-01', '1900-01-31', 0);

    ;WITH Numbers AS (
        SELECT TOP (DATEDIFF(DAY, @StartDate, @EndDate) + 1)
               ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS n
        FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b
    ), Dates AS (
        SELECT DATEADD(DAY, n, @StartDate) AS d FROM Numbers
    )
    INSERT dw.DimDate
    SELECT YEAR(d) * 10000 + MONTH(d) * 100 + DAY(d),
           d,
           YEAR(d),
           DATEPART(QUARTER, d),
           CONCAT('Q', DATEPART(QUARTER, d)),
           MONTH(d),
           DATENAME(MONTH, d),
           LEFT(DATENAME(MONTH, d), 3),
           CONVERT(CHAR(7), d, 126),
           DAY(d),
           DATEPART(WEEKDAY, d),
           DATENAME(WEEKDAY, d),
           DATEPART(ISO_WEEK, d),
           IIF(DATEPART(WEEKDAY, d) IN (6, 7), 1, 0),
           YEAR(d) + IIF(@FiscalStartMonth > 1 AND MONTH(d) >= @FiscalStartMonth, 1, 0),
           ((MONTH(d) - @FiscalStartMonth + 12) % 12) + 1,
           DATEFROMPARTS(YEAR(d), MONTH(d), 1),
           EOMONTH(d),
           IIF(d = EOMONTH(d), 1, 0)
    FROM Dates
    WHERE NOT EXISTS (SELECT 1 FROM dw.DimDate AS x WHERE x.[Date] = Dates.d);
END;
GO

-- ---------------------------------------------------------
-- DimSite (SCD1)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimSite', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimSite (
        SiteKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimSite PRIMARY KEY,
        SiteID NVARCHAR(50) NOT NULL CONSTRAINT UQ_DimSite_SiteID UNIQUE,
        SiteName NVARCHAR(255) NOT NULL,
        Town NVARCHAR(255) NULL,
        SiteType NVARCHAR(50) NOT NULL,
        IsBranch BIT NOT NULL,
        IsDistributionCentre BIT NOT NULL,
        IsHeadOffice BIT NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimSite_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimSite_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- ---------------------------------------------------------
-- DimDepartment (SCD1)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimDepartment', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimDepartment (
        DepartmentKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimDepartment PRIMARY KEY,
        DepartmentID NVARCHAR(50) NOT NULL CONSTRAINT UQ_DimDepartment_ID UNIQUE,
        DepartmentName NVARCHAR(255) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimDepartment_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimDepartment_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- ---------------------------------------------------------
-- DimAccount (SCD1, from config/chart_of_accounts.csv)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimAccount', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimAccount (
        AccountKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimAccount PRIMARY KEY,
        AccountCode NVARCHAR(20) NOT NULL CONSTRAINT UQ_DimAccount_Code UNIQUE,
        AccountName NVARCHAR(255) NOT NULL,
        AccountType NVARCHAR(50) NOT NULL,
        FinancialStatement NVARCHAR(50) NOT NULL,
        NormalBalance NVARCHAR(10) NOT NULL,
        NormalSign SMALLINT NOT NULL,            -- +1 debit-normal, -1 credit-normal
        StatementSection NVARCHAR(30) NOT NULL,  -- Assets, Liabilities, Equity, Revenue, Expenses
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimAccount_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimAccount_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT CK_DimAccount_Sign CHECK (NormalSign IN (-1, 1))
    );
END;
GO

-- ---------------------------------------------------------
-- DimPaymentMethod (static reference data)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimPaymentMethod', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimPaymentMethod (
        PaymentMethodKey INT NOT NULL CONSTRAINT PK_DimPaymentMethod PRIMARY KEY,
        PaymentMethodCode NVARCHAR(20) NOT NULL CONSTRAINT UQ_DimPaymentMethod_Code UNIQUE,
        PaymentMethodName NVARCHAR(50) NOT NULL,
        IsCash BIT NOT NULL,
        SettlementType NVARCHAR(20) NOT NULL,    -- IMMEDIATE or ON_ACCOUNT
        CashupLabel NVARCHAR(50) NOT NULL        -- label on branch cash-up documents
    );
END;
GO

MERGE dw.DimPaymentMethod AS t
USING (VALUES
    (-1, N'UNKNOWN', N'Unknown', 0, N'UNKNOWN', N'Unknown'),
    (1, N'CASH', N'Cash', 1, N'IMMEDIATE', N'Cash Sales'),
    (2, N'CARD', N'Card', 0, N'IMMEDIATE', N'Card Sales'),
    (3, N'CREDIT', N'Account (credit)', 0, N'ON_ACCOUNT', N'Account Sales')
) AS s (PaymentMethodKey, PaymentMethodCode, PaymentMethodName, IsCash, SettlementType, CashupLabel)
ON t.PaymentMethodKey = s.PaymentMethodKey
WHEN NOT MATCHED THEN INSERT VALUES (s.PaymentMethodKey, s.PaymentMethodCode, s.PaymentMethodName,
                                     s.IsCash, s.SettlementType, s.CashupLabel);
GO

-- ---------------------------------------------------------
-- DimProduct (SCD2)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimProduct', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimProduct (
        ProductKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimProduct PRIMARY KEY,
        ProductID NVARCHAR(50) NOT NULL,
        ProductName NVARCHAR(255) NOT NULL,
        Category NVARCHAR(255) NOT NULL,
        StandardCost DECIMAL(19, 6) NOT NULL,
        SellingPrice DECIMAL(19, 6) NOT NULL,
        IsDCStocked BIT NULL,
        SupplyRoute NVARCHAR(30) NOT NULL,       -- DC_REPLENISHED or DIRECT_FROM_SUPPLIER
        EffectiveFrom DATETIME2(7) NOT NULL,
        EffectiveTo DATETIME2(7) NULL,
        IsCurrent BIT NOT NULL,
        RevisionNumber INT NOT NULL,
        ChangeType VARCHAR(20) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimProduct_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        RecordedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimProduct_RecordedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_DimProduct_Revision UNIQUE (ProductID, RevisionNumber),
        CONSTRAINT CK_DimProduct_ChangeType CHECK (ChangeType IN ('INITIAL', 'UPDATE', 'SYSTEM')),
        CONSTRAINT CK_DimProduct_Dates CHECK (EffectiveTo IS NULL OR EffectiveTo > EffectiveFrom),
        CONSTRAINT CK_DimProduct_Current CHECK ((IsCurrent = 1 AND EffectiveTo IS NULL) OR (IsCurrent = 0 AND EffectiveTo IS NOT NULL))
    );
    CREATE UNIQUE INDEX UX_DimProduct_Current ON dw.DimProduct(ProductID) WHERE IsCurrent = 1;
END;
GO

-- ---------------------------------------------------------
-- DimCustomer (SCD2)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimCustomer', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimCustomer (
        CustomerKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimCustomer PRIMARY KEY,
        CustomerID NVARCHAR(50) NOT NULL,
        CustomerName NVARCHAR(255) NOT NULL,     -- RESTRICTED: personal data for retail customers
        CustomerType NVARCHAR(20) NOT NULL,
        Town NVARCHAR(255) NULL,                 -- RESTRICTED for retail customers
        CreditLimit DECIMAL(19, 2) NULL,
        HasCreditFacility BIT NOT NULL,
        EffectiveFrom DATETIME2(7) NOT NULL,
        EffectiveTo DATETIME2(7) NULL,
        IsCurrent BIT NOT NULL,
        RevisionNumber INT NOT NULL,
        ChangeType VARCHAR(20) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimCustomer_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        RecordedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimCustomer_RecordedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_DimCustomer_Revision UNIQUE (CustomerID, RevisionNumber),
        CONSTRAINT CK_DimCustomer_ChangeType CHECK (ChangeType IN ('INITIAL', 'UPDATE', 'SYSTEM')),
        CONSTRAINT CK_DimCustomer_Dates CHECK (EffectiveTo IS NULL OR EffectiveTo > EffectiveFrom),
        CONSTRAINT CK_DimCustomer_Current CHECK ((IsCurrent = 1 AND EffectiveTo IS NULL) OR (IsCurrent = 0 AND EffectiveTo IS NOT NULL))
    );
    CREATE UNIQUE INDEX UX_DimCustomer_Current ON dw.DimCustomer(CustomerID) WHERE IsCurrent = 1;
END;
GO

-- ---------------------------------------------------------
-- DimAsset (SCD1, ERP fixed assets)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimAsset', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimAsset (
        AssetKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimAsset PRIMARY KEY,
        AssetID NVARCHAR(50) NOT NULL CONSTRAINT UQ_DimAsset_ID UNIQUE,
        SiteID NVARCHAR(50) NOT NULL,
        AssetClass NVARCHAR(255) NOT NULL,
        AcquisitionDate DATE NOT NULL,
        AcquisitionCost DECIMAL(19, 2) NOT NULL,
        UsefulLifeYears INT NOT NULL,
        MonthlyDepreciation DECIMAL(19, 2) NOT NULL,
        SourceAccumulatedDepreciation DECIMAL(19, 2) NULL,
        SourceNetBookValue DECIMAL(19, 2) NULL,
        DepreciationMethod NVARCHAR(30) NOT NULL,
        AssetStatus NVARCHAR(30) NOT NULL,
        FullyDepreciatedDate DATE NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimAsset_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimAsset_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- ---------------------------------------------------------
-- DimLocalAsset (SCD1, branch / departmental registers)
-- ---------------------------------------------------------
IF OBJECT_ID(N'dw.DimLocalAsset', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimLocalAsset (
        LocalAssetKey INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DimLocalAsset PRIMARY KEY,
        LocalAssetID NVARCHAR(50) NOT NULL CONSTRAINT UQ_DimLocalAsset_ID UNIQUE,
        SiteID NVARCHAR(50) NOT NULL,
        DepartmentID NVARCHAR(50) NOT NULL,
        AssetDescription NVARCHAR(255) NOT NULL,
        AssetCondition NVARCHAR(50) NULL,
        AssignedTo NVARCHAR(255) NULL,           -- CONFIDENTIAL: employee name
        IsAssigned BIT NOT NULL,
        RecordedDate DATE NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_DimLocalAsset_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DimLocalAsset_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL
    );
END;
GO

-- ---------------------------------------------------------
-- Unknown members (key -1)
-- ---------------------------------------------------------
DECLARE @SystemRun VARCHAR(40) = 'ING-00000000_000000';     -- sorts before every real batch
DECLARE @NoHash CHAR(64) = REPLICATE('0', 64);
DECLARE @Hash BINARY(32) = HASHBYTES('SHA2_256', 'UNKNOWN');

IF NOT EXISTS (SELECT 1 FROM dw.DimSite WHERE SiteKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimSite ON;
    INSERT dw.DimSite (SiteKey, SiteID, SiteName, Town, SiteType, IsBranch, IsDistributionCentre, IsHeadOffice,
                       IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'Unknown site', NULL, N'UNKNOWN', 0, 0, 0, @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimSite OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimDepartment WHERE DepartmentKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimDepartment ON;
    INSERT dw.DimDepartment (DepartmentKey, DepartmentID, DepartmentName, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'Unknown department', @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimDepartment OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimAccount WHERE AccountKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimAccount ON;
    INSERT dw.DimAccount (AccountKey, AccountCode, AccountName, AccountType, FinancialStatement, NormalBalance,
                          NormalSign, StatementSection, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'Unknown account', N'Unknown', N'Unknown', N'Debit', 1, N'Unknown',
            @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimAccount OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimProduct WHERE ProductKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimProduct ON;
    INSERT dw.DimProduct (ProductKey, ProductID, ProductName, Category, StandardCost, SellingPrice, IsDCStocked,
                          SupplyRoute, EffectiveFrom, EffectiveTo, IsCurrent, RevisionNumber, ChangeType,
                          IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'Unknown product', N'Unknown', 0, 0, NULL, N'UNKNOWN', '1900-01-01', NULL, 1, 1,
            'SYSTEM', @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimProduct OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimCustomer WHERE CustomerKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimCustomer ON;
    INSERT dw.DimCustomer (CustomerKey, CustomerID, CustomerName, CustomerType, Town, CreditLimit, HasCreditFacility,
                           EffectiveFrom, EffectiveTo, IsCurrent, RevisionNumber, ChangeType,
                           IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'Unknown customer', N'UNKNOWN', NULL, NULL, 0, '1900-01-01', NULL, 1, 1, 'SYSTEM',
            @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimCustomer OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimAsset WHERE AssetKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimAsset ON;
    INSERT dw.DimAsset (AssetKey, AssetID, SiteID, AssetClass, AcquisitionDate, AcquisitionCost, UsefulLifeYears,
                        MonthlyDepreciation, DepreciationMethod, AssetStatus, FullyDepreciatedDate,
                        IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'UNKNOWN', N'Unknown', '1900-01-01', 0, 1, 0, N'UNKNOWN', N'UNKNOWN', '1900-01-01',
            @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimAsset OFF;
END;

IF NOT EXISTS (SELECT 1 FROM dw.DimLocalAsset WHERE LocalAssetKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimLocalAsset ON;
    INSERT dw.DimLocalAsset (LocalAssetKey, LocalAssetID, SiteID, DepartmentID, AssetDescription, AssetCondition,
                             AssignedTo, IsAssigned, RecordedDate, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash)
    VALUES (-1, N'UNKNOWN', N'UNKNOWN', N'UNKNOWN', N'Unknown asset', NULL, NULL, 0, NULL,
            @SystemRun, @NoHash, 0, @Hash);
    SET IDENTITY_INSERT dw.DimLocalAsset OFF;
END;

-- Supplier (existing SCD2 table, 008): add the Unknown member so facts can
-- always resolve a supplier key. The system batch ID sorts before every
-- real batch, so the supplier load's replay protection is unaffected.
IF NOT EXISTS (SELECT 1 FROM dw.DimSupplier WHERE SupplierKey = -1)
BEGIN
    SET IDENTITY_INSERT dw.DimSupplier ON;
    INSERT dw.DimSupplier (SupplierKey, SupplierID, SupplierName, Location, MinimumOrderValueNAD, RebateRate,
                           PaymentTermsDays, EffectiveFrom, EffectiveTo, IsCurrent, RevisionNumber, ChangeType,
                           ChangeReason, IngestionRunID, SourceFile, SourceFileHash, SourceRowNumber)
    VALUES (-1, 'UNKNOWN', N'Unknown supplier', NULL, 0, NULL, NULL, '1900-01-01', NULL, 1, 1, 'INITIAL',
            N'System unknown member', @SystemRun, N'SYSTEM', @NoHash, 0);
    SET IDENTITY_INSERT dw.DimSupplier OFF;
END;
GO

EXEC dw.usp_LoadDimDate;
GO
