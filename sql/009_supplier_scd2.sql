USE RetailDataPlatform;
GO

IF OBJECT_ID(N'audit.SupplierWarehouseExecution', N'U') IS NULL
BEGIN
    CREATE TABLE audit.SupplierWarehouseExecution (
        ExecutionID BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        IngestionRunID VARCHAR(40) NOT NULL,
        StartedAt DATETIME2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
        CompletedAt DATETIME2(7) NULL,
        Status VARCHAR(20) NOT NULL,
        SourceRows INT NOT NULL DEFAULT 0,
        InsertedSuppliers INT NOT NULL DEFAULT 0,
        RevisedSuppliers INT NOT NULL DEFAULT 0,
        UnchangedSuppliers INT NOT NULL DEFAULT 0,
        ErrorMessage NVARCHAR(4000) NULL,
        CONSTRAINT CK_SupplierWarehouseExecution_Status
            CHECK (Status IN ('STARTED','COMPLETED','SKIPPED','FAILED'))
    );
END;
GO

CREATE OR ALTER PROCEDURE dw.usp_LoadDimSupplier
    @IngestionRunID VARCHAR(40)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @ExecutionID BIGINT;
    DECLARE @LockResult INT;
    DECLARE @AsOf DATETIME2(7) = SYSUTCDATETIME();
    DECLARE @SourceRows INT = 0, @Inserted INT = 0, @Revised INT = 0, @Unchanged INT = 0;

    IF NULLIF(LTRIM(RTRIM(@IngestionRunID)), '') IS NULL
        THROW 51000, 'IngestionRunID is required.', 1;

    BEGIN TRY
        BEGIN TRANSACTION;

        EXEC @LockResult = sys.sp_getapplock
            @Resource = 'dw.DimSupplier.SCD2',
            @LockMode = 'Exclusive',
            @LockOwner = 'Transaction',
            @LockTimeout = 15000;
        IF @LockResult < 0
            THROW 51001, 'Could not acquire Supplier warehouse load lock.', 1;

        IF NOT EXISTS (
            SELECT 1 FROM audit.LoadExecution
            WHERE IngestionRunID = @IngestionRunID
              AND SourceName = 'supplier_master'
              AND LoadStatus = 'COMPLETED'
        )
            THROW 51002, 'No completed supplier staging load for this ingestion run.', 1;

        SELECT @SourceRows = COUNT(*)
        FROM stg.Supplier
        WHERE IngestionRunID = @IngestionRunID;

        IF @SourceRows = 0
            THROW 51003, 'No supplier staging rows for this ingestion run.', 1;

        IF EXISTS (
            SELECT supplier_id
            FROM stg.Supplier
            WHERE IngestionRunID = @IngestionRunID
            GROUP BY supplier_id
            HAVING COUNT(*) > 1
        )
            THROW 51004, 'Duplicate supplier business keys in staging batch.', 1;

        -- Require the staging batch to be exactly one completed, audited file version.
        IF (SELECT COUNT(*) FROM (
                SELECT SourceFileHash, SourceFile
                FROM stg.Supplier
                WHERE IngestionRunID = @IngestionRunID
                GROUP BY SourceFileHash, SourceFile
            ) AS Versions) <> 1
            THROW 51005, 'Staging run contains multiple supplier file versions.', 1;

        IF NOT EXISTS (
            SELECT 1
            FROM audit.LoadExecution AS a
            CROSS APPLY (
                SELECT COUNT(*) AS BatchRows, MIN(s.SourceFileHash) AS FileHash,
                       MIN(s.SourceFile) AS FileName
                FROM stg.Supplier AS s
                WHERE s.IngestionRunID = @IngestionRunID
            ) AS b
            WHERE a.IngestionRunID = @IngestionRunID
              AND a.SourceName = 'supplier_master'
              AND a.LoadStatus = 'COMPLETED'
              AND a.SourceFileHash = b.FileHash
              AND a.SourceFile = b.FileName
              AND a.RowsInserted = b.BatchRows
              AND a.RowsRejected = 0
        )
            THROW 51006, 'Staging batch does not reconcile with completed load audit.', 1;

        INSERT audit.SupplierWarehouseExecution (IngestionRunID, Status, SourceRows)
        VALUES (@IngestionRunID, 'STARTED', @SourceRows);
        SET @ExecutionID = SCOPE_IDENTITY();

        -- Prevent replaying an old batch after a newer batch has been processed.
        IF EXISTS (
            SELECT 1 FROM dw.DimSupplier
            WHERE IngestionRunID > @IngestionRunID
        )
            THROW 51007, 'A newer supplier batch is already in the warehouse; historical replay needs a controlled process.', 1;

        SELECT s.* INTO #Batch
        FROM stg.Supplier AS s
        WHERE s.IngestionRunID = @IngestionRunID;

        SELECT b.*,
               d.SupplierKey AS CurrentSupplierKey,
               d.RevisionNumber AS CurrentRevision,
               CASE WHEN d.SupplierKey IS NULL THEN 'INITIAL'
                    WHEN EXISTS (
                        SELECT b.supplier_name, b.location, b.minimum_order_value_nad,
                               b.rebate_rate, b.payment_terms_days
                        EXCEPT
                        SELECT d.SupplierName, d.Location, d.MinimumOrderValueNAD,
                               d.RebateRate, d.PaymentTermsDays
                    ) THEN 'UPDATE'
                    ELSE 'UNCHANGED' END AS ActionType
        INTO #Changes
        FROM #Batch AS b
        LEFT JOIN dw.DimSupplier AS d WITH (UPDLOCK, HOLDLOCK)
          ON d.SupplierID = b.supplier_id AND d.IsCurrent = 1;

        SELECT @Inserted = COUNT(*) FROM #Changes WHERE ActionType = 'INITIAL';
        SELECT @Revised = COUNT(*) FROM #Changes WHERE ActionType = 'UPDATE';
        SELECT @Unchanged = COUNT(*) FROM #Changes WHERE ActionType = 'UNCHANGED';

        -- Use a single timestamp for closing and opening revisions.
        SET @AsOf = SYSUTCDATETIME();

        UPDATE d
        SET d.EffectiveTo = @AsOf,
            d.IsCurrent = 0
        FROM dw.DimSupplier AS d
        INNER JOIN #Changes AS c ON c.CurrentSupplierKey = d.SupplierKey
        WHERE c.ActionType = 'UPDATE';

        INSERT dw.DimSupplier (
            SupplierID, SupplierName, Location, MinimumOrderValueNAD,
            RebateRate, PaymentTermsDays, EffectiveFrom, EffectiveTo,
            IsCurrent, RevisionNumber, ChangeType, ChangeReason,
            IngestionRunID, SourceFile, SourceFileHash, SourceRowNumber
        )
        SELECT c.supplier_id, c.supplier_name, c.location,
               c.minimum_order_value_nad, c.rebate_rate, c.payment_terms_days,
               @AsOf, NULL, 1, COALESCE(c.CurrentRevision, 0) + 1,
               c.ActionType, CASE WHEN c.ActionType = 'INITIAL'
                                  THEN 'First observed supplier version'
                                  ELSE 'Tracked supplier attributes changed' END,
               c.IngestionRunID, c.SourceFile, c.SourceFileHash, c.SourceRowNumber
        FROM #Changes AS c
        WHERE c.ActionType IN ('INITIAL', 'UPDATE');

        UPDATE audit.SupplierWarehouseExecution
        SET Status = CASE WHEN @Inserted + @Revised = 0 THEN 'SKIPPED' ELSE 'COMPLETED' END,
            InsertedSuppliers = @Inserted,
            RevisedSuppliers = @Revised,
            UnchangedSuppliers = @Unchanged,
            CompletedAt = SYSUTCDATETIME()
        WHERE ExecutionID = @ExecutionID;

        COMMIT TRANSACTION;

        SELECT @ExecutionID AS ExecutionID, @SourceRows AS SourceRows,
               @Inserted AS InsertedSuppliers, @Revised AS RevisedSuppliers,
               @Unchanged AS UnchangedSuppliers;
    END TRY
    BEGIN CATCH
        DECLARE @ErrorMessage NVARCHAR(4000) = ERROR_MESSAGE();
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        INSERT audit.SupplierWarehouseExecution (
            IngestionRunID, Status, CompletedAt, ErrorMessage
        ) VALUES (@IngestionRunID, 'FAILED', SYSUTCDATETIME(), @ErrorMessage);
        THROW;
    END CATCH
END;
GO
