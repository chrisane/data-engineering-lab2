USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/* Step 13A: additive metadata and operational audit schema. */
IF OBJECT_ID(N'meta.MappingDependency',N'U') IS NULL
BEGIN
 CREATE TABLE meta.MappingDependency (
  MappingID INT NOT NULL,
  DependsOnMappingID INT NOT NULL,
  CreatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_MappingDependency_CreatedAt DEFAULT SYSUTCDATETIME(),
  CONSTRAINT PK_MappingDependency PRIMARY KEY (MappingID,DependsOnMappingID),
  CONSTRAINT FK_MappingDependency_Mapping FOREIGN KEY (MappingID) REFERENCES meta.SourceTargetMapping(MappingID),
  CONSTRAINT FK_MappingDependency_Parent FOREIGN KEY (DependsOnMappingID) REFERENCES meta.SourceTargetMapping(MappingID),
  CONSTRAINT CK_MappingDependency_NotSelf CHECK (MappingID<>DependsOnMappingID)
 );
END;
GO
IF OBJECT_ID(N'audit.PipelineExecution',N'U') IS NULL
BEGIN
 CREATE TABLE audit.PipelineExecution (
  PipelineExecutionID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_PipelineExecution PRIMARY KEY,
  PipelineName SYSNAME NOT NULL CONSTRAINT DF_PipelineExecution_Name DEFAULT N'RetailDataPlatform',
  IngestionRunID VARCHAR(40) NULL,
  ExecutionStatus VARCHAR(20) NOT NULL CONSTRAINT DF_PipelineExecution_Status DEFAULT 'STARTED',
  StartedAt DATETIME2(7) NOT NULL CONSTRAINT DF_PipelineExecution_StartedAt DEFAULT SYSUTCDATETIME(),
  CompletedAt DATETIME2(7) NULL,
  ErrorMessage NVARCHAR(2000) NULL,
  CONSTRAINT CK_PipelineExecution_Status CHECK (ExecutionStatus IN ('STARTED','COMPLETED','FAILED','CANCELLED')),
  CONSTRAINT CK_PipelineExecution_Times CHECK (CompletedAt IS NULL OR CompletedAt>=StartedAt)
 );
 CREATE INDEX IX_PipelineExecution_StartedAt ON audit.PipelineExecution(StartedAt DESC);
END;
GO
IF OBJECT_ID(N'audit.MappingExecution',N'U') IS NULL
BEGIN
 CREATE TABLE audit.MappingExecution (
  MappingExecutionID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_AuditMappingExecution PRIMARY KEY,
  PipelineExecutionID BIGINT NOT NULL,
  MappingID INT NOT NULL,
  HandlerID INT NOT NULL,
  ExecutionStatus VARCHAR(20) NOT NULL CONSTRAINT DF_AuditMappingExecution_Status DEFAULT 'STARTED',
  StartedAt DATETIME2(7) NOT NULL CONSTRAINT DF_AuditMappingExecution_StartedAt DEFAULT SYSUTCDATETIME(),
  CompletedAt DATETIME2(7) NULL,
  OutcomeMessage NVARCHAR(2000) NULL,
  CONSTRAINT FK_AuditMappingExecution_Pipeline FOREIGN KEY (PipelineExecutionID) REFERENCES audit.PipelineExecution(PipelineExecutionID),
  CONSTRAINT FK_AuditMappingExecution_Mapping FOREIGN KEY (MappingID) REFERENCES meta.SourceTargetMapping(MappingID),
  CONSTRAINT FK_AuditMappingExecution_Handler FOREIGN KEY (HandlerID) REFERENCES meta.ExecutionHandler(HandlerID),
  CONSTRAINT UQ_AuditMappingExecution_RunMapping UNIQUE(PipelineExecutionID,MappingID),
  CONSTRAINT CK_AuditMappingExecution_Status CHECK (ExecutionStatus IN ('STARTED','COMPLETED','FAILED','SKIPPED','BLOCKED','CANCELLED')),
  CONSTRAINT CK_AuditMappingExecution_Times CHECK (CompletedAt IS NULL OR CompletedAt>=StartedAt)
 );
 CREATE INDEX IX_AuditMappingExecution_MappingStarted ON audit.MappingExecution(MappingID,StartedAt DESC);
END;
GO
/* Register Supplier dependency without assuming identity values. */
BEGIN TRANSACTION;
DECLARE @StagingID INT, @WarehouseID INT;
SELECT @StagingID=m.MappingID FROM meta.SourceTargetMapping AS m
JOIN meta.DataSource AS s ON s.SourceID=m.SourceID
WHERE s.SourceName='supplier_master' AND m.TargetSchema='stg' AND m.TargetTable='Supplier';
SELECT @WarehouseID=m.MappingID FROM meta.SourceTargetMapping AS m
JOIN meta.DataSource AS s ON s.SourceID=m.SourceID
WHERE s.SourceName='supplier_master' AND m.TargetSchema='dw' AND m.TargetTable='DimSupplier';
IF @StagingID IS NULL OR @WarehouseID IS NULL
 THROW 51001, 'Expected Supplier staging and warehouse mappings are missing.', 1;
IF NOT EXISTS (SELECT 1 FROM meta.MappingDependency WHERE MappingID=@WarehouseID AND DependsOnMappingID=@StagingID)
 INSERT INTO meta.MappingDependency(MappingID,DependsOnMappingID) VALUES (@WarehouseID,@StagingID);
COMMIT TRANSACTION;
GO
/* Verification: one Supplier dependency; audit tables are empty until Python integration. */
SELECT child.MappingID, CONCAT(child.TargetSchema,'.',child.TargetTable) AS Target,
 parent.MappingID AS DependsOnMappingID, CONCAT(parent.TargetSchema,'.',parent.TargetTable) AS DependsOn
FROM meta.MappingDependency d
JOIN meta.SourceTargetMapping child ON child.MappingID=d.MappingID
JOIN meta.SourceTargetMapping parent ON parent.MappingID=d.DependsOnMappingID;
SELECT COUNT(*) AS PipelineExecutions FROM audit.PipelineExecution;
SELECT COUNT(*) AS MappingExecutions FROM audit.MappingExecution;
GO
