USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    015 - Operational governance and metadata catalogue (additive only).

    - Governance attributes on meta.DataSource (owner, steward, classification...)
    - Mapping codes, retry policy and descriptions on meta.SourceTargetMapping
    - RECONCILIATION handler type and meta.ReconciliationControl
    - Richer audit: pipeline heartbeat/abandonment, attempts, row counts,
      per-pipeline source batch selection, detailed execution errors,
      generic warehouse execution audit and fact correction log
    - Catalogue: business rules, column lineage, settings, metadata sync log
*/

-- ---------------------------------------------------------
-- meta.DataSource: governance and contract attributes
-- ---------------------------------------------------------
IF COL_LENGTH(N'meta.DataSource', N'FileFormat') IS NULL
    ALTER TABLE meta.DataSource ADD
        FileFormat VARCHAR(10) NULL,
        PathPattern NVARCHAR(260) NULL,
        StagingTable NVARCHAR(256) NULL,
        SystemOfRecord NVARCHAR(100) NULL,
        DataOwner NVARCHAR(200) NULL,
        DataSteward NVARCHAR(200) NULL,
        Classification VARCHAR(20) NULL,
        ContainsPersonalData BIT NULL,
        RetentionYears INT NULL,
        UpdateFrequency NVARCHAR(50) NULL,
        FreshnessThresholdHours INT NULL,
        IsCritical BIT NOT NULL CONSTRAINT DF_DataSource_IsCritical DEFAULT 0,
        UpdatedAt DATETIME2(7) NULL;
GO

-- ---------------------------------------------------------
-- audit.LoadExecution: link staging loads to the pipeline run
-- ---------------------------------------------------------
IF COL_LENGTH(N'audit.LoadExecution', N'PipelineExecutionID') IS NULL
    ALTER TABLE audit.LoadExecution ADD
        PipelineExecutionID BIGINT NULL,
        StagingTable NVARCHAR(256) NULL;
GO

-- ---------------------------------------------------------
-- meta.SourceTargetMapping: codes, retry policy, description
-- ---------------------------------------------------------
IF COL_LENGTH(N'meta.SourceTargetMapping', N'MappingCode') IS NULL
    ALTER TABLE meta.SourceTargetMapping ADD
        MappingCode VARCHAR(80) NULL,
        Description NVARCHAR(500) NULL,
        MaxRetries INT NOT NULL CONSTRAINT DF_SourceTargetMapping_MaxRetries DEFAULT 0,
        RetryDelaySeconds INT NOT NULL CONSTRAINT DF_SourceTargetMapping_RetryDelay DEFAULT 5,
        Schedule NVARCHAR(100) NULL,
        UpdatedAt DATETIME2(7) NULL;
GO

UPDATE m SET MappingCode = v.MappingCode
FROM meta.SourceTargetMapping AS m
JOIN (VALUES ('stg', 'Supplier', 'STG_SUPPLIER'),
             ('dw', 'DimSupplier', 'DW_DIM_SUPPLIER')) AS v(TargetSchema, TargetTable, MappingCode)
  ON v.TargetSchema = m.TargetSchema AND v.TargetTable = m.TargetTable
WHERE m.MappingCode IS NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID(N'meta.SourceTargetMapping') AND name = N'UX_SourceTargetMapping_Code')
    CREATE UNIQUE INDEX UX_SourceTargetMapping_Code
        ON meta.SourceTargetMapping(MappingCode) WHERE MappingCode IS NOT NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_SourceTargetMapping_Retry')
    ALTER TABLE meta.SourceTargetMapping ADD CONSTRAINT CK_SourceTargetMapping_Retry
        CHECK (MaxRetries BETWEEN 0 AND 5 AND RetryDelaySeconds BETWEEN 0 AND 300);
GO

-- ---------------------------------------------------------
-- Handlers: allow RECONCILIATION handlers, record implementation
-- ---------------------------------------------------------
IF COL_LENGTH(N'meta.ExecutionHandler', N'Implementation') IS NULL
    ALTER TABLE meta.ExecutionHandler ADD Implementation NVARCHAR(256) NULL;
GO

ALTER TABLE meta.ExecutionHandler DROP CONSTRAINT CK_ExecutionHandler_Type;
ALTER TABLE meta.ExecutionHandler ADD CONSTRAINT CK_ExecutionHandler_Type
    CHECK (HandlerType IN ('PYTHON', 'STORED_PROCEDURE', 'RECONCILIATION'));
GO

-- ---------------------------------------------------------
-- Reconciliation controls (post-load quality gates)
-- ---------------------------------------------------------
IF OBJECT_ID(N'meta.ReconciliationControl', N'U') IS NULL
BEGIN
    CREATE TABLE meta.ReconciliationControl (
        ControlID INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ReconciliationControl PRIMARY KEY,
        ControlCode VARCHAR(120) NOT NULL CONSTRAINT UQ_ReconciliationControl_Code UNIQUE,
        MappingID INT NOT NULL,
        HandlerID INT NOT NULL,
        Severity VARCHAR(10) NOT NULL,
        IsMandatory BIT NOT NULL CONSTRAINT DF_ReconciliationControl_Mandatory DEFAULT 1,
        Tolerance DECIMAL(19, 6) NULL,
        ToleranceType VARCHAR(10) NULL,
        ParametersJson NVARCHAR(MAX) NULL,
        Description NVARCHAR(500) NULL,
        IsEnabled BIT NOT NULL CONSTRAINT DF_ReconciliationControl_Enabled DEFAULT 1,
        CreatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_ReconciliationControl_CreatedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(7) NULL,
        CONSTRAINT FK_ReconciliationControl_Mapping FOREIGN KEY (MappingID)
            REFERENCES meta.SourceTargetMapping(MappingID),
        CONSTRAINT FK_ReconciliationControl_Handler FOREIGN KEY (HandlerID)
            REFERENCES meta.ExecutionHandler(HandlerID),
        CONSTRAINT CK_ReconciliationControl_Severity CHECK (Severity IN ('CRITICAL', 'ERROR', 'WARNING')),
        CONSTRAINT CK_ReconciliationControl_ToleranceType CHECK (ToleranceType IS NULL OR ToleranceType IN ('ABSOLUTE', 'PERCENT')),
        CONSTRAINT CK_ReconciliationControl_Tolerance CHECK (Tolerance IS NULL OR Tolerance >= 0),
        CONSTRAINT CK_ReconciliationControl_Json CHECK (ParametersJson IS NULL OR ISJSON(ParametersJson) = 1)
    );
END;
GO

IF COL_LENGTH(N'audit.ReconciliationResult', N'PipelineExecutionID') IS NULL
    ALTER TABLE audit.ReconciliationResult ADD
        PipelineExecutionID BIGINT NULL,
        MappingID INT NULL,
        ControlCode VARCHAR(120) NULL,
        Severity VARCHAR(10) NULL,
        SourceName NVARCHAR(100) NULL,
        TargetObject NVARCHAR(256) NULL,
        ObservedAmount DECIMAL(38, 6) NULL,
        ExpectedAmount DECIMAL(38, 6) NULL,
        Tolerance DECIMAL(19, 6) NULL,
        RecommendedAction NVARCHAR(1000) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE object_id = OBJECT_ID(N'audit.ReconciliationResult') AND name = N'IX_ReconciliationResult_Pipeline')
    CREATE INDEX IX_ReconciliationResult_Pipeline
        ON audit.ReconciliationResult(PipelineExecutionID, ControlCode);
GO

-- ---------------------------------------------------------
-- Pipeline and mapping execution audit
-- ---------------------------------------------------------
ALTER TABLE audit.PipelineExecution DROP CONSTRAINT CK_PipelineExecution_Status;
ALTER TABLE audit.PipelineExecution ADD CONSTRAINT CK_PipelineExecution_Status
    CHECK (ExecutionStatus IN ('STARTED', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED', 'ABANDONED'));
GO

IF COL_LENGTH(N'audit.PipelineExecution', N'HeartbeatAt') IS NULL
    ALTER TABLE audit.PipelineExecution ADD
        HeartbeatAt DATETIME2(7) NULL,
        TriggeredBy NVARCHAR(128) NOT NULL CONSTRAINT DF_PipelineExecution_TriggeredBy DEFAULT SUSER_SNAME(),
        HostName NVARCHAR(128) NULL CONSTRAINT DF_PipelineExecution_HostName DEFAULT HOST_NAME(),
        ConfigChecksum CHAR(64) NULL,
        MappingsPlanned INT NULL,
        MappingsCompleted INT NULL,
        MappingsFailed INT NULL,
        MappingsBlocked INT NULL;
GO

ALTER TABLE audit.MappingExecution DROP CONSTRAINT CK_AuditMappingExecution_Status;
ALTER TABLE audit.MappingExecution ADD CONSTRAINT CK_AuditMappingExecution_Status
    CHECK (ExecutionStatus IN ('STARTED', 'COMPLETED', 'FAILED', 'SKIPPED', 'BLOCKED', 'CANCELLED', 'ABANDONED'));
GO

IF COL_LENGTH(N'audit.MappingExecution', N'AttemptCount') IS NULL
    ALTER TABLE audit.MappingExecution ADD
        AttemptCount INT NOT NULL CONSTRAINT DF_AuditMappingExecution_Attempts DEFAULT 1,
        RowsRead BIGINT NULL,
        RowsWritten BIGINT NULL,
        ErrorCategory VARCHAR(30) NULL;
GO

IF OBJECT_ID(N'audit.PipelineSourceBatch', N'U') IS NULL
BEGIN
    -- The exact file version selected for each source in a pipeline run.
    CREATE TABLE audit.PipelineSourceBatch (
        PipelineExecutionID BIGINT NOT NULL,
        SourceName NVARCHAR(100) NOT NULL,
        FileName NVARCHAR(500) NOT NULL,
        IngestionRunID VARCHAR(40) NULL,
        SourceFileHash CHAR(64) NULL,
        RawPath NVARCHAR(500) NULL,
        Decision VARCHAR(20) NOT NULL,
        Reason NVARCHAR(2000) NULL,
        IsCritical BIT NOT NULL CONSTRAINT DF_PipelineSourceBatch_Critical DEFAULT 0,
        -- Independent evidence for source-to-staging reconciliation:
        -- the row count established by validation, and its validation IDs.
        ValidatedRowCount BIGINT NULL,
        StructureValidationID VARCHAR(60) NULL,
        DataValidationID VARCHAR(60) NULL,
        IngestedAt DATETIME2(0) NULL,
        RecordedAt DATETIME2(7) NOT NULL CONSTRAINT DF_PipelineSourceBatch_RecordedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_PipelineSourceBatch PRIMARY KEY (PipelineExecutionID, SourceName, FileName),
        CONSTRAINT FK_PipelineSourceBatch_Pipeline FOREIGN KEY (PipelineExecutionID)
            REFERENCES audit.PipelineExecution(PipelineExecutionID),
        CONSTRAINT CK_PipelineSourceBatch_Decision
            CHECK (Decision IN ('ELIGIBLE', 'BLOCKED', 'HELD', 'WITHDRAWN'))
    );
END;
GO

IF OBJECT_ID(N'audit.ExecutionError', N'U') IS NULL
BEGIN
    -- Actionable errors: where, what rule, actual vs expected, impact, fix.
    CREATE TABLE audit.ExecutionError (
        ExecutionErrorID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ExecutionError PRIMARY KEY,
        PipelineExecutionID BIGINT NULL,
        MappingExecutionID BIGINT NULL,
        MappingCode VARCHAR(80) NULL,
        Stage VARCHAR(30) NOT NULL,
        Severity VARCHAR(10) NOT NULL,
        ErrorCategory VARCHAR(30) NOT NULL,
        SourceName NVARCHAR(100) NULL,
        SourceFile NVARCHAR(500) NULL,
        IngestionRunID VARCHAR(40) NULL,
        SheetName NVARCHAR(100) NULL,
        RowNumber INT NULL,
        ColumnName NVARCHAR(128) NULL,
        RuleCode VARCHAR(120) NULL,
        ActualValue NVARCHAR(1000) NULL,
        ExpectedValue NVARCHAR(1000) NULL,
        BusinessImpact NVARCHAR(1000) NULL,
        RecommendedAction NVARCHAR(1000) NULL,
        ErrorMessage NVARCHAR(4000) NOT NULL,
        TechnicalDetail NVARCHAR(4000) NULL,
        RecordedAt DATETIME2(7) NOT NULL CONSTRAINT DF_ExecutionError_RecordedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_ExecutionError_Severity CHECK (Severity IN ('CRITICAL', 'ERROR', 'WARNING')),
        CONSTRAINT FK_ExecutionError_Pipeline FOREIGN KEY (PipelineExecutionID)
            REFERENCES audit.PipelineExecution(PipelineExecutionID)
    );
    CREATE INDEX IX_ExecutionError_Pipeline ON audit.ExecutionError(PipelineExecutionID, Severity);
END;
GO

IF OBJECT_ID(N'audit.WarehouseExecution', N'U') IS NULL
BEGIN
    CREATE TABLE audit.WarehouseExecution (
        WarehouseExecutionID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_WarehouseExecution PRIMARY KEY,
        PipelineExecutionID BIGINT NOT NULL,
        MappingCode VARCHAR(80) NOT NULL,
        TargetTable NVARCHAR(256) NOT NULL,
        SourceName NVARCHAR(100) NULL,
        IngestionRunID VARCHAR(40) NULL,
        SourceFileHash CHAR(64) NULL,
        ExecutionStatus VARCHAR(20) NOT NULL CONSTRAINT DF_WarehouseExecution_Status DEFAULT 'STARTED',
        SourceRows BIGINT NOT NULL CONSTRAINT DF_WarehouseExecution_SourceRows DEFAULT 0,
        InsertedRows BIGINT NOT NULL CONSTRAINT DF_WarehouseExecution_Inserted DEFAULT 0,
        UpdatedRows BIGINT NOT NULL CONSTRAINT DF_WarehouseExecution_Updated DEFAULT 0,
        UnchangedRows BIGINT NOT NULL CONSTRAINT DF_WarehouseExecution_Unchanged DEFAULT 0,
        SoftDeletedRows BIGINT NOT NULL CONSTRAINT DF_WarehouseExecution_SoftDeleted DEFAULT 0,
        UnknownMemberRows BIGINT NOT NULL CONSTRAINT DF_WarehouseExecution_Unknown DEFAULT 0,
        StartedAt DATETIME2(7) NOT NULL CONSTRAINT DF_WarehouseExecution_StartedAt DEFAULT SYSUTCDATETIME(),
        CompletedAt DATETIME2(7) NULL,
        ErrorMessage NVARCHAR(4000) NULL,
        CONSTRAINT CK_WarehouseExecution_Status CHECK (ExecutionStatus IN ('STARTED', 'COMPLETED', 'SKIPPED', 'FAILED')),
        CONSTRAINT FK_WarehouseExecution_Pipeline FOREIGN KEY (PipelineExecutionID)
            REFERENCES audit.PipelineExecution(PipelineExecutionID)
    );
    CREATE INDEX IX_WarehouseExecution_Pipeline ON audit.WarehouseExecution(PipelineExecutionID, MappingCode);
END;
GO

IF OBJECT_ID(N'audit.FactChangeLog', N'U') IS NULL
BEGIN
    -- Corrections to fact rows: a changed source row, a row missing from a
    -- later full extract (soft delete) or a row that reappears (restore).
    CREATE TABLE audit.FactChangeLog (
        FactChangeLogID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_FactChangeLog PRIMARY KEY,
        WarehouseExecutionID BIGINT NOT NULL,
        TargetTable NVARCHAR(256) NOT NULL,
        BusinessKey NVARCHAR(200) NOT NULL,
        ChangeType VARCHAR(20) NOT NULL,
        OldRowHash BINARY(32) NULL,
        NewRowHash BINARY(32) NULL,
        ChangedAt DATETIME2(7) NOT NULL CONSTRAINT DF_FactChangeLog_ChangedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_FactChangeLog_Type CHECK (ChangeType IN ('UPDATE', 'SOFT_DELETE', 'RESTORE')),
        CONSTRAINT FK_FactChangeLog_Execution FOREIGN KEY (WarehouseExecutionID)
            REFERENCES audit.WarehouseExecution(WarehouseExecutionID)
    );
    CREATE INDEX IX_FactChangeLog_Table ON audit.FactChangeLog(TargetTable, BusinessKey);
END;
GO

-- ---------------------------------------------------------
-- Catalogue: business rules, lineage, settings, sync log
-- ---------------------------------------------------------
IF COL_LENGTH(N'meta.DataElement', N'ColumnOrder') IS NULL
    ALTER TABLE meta.DataElement ADD
        ColumnOrder INT NULL,
        ContainsPersonalData BIT NOT NULL CONSTRAINT DF_DataElement_Personal DEFAULT 0,
        StagingColumn NVARCHAR(256) NULL,
        UpdatedAt DATETIME2(7) NULL;
GO

IF OBJECT_ID(N'meta.BusinessRule', N'U') IS NULL
BEGIN
    CREATE TABLE meta.BusinessRule (
        BusinessRuleID INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_BusinessRule PRIMARY KEY,
        RuleCode VARCHAR(200) NOT NULL CONSTRAINT UQ_BusinessRule_Code UNIQUE,
        SourceName NVARCHAR(100) NOT NULL,
        RuleStage VARCHAR(30) NOT NULL,
        RuleType VARCHAR(60) NOT NULL,
        FieldName NVARCHAR(256) NULL,
        Severity VARCHAR(10) NOT NULL,
        Definition NVARCHAR(1000) NOT NULL,
        ParametersJson NVARCHAR(MAX) NULL,
        RuleOwner NVARCHAR(200) NULL,
        DqDimension VARCHAR(30) NULL,
        IsActive BIT NOT NULL CONSTRAINT DF_BusinessRule_Active DEFAULT 1,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_BusinessRule_UpdatedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_BusinessRule_Json CHECK (ParametersJson IS NULL OR ISJSON(ParametersJson) = 1)
    );
END;
GO

IF OBJECT_ID(N'meta.ColumnLineage', N'U') IS NULL
BEGIN
    CREATE TABLE meta.ColumnLineage (
        ColumnLineageID INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ColumnLineage PRIMARY KEY,
        MappingCode VARCHAR(80) NOT NULL,
        TargetObject NVARCHAR(256) NOT NULL,
        TargetColumn NVARCHAR(128) NOT NULL,
        SourceName NVARCHAR(100) NULL,
        SourceObject NVARCHAR(256) NULL,
        SourceColumns NVARCHAR(1000) NULL,
        TransformationRule NVARCHAR(1000) NOT NULL,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_ColumnLineage_UpdatedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_ColumnLineage UNIQUE (TargetObject, TargetColumn)
    );
END;
GO

IF OBJECT_ID(N'meta.Setting', N'U') IS NULL
BEGIN
    -- Business-rule parameters (VAT rate, thresholds) used by SQL logic,
    -- synchronised from config/business_rules.yaml.
    CREATE TABLE meta.Setting (
        SettingKey VARCHAR(200) NOT NULL CONSTRAINT PK_Setting PRIMARY KEY,
        SettingValue NVARCHAR(1000) NOT NULL,
        ValueType VARCHAR(20) NOT NULL,
        Description NVARCHAR(500) NULL,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_Setting_UpdatedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_Setting_Type CHECK (ValueType IN ('DECIMAL', 'INT', 'BOOL', 'TEXT'))
    );
END;
GO

IF OBJECT_ID(N'meta.MetadataSyncLog', N'U') IS NULL
BEGIN
    CREATE TABLE meta.MetadataSyncLog (
        MetadataSyncID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_MetadataSyncLog PRIMARY KEY,
        SyncedAt DATETIME2(7) NOT NULL CONSTRAINT DF_MetadataSyncLog_SyncedAt DEFAULT SYSUTCDATETIME(),
        SyncedBy NVARCHAR(128) NOT NULL CONSTRAINT DF_MetadataSyncLog_SyncedBy DEFAULT SUSER_SNAME(),
        ConfigChecksum CHAR(64) NOT NULL,
        GitCommit VARCHAR(40) NULL,
        ItemsInserted INT NOT NULL,
        ItemsUpdated INT NOT NULL,
        ItemsDisabled INT NOT NULL,
        Summary NVARCHAR(2000) NULL
    );
END;
GO
