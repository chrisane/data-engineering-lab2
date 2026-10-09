USE RetailDataPlatform;
GO

-- Step 10: Field-level audit for supplier SCD2 revisions.
-- Execute after 007_create_dim_supplier.sql and 008_supplier_scd2.sql.
IF OBJECT_ID(N'audit.SupplierChangeLog', N'U') IS NULL
BEGIN
    CREATE TABLE audit.SupplierChangeLog (
        ChangeLogID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_SupplierChangeLog PRIMARY KEY,
        ExecutionID BIGINT NOT NULL,
        SupplierID VARCHAR(20) NOT NULL,
        PreviousSupplierKey BIGINT NOT NULL,
        PreviousRevisionNumber INT NOT NULL,
        NewRevisionNumber INT NOT NULL,
        FieldName NVARCHAR(128) NOT NULL,
        OldValue NVARCHAR(4000) NULL,
        NewValue NVARCHAR(4000) NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFile NVARCHAR(500) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,
        ChangedAt DATETIME2(7) NOT NULL,
        CONSTRAINT FK_SupplierChangeLog_Execution FOREIGN KEY (ExecutionID)
            REFERENCES audit.SupplierWarehouseExecution(ExecutionID),
        CONSTRAINT FK_SupplierChangeLog_PreviousVersion FOREIGN KEY (PreviousSupplierKey)
            REFERENCES dw.DimSupplier(SupplierKey),
        CONSTRAINT UQ_SupplierChangeLog_Field UNIQUE (ExecutionID, SupplierID, FieldName),
        CONSTRAINT CK_SupplierChangeLog_Revision CHECK (NewRevisionNumber = PreviousRevisionNumber + 1)
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

        -- Record field-level differences BEFORE closing the old dimension versions.
        -- Store all changes in the same transaction as the dimension update.
        INSERT audit.SupplierChangeLog (
            ExecutionID, SupplierID, PreviousSupplierKey, PreviousRevisionNumber,
            NewRevisionNumber, FieldName, OldValue, NewValue,
            IngestionRunID, SourceFile, SourceFileHash, SourceRowNumber, ChangedAt
        )
        SELECT @ExecutionID, c.supplier_id, d.SupplierKey, d.RevisionNumber,
               d.RevisionNumber + 1, v.FieldName, v.OldValue, v.NewValue,
               c.IngestionRunID, c.SourceFile, c.SourceFileHash,
               c.SourceRowNumber, @AsOf
        FROM #Changes AS c
        INNER JOIN dw.DimSupplier AS d ON d.SupplierKey = c.CurrentSupplierKey
        CROSS APPLY (VALUES
            (N'SupplierName', CONVERT(NVARCHAR(4000), d.SupplierName), CONVERT(NVARCHAR(4000), c.supplier_name)),
            (N'Location', CONVERT(NVARCHAR(4000), d.Location), CONVERT(NVARCHAR(4000), c.location)),
            (N'MinimumOrderValueNAD', CONVERT(NVARCHAR(4000), d.MinimumOrderValueNAD), CONVERT(NVARCHAR(4000), c.minimum_order_value_nad)),
            (N'RebateRate', CONVERT(NVARCHAR(4000), d.RebateRate), CONVERT(NVARCHAR(4000), c.rebate_rate)),
            (N'PaymentTermsDays', CONVERT(NVARCHAR(4000), d.PaymentTermsDays), CONVERT(NVARCHAR(4000), c.payment_terms_days))
        ) AS v(FieldName, OldValue, NewValue)
        WHERE c.ActionType = 'UPDATE'
          AND EXISTS (SELECT v.OldValue EXCEPT SELECT v.NewValue);

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

-- Backfill previously observed changes (such as SUP0001 revision 2).
-- Only reconstruct values where both consecutive versions exist and the
-- original successful warehouse execution can be identified unambiguously.
-- These are reconstructed audit entries, not proof that the logging mechanism
-- was active during the original execution.
;WITH Pairs AS (
    SELECT newv.SupplierKey AS NewSupplierKey,
           oldv.SupplierKey AS PreviousSupplierKey,
           oldv.RevisionNumber AS PreviousRevisionNumber,
           newv.RevisionNumber AS NewRevisionNumber,
           newv.SupplierID,
           newv.IngestionRunID,
           newv.SourceFile,
           newv.SourceFileHash,
           newv.SourceRowNumber,
           newv.EffectiveFrom AS ChangedAt,
           oldv.SupplierName AS OldSupplierName, newv.SupplierName AS NewSupplierName,
           oldv.Location AS OldLocation, newv.Location AS NewLocation,
           oldv.MinimumOrderValueNAD AS OldMinimum, newv.MinimumOrderValueNAD AS NewMinimum,
           oldv.RebateRate AS OldRebate, newv.RebateRate AS NewRebate,
           oldv.PaymentTermsDays AS OldTerms, newv.PaymentTermsDays AS NewTerms
    FROM dw.DimSupplier AS newv
    JOIN dw.DimSupplier AS oldv
      ON oldv.SupplierID = newv.SupplierID
     AND oldv.RevisionNumber = newv.RevisionNumber - 1
    WHERE newv.ChangeType = 'UPDATE'
), UniqueExecution AS (
    SELECT IngestionRunID, MIN(ExecutionID) AS ExecutionID
    FROM audit.SupplierWarehouseExecution
    WHERE Status = 'COMPLETED'
    GROUP BY IngestionRunID
    HAVING COUNT(*) = 1
)
INSERT audit.SupplierChangeLog (
    ExecutionID, SupplierID, PreviousSupplierKey, PreviousRevisionNumber,
    NewRevisionNumber, FieldName, OldValue, NewValue,
    IngestionRunID, SourceFile, SourceFileHash, SourceRowNumber, ChangedAt
)
SELECT e.ExecutionID, p.SupplierID, p.PreviousSupplierKey,
       p.PreviousRevisionNumber, p.NewRevisionNumber,
       v.FieldName, v.OldValue, v.NewValue,
       p.IngestionRunID, p.SourceFile, p.SourceFileHash,
       p.SourceRowNumber, p.ChangedAt
FROM Pairs AS p
JOIN UniqueExecution AS e ON e.IngestionRunID = p.IngestionRunID
CROSS APPLY (VALUES
    (N'SupplierName', CONVERT(NVARCHAR(4000), p.OldSupplierName), CONVERT(NVARCHAR(4000), p.NewSupplierName)),
    (N'Location', CONVERT(NVARCHAR(4000), p.OldLocation), CONVERT(NVARCHAR(4000), p.NewLocation)),
    (N'MinimumOrderValueNAD', CONVERT(NVARCHAR(4000), p.OldMinimum), CONVERT(NVARCHAR(4000), p.NewMinimum)),
    (N'RebateRate', CONVERT(NVARCHAR(4000), p.OldRebate), CONVERT(NVARCHAR(4000), p.NewRebate)),
    (N'PaymentTermsDays', CONVERT(NVARCHAR(4000), p.OldTerms), CONVERT(NVARCHAR(4000), p.NewTerms))
) AS v(FieldName, OldValue, NewValue)
WHERE EXISTS (SELECT v.OldValue EXCEPT SELECT v.NewValue)
  AND NOT EXISTS (
      SELECT 1 FROM audit.SupplierChangeLog AS log
      WHERE log.ExecutionID = e.ExecutionID
        AND log.SupplierID = p.SupplierID
        AND log.FieldName = v.FieldName
  );
GO
