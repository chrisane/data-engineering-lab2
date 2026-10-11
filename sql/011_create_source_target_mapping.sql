USE RetailDataPlatform;
GO

IF OBJECT_ID(N'meta.SourceTargetMapping', N'U') IS NULL
BEGIN
    CREATE TABLE meta.SourceTargetMapping (
        MappingID INT IDENTITY(1,1) PRIMARY KEY,

        SourceID INT NOT NULL,
        TargetSchema SYSNAME NOT NULL,
        TargetTable SYSNAME NOT NULL,

        LoadLayer VARCHAR(20) NOT NULL,
        LoadStrategy VARCHAR(30) NOT NULL,

        ExecutionOrder INT NOT NULL DEFAULT 100,
        IsEnabled BIT NOT NULL DEFAULT 0,

        CreatedAt DATETIME2 NOT NULL
            DEFAULT SYSUTCDATETIME(),

        CONSTRAINT FK_SourceTargetMapping_Source
            FOREIGN KEY (SourceID)
            REFERENCES meta.DataSource(SourceID),

        CONSTRAINT CK_SourceTargetMapping_Layer
            CHECK (LoadLayer IN ('STAGING', 'WAREHOUSE', 'MART')),

        CONSTRAINT CK_SourceTargetMapping_Strategy
            CHECK (LoadStrategy IN (
                'APPEND', 'UPSERT', 'SCD1', 'SCD2', 'FULL_REFRESH'
            )),

        CONSTRAINT UQ_SourceTargetMapping
            UNIQUE (SourceID, TargetSchema, TargetTable)
    );
END;
GO