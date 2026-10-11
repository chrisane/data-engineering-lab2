USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    028 - Cash-up template version.

    Branch cash-up PDFs can now arrive on more than one registered layout
    at the same time (extraction.templates in config/source_registry.yaml),
    so branches can move to a new template at different times. Fields keep
    their names whatever the labels, so only one column is added: which
    template each document was read with, for traceability.

    Nullable: rows staged before this migration were all read with
    template 1 but are not rewritten (staging history is kept as loaded).
*/

IF COL_LENGTH(N'stg.BranchCashup', N'template_version') IS NULL
BEGIN
    ALTER TABLE stg.BranchCashup ADD [template_version] NVARCHAR(50) NULL;
END;
GO
