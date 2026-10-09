USE RetailDataPlatform;
GO

-- 1. SOURCE REGISTRY
CREATE TABLE meta.DataSource (
    SourceID INT IDENTITY(1,1) PRIMARY KEY,
    SourceName NVARCHAR(100) NOT NULL UNIQUE,
    SourceType NVARCHAR(50) NOT NULL,
    Description NVARCHAR(500) NULL,
    IsActive BIT NOT NULL DEFAULT 1,
    CreatedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

-- 2. DATA ENTITY
CREATE TABLE meta.DataEntity (
    EntityID INT IDENTITY(1,1) PRIMARY KEY,
    SourceID INT NOT NULL,
    EntityName NVARCHAR(100) NOT NULL,
    BusinessDefinition NVARCHAR(1000) NOT NULL,
    BusinessDomain NVARCHAR(100) NULL,
    CreatedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT FK_DataEntity_Source
        FOREIGN KEY (SourceID)
        REFERENCES meta.DataSource(SourceID),

    CONSTRAINT UQ_DataEntity_Source_Name
        UNIQUE (SourceID, EntityName)
);
GO

-- 3. DATA DICTIONARY
CREATE TABLE meta.DataElement (
    ElementID INT IDENTITY(1,1) PRIMARY KEY,
    EntityID INT NOT NULL,
    ElementName NVARCHAR(128) NOT NULL,
    BusinessDefinition NVARCHAR(1000) NOT NULL,
    DataType NVARCHAR(100) NOT NULL,
    IsNullable BIT NOT NULL,
    IsBusinessKey BIT NOT NULL DEFAULT 0,
    DataClassification NVARCHAR(50) NOT NULL DEFAULT 'INTERNAL',
    ValidationRule NVARCHAR(1000) NULL,
    CreatedAt DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),

    CONSTRAINT FK_DataElement_Entity
        FOREIGN KEY (EntityID)
        REFERENCES meta.DataEntity(EntityID),

    CONSTRAINT UQ_DataElement_Entity_Name
        UNIQUE (EntityID, ElementName),

    CONSTRAINT CK_DataElement_Classification
        CHECK (DataClassification IN (
            'PUBLIC',
            'INTERNAL',
            'CONFIDENTIAL',
            'RESTRICTED'
        ))
);
GO