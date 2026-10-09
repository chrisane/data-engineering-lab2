USE RetailDataPlatform;
GO

-- Register source
IF NOT EXISTS (
    SELECT 1 FROM meta.DataSource
    WHERE SourceName = 'supplier_master'
)
BEGIN
    INSERT INTO meta.DataSource (
        SourceName,
        SourceType,
        Description
    )
    VALUES (
        'supplier_master',
        'Excel',
        'Supplier master data received from the retail source system.'
    );
END;
GO

-- Register business entity
DECLARE @SourceID INT;

SELECT @SourceID = SourceID
FROM meta.DataSource
WHERE SourceName = 'supplier_master';

IF NOT EXISTS (
    SELECT 1 FROM meta.DataEntity
    WHERE SourceID = @SourceID
      AND EntityName = 'Supplier'
)
BEGIN
    INSERT INTO meta.DataEntity (
        SourceID,
        EntityName,
        BusinessDefinition,
        BusinessDomain
    )
    VALUES (
        @SourceID,
        'Supplier',
        'Master record of suppliers used for procurement, payment terms and rebates.',
        'Procurement'
    );
END;
GO