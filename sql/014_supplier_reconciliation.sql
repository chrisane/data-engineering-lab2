USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
IF OBJECT_ID(N'audit.ReconciliationResult', N'U') IS NULL
BEGIN
    CREATE TABLE audit.ReconciliationResult (
        ReconciliationResultID BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        CheckedAt DATETIME2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        CheckCode VARCHAR(80) NOT NULL,
        CheckStatus VARCHAR(10) NOT NULL,
        ObservedValue BIGINT NULL,
        ExpectedValue BIGINT NULL,
        Details NVARCHAR(1000) NULL,
        CONSTRAINT CK_ReconciliationResult_Status CHECK (CheckStatus IN ('PASS','FAIL'))
    );
    CREATE INDEX IX_ReconciliationResult_Batch ON audit.ReconciliationResult(IngestionRunID,SourceFileHash,CheckedAt DESC);
END;
GO
CREATE OR ALTER PROCEDURE audit.usp_ReconcileSupplier
    @IngestionRunID VARCHAR(40),
    @SourceFileHash CHAR(64),
    @FailOnError BIT = 1
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @Results TABLE (CheckCode VARCHAR(80), CheckStatus VARCHAR(10), ObservedValue BIGINT, ExpectedValue BIGINT, Details NVARCHAR(1000));
    DECLARE @Rows BIGINT, @DistinctRows BIGINT, @MissingCurrent BIGINT, @InvalidCurrent BIGINT, @Overlaps BIGINT, @MissingLineage BIGINT;

    SELECT @Rows=COUNT_BIG(*), @DistinctRows=COUNT(DISTINCT supplier_id)
    FROM stg.Supplier
    WHERE IngestionRunID=@IngestionRunID AND SourceFileHash=@SourceFileHash;

    IF @Rows=0
        THROW 51301, 'No staged Supplier rows for specified ingestion run and source hash.', 1;

    INSERT @Results VALUES ('STAGING_BUSINESS_KEY_UNIQUE', IIF(@Rows=@DistinctRows,'PASS','FAIL'), @DistinctRows,@Rows,
        N'Distinct non-null supplier_id count must equal staged row count.');

    SELECT @MissingCurrent=COUNT_BIG(*)
    FROM stg.Supplier s
    WHERE s.IngestionRunID=@IngestionRunID AND s.SourceFileHash=@SourceFileHash
      AND NOT EXISTS (SELECT 1 FROM dw.DimSupplier d WHERE d.SupplierID=s.supplier_id AND d.IsCurrent=1);
    INSERT @Results VALUES ('WAREHOUSE_KEY_COVERAGE', IIF(@MissingCurrent=0,'PASS','FAIL'), @MissingCurrent,0,
        N'Number of staged rows without a current warehouse supplier.');

    SELECT @InvalidCurrent=COUNT_BIG(*)
    FROM (SELECT SupplierID FROM dw.DimSupplier GROUP BY SupplierID
          HAVING SUM(CASE WHEN IsCurrent=1 THEN 1 ELSE 0 END)<>1) x;
    INSERT @Results VALUES ('SCD2_ONE_CURRENT', IIF(@InvalidCurrent=0,'PASS','FAIL'),@InvalidCurrent,0,
        N'Number of warehouse SupplierIDs without exactly one current row.');

    SELECT @Overlaps=COUNT_BIG(*)
    FROM (SELECT SupplierID, EffectiveFrom, EffectiveTo,
                 LAG(EffectiveTo) OVER (PARTITION BY SupplierID ORDER BY EffectiveFrom, RevisionNumber) AS PreviousTo,
                 ROW_NUMBER() OVER (PARTITION BY SupplierID ORDER BY EffectiveFrom, RevisionNumber) AS rn
          FROM dw.DimSupplier) h
    WHERE h.rn>1 AND (h.PreviousTo IS NULL OR h.PreviousTo>h.EffectiveFrom);
    INSERT @Results VALUES ('SCD2_NO_OVERLAP', IIF(@Overlaps=0,'PASS','FAIL'),@Overlaps,0,
        N'Number of overlapping or unclosed historical revision boundaries.');

    SELECT @MissingLineage=COUNT_BIG(*)
    FROM stg.Supplier s
    WHERE s.IngestionRunID=@IngestionRunID AND s.SourceFileHash=@SourceFileHash
      AND (s.SourceFile IS NULL OR LTRIM(RTRIM(s.SourceFile))='' OR s.SourceRowNumber IS NULL OR s.SourceRowNumber<2);
    INSERT @Results VALUES ('STAGING_LINEAGE_FIELDS', IIF(@MissingLineage=0,'PASS','FAIL'),@MissingLineage,0,
        N'Rows lacking file name or original worksheet row number.');

    -- Warehouse coverage is not equality of row counts: SCD2 retains historical revisions.
    -- Original workbook row-count comparison requires a separately persisted source-row count.
    INSERT audit.ReconciliationResult (IngestionRunID,SourceFileHash,CheckCode,CheckStatus,ObservedValue,ExpectedValue,Details)
    SELECT @IngestionRunID,@SourceFileHash,CheckCode,CheckStatus,ObservedValue,ExpectedValue,Details FROM @Results;

    SELECT CheckCode,CheckStatus,ObservedValue,ExpectedValue,Details FROM @Results ORDER BY CheckCode;
    IF @FailOnError=1 AND EXISTS (SELECT 1 FROM @Results WHERE CheckStatus='FAIL')
        THROW 51302, 'Supplier reconciliation failed. See audit.ReconciliationResult.', 1;
END;
GO
-- Replace values below with the exact batch ID and full 64-character hash from stg.Supplier.
SELECT TOP (10) IngestionRunID, SourceFileHash, COUNT_BIG(*) AS StagedRows
FROM stg.Supplier
GROUP BY IngestionRunID,SourceFileHash
ORDER BY IngestionRunID DESC;
GO
