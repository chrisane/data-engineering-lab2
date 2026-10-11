USE RetailDataPlatform;
GO

IF NOT EXISTS (
    SELECT 1
    FROM sys.fn_listextendedproperty(
        N'MS_Description', N'SCHEMA', N'stg', N'TABLE', N'Supplier', NULL, NULL
    )
)
BEGIN
    EXEC sys.sp_addextendedproperty
        @name = N'MS_Description',
        @value = N'Staging table containing validated supplier records, preserving source-file and ingestion-batch lineage.',
        @level0type = N'SCHEMA',
        @level0name = N'stg',
        @level1type = N'TABLE',
        @level1name = N'Supplier';
END;
GO
