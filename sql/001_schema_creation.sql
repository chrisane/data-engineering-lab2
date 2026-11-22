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

IF SCHEMA_ID(N'silver') IS NULL
    EXEC(N'CREATE SCHEMA silver');
GO

IF SCHEMA_ID(N'gold') IS NULL
    EXEC(N'CREATE SCHEMA gold');
GO

IF SCHEMA_ID(N'presentation') IS NULL
    EXEC(N'CREATE SCHEMA presentation');
GO

SELECT
    name AS SchemaName
FROM sys.schemas
WHERE name IN (
    'meta',
    'audit',
    'silver',
    'gold',
    'presentation'
)
ORDER BY name;