USE RetailDataPlatform;
GO

DECLARE @EntityID INT;

SELECT @EntityID = e.EntityID
FROM meta.DataEntity e
INNER JOIN meta.DataSource s
    ON e.SourceID = s.SourceID
WHERE s.SourceName = 'supplier_master'
  AND e.EntityName = 'Supplier';

IF @EntityID IS NULL
    THROW 50001, 'Supplier metadata entity not found.', 1;

DECLARE @Fields TABLE (
    ElementName NVARCHAR(128),
    BusinessDefinition NVARCHAR(1000),
    DataType NVARCHAR(100),
    IsNullable BIT,
    IsBusinessKey BIT,
    DataClassification NVARCHAR(50),
    ValidationRule NVARCHAR(1000)
);

INSERT INTO @Fields VALUES
('supplier_id',
 'Unique identifier assigned to a supplier.',
 'VARCHAR(20)', 0, 1, 'INTERNAL',
 'Required and unique.'),

('supplier_name',
 'Name of the supplier.',
 'NVARCHAR(200)', 0, 0, 'INTERNAL',
 'Required.'),

('location',
 'Geographical location of the supplier.',
 'NVARCHAR(100)', 1, 0, 'INTERNAL',
 NULL),

('minimum_order_value_nad',
 'Minimum monetary value permitted for supplier purchase orders, in NAD.',
 'DECIMAL(18,2)', 0, 0, 'INTERNAL',
 'Required; value must be greater than or equal to zero.'),

('rebate_rate',
 'Supplier rebate rate represented as a decimal fraction.',
 'DECIMAL(9,6)', 1, 0, 'INTERNAL',
 'If provided, value must be between 0 and 1.'),

('payment_terms_days',
 'Number of days allowed for supplier payment.',
 'INT', 1, 0, 'INTERNAL',
 'If provided, value must be greater than or equal to zero.');

INSERT INTO meta.DataElement (
    EntityID,
    ElementName,
    BusinessDefinition,
    DataType,
    IsNullable,
    IsBusinessKey,
    DataClassification,
    ValidationRule
)
SELECT
    @EntityID,
    f.ElementName,
    f.BusinessDefinition,
    f.DataType,
    f.IsNullable,
    f.IsBusinessKey,
    f.DataClassification,
    f.ValidationRule
FROM @Fields f
WHERE NOT EXISTS (
    SELECT 1
    FROM meta.DataElement d
    WHERE d.EntityID = @EntityID
      AND d.ElementName = f.ElementName
);
GO