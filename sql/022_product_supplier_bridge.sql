USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    022 - Product-supplier bridge (approved suppliers and agreed unit costs).

    GRAIN: one row per product and approved supplier (product_id, supplier_id).
    SCD1 with soft delete: a relationship missing from a later full extract
    is flagged IsDeletedInSource (no longer an approved supplier).
*/

IF OBJECT_ID(N'dw.BridgeProductSupplier', N'U') IS NULL
BEGIN
    CREATE TABLE dw.BridgeProductSupplier (
        ProductSupplierKey BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_BridgeProductSupplier PRIMARY KEY,
        ProductID NVARCHAR(50) NOT NULL,
        SupplierID NVARCHAR(50) NOT NULL,
        ProductKey INT NOT NULL CONSTRAINT FK_BridgeProductSupplier_Product REFERENCES dw.DimProduct(ProductKey),
        SupplierKey BIGINT NOT NULL CONSTRAINT FK_BridgeProductSupplier_Supplier REFERENCES dw.DimSupplier(SupplierKey),
        SupplierUnitCost DECIMAL(19, 4) NOT NULL,
        IsPreferredSupplier BIT NOT NULL,       -- lowest agreed unit cost for the product
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        RowHash BINARY(32) NOT NULL,
        IsDeletedInSource BIT NOT NULL CONSTRAINT DF_BridgeProductSupplier_Deleted DEFAULT 0,
        InsertedPipelineExecutionID BIGINT NOT NULL,
        UpdatedPipelineExecutionID BIGINT NULL,
        InsertedAt DATETIME2(7) NOT NULL CONSTRAINT DF_BridgeProductSupplier_InsertedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT UQ_BridgeProductSupplier UNIQUE (ProductID, SupplierID)
    );
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadBridgeProductSupplier @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Run VARCHAR(40), @Hash CHAR(64), @Rows BIGINT, @Unknown BIGINT;

    BEGIN TRY
        EXEC audit.usp_RequireSingleBatch @PipelineExecutionID, N'product_supplier', @Run OUTPUT, @Hash OUTPUT;

        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'dw.BridgeProductSupplier';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        SELECT ps.product_id, ps.supplier_id, ps.supplier_unit_cost, ps.SourceRowNumber,
               ISNULL(p.ProductKey, -1) AS ProductKey, ISNULL(s.SupplierKey, -1) AS SupplierKey,
               IIF(ROW_NUMBER() OVER (PARTITION BY ps.product_id ORDER BY ps.supplier_unit_cost, ps.supplier_id) = 1,
                   1, 0) AS IsPreferred
        INTO #Source
        FROM stg.ProductSupplier AS ps
        LEFT JOIN dw.DimProduct AS p ON p.ProductID = ps.product_id AND p.IsCurrent = 1
        LEFT JOIN dw.DimSupplier AS s ON s.SupplierID = ps.supplier_id AND s.IsCurrent = 1
        WHERE ps.IngestionRunID = @Run AND ps.SourceFileHash = @Hash;
        SET @Rows = @@ROWCOUNT;

        SELECT @Unknown = COUNT(*) FROM #Source WHERE ProductKey = -1 OR SupplierKey = -1;

        MERGE dw.BridgeProductSupplier AS t
        USING (SELECT *, HASHBYTES('SHA2_256', CONCAT_WS(N'|', ProductKey, SupplierKey,
                        CONVERT(NVARCHAR(50), supplier_unit_cost), IsPreferred)) AS RowHash
               FROM #Source) AS s
        ON t.ProductID = s.product_id AND t.SupplierID = s.supplier_id
        WHEN MATCHED AND (t.RowHash <> s.RowHash OR t.IsDeletedInSource = 1) THEN
            UPDATE SET ProductKey = s.ProductKey, SupplierKey = s.SupplierKey, SupplierUnitCost = s.supplier_unit_cost,
                       IsPreferredSupplier = s.IsPreferred, IngestionRunID = @Run, SourceFileHash = @Hash,
                       SourceRowNumber = s.SourceRowNumber, RowHash = s.RowHash, IsDeletedInSource = 0,
                       UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (ProductID, SupplierID, ProductKey, SupplierKey, SupplierUnitCost, IsPreferredSupplier,
                    IngestionRunID, SourceFileHash, SourceRowNumber, RowHash, InsertedPipelineExecutionID)
            VALUES (s.product_id, s.supplier_id, s.ProductKey, s.SupplierKey, s.supplier_unit_cost, s.IsPreferred,
                    @Run, @Hash, s.SourceRowNumber, s.RowHash, @PipelineExecutionID)
        WHEN NOT MATCHED BY SOURCE AND t.IsDeletedInSource = 0 THEN
            UPDATE SET IsDeletedInSource = 1, UpdatedPipelineExecutionID = @PipelineExecutionID, UpdatedAt = SYSUTCDATETIME()
        OUTPUT $action, CONCAT(COALESCE(inserted.ProductID, deleted.ProductID), N'|', COALESCE(inserted.SupplierID, deleted.SupplierID)),
               deleted.RowHash, inserted.RowHash, deleted.IsDeletedInSource, inserted.IsDeletedInSource
        INTO #MergeActions;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'DW_BRIDGE_PRODUCT_SUPPLIER',
             N'dw.BridgeProductSupplier', N'product_supplier', @Run, @Hash, @Rows, @Unknown;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'DW_BRIDGE_PRODUCT_SUPPLIER', N'dw.BridgeProductSupplier', N'product_supplier', @Error;
        THROW;
    END CATCH
END;
GO
