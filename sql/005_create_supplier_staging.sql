USE RetailDataPlatform;
GO

IF OBJECT_ID(N'stg.Supplier', N'U') IS NULL
BEGIN
    CREATE TABLE stg.Supplier (
        StagingID BIGINT IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_stg_Supplier PRIMARY KEY,

        -- Source data
        supplier_id VARCHAR(20) NOT NULL,
        supplier_name NVARCHAR(200) NOT NULL,
        location NVARCHAR(100) NULL,
        minimum_order_value_nad DECIMAL(18,2) NOT NULL,
        rebate_rate DECIMAL(9,6) NULL,
        payment_terms_days INT NULL,

        -- Lineage and operational metadata
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFile NVARCHAR(500) NOT NULL,
        SourceRowNumber INT NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        LoadedAt DATETIME2 NOT NULL
            CONSTRAINT DF_stg_Supplier_LoadedAt
            DEFAULT SYSUTCDATETIME(),

        -- Basic integrity checks
        CONSTRAINT CK_stg_Supplier_MinimumOrder
            CHECK (minimum_order_value_nad >= 0),

        CONSTRAINT CK_stg_Supplier_Rebate
            CHECK (rebate_rate BETWEEN 0 AND 1),

        CONSTRAINT CK_stg_Supplier_PaymentTerms
            CHECK (payment_terms_days >= 0),

        CONSTRAINT CK_stg_Supplier_RowNumber
            CHECK (SourceRowNumber >= 2),

        CONSTRAINT UQ_stg_Supplier_SourceRow
            UNIQUE (
                IngestionRunID,
                SourceFileHash,
                SourceRowNumber
            )
    );
END;
GO