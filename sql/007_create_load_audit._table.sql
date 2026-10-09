USE RetailDataPlatform;
GO

IF OBJECT_ID(N'audit.LoadExecution', N'U') IS NULL
BEGIN
    CREATE TABLE audit.LoadExecution (
        LoadID BIGINT IDENTITY(1,1) PRIMARY KEY,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceName NVARCHAR(100) NOT NULL,
        SourceFile NVARCHAR(500) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,

        LoadStatus VARCHAR(30) NOT NULL,
        RowsRead INT NOT NULL DEFAULT 0,
        RowsInserted INT NOT NULL DEFAULT 0,
        RowsRejected INT NOT NULL DEFAULT 0,

        StartedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CompletedAt DATETIME2 NULL,
        ErrorMessage NVARCHAR(MAX) NULL,

        CONSTRAINT CK_LoadExecution_Status
            CHECK (LoadStatus IN (
                'STARTED',
                'COMPLETED',
                'FAILED',
                'SKIPPED'
            ))
    );
END;
GO