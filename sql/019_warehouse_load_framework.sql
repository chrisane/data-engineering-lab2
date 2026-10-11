USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    019 - Shared warehouse load framework and dimension loads.

    Every load procedure:
      - takes @PipelineExecutionID only; the exact source file version is
        read from audit.PipelineSourceBatch (the gate decision recorded by
        the orchestrator for that run), never chosen by the procedure
      - serialises on an application lock per target
      - runs in one transaction; counts inserts, updates, soft deletes and
        restores in audit.WarehouseExecution; logs each changed row in
        audit.FactChangeLog
      - records FAILED with the error, then re-raises it
*/

-- ---------------------------------------------------------
-- Settings and posting rules (populated by metadata sync)
-- ---------------------------------------------------------
IF OBJECT_ID(N'meta.PostingRule', N'U') IS NULL
BEGIN
    -- config/posting_rules.csv: default debit/credit accounts per transaction type.
    CREATE TABLE meta.PostingRule (
        TransactionType NVARCHAR(50) NOT NULL CONSTRAINT PK_PostingRule PRIMARY KEY,
        DebitAccount NVARCHAR(20) NULL,
        CreditAccount NVARCHAR(20) NULL,
        Description NVARCHAR(500) NULL,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_PostingRule_UpdatedAt DEFAULT SYSUTCDATETIME()
    );
END;
GO

IF OBJECT_ID(N'meta.RebateThreshold', N'U') IS NULL
BEGIN
    -- config/business_rules.yaml rebates.quarterly_thresholds
    CREATE TABLE meta.RebateThreshold (
        ThresholdNAD DECIMAL(19, 2) NOT NULL CONSTRAINT PK_RebateThreshold PRIMARY KEY,
        RebateRate DECIMAL(9, 6) NOT NULL,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_RebateThreshold_UpdatedAt DEFAULT SYSUTCDATETIME()
    );
END;
GO

CREATE OR ALTER FUNCTION meta.fn_SettingDecimal (@SettingKey VARCHAR(200), @Default DECIMAL(19, 6))
RETURNS DECIMAL(19, 6)
AS
BEGIN
    RETURN COALESCE((SELECT TRY_CONVERT(DECIMAL(19, 6), SettingValue)
                     FROM meta.Setting WHERE SettingKey = @SettingKey), @Default);
END;
GO

-- ---------------------------------------------------------
-- Batch selection and execution audit helpers
-- ---------------------------------------------------------
CREATE OR ALTER FUNCTION audit.fn_PipelineBatch (@PipelineExecutionID BIGINT, @SourceName NVARCHAR(100))
RETURNS TABLE
AS
RETURN
    SELECT IngestionRunID, SourceFileHash, FileName
    FROM audit.PipelineSourceBatch
    WHERE PipelineExecutionID = @PipelineExecutionID
      AND SourceName = @SourceName
      AND Decision = 'ELIGIBLE';
GO

CREATE OR ALTER FUNCTION dw.fn_DateKey (@Date DATE)
RETURNS INT
WITH SCHEMABINDING
AS
BEGIN
    RETURN CASE WHEN @Date IS NULL THEN -1
                ELSE YEAR(@Date) * 10000 + MONTH(@Date) * 100 + DAY(@Date) END;
END;
GO

CREATE OR ALTER PROCEDURE audit.usp_RequireSingleBatch
    @PipelineExecutionID BIGINT,
    @SourceName NVARCHAR(100),
    @IngestionRunID VARCHAR(40) OUTPUT,
    @SourceFileHash CHAR(64) OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    IF (SELECT COUNT(*) FROM audit.fn_PipelineBatch(@PipelineExecutionID, @SourceName)) <> 1
    BEGIN
        DECLARE @Message NVARCHAR(400) = CONCAT(
            N'Expected exactly one ELIGIBLE batch of ', @SourceName,
            N' for pipeline execution ', @PipelineExecutionID,
            N'. Check audit.PipelineSourceBatch and the load eligibility decision.');
        THROW 52001, @Message, 1;
    END;

    SELECT @IngestionRunID = IngestionRunID, @SourceFileHash = SourceFileHash
    FROM audit.fn_PipelineBatch(@PipelineExecutionID, @SourceName);
END;
GO

CREATE OR ALTER PROCEDURE audit.usp_RecordWarehouseFailure
    @PipelineExecutionID BIGINT,
    @MappingCode VARCHAR(80),
    @TargetTable NVARCHAR(256),
    @SourceName NVARCHAR(100),
    @ErrorMessage NVARCHAR(4000)
AS
BEGIN
    SET NOCOUNT ON;
    INSERT audit.WarehouseExecution
        (PipelineExecutionID, MappingCode, TargetTable, SourceName, ExecutionStatus, CompletedAt, ErrorMessage)
    VALUES (@PipelineExecutionID, @MappingCode, @TargetTable, @SourceName, 'FAILED', SYSUTCDATETIME(), @ErrorMessage);
END;
GO

CREATE OR ALTER PROCEDURE audit.usp_AcquireLoadLock @Resource NVARCHAR(255)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @Result INT;
    EXEC @Result = sys.sp_getapplock @Resource = @Resource, @LockMode = 'Exclusive',
                                     @LockOwner = 'Transaction', @LockTimeout = 30000;
    IF @Result < 0
    BEGIN
        DECLARE @Message NVARCHAR(400) = CONCAT(N'Could not acquire load lock ', @Resource, N'.');
        THROW 52002, @Message, 1;
    END;
END;
GO

/*
    Summarise MERGE outcomes captured in #MergeActions (created by the
    caller) into audit.WarehouseExecution and audit.FactChangeLog.
    #MergeActions columns: ActionType, BusinessKey, OldRowHash, NewRowHash,
    OldDeleted, NewDeleted.
*/
CREATE OR ALTER PROCEDURE audit.usp_CompleteWarehouseExecution
    @PipelineExecutionID BIGINT,
    @MappingCode VARCHAR(80),
    @TargetTable NVARCHAR(256),
    @SourceName NVARCHAR(100),
    @IngestionRunID VARCHAR(40),
    @SourceFileHash CHAR(64),
    @SourceRows BIGINT,
    @UnknownMemberRows BIGINT = 0,
    @RebuiltRows BIGINT = NULL      -- FULL_REFRESH tables: rows rebuilt (no MERGE actions)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Inserted BIGINT, @Updated BIGINT, @SoftDeleted BIGINT, @ExecutionID BIGINT;

    IF @RebuiltRows IS NOT NULL
        SELECT @Inserted = @RebuiltRows, @Updated = 0, @SoftDeleted = 0;
    ELSE
        SELECT @Inserted = SUM(IIF(ActionType = 'INSERT', 1, 0)),
               @SoftDeleted = SUM(IIF(ActionType = 'UPDATE' AND NewDeleted = 1 AND ISNULL(OldDeleted, 0) = 0, 1, 0)),
               @Updated = SUM(IIF(ActionType = 'UPDATE' AND NOT (NewDeleted = 1 AND ISNULL(OldDeleted, 0) = 0), 1, 0))
        FROM #MergeActions;

    SET @Inserted = ISNULL(@Inserted, 0);
    SET @Updated = ISNULL(@Updated, 0);
    SET @SoftDeleted = ISNULL(@SoftDeleted, 0);

    INSERT audit.WarehouseExecution
        (PipelineExecutionID, MappingCode, TargetTable, SourceName, IngestionRunID, SourceFileHash,
         ExecutionStatus, SourceRows, InsertedRows, UpdatedRows, UnchangedRows, SoftDeletedRows,
         UnknownMemberRows, CompletedAt)
    VALUES (@PipelineExecutionID, @MappingCode, @TargetTable, @SourceName, @IngestionRunID, @SourceFileHash,
            IIF(@RebuiltRows IS NULL AND @Inserted + @Updated + @SoftDeleted = 0, 'SKIPPED', 'COMPLETED'),
            @SourceRows, @Inserted, @Updated,
            IIF(@SourceRows - @Inserted - @Updated < 0, 0, @SourceRows - @Inserted - @Updated),
            @SoftDeleted, @UnknownMemberRows, SYSUTCDATETIME());

    SET @ExecutionID = SCOPE_IDENTITY();

    INSERT audit.FactChangeLog (WarehouseExecutionID, TargetTable, BusinessKey, ChangeType, OldRowHash, NewRowHash)
    SELECT @ExecutionID, @TargetTable, BusinessKey,
           CASE WHEN NewDeleted = 1 AND ISNULL(OldDeleted, 0) = 0 THEN 'SOFT_DELETE'
                WHEN NewDeleted = 0 AND OldDeleted = 1 THEN 'RESTORE'
                ELSE 'UPDATE' END,
           OldRowHash, NewRowHash
    FROM #MergeActions
    WHERE ActionType = 'UPDATE' AND @RebuiltRows IS NULL;

    SELECT @ExecutionID AS WarehouseExecutionID, @SourceRows AS SourceRows, @Inserted AS InsertedRows,
           @Updated AS UpdatedRows, @SoftDeleted AS SoftDeletedRows, @UnknownMemberRows AS UnknownMemberRows;
END;
GO

-- =========================================================
-- DIMENSION LOADS
-- =========================================================

-- DimSite (SCD1) --------------------------------------------
CREATE OR ALTER PROCEDURE dw.usp_LoadDimSite @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'site_master', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimSite';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT site_id, site_name, town, site_type, SourceRowNumber,
               HASHBYTES('SHA2_256', CONCAT_WS(N'|', site_name, ISNULL(town, N'<NULL>'), site_type)) AS RowHash
        INTO #Source
        FROM stg.Site WHERE IngestionRunID = @Run AND SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        MERGE dw.DimSite AS t
        USING #Source AS s ON t.SiteID = s.site_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET SiteName = s.site_name, Town = s.town, SiteType = s.site_type,
                       IsBranch = IIF(s.site_type = N'BRANCH', 1, 0),
                       IsDistributionCentre = IIF(s.site_type = N'DISTRIBUTION_CENTRE', 1, 0),
                       IsHeadOffice = IIF(s.site_type = N'HEAD_OFFICE', 1, 0),
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (SiteID, SiteName, Town, SiteType, IsBranch, IsDistributionCentre, IsHeadOffice,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.site_id, s.site_name, s.town, s.site_type,
                    IIF(s.site_type = N'BRANCH', 1, 0), IIF(s.site_type = N'DISTRIBUTION_CENTRE', 1, 0),
                    IIF(s.site_type = N'HEAD_OFFICE', 1, 0),
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.SiteKey <> -1 AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.SiteID, deleted.SiteID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_SITE', N'dw.DimSite',
             N'site_master', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_SITE', N'dw.DimSite', N'site_master', @Error;
        THROW;
    END CATCH
END;
GO

-- DimDepartment (SCD1) --------------------------------------
CREATE OR ALTER PROCEDURE dw.usp_LoadDimDepartment @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'department_master', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimDepartment';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT department_id, department_name, SourceRowNumber,
               HASHBYTES('SHA2_256', department_name) AS RowHash
        INTO #Source
        FROM stg.Department WHERE IngestionRunID = @Run AND SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        MERGE dw.DimDepartment AS t
        USING #Source AS s ON t.DepartmentID = s.department_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET DepartmentName = s.department_name, IngestionRunID = @Run, SourceFileHash = @Hash,
                       SourceRowNumber = s.SourceRowNumber, RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (DepartmentID, DepartmentName, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash,
                    InsertedPipelineExecutionID)
            VALUES (s.department_id, s.department_name, @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.DepartmentKey <> -1 AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.DepartmentID, deleted.DepartmentID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_DEPARTMENT', N'dw.DimDepartment',
             N'department_master', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_DEPARTMENT', N'dw.DimDepartment', N'department_master', @Error;
        THROW;
    END CATCH
END;
GO

-- DimProduct (SCD2) -----------------------------------------
CREATE OR ALTER PROCEDURE dw.usp_LoadDimProduct @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @AsOf DATETIME2(7);

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'product_master', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimProduct';

        IF EXISTS (SELECT 1 FROM dw.DimProduct WHERE IsCurrent = 1 AND IngestionRunID > @Run)
            THROW 52101, 'A newer product batch is already in the warehouse; replaying an older batch needs a controlled process.', 1;

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT s.product_id, s.product_name, s.category, s.standard_cost, s.selling_price, s.dc_stocked,
               s.SourceRowNumber,
               HASHBYTES('SHA2_256', CONCAT_WS(N'|', s.product_name, s.category,
                         CONVERT(NVARCHAR(50), s.standard_cost), CONVERT(NVARCHAR(50), s.selling_price),
                         ISNULL(CONVERT(NVARCHAR(5), s.dc_stocked), N'<NULL>'))) AS RowHash,
               d.ProductKey AS CurrentKey, d.RevisionNumber AS CurrentRevision, d.RowHash AS CurrentHash,
               d.IsDeletedInSource AS CurrentDeleted
        INTO #Changes
        FROM stg.Product AS s
        LEFT JOIN dw.DimProduct AS d ON d.ProductID = s.product_id AND d.IsCurrent = 1
        WHERE s.IngestionRunID = @Run AND s.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SET @AsOf = SYSUTCDATETIME();

        -- Close versions whose tracked attributes changed.
        UPDATE d SET EffectiveTo = @AsOf, IsCurrent = 0
        FROM dw.DimProduct AS d
        JOIN #Changes AS c ON c.CurrentKey = d.ProductKey
        WHERE c.RowHash <> c.CurrentHash;

        INSERT dw.DimProduct (ProductID, ProductName, Category, StandardCost, SellingPrice, IsDCStocked, SupplyRoute,
                              EffectiveFrom, EffectiveTo, IsCurrent, RevisionNumber, ChangeType,
                              IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
        OUTPUT IIF(inserted.RevisionNumber = 1, 'INSERT', 'UPDATE'), inserted.ProductID, NULL, inserted.RowHash, NULL, 0
        INTO #MergeActions
        SELECT c.product_id, c.product_name, c.category, c.standard_cost, c.selling_price, c.dc_stocked,
               IIF(c.dc_stocked = 1, N'DC_REPLENISHED', N'DIRECT_FROM_SUPPLIER'),
               @AsOf, NULL, 1, ISNULL(c.CurrentRevision, 0) + 1,
               IIF(c.CurrentKey IS NULL, 'INITIAL', 'UPDATE'),
               @Run, @Hash, c.SourceRowNumber, c.RowHash, @PipelineExecutionID
        FROM #Changes AS c
        WHERE c.CurrentKey IS NULL OR c.RowHash <> c.CurrentHash;

        -- Unchanged but previously missing from the source: restore.
        UPDATE d SET IsDeletedInSource = 0
        OUTPUT 'UPDATE', inserted.ProductID, deleted.RowHash, inserted.RowHash, 1, 0 INTO #MergeActions
        FROM dw.DimProduct AS d
        JOIN #Changes AS c ON c.CurrentKey = d.ProductKey
        WHERE c.RowHash = c.CurrentHash AND d.IsDeletedInSource = 1;

        -- Current products absent from this full extract: flag, keep history.
        UPDATE d SET IsDeletedInSource = 1
        OUTPUT 'UPDATE', inserted.ProductID, deleted.RowHash, inserted.RowHash, 0, 1 INTO #MergeActions
        FROM dw.DimProduct AS d
        WHERE d.IsCurrent = 1 AND d.ProductKey <> -1 AND d.IsDeletedInSource = 0
          AND NOT EXISTS (SELECT 1 FROM #Changes AS c WHERE c.product_id = d.ProductID);

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_PRODUCT', N'dw.DimProduct',
             N'product_master', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_PRODUCT', N'dw.DimProduct', N'product_master', @Error;
        THROW;
    END CATCH
END;
GO

-- DimCustomer (SCD2) ----------------------------------------
CREATE OR ALTER PROCEDURE dw.usp_LoadDimCustomer @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @AsOf DATETIME2(7);

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'customer_master', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimCustomer';

        IF EXISTS (SELECT 1 FROM dw.DimCustomer WHERE IsCurrent = 1 AND IngestionRunID > @Run)
            THROW 52102, 'A newer customer batch is already in the warehouse; replaying an older batch needs a controlled process.', 1;

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT s.customer_id, s.customer_name, s.customer_type, s.town, s.credit_limit, s.SourceRowNumber,
               HASHBYTES('SHA2_256', CONCAT_WS(N'|', s.customer_name, s.customer_type, ISNULL(s.town, N'<NULL>'),
                         ISNULL(CONVERT(NVARCHAR(50), s.credit_limit), N'<NULL>'))) AS RowHash,
               d.CustomerKey AS CurrentKey, d.RevisionNumber AS CurrentRevision, d.RowHash AS CurrentHash
        INTO #Changes
        FROM stg.Customer AS s
        LEFT JOIN dw.DimCustomer AS d ON d.CustomerID = s.customer_id AND d.IsCurrent = 1
        WHERE s.IngestionRunID = @Run AND s.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SET @AsOf = SYSUTCDATETIME();

        UPDATE d SET EffectiveTo = @AsOf, IsCurrent = 0
        FROM dw.DimCustomer AS d
        JOIN #Changes AS c ON c.CurrentKey = d.CustomerKey
        WHERE c.RowHash <> c.CurrentHash;

        INSERT dw.DimCustomer (CustomerID, CustomerName, CustomerType, Town, CreditLimit, HasCreditFacility,
                               EffectiveFrom, EffectiveTo, IsCurrent, RevisionNumber, ChangeType,
                               IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
        OUTPUT IIF(inserted.RevisionNumber = 1, 'INSERT', 'UPDATE'), inserted.CustomerID, NULL, inserted.RowHash, NULL, 0
        INTO #MergeActions
        SELECT c.customer_id, c.customer_name, c.customer_type, c.town, c.credit_limit,
               IIF(ISNULL(c.credit_limit, 0) > 0, 1, 0),
               @AsOf, NULL, 1, ISNULL(c.CurrentRevision, 0) + 1, IIF(c.CurrentKey IS NULL, 'INITIAL', 'UPDATE'),
               @Run, @Hash, c.SourceRowNumber, c.RowHash, @PipelineExecutionID
        FROM #Changes AS c
        WHERE c.CurrentKey IS NULL OR c.RowHash <> c.CurrentHash;

        UPDATE d SET IsDeletedInSource = 0
        OUTPUT 'UPDATE', inserted.CustomerID, deleted.RowHash, inserted.RowHash, 1, 0 INTO #MergeActions
        FROM dw.DimCustomer AS d
        JOIN #Changes AS c ON c.CurrentKey = d.CustomerKey
        WHERE c.RowHash = c.CurrentHash AND d.IsDeletedInSource = 1;

        UPDATE d SET IsDeletedInSource = 1
        OUTPUT 'UPDATE', inserted.CustomerID, deleted.RowHash, inserted.RowHash, 0, 1 INTO #MergeActions
        FROM dw.DimCustomer AS d
        WHERE d.IsCurrent = 1 AND d.CustomerKey <> -1 AND d.IsDeletedInSource = 0
          AND NOT EXISTS (SELECT 1 FROM #Changes AS c WHERE c.customer_id = d.CustomerID);

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_CUSTOMER', N'dw.DimCustomer',
             N'customer_master', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_CUSTOMER', N'dw.DimCustomer', N'customer_master', @Error;
        THROW;
    END CATCH
END;
GO

-- DimAsset (SCD1) -------------------------------------------
CREATE OR ALTER PROCEDURE dw.usp_LoadDimAsset @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'fixed_assets_erp', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimAsset';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT asset_id, site_id, asset_class, acquisition_date, acquisition_cost, useful_life_years,
               monthly_depreciation, accumulated_depreciation, net_book_value, status, SourceRowNumber,
               -- Straight line from the month after acquisition (ERP register convention).
               EOMONTH(acquisition_date, useful_life_years * 12) AS FullyDepreciatedDate,
               HASHBYTES('SHA2_256', CONCAT_WS(N'|', site_id, asset_class, CONVERT(NVARCHAR(10), acquisition_date, 23),
                         CONVERT(NVARCHAR(50), acquisition_cost), CONVERT(NVARCHAR(10), useful_life_years),
                         ISNULL(CONVERT(NVARCHAR(50), monthly_depreciation), N'<NULL>'),
                         ISNULL(CONVERT(NVARCHAR(50), accumulated_depreciation), N'<NULL>'),
                         ISNULL(CONVERT(NVARCHAR(50), net_book_value), N'<NULL>'), status)) AS RowHash
        INTO #Source
        FROM stg.FixedAsset WHERE IngestionRunID = @Run AND SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        MERGE dw.DimAsset AS t
        USING #Source AS s ON t.AssetID = s.asset_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET SiteID = s.site_id, AssetClass = s.asset_class, AcquisitionDate = s.acquisition_date,
                       AcquisitionCost = s.acquisition_cost, UsefulLifeYears = s.useful_life_years,
                       MonthlyDepreciation = ISNULL(s.monthly_depreciation, 0),
                       SourceAccumulatedDepreciation = s.accumulated_depreciation,
                       SourceNetBookValue = s.net_book_value, AssetStatus = s.status,
                       FullyDepreciatedDate = s.FullyDepreciatedDate,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (AssetID, SiteID, AssetClass, AcquisitionDate, AcquisitionCost, UsefulLifeYears,
                    MonthlyDepreciation, SourceAccumulatedDepreciation, SourceNetBookValue, DepreciationMethod,
                    AssetStatus, FullyDepreciatedDate, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash,
                    InsertedPipelineExecutionID)
            VALUES (s.asset_id, s.site_id, s.asset_class, s.acquisition_date, s.acquisition_cost, s.useful_life_years,
                    ISNULL(s.monthly_depreciation, 0), s.accumulated_depreciation, s.net_book_value, N'STRAIGHT_LINE',
                    s.status, s.FullyDepreciatedDate, @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.AssetKey <> -1 AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.AssetID, deleted.AssetID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_ASSET', N'dw.DimAsset',
             N'fixed_assets_erp', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_ASSET', N'dw.DimAsset', N'fixed_assets_erp', @Error;
        THROW;
    END CATCH
END;
GO

-- DimLocalAsset (SCD1) --------------------------------------
CREATE OR ALTER PROCEDURE dw.usp_LoadDimLocalAsset @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'branch_local_assets', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimLocalAsset';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT local_asset_id, site_id, department_id, description, condition, assigned_to, recorded_date,
               SourceRowNumber,
               HASHBYTES('SHA2_256', CONCAT_WS(N'|', site_id, department_id, description,
                         ISNULL(condition, N'<NULL>'), ISNULL(assigned_to, N'<NULL>'),
                         ISNULL(CONVERT(NVARCHAR(10), recorded_date, 23), N'<NULL>'))) AS RowHash
        INTO #Source
        FROM stg.BranchLocalAsset WHERE IngestionRunID = @Run AND SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        MERGE dw.DimLocalAsset AS t
        USING #Source AS s ON t.LocalAssetID = s.local_asset_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET SiteID = s.site_id, DepartmentID = s.department_id, AssetDescription = s.description,
                       AssetCondition = s.condition, AssignedTo = s.assigned_to,
                       IsAssigned = IIF(NULLIF(LTRIM(RTRIM(s.assigned_to)), N'') IS NULL, 0, 1),
                       RecordedDate = s.recorded_date,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (LocalAssetID, SiteID, DepartmentID, AssetDescription, AssetCondition, AssignedTo, IsAssigned,
                    RecordedDate, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.local_asset_id, s.site_id, s.department_id, s.description, s.condition, s.assigned_to,
                    IIF(NULLIF(LTRIM(RTRIM(s.assigned_to)), N'') IS NULL, 0, 1), s.recorded_date,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.LocalAssetKey <> -1 AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.LocalAssetID, deleted.LocalAssetID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_LOCAL_ASSET', N'dw.DimLocalAsset',
             N'branch_local_assets', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_LOCAL_ASSET', N'dw.DimLocalAsset', N'branch_local_assets', @Error;
        THROW;
    END CATCH
END;
GO
