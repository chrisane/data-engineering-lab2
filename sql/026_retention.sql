USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    026 - Data retention audit.

    Staging keeps the latest batches per table (pipeline.yaml
    staging_batches_retained). Older batches are archived to Parquet
    (data/archive/staging/) with a SHA-256 checksum and only then removed,
    by scripts/apply_retention.py --apply. Every action is recorded here.
    Warehouse history and RAW evidence are never purged automatically.
*/

IF OBJECT_ID(N'audit.RetentionAction', N'U') IS NULL
BEGIN
    CREATE TABLE audit.RetentionAction (
        RetentionActionID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_RetentionAction PRIMARY KEY,
        ActionedAt DATETIME2(7) NOT NULL CONSTRAINT DF_RetentionAction_At DEFAULT SYSUTCDATETIME(),
        ActionedBy NVARCHAR(128) NOT NULL CONSTRAINT DF_RetentionAction_By DEFAULT SUSER_SNAME(),
        ObjectName NVARCHAR(256) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceFileHash CHAR(64) NOT NULL,
        RowsArchived BIGINT NOT NULL,
        RowsDeleted BIGINT NOT NULL,
        ArchivePath NVARCHAR(500) NOT NULL,
        ArchiveChecksum CHAR(64) NOT NULL,
        Reason NVARCHAR(500) NOT NULL
    );
END;
GO
