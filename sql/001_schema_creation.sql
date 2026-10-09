USE master;
GO

IF DB_ID(N'RetailDataPlatform') IS NULL
BEGIN
    CREATE DATABASE RetailDataPlatform;
END;
GO

USE RetailDataPlatform;
GO

IF SCHEMA_ID(N'meta') IS NULL
    EXEC(N'CREATE SCHEMA meta');
GO

IF SCHEMA_ID(N'audit') IS NULL
    EXEC(N'CREATE SCHEMA audit');
GO

IF SCHEMA_ID(N'stg') IS NULL
    EXEC(N'CREATE SCHEMA stg');
GO

IF SCHEMA_ID(N'dw') IS NULL
    EXEC(N'CREATE SCHEMA dw');
GO

IF SCHEMA_ID(N'mart') IS NULL
    EXEC(N'CREATE SCHEMA mart');
GO

SELECT
    name AS SchemaName
FROM sys.schemas
WHERE name IN (
    'meta',
    'audit',
    'stg',
    'dw',
    'mart'
)
ORDER BY name;