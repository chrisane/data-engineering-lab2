USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    020 - Reference staging, account dimension and fact load procedures.

    Dimension lookups use the business key; SCD2 dimensions resolve to the
    version current at load time. A key that cannot be resolved maps to
    the Unknown member (-1) and is counted in UnknownMemberRows, which the
    reconciliation controls check (late-arriving / missing master data).
*/

-- ---------------------------------------------------------
-- Chart of accounts: reference staging + DimAccount
-- ---------------------------------------------------------
IF OBJECT_ID(N'stg.ChartOfAccounts', N'U') IS NULL
BEGIN
    CREATE TABLE stg.ChartOfAccounts (
        StagingID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_stg_ChartOfAccounts PRIMARY KEY,
        account_code NVARCHAR(20) NOT NULL,
        account_name NVARCHAR(255) NOT NULL,
        account_type NVARCHAR(50) NOT NULL,
        financial_statement NVARCHAR(50) NOT NULL,
        normal_balance NVARCHAR(10) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFile NVARCHAR(500) NOT NULL,
        SourceRowNumber INT NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        LoadedAt DATETIME2(7) NOT NULL CONSTRAINT DF_stg_ChartOfAccounts_LoadedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_stg_ChartOfAccounts_SourceRow UNIQUE (IngestionRunID, SourceFileHash, SourceRowNumber)
    );
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadDimAccount @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'chart_of_accounts', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.DimAccount';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT account_code, account_name, account_type, financial_statement, normal_balance, SourceRowNumber,
               IIF(normal_balance = N'Credit', -1, 1) AS NormalSign,
               CASE WHEN account_type IN (N'Asset', N'Contra Asset') THEN N'Assets'
                    WHEN account_type = N'Liability' THEN N'Liabilities'
                    WHEN account_type = N'Equity' THEN N'Equity'
                    WHEN account_type IN (N'Revenue', N'Contra Revenue') THEN N'Revenue'
                    WHEN account_type = N'Expense' THEN N'Expenses'
                    ELSE N'Other' END AS StatementSection,
               HASHBYTES('SHA2_256', CONCAT_WS(N'|', account_name, account_type, financial_statement, normal_balance)) AS RowHash
        INTO #Source
        FROM stg.ChartOfAccounts WHERE IngestionRunID = @Run AND SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        MERGE dw.DimAccount AS t
        USING #Source AS s ON t.AccountCode = s.account_code
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET AccountName = s.account_name, AccountType = s.account_type,
                       FinancialStatement = s.financial_statement, NormalBalance = s.normal_balance,
                       NormalSign = s.NormalSign, StatementSection = s.StatementSection,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (AccountCode, AccountName, AccountType, FinancialStatement, NormalBalance, NormalSign,
                    StatementSection, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.account_code, s.account_name, s.account_type, s.financial_statement, s.normal_balance,
                    s.NormalSign, s.StatementSection, @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.AccountKey <> -1 AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.AccountCode, deleted.AccountCode), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_DIM_ACCOUNT', N'dw.DimAccount',
             N'chart_of_accounts', @Run, @Hash, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_DIM_ACCOUNT', N'dw.DimAccount', N'chart_of_accounts', @Error;
        THROW;
    END CATCH
END;
GO

-- =========================================================
-- SALES
-- =========================================================
CREATE OR ALTER PROCEDURE dw.usp_LoadFactSales @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'sales_transactions', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactSales';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT s.sale_id, dw.fn_DateKey(s.sale_date) AS SaleDateKey,
               ISNULL(site.SiteKey, -1) AS SiteKey, ISNULL(cust.CustomerKey, -1) AS CustomerKey,
               ISNULL(pm.PaymentMethodKey, -1) AS PaymentMethodKey,
               s.net_amount, s.vat_amount, s.total_amount, s.SourceRowNumber
        INTO #Source
        FROM stg.SalesTransaction AS s
        LEFT JOIN dw.DimSite AS site ON site.SiteID = s.site_id
        LEFT JOIN dw.DimCustomer AS cust ON cust.CustomerID = s.customer_id AND cust.IsCurrent = 1
        LEFT JOIN dw.DimPaymentMethod AS pm ON pm.PaymentMethodCode = s.payment_method
        WHERE s.IngestionRunID = @Run AND s.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source
        WHERE SiteKey = -1 OR CustomerKey = -1 OR PaymentMethodKey = -1 OR SaleDateKey = -1;

        MERGE dw.FactSales AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', SaleDateKey, SiteKey, CustomerKey, PaymentMethodKey,
                        CONVERT(NVARCHAR(50), net_amount), CONVERT(NVARCHAR(50), vat_amount),
                        CONVERT(NVARCHAR(50), total_amount))) AS RowHash
               FROM #Source) AS s
        ON t.SaleID = s.sale_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET SaleDateKey = s.SaleDateKey, SiteKey = s.SiteKey, CustomerKey = s.CustomerKey,
                       PaymentMethodKey = s.PaymentMethodKey, NetAmount = s.net_amount, VATAmount = s.vat_amount,
                       TotalAmount = s.total_amount, IngestionRunID = @Run, SourceFileHash = @Hash,
                       SourceRowNumber = s.SourceRowNumber, RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (SaleID, SaleDateKey, SiteKey, CustomerKey, PaymentMethodKey, NetAmount, VATAmount, TotalAmount,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.sale_id, s.SaleDateKey, s.SiteKey, s.CustomerKey, s.PaymentMethodKey, s.net_amount,
                    s.vat_amount, s.total_amount, @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.SaleID, deleted.SaleID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_SALES', N'dw.FactSales',
             N'sales_transactions', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_SALES', N'dw.FactSales', N'sales_transactions', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadFactSalesLine @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @HeaderRun VARCHAR(40), @HeaderHash CHAR(64),
            @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'sales_lines', @Run OUTPUT, @Hash OUTPUT;
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'sales_transactions', @HeaderRun OUTPUT, @HeaderHash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactSalesLine';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT l.sale_id, l.product_id, l.quantity, l.unit_price, l.line_net, l.unit_cost, l.SourceRowNumber,
               ISNULL(dw.fn_DateKey(h.sale_date), -1) AS SaleDateKey,
               ISNULL(site.SiteKey, -1) AS SiteKey, ISNULL(cust.CustomerKey, -1) AS CustomerKey,
               ISNULL(prod.ProductKey, -1) AS ProductKey, ISNULL(pm.PaymentMethodKey, -1) AS PaymentMethodKey,
               CONVERT(DECIMAL(19, 2), ROUND(l.quantity * l.unit_cost, 2)) AS LineCost
        INTO #Source
        FROM stg.SalesLine AS l
        LEFT JOIN stg.SalesTransaction AS h
               ON h.sale_id = l.sale_id AND h.IngestionRunID = @HeaderRun AND h.SourceFileHash = @HeaderHash
        LEFT JOIN dw.DimSite AS site ON site.SiteID = h.site_id
        LEFT JOIN dw.DimCustomer AS cust ON cust.CustomerID = h.customer_id AND cust.IsCurrent = 1
        LEFT JOIN dw.DimProduct AS prod ON prod.ProductID = l.product_id AND prod.IsCurrent = 1
        LEFT JOIN dw.DimPaymentMethod AS pm ON pm.PaymentMethodCode = h.payment_method
        WHERE l.IngestionRunID = @Run AND l.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source
        WHERE SaleDateKey = -1 OR SiteKey = -1 OR CustomerKey = -1 OR ProductKey = -1 OR PaymentMethodKey = -1;

        MERGE dw.FactSalesLine AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', SaleDateKey, SiteKey, CustomerKey, ProductKey,
                        PaymentMethodKey, quantity, CONVERT(NVARCHAR(50), unit_price), CONVERT(NVARCHAR(50), line_net),
                        CONVERT(NVARCHAR(50), unit_cost))) AS RowHash
               FROM #Source) AS s
        ON t.SaleID = s.sale_id AND t.ProductID = s.product_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET SaleDateKey = s.SaleDateKey, SiteKey = s.SiteKey, CustomerKey = s.CustomerKey,
                       ProductKey = s.ProductKey, PaymentMethodKey = s.PaymentMethodKey, Quantity = s.quantity,
                       UnitPrice = s.unit_price, LineNetAmount = s.line_net, UnitCost = s.unit_cost,
                       LineCostAmount = s.LineCost, GrossMarginAmount = s.line_net - s.LineCost,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (SaleID, ProductID, SaleDateKey, SiteKey, CustomerKey, ProductKey, PaymentMethodKey, Quantity,
                    UnitPrice, LineNetAmount, UnitCost, LineCostAmount, GrossMarginAmount,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.sale_id, s.product_id, s.SaleDateKey, s.SiteKey, s.CustomerKey, s.ProductKey, s.PaymentMethodKey,
                    s.quantity, s.unit_price, s.line_net, s.unit_cost, s.LineCost, s.line_net - s.LineCost,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, CONCAT(COALESCE(inserted.SaleID, deleted.SaleID), N'|', COALESCE(inserted.ProductID, deleted.ProductID)),
               deleted.RowHash, inserted.RowHash, deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_SALES_LINE', N'dw.FactSalesLine',
             N'sales_lines', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_SALES_LINE', N'dw.FactSalesLine', N'sales_lines', @Error;
        THROW;
    END CATCH
END;
GO

-- =========================================================
-- INVENTORY
-- =========================================================
CREATE OR ALTER PROCEDURE dw.usp_LoadFactInventoryMovement @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'inventory_movements', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactInventoryMovement';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT m.movement_id, dw.fn_DateKey(m.movement_date) AS MovementDateKey,
               ISNULL(site.SiteKey, -1) AS SiteKey, ISNULL(prod.ProductKey, -1) AS ProductKey,
               m.movement_type, IIF(m.quantity >= 0, 'IN', 'OUT') AS Direction, m.quantity, m.unit_cost,
               CONVERT(DECIMAL(19, 2), ROUND(m.quantity * m.unit_cost, 2)) AS MovementValue,
               m.source_document_id, m.SourceRowNumber
        INTO #Source
        FROM stg.InventoryMovement AS m
        LEFT JOIN dw.DimSite AS site ON site.SiteID = m.site_id
        LEFT JOIN dw.DimProduct AS prod ON prod.ProductID = m.product_id AND prod.IsCurrent = 1
        WHERE m.IngestionRunID = @Run AND m.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE SiteKey = -1 OR ProductKey = -1 OR MovementDateKey = -1;

        MERGE dw.FactInventoryMovement AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', MovementDateKey, SiteKey, ProductKey, movement_type,
                        quantity, CONVERT(NVARCHAR(50), unit_cost), source_document_id)) AS RowHash
               FROM #Source) AS s
        ON t.MovementID = s.movement_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET MovementDateKey = s.MovementDateKey, SiteKey = s.SiteKey, ProductKey = s.ProductKey,
                       MovementType = s.movement_type, Direction = s.Direction, Quantity = s.quantity,
                       UnitCost = s.unit_cost, MovementValue = s.MovementValue, SourceDocumentID = s.source_document_id,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (MovementID, MovementDateKey, SiteKey, ProductKey, MovementType, Direction, Quantity, UnitCost,
                    MovementValue, SourceDocumentID, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash,
                    InsertedPipelineExecutionID)
            VALUES (s.movement_id, s.MovementDateKey, s.SiteKey, s.ProductKey, s.movement_type, s.Direction, s.quantity,
                    s.unit_cost, s.MovementValue, s.source_document_id, @Run, @Hash, s.SourceRowNumber, s.RowHash,
                    @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.MovementID, deleted.MovementID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_INVENTORY_MOVEMENT',
             N'dw.FactInventoryMovement', N'inventory_movements', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_INVENTORY_MOVEMENT', N'dw.FactInventoryMovement', N'inventory_movements', @Error;
        THROW;
    END CATCH
END;
GO

-- Month-end stock positions, rebuilt from active movements.
CREATE OR ALTER PROCEDURE dw.usp_BuildFactInventorySnapshot @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Rows BIGINT;

    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactInventorySnapshot';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        DELETE FROM dw.FactInventorySnapshot;

        ;WITH Monthly AS (
            SELECT m.SiteKey, m.ProductKey, d.MonthEndDate,
                   SUM(IIF(m.Quantity > 0, m.Quantity, 0)) AS QuantityIn,
                   SUM(IIF(m.Quantity < 0, -m.Quantity, 0)) AS QuantityOut,
                   SUM(m.Quantity) AS NetQuantity
            FROM dw.FactInventoryMovement AS m
            JOIN dw.DimDate AS d ON d.DateKey = m.MovementDateKey
            WHERE m.IsDeletedInSource = 0
            GROUP BY m.SiteKey, m.ProductKey, d.MonthEndDate
        ), Positions AS (
            SELECT SiteKey, ProductKey FROM Monthly GROUP BY SiteKey, ProductKey
        ), MonthEnds AS (
            SELECT DISTINCT MonthEndDate FROM Monthly
        ), Grid AS (
            SELECT p.SiteKey, p.ProductKey, e.MonthEndDate,
                   ISNULL(m.QuantityIn, 0) AS QuantityIn, ISNULL(m.QuantityOut, 0) AS QuantityOut,
                   ISNULL(m.NetQuantity, 0) AS NetQuantity
            FROM Positions AS p
            CROSS JOIN MonthEnds AS e
            LEFT JOIN Monthly AS m
                   ON m.SiteKey = p.SiteKey AND m.ProductKey = p.ProductKey AND m.MonthEndDate = e.MonthEndDate
        ), Running AS (
            SELECT *, SUM(NetQuantity) OVER (PARTITION BY SiteKey, ProductKey ORDER BY MonthEndDate
                                             ROWS UNBOUNDED PRECEDING) AS ClosingQuantity
            FROM Grid
        )
        INSERT dw.FactInventorySnapshot (SnapshotDateKey, SiteKey, ProductKey, QuantityIn, QuantityOut,
                                         ClosingQuantity, ClosingValue, PipelineExecutionID)
        SELECT dw.fn_DateKey(r.MonthEndDate), r.SiteKey, r.ProductKey, r.QuantityIn, r.QuantityOut, r.ClosingQuantity,
               CONVERT(DECIMAL(19, 2), ROUND(r.ClosingQuantity * ISNULL(p.StandardCost, 0), 2)), @PipelineExecutionID
        FROM Running AS r
        LEFT JOIN dw.DimProduct AS cur ON cur.ProductKey = r.ProductKey
        LEFT JOIN dw.DimProduct AS p ON p.ProductID = cur.ProductID AND p.IsCurrent = 1;
        SET @Rows = @@ROWCOUNT;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_INVENTORY_SNAPSHOT',
             N'dw.FactInventorySnapshot', N'inventory_movements', NULL, NULL, @Rows, 0, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_INVENTORY_SNAPSHOT', N'dw.FactInventorySnapshot', N'inventory_movements', @Error;
        THROW;
    END CATCH
END;
GO

-- =========================================================
-- PROCUREMENT
-- =========================================================
CREATE OR ALTER PROCEDURE dw.usp_LoadFactPurchaseOrder @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'purchase_orders', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactPurchaseOrder';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT po.po_id, dw.fn_DateKey(po.po_date) AS OrderDateKey,
               ISNULL(sup.SupplierKey, -1) AS SupplierKey, ISNULL(site.SiteKey, -1) AS DeliverySiteKey,
               po.status, po.po_value, ISNULL(sup.MinimumOrderValueNAD, 0) AS MinimumOrderValue, po.SourceRowNumber
        INTO #Source
        FROM stg.PurchaseOrder AS po
        LEFT JOIN dw.DimSupplier AS sup ON sup.SupplierID = po.supplier_id AND sup.IsCurrent = 1
        LEFT JOIN dw.DimSite AS site ON site.SiteID = po.delivery_site_id
        WHERE po.IngestionRunID = @Run AND po.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE SupplierKey = -1 OR DeliverySiteKey = -1 OR OrderDateKey = -1;

        MERGE dw.FactPurchaseOrder AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', OrderDateKey, SupplierKey, DeliverySiteKey, status,
                        CONVERT(NVARCHAR(50), po_value), CONVERT(NVARCHAR(50), MinimumOrderValue))) AS RowHash
               FROM #Source) AS s
        ON t.POID = s.po_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET OrderDateKey = s.OrderDateKey, SupplierKey = s.SupplierKey, DeliverySiteKey = s.DeliverySiteKey,
                       POStatus = s.status, POValue = s.po_value, SupplierMinimumOrderValue = s.MinimumOrderValue,
                       MeetsMinimumOrderValue = IIF(s.po_value >= s.MinimumOrderValue, 1, 0),
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (POID, OrderDateKey, SupplierKey, DeliverySiteKey, POStatus, POValue, SupplierMinimumOrderValue,
                    MeetsMinimumOrderValue, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash,
                    InsertedPipelineExecutionID)
            VALUES (s.po_id, s.OrderDateKey, s.SupplierKey, s.DeliverySiteKey, s.status, s.po_value, s.MinimumOrderValue,
                    IIF(s.po_value >= s.MinimumOrderValue, 1, 0), @Run, @Hash, s.SourceRowNumber, s.RowHash,
                    @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.POID, deleted.POID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_PURCHASE_ORDER',
             N'dw.FactPurchaseOrder', N'purchase_orders', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_PURCHASE_ORDER', N'dw.FactPurchaseOrder', N'purchase_orders', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadFactPurchaseOrderLine @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @HeaderRun VARCHAR(40), @HeaderHash CHAR(64),
            @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'purchase_order_lines', @Run OUTPUT, @Hash OUTPUT;
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'purchase_orders', @HeaderRun OUTPUT, @HeaderHash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactPurchaseOrderLine';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT l.po_id, l.product_id, l.quantity, l.unit_cost, l.line_value, l.SourceRowNumber,
               ISNULL(dw.fn_DateKey(h.po_date), -1) AS OrderDateKey,
               ISNULL(sup.SupplierKey, -1) AS SupplierKey, ISNULL(prod.ProductKey, -1) AS ProductKey,
               ISNULL(site.SiteKey, -1) AS DeliverySiteKey
        INTO #Source
        FROM stg.PurchaseOrderLine AS l
        LEFT JOIN stg.PurchaseOrder AS h
               ON h.po_id = l.po_id AND h.IngestionRunID = @HeaderRun AND h.SourceFileHash = @HeaderHash
        LEFT JOIN dw.DimSupplier AS sup ON sup.SupplierID = h.supplier_id AND sup.IsCurrent = 1
        LEFT JOIN dw.DimProduct AS prod ON prod.ProductID = l.product_id AND prod.IsCurrent = 1
        LEFT JOIN dw.DimSite AS site ON site.SiteID = h.delivery_site_id
        WHERE l.IngestionRunID = @Run AND l.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source
        WHERE OrderDateKey = -1 OR SupplierKey = -1 OR ProductKey = -1 OR DeliverySiteKey = -1;

        MERGE dw.FactPurchaseOrderLine AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', OrderDateKey, SupplierKey, ProductKey, DeliverySiteKey,
                        quantity, CONVERT(NVARCHAR(50), unit_cost), CONVERT(NVARCHAR(50), line_value))) AS RowHash
               FROM #Source) AS s
        ON t.POID = s.po_id AND t.ProductID = s.product_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET OrderDateKey = s.OrderDateKey, SupplierKey = s.SupplierKey, ProductKey = s.ProductKey,
                       DeliverySiteKey = s.DeliverySiteKey, Quantity = s.quantity, UnitCost = s.unit_cost,
                       LineValue = s.line_value, IngestionRunID = @Run, SourceFileHash = @Hash,
                       SourceRowNumber = s.SourceRowNumber, RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (POID, ProductID, OrderDateKey, SupplierKey, ProductKey, DeliverySiteKey, Quantity, UnitCost,
                    LineValue, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.po_id, s.product_id, s.OrderDateKey, s.SupplierKey, s.ProductKey, s.DeliverySiteKey, s.quantity,
                    s.unit_cost, s.line_value, @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, CONCAT(COALESCE(inserted.POID, deleted.POID), N'|', COALESCE(inserted.ProductID, deleted.ProductID)),
               deleted.RowHash, inserted.RowHash, deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_PURCHASE_ORDER_LINE',
             N'dw.FactPurchaseOrderLine', N'purchase_order_lines', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_PURCHASE_ORDER_LINE', N'dw.FactPurchaseOrderLine', N'purchase_order_lines', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadFactGoodsReceipt @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @HeaderRun VARCHAR(40), @HeaderHash CHAR(64),
            @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'goods_receipts', @Run OUTPUT, @Hash OUTPUT;
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'purchase_orders', @HeaderRun OUTPUT, @HeaderHash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactGoodsReceipt';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT g.grn_id, g.po_id, dw.fn_DateKey(g.receipt_date) AS ReceiptDateKey,
               ISNULL(dw.fn_DateKey(po.po_date), -1) AS OrderDateKey,
               ISNULL(sup.SupplierKey, -1) AS SupplierKey, ISNULL(site.SiteKey, -1) AS SiteKey,
               DATEDIFF(DAY, po.po_date, g.receipt_date) AS LeadTimeDays, g.SourceRowNumber
        INTO #Source
        FROM stg.GoodsReceipt AS g
        LEFT JOIN stg.PurchaseOrder AS po
               ON po.po_id = g.po_id AND po.IngestionRunID = @HeaderRun AND po.SourceFileHash = @HeaderHash
        LEFT JOIN dw.DimSupplier AS sup ON sup.SupplierID = g.supplier_id AND sup.IsCurrent = 1
        LEFT JOIN dw.DimSite AS site ON site.SiteID = g.site_id
        WHERE g.IngestionRunID = @Run AND g.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source
        WHERE OrderDateKey = -1 OR SupplierKey = -1 OR SiteKey = -1 OR ReceiptDateKey = -1;

        MERGE dw.FactGoodsReceipt AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', po_id, ReceiptDateKey, OrderDateKey, SupplierKey, SiteKey,
                        ISNULL(CONVERT(NVARCHAR(10), LeadTimeDays), N'<NULL>'))) AS RowHash
               FROM #Source) AS s
        ON t.GRNID = s.grn_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET POID = s.po_id, ReceiptDateKey = s.ReceiptDateKey, OrderDateKey = s.OrderDateKey,
                       SupplierKey = s.SupplierKey, SiteKey = s.SiteKey, LeadTimeDays = s.LeadTimeDays,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (GRNID, POID, ReceiptDateKey, OrderDateKey, SupplierKey, SiteKey, LeadTimeDays,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.grn_id, s.po_id, s.ReceiptDateKey, s.OrderDateKey, s.SupplierKey, s.SiteKey, s.LeadTimeDays,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.GRNID, deleted.GRNID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_GOODS_RECEIPT',
             N'dw.FactGoodsReceipt', N'goods_receipts', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_GOODS_RECEIPT', N'dw.FactGoodsReceipt', N'goods_receipts', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadFactSupplierInvoice @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @HeaderRun VARCHAR(40), @HeaderHash CHAR(64),
            @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'supplier_invoices', @Run OUTPUT, @Hash OUTPUT;
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'purchase_orders', @HeaderRun OUTPUT, @HeaderHash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactSupplierInvoice';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT i.invoice_id, i.po_id, dw.fn_DateKey(i.invoice_date) AS InvoiceDateKey,
               dw.fn_DateKey(DATEADD(DAY, ISNULL(sup.PaymentTermsDays, 0), i.invoice_date)) AS DueDateKey,
               ISNULL(sup.SupplierKey, -1) AS SupplierKey, i.status, i.net_amount, i.vat_amount, i.total_amount,
               sup.PaymentTermsDays, po.po_value AS OrderedValue, i.SourceRowNumber
        INTO #Source
        FROM stg.SupplierInvoice AS i
        LEFT JOIN stg.PurchaseOrder AS po
               ON po.po_id = i.po_id AND po.IngestionRunID = @HeaderRun AND po.SourceFileHash = @HeaderHash
        LEFT JOIN dw.DimSupplier AS sup ON sup.SupplierID = i.supplier_id AND sup.IsCurrent = 1
        WHERE i.IngestionRunID = @Run AND i.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE SupplierKey = -1 OR InvoiceDateKey = -1 OR OrderedValue IS NULL;

        MERGE dw.FactSupplierInvoice AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', po_id, InvoiceDateKey, DueDateKey, SupplierKey, status,
                        CONVERT(NVARCHAR(50), net_amount), CONVERT(NVARCHAR(50), vat_amount),
                        CONVERT(NVARCHAR(50), total_amount), ISNULL(CONVERT(NVARCHAR(50), OrderedValue), N'<NULL>'))) AS RowHash
               FROM #Source) AS s
        ON t.InvoiceID = s.invoice_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET POID = s.po_id, InvoiceDateKey = s.InvoiceDateKey, DueDateKey = s.DueDateKey,
                       SupplierKey = s.SupplierKey, InvoiceStatus = s.status, NetAmount = s.net_amount,
                       VATAmount = s.vat_amount, TotalAmount = s.total_amount, PaymentTermsDays = s.PaymentTermsDays,
                       OrderedValue = s.OrderedValue, InvoiceToOrderVariance = s.net_amount - s.OrderedValue,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (InvoiceID, POID, InvoiceDateKey, DueDateKey, SupplierKey, InvoiceStatus, NetAmount, VATAmount,
                    TotalAmount, PaymentTermsDays, OrderedValue, InvoiceToOrderVariance,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.invoice_id, s.po_id, s.InvoiceDateKey, s.DueDateKey, s.SupplierKey, s.status, s.net_amount,
                    s.vat_amount, s.total_amount, s.PaymentTermsDays, s.OrderedValue, s.net_amount - s.OrderedValue,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.InvoiceID, deleted.InvoiceID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_SUPPLIER_INVOICE',
             N'dw.FactSupplierInvoice', N'supplier_invoices', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_SUPPLIER_INVOICE', N'dw.FactSupplierInvoice', N'supplier_invoices', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadFactSupplierRebate @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'supplier_rebates', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactSupplierRebate';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT r.supplier_id, r.period, r.qualifying_spend, r.rebate_rate, r.rebate_amount, r.SourceRowNumber,
               dw.fn_DateKey(EOMONTH(DATEFROMPARTS(CONVERT(INT, LEFT(r.period, 4)),
                                                   CONVERT(INT, RIGHT(r.period, 1)) * 3, 1))) AS PeriodEndDateKey,
               ISNULL(sup.SupplierKey, -1) AS SupplierKey,
               ISNULL((SELECT TOP 1 t.RebateRate FROM meta.RebateThreshold AS t
                       WHERE r.qualifying_spend >= t.ThresholdNAD ORDER BY t.ThresholdNAD DESC), 0) AS ExpectedRate
        INTO #Source
        FROM stg.SupplierRebate AS r
        LEFT JOIN dw.DimSupplier AS sup ON sup.SupplierID = r.supplier_id AND sup.IsCurrent = 1
        WHERE r.IngestionRunID = @Run AND r.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE SupplierKey = -1;

        MERGE dw.FactSupplierRebate AS t
        USING (SELECT *, CONVERT(DECIMAL(19, 2), ROUND(qualifying_spend * ExpectedRate, 2)) AS ExpectedAmount,
                      HASHBYTES('SHA2_256', CONCAT_WS(N'|', PeriodEndDateKey, SupplierKey,
                        CONVERT(NVARCHAR(50), qualifying_spend), CONVERT(NVARCHAR(50), rebate_rate),
                        CONVERT(NVARCHAR(50), rebate_amount), CONVERT(NVARCHAR(50), ExpectedRate))) AS RowHash
               FROM #Source) AS s
        ON t.SupplierID = s.supplier_id AND t.RebatePeriod = s.period
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET PeriodEndDateKey = s.PeriodEndDateKey, SupplierKey = s.SupplierKey,
                       QualifyingSpend = s.qualifying_spend, RebateRate = s.rebate_rate, RebateAmount = s.rebate_amount,
                       ExpectedRebateRate = s.ExpectedRate, ExpectedRebateAmount = s.ExpectedAmount,
                       RebateVariance = s.rebate_amount - s.ExpectedAmount,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (SupplierID, RebatePeriod, PeriodEndDateKey, SupplierKey, QualifyingSpend, RebateRate, RebateAmount,
                    ExpectedRebateRate, ExpectedRebateAmount, RebateVariance,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.supplier_id, s.period, s.PeriodEndDateKey, s.SupplierKey, s.qualifying_spend, s.rebate_rate,
                    s.rebate_amount, s.ExpectedRate, s.ExpectedAmount, s.rebate_amount - s.ExpectedAmount,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, CONCAT(COALESCE(inserted.SupplierID, deleted.SupplierID), N'|', COALESCE(inserted.RebatePeriod, deleted.RebatePeriod)),
               deleted.RowHash, inserted.RowHash, deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_SUPPLIER_REBATE',
             N'dw.FactSupplierRebate', N'supplier_rebates', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_SUPPLIER_REBATE', N'dw.FactSupplierRebate', N'supplier_rebates', @Error;
        THROW;
    END CATCH
END;
GO

-- =========================================================
-- FINANCE
-- =========================================================
CREATE OR ALTER PROCEDURE dw.usp_LoadFactGeneralLedger @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'general_ledger', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactGeneralLedger';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        -- Journal line number: position of the line within its journal in the source file.
        SELECT g.journal_id,
               ROW_NUMBER() OVER (PARTITION BY g.journal_id ORDER BY g.SourceRowNumber) AS JournalLineNumber,
               dw.fn_DateKey(g.posting_date) AS PostingDateKey,
               ISNULL(acc.AccountKey, -1) AS AccountKey, ISNULL(site.SiteKey, -1) AS SiteKey,
               g.source_document_id, g.transaction_type,
               CONVERT(DECIMAL(19, 2), ISNULL(g.debit, 0)) AS Debit,
               CONVERT(DECIMAL(19, 2), ISNULL(g.credit, 0)) AS Credit,
               g.SourceRowNumber
        INTO #Source
        FROM stg.GeneralLedger AS g
        LEFT JOIN dw.DimAccount AS acc ON acc.AccountCode = g.account_code
        LEFT JOIN dw.DimSite AS site ON site.SiteID = g.site_id
        WHERE g.IngestionRunID = @Run AND g.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE AccountKey = -1 OR SiteKey = -1 OR PostingDateKey = -1;

        MERGE dw.FactGeneralLedger AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', PostingDateKey, AccountKey, SiteKey, source_document_id,
                        transaction_type, CONVERT(NVARCHAR(50), Debit), CONVERT(NVARCHAR(50), Credit))) AS RowHash
               FROM #Source) AS s
        ON t.JournalID = s.journal_id AND t.JournalLineNumber = s.JournalLineNumber
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET PostingDateKey = s.PostingDateKey, AccountKey = s.AccountKey, SiteKey = s.SiteKey,
                       SourceDocumentID = s.source_document_id, TransactionType = s.transaction_type,
                       DebitAmount = s.Debit, CreditAmount = s.Credit, NetAmount = s.Debit - s.Credit,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (JournalID, JournalLineNumber, PostingDateKey, AccountKey, SiteKey, SourceDocumentID, TransactionType,
                    DebitAmount, CreditAmount, NetAmount, IngestionRunID, SourceFileHash, SourceRowNumber, RowHash,
                    InsertedPipelineExecutionID)
            VALUES (s.journal_id, s.JournalLineNumber, s.PostingDateKey, s.AccountKey, s.SiteKey, s.source_document_id,
                    s.transaction_type, s.Debit, s.Credit, s.Debit - s.Credit, @Run, @Hash, s.SourceRowNumber,
                    s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, CONCAT(COALESCE(inserted.JournalID, deleted.JournalID), N'#',
                               COALESCE(inserted.JournalLineNumber, deleted.JournalLineNumber)),
               deleted.RowHash, inserted.RowHash, deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_GENERAL_LEDGER',
             N'dw.FactGeneralLedger', N'general_ledger', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_GENERAL_LEDGER', N'dw.FactGeneralLedger', N'general_ledger', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadFactOperatingExpense @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'operating_expenses', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactOperatingExpense';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        -- Expense account: the posting rule's debit account for the expense type.
        SELECT e.expense_id, dw.fn_DateKey(e.expense_date) AS ExpenseDateKey,
               ISNULL(site.SiteKey, -1) AS SiteKey, ISNULL(acc.AccountKey, -1) AS AccountKey,
               e.expense_type, e.description, e.amount, e.SourceRowNumber
        INTO #Source
        FROM stg.OperatingExpense AS e
        LEFT JOIN dw.DimSite AS site ON site.SiteID = e.site_id
        LEFT JOIN meta.PostingRule AS rule_ ON rule_.TransactionType = e.expense_type
        LEFT JOIN dw.DimAccount AS acc ON acc.AccountCode = rule_.DebitAccount
        WHERE e.IngestionRunID = @Run AND e.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE SiteKey = -1 OR AccountKey = -1 OR ExpenseDateKey = -1;

        MERGE dw.FactOperatingExpense AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', ExpenseDateKey, SiteKey, AccountKey, expense_type,
                        ISNULL(description, N'<NULL>'), CONVERT(NVARCHAR(50), amount))) AS RowHash
               FROM #Source) AS s
        ON t.ExpenseID = s.expense_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET ExpenseDateKey = s.ExpenseDateKey, SiteKey = s.SiteKey, AccountKey = s.AccountKey,
                       ExpenseType = s.expense_type, ExpenseDescription = s.description, Amount = s.amount,
                       IngestionRunID = @Run, SourceFileHash = @Hash, SourceRowNumber = s.SourceRowNumber,
                       RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (ExpenseID, ExpenseDateKey, SiteKey, AccountKey, ExpenseType, ExpenseDescription, Amount,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.expense_id, s.ExpenseDateKey, s.SiteKey, s.AccountKey, s.expense_type, s.description, s.amount,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, COALESCE(inserted.ExpenseID, deleted.ExpenseID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_OPERATING_EXPENSE',
             N'dw.FactOperatingExpense', N'operating_expenses', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_OPERATING_EXPENSE', N'dw.FactOperatingExpense', N'operating_expenses', @Error;
        THROW;
    END CATCH
END;
GO

-- =========================================================
-- ASSETS
-- =========================================================
-- Straight-line schedule: depreciation starts the month after acquisition
-- (the ERP register convention) and runs until fully depreciated or the
-- reporting as-at date (meta.Setting reporting.as_at_date).
CREATE OR ALTER PROCEDURE dw.usp_BuildFactAssetDepreciation @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Rows BIGINT;
    DECLARE @AsAt DATE = COALESCE(
        (SELECT TRY_CONVERT(DATE, SettingValue) FROM meta.Setting WHERE SettingKey = 'reporting.as_at_date'),
        EOMONTH(SYSUTCDATETIME()));

    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactAssetDepreciation';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        DELETE FROM dw.FactAssetDepreciation;

        ;WITH Schedule AS (
            SELECT a.AssetKey, a.SiteID, a.AcquisitionCost, a.MonthlyDepreciation, d.DateKey, d.[Date],
                   ROW_NUMBER() OVER (PARTITION BY a.AssetKey ORDER BY d.[Date]) AS MonthsInService
            FROM dw.DimAsset AS a
            JOIN dw.DimDate AS d
              ON d.IsMonthEnd = 1
             AND d.[Date] > EOMONTH(a.AcquisitionDate)
             AND d.[Date] <= a.FullyDepreciatedDate
             AND d.[Date] <= @AsAt
            WHERE a.AssetKey <> -1 AND a.IsDeletedInSource = 0
        )
        INSERT dw.FactAssetDepreciation (AssetKey, PeriodEndDateKey, SiteKey, DepreciationAmount,
                                         AccumulatedDepreciation, NetBookValue, PipelineExecutionID)
        SELECT s.AssetKey, s.DateKey, ISNULL(site.SiteKey, -1),
               -- the final month absorbs rounding so the asset depreciates exactly to zero
               IIF(s.MonthlyDepreciation * s.MonthsInService > s.AcquisitionCost,
                   s.AcquisitionCost - s.MonthlyDepreciation * (s.MonthsInService - 1), s.MonthlyDepreciation),
               IIF(s.MonthlyDepreciation * s.MonthsInService > s.AcquisitionCost,
                   s.AcquisitionCost, s.MonthlyDepreciation * s.MonthsInService),
               s.AcquisitionCost - IIF(s.MonthlyDepreciation * s.MonthsInService > s.AcquisitionCost,
                                       s.AcquisitionCost, s.MonthlyDepreciation * s.MonthsInService),
               @PipelineExecutionID
        FROM Schedule AS s
        LEFT JOIN dw.DimSite AS site ON site.SiteID = s.SiteID;
        SET @Rows = @@ROWCOUNT;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_ASSET_DEPRECIATION',
             N'dw.FactAssetDepreciation', N'fixed_assets_erp', NULL, NULL, @Rows, 0, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_ASSET_DEPRECIATION', N'dw.FactAssetDepreciation', N'fixed_assets_erp', @Error;
        THROW;
    END CATCH
END;
GO

-- =========================================================
-- BRANCH CASH-UPS (document set: every ELIGIBLE document of the run)
-- =========================================================
CREATE OR ALTER PROCEDURE dw.usp_LoadFactBranchCashup @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Rows BIGINT, @Unknown BIGINT;
    DECLARE @Tolerance DECIMAL(19, 6) = meta.fn_SettingDecimal('cashup.variance_tolerance_nad', 1.00);

    BEGIN TRY
        IF NOT EXISTS (SELECT 1 FROM audit.fn_PipelineBatch(@PipelineExecutionID, N'branch_cashup'))
            THROW 52003, 'No ELIGIBLE branch_cashup documents recorded for this pipeline execution.', 1;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.FactBranchCashup';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT c.shift_id, dw.fn_DateKey(c.business_date) AS BusinessDateKey, ISNULL(site.SiteKey, -1) AS SiteKey,
               c.pos_terminal, c.cashier, c.opening_float, c.cash_sales, c.card_sales, c.account_sales, c.total_sales,
               ISNULL(c.internal_expenses, 0) AS internal_expenses, c.cash_removed, c.expected_closing_cash,
               c.actual_closing_cash, c.variance, c.manager_approval, c.SourceFile, c.IngestionRunID,
               c.SourceFileHash, c.SourceRowNumber,
               CASE WHEN ABS(c.variance) <= @Tolerance THEN N'BALANCED'
                    WHEN c.variance < 0 THEN N'SHORTAGE' ELSE N'OVERAGE' END AS VarianceStatus
        INTO #Source
        FROM stg.BranchCashup AS c
        JOIN audit.fn_PipelineBatch(@PipelineExecutionID, N'branch_cashup') AS b
          ON b.IngestionRunID = c.IngestionRunID AND b.SourceFileHash = c.SourceFileHash
        LEFT JOIN dw.DimSite AS site ON site.SiteID = c.site_code;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE SiteKey = -1 OR BusinessDateKey = -1;

        -- Each document stands alone: a document not in this run is not
        -- treated as deleted (no NOT MATCHED BY SOURCE clause).
        MERGE dw.FactBranchCashup AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', BusinessDateKey, SiteKey, ISNULL(pos_terminal, N'<NULL>'),
                        ISNULL(cashier, N'<NULL>'), CONVERT(NVARCHAR(50), opening_float), CONVERT(NVARCHAR(50), cash_sales),
                        CONVERT(NVARCHAR(50), card_sales), CONVERT(NVARCHAR(50), account_sales),
                        CONVERT(NVARCHAR(50), total_sales), CONVERT(NVARCHAR(50), internal_expenses),
                        CONVERT(NVARCHAR(50), cash_removed), CONVERT(NVARCHAR(50), expected_closing_cash),
                        CONVERT(NVARCHAR(50), actual_closing_cash), CONVERT(NVARCHAR(50), variance),
                        ISNULL(manager_approval, N'<NULL>'), SourceFileHash)) AS RowHash
               FROM #Source) AS s
        ON t.ShiftID = s.shift_id
        WHEN MATCHED AND t.RowHash <> s.RowHash THEN
            UPDATE SET BusinessDateKey = s.BusinessDateKey, SiteKey = s.SiteKey, PosTerminal = s.pos_terminal,
                       Cashier = s.cashier, OpeningFloat = s.opening_float, CashSales = s.cash_sales,
                       CardSales = s.card_sales, AccountSales = s.account_sales, TotalSales = s.total_sales,
                       InternalExpenses = s.internal_expenses, CashRemoved = s.cash_removed,
                       ExpectedClosingCash = s.expected_closing_cash, ActualClosingCash = s.actual_closing_cash,
                       CashVariance = s.variance, CashVarianceStatus = s.VarianceStatus,
                       ManagerApproval = s.manager_approval, SourceFile = s.SourceFile,
                       IngestionRunID = s.IngestionRunID, SourceFileHash = s.SourceFileHash,
                       SourceRowNumber = s.SourceRowNumber, RowHash = s.RowHash,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (ShiftID, BusinessDateKey, SiteKey, PosTerminal, Cashier, OpeningFloat, CashSales, CardSales,
                    AccountSales, TotalSales, InternalExpenses, CashRemoved, ExpectedClosingCash, ActualClosingCash,
                    CashVariance, CashVarianceStatus, ManagerApproval, SourceFile, IngestionRunID, SourceFileHash,
                    SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.shift_id, s.BusinessDateKey, s.SiteKey, s.pos_terminal, s.cashier, s.opening_float, s.cash_sales,
                    s.card_sales, s.account_sales, s.total_sales, s.internal_expenses, s.cash_removed,
                    s.expected_closing_cash, s.actual_closing_cash, s.variance, s.VarianceStatus, s.manager_approval,
                    s.SourceFile, s.IngestionRunID, s.SourceFileHash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        OUTPUT $action, COALESCE(inserted.ShiftID, deleted.ShiftID), deleted.RowHash, inserted.RowHash,
               deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_FACT_BRANCH_CASHUP',
             N'dw.FactBranchCashup', N'branch_cashup', NULL, NULL, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_FACT_BRANCH_CASHUP', N'dw.FactBranchCashup', N'branch_cashup', @Error;
        THROW;
    END CATCH
END;
GO
