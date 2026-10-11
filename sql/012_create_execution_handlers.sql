USE RetailDataPlatform;
GO

IF OBJECT_ID(N'meta.ExecutionHandler', N'U') IS NULL
BEGIN
    CREATE TABLE meta.ExecutionHandler (
        HandlerID INT IDENTITY(1,1) PRIMARY KEY,
        HandlerCode VARCHAR(100) NOT NULL UNIQUE,
        HandlerType VARCHAR(30) NOT NULL,
        Description NVARCHAR(500) NULL,
        IsApproved BIT NOT NULL DEFAULT 0,
        IsActive BIT NOT NULL DEFAULT 1,
        CreatedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),

        CONSTRAINT CK_ExecutionHandler_Type
            CHECK (HandlerType IN ('PYTHON', 'STORED_PROCEDURE'))
    );
END;
GO

IF OBJECT_ID(N'meta.MappingExecution', N'U') IS NULL
BEGIN
    CREATE TABLE meta.MappingExecution (
        MappingID INT NOT NULL PRIMARY KEY,
        HandlerID INT NOT NULL,
        StopOnFailure BIT NOT NULL DEFAULT 1,

        CONSTRAINT FK_MappingExecution_Mapping
            FOREIGN KEY (MappingID)
            REFERENCES meta.SourceTargetMapping(MappingID),

        CONSTRAINT FK_MappingExecution_Handler
            FOREIGN KEY (HandlerID)
            REFERENCES meta.ExecutionHandler(HandlerID)
    );
END;
GO

/*
    Supplier mappings and approved handlers.
    These rows were first created manually; they are seeded here so a
    database built from the migrations alone matches the original.
    Every statement is idempotent.
*/
DECLARE @SupplierSourceID INT =
    (SELECT SourceID FROM meta.DataSource WHERE SourceName = 'supplier_master');

IF @SupplierSourceID IS NULL
    THROW 51200, 'supplier_master must be registered (003) before its mappings.', 1;

IF NOT EXISTS (SELECT 1 FROM meta.ExecutionHandler WHERE HandlerCode = 'SUPPLIER_STAGING')
    INSERT INTO meta.ExecutionHandler (HandlerCode, HandlerType, Description, IsApproved, IsActive)
    VALUES ('SUPPLIER_STAGING', 'PYTHON', N'Validated Supplier Excel to SQL staging loader', 1, 1);

IF NOT EXISTS (SELECT 1 FROM meta.ExecutionHandler WHERE HandlerCode = 'SUPPLIER_SCD2')
    INSERT INTO meta.ExecutionHandler (HandlerCode, HandlerType, Description, IsApproved, IsActive)
    VALUES ('SUPPLIER_SCD2', 'PYTHON', N'Supplier SCD Type 2 warehouse transformation runner', 1, 1);

IF NOT EXISTS (SELECT 1 FROM meta.SourceTargetMapping
               WHERE SourceID = @SupplierSourceID AND TargetSchema = 'stg' AND TargetTable = 'Supplier')
    INSERT INTO meta.SourceTargetMapping
        (SourceID, TargetSchema, TargetTable, LoadLayer, LoadStrategy, ExecutionOrder, IsEnabled)
    VALUES (@SupplierSourceID, 'stg', 'Supplier', 'STAGING', 'APPEND', 10, 1);

IF NOT EXISTS (SELECT 1 FROM meta.SourceTargetMapping
               WHERE SourceID = @SupplierSourceID AND TargetSchema = 'dw' AND TargetTable = 'DimSupplier')
    INSERT INTO meta.SourceTargetMapping
        (SourceID, TargetSchema, TargetTable, LoadLayer, LoadStrategy, ExecutionOrder, IsEnabled)
    VALUES (@SupplierSourceID, 'dw', 'DimSupplier', 'WAREHOUSE', 'SCD2', 20, 1);

INSERT INTO meta.MappingExecution (MappingID, HandlerID, StopOnFailure)
SELECT m.MappingID, h.HandlerID, 1
FROM meta.SourceTargetMapping AS m
JOIN (VALUES ('stg', 'Supplier', 'SUPPLIER_STAGING'),
             ('dw', 'DimSupplier', 'SUPPLIER_SCD2')) AS v(TargetSchema, TargetTable, HandlerCode)
  ON v.TargetSchema = m.TargetSchema AND v.TargetTable = m.TargetTable
JOIN meta.ExecutionHandler AS h ON h.HandlerCode = v.HandlerCode
WHERE m.SourceID = @SupplierSourceID
  AND NOT EXISTS (SELECT 1 FROM meta.MappingExecution AS e WHERE e.MappingID = m.MappingID);
GO
