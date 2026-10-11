USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    025 - Access control and protection of personal data.

    Roles (grant to Windows groups or users; no logins are created here):
      role_report_reader   SELECT on the presentation layer (mart) only
      role_data_steward    + metadata catalogue, audit and data-quality results,
                           and UNMASK (sees personal data to resolve issues)
      role_data_engineer   + staging and warehouse, EXECUTE on load procedures

    Dynamic data masking hides personal data (classification RESTRICTED /
    CONFIDENTIAL in the registry) from anyone without UNMASK, including
    through the mart views:
      dw.DimCustomer.CustomerName, dw.DimCustomer.Town
      dw.DimLocalAsset.AssignedTo
      dw.FactBranchCashup.Cashier
    Masking is a presentation control, not encryption: it does not stop a
    privileged user (db_owner) from reading the data.

    Example grants:
      ALTER ROLE role_report_reader ADD MEMBER [DOMAIN\Retail-Analysts];
*/

IF DATABASE_PRINCIPAL_ID(N'role_report_reader') IS NULL CREATE ROLE role_report_reader;
IF DATABASE_PRINCIPAL_ID(N'role_data_steward') IS NULL CREATE ROLE role_data_steward;
IF DATABASE_PRINCIPAL_ID(N'role_data_engineer') IS NULL CREATE ROLE role_data_engineer;
GO

GRANT SELECT ON SCHEMA::mart TO role_report_reader;

GRANT SELECT ON SCHEMA::mart TO role_data_steward;
GRANT SELECT ON SCHEMA::meta TO role_data_steward;
GRANT SELECT ON SCHEMA::audit TO role_data_steward;
GRANT UNMASK TO role_data_steward;

GRANT SELECT ON SCHEMA::mart TO role_data_engineer;
GRANT SELECT ON SCHEMA::meta TO role_data_engineer;
GRANT SELECT ON SCHEMA::audit TO role_data_engineer;
GRANT SELECT ON SCHEMA::stg TO role_data_engineer;
GRANT SELECT ON SCHEMA::dw TO role_data_engineer;
GRANT EXECUTE ON SCHEMA::dw TO role_data_engineer;
GRANT EXECUTE ON SCHEMA::audit TO role_data_engineer;
GRANT UNMASK TO role_data_engineer;
GO

-- Personal data masking ------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.masked_columns WHERE object_id = OBJECT_ID(N'dw.DimCustomer') AND name = N'CustomerName')
    ALTER TABLE dw.DimCustomer ALTER COLUMN CustomerName ADD MASKED WITH (FUNCTION = 'partial(1, "xxxx", 0)');

IF NOT EXISTS (SELECT 1 FROM sys.masked_columns WHERE object_id = OBJECT_ID(N'dw.DimCustomer') AND name = N'Town')
    ALTER TABLE dw.DimCustomer ALTER COLUMN Town ADD MASKED WITH (FUNCTION = 'default()');

IF NOT EXISTS (SELECT 1 FROM sys.masked_columns WHERE object_id = OBJECT_ID(N'dw.DimLocalAsset') AND name = N'AssignedTo')
    ALTER TABLE dw.DimLocalAsset ALTER COLUMN AssignedTo ADD MASKED WITH (FUNCTION = 'partial(1, "xxxx", 0)');

IF NOT EXISTS (SELECT 1 FROM sys.masked_columns WHERE object_id = OBJECT_ID(N'dw.FactBranchCashup') AND name = N'Cashier')
    ALTER TABLE dw.FactBranchCashup ALTER COLUMN Cashier ADD MASKED WITH (FUNCTION = 'partial(1, "xxxx", 0)');
GO
