USE RetailDataPlatform;
GO

IF OBJECT_ID(N'dw.DimSupplier', N'U') IS NULL
BEGIN
    CREATE TABLE dw.DimSupplier (
        SupplierKey BIGINT IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_DimSupplier PRIMARY KEY,

        SupplierID VARCHAR(20) NOT NULL,
        SupplierName NVARCHAR(200) NOT NULL,
        Location NVARCHAR(100) NULL,
        MinimumOrderValueNAD DECIMAL(18,2) NOT NULL,
        RebateRate DECIMAL(9,6) NULL,
        PaymentTermsDays INT NULL,

        -- Business-effective history
        EffectiveFrom DATETIME2(7) NOT NULL,
        EffectiveTo DATETIME2(7) NULL,
        IsCurrent BIT NOT NULL,

        -- Revision tracking
        RevisionNumber INT NOT NULL,
        ChangeType VARCHAR(20) NOT NULL,
        ChangeReason NVARCHAR(500) NULL,

        -- Source lineage
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFile NVARCHAR(500) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        SourceRowNumber INT NOT NULL,

        -- System-recorded history
        RecordedAt DATETIME2(7) NOT NULL
            CONSTRAINT DF_DimSupplier_RecordedAt
            DEFAULT SYSUTCDATETIME(),

        RecordedBy NVARCHAR(128) NOT NULL
            CONSTRAINT DF_DimSupplier_RecordedBy
            DEFAULT SUSER_SNAME(),

        CONSTRAINT UQ_DimSupplier_Revision
            UNIQUE (SupplierID, RevisionNumber),

        CONSTRAINT CK_DimSupplier_Revision
            CHECK (RevisionNumber >= 1),

        CONSTRAINT CK_DimSupplier_ChangeType
            CHECK (ChangeType IN (
                'INITIAL', 'UPDATE', 'CORRECTION'
            )),

        CONSTRAINT CK_DimSupplier_EffectiveDates
            CHECK (
                EffectiveTo IS NULL
                OR EffectiveTo > EffectiveFrom
            ),

        CONSTRAINT CK_DimSupplier_CurrentDates
            CHECK (
                (IsCurrent = 1 AND EffectiveTo IS NULL)
                OR
                (IsCurrent = 0 AND EffectiveTo IS NOT NULL)
            )
    );
END;
GO

-- Only one current revision per supplier
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dw.DimSupplier')
      AND name = 'UX_DimSupplier_Current'
)
BEGIN
    CREATE UNIQUE INDEX UX_DimSupplier_Current
    ON dw.DimSupplier(SupplierID)
    WHERE IsCurrent = 1;
END;
GO