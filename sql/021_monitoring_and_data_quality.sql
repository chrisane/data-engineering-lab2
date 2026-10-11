USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    021 - Data-quality results in SQL and monitoring views.

    audit.ValidationOutcome / ValidationFailure hold the outcome (and every
    failed check) of the validations behind each loaded file version; the
    orchestrator copies them from logs/ once per validation ID.

    Monitoring views are alert-ready: each returns one row per item with a
    clear status, so a scheduler or Power BI can raise or show alerts.
*/

IF OBJECT_ID(N'audit.ValidationOutcome', N'U') IS NULL
BEGIN
    CREATE TABLE audit.ValidationOutcome (
        ValidationOutcomeID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ValidationOutcome PRIMARY KEY,
        ValidationID VARCHAR(60) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceName NVARCHAR(100) NOT NULL,
        FileName NVARCHAR(500) NOT NULL,
        Stage VARCHAR(30) NOT NULL,
        ValidationStatus VARCHAR(40) NOT NULL,
        RowsChecked BIGINT NULL,
        Errors INT NOT NULL,
        Warnings INT NOT NULL,
        QuarantinePath NVARCHAR(500) NULL,
        DataSteward NVARCHAR(200) NULL,
        ValidatedAt DATETIME2(0) NOT NULL,
        FirstSeenPipelineExecutionID BIGINT NULL,
        CONSTRAINT UQ_ValidationOutcome UNIQUE (ValidationID, FileName)
    );
    CREATE INDEX IX_ValidationOutcome_Source ON audit.ValidationOutcome(SourceName, IngestionRunID);
END;
GO

IF OBJECT_ID(N'audit.ValidationFailure', N'U') IS NULL
BEGIN
    CREATE TABLE audit.ValidationFailure (
        ValidationFailureID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ValidationFailure PRIMARY KEY,
        ValidationID VARCHAR(60) NOT NULL,
        IngestionRunID VARCHAR(40) NOT NULL,
        SourceName NVARCHAR(100) NOT NULL,
        FileName NVARCHAR(500) NOT NULL,
        SheetName NVARCHAR(100) NULL,
        RowNumber INT NULL,
        RecordKey NVARCHAR(400) NULL,
        Stage VARCHAR(40) NOT NULL,
        RuleCode VARCHAR(120) NOT NULL,
        FieldName NVARCHAR(256) NULL,
        Severity VARCHAR(10) NOT NULL,
        ActualValue NVARCHAR(1000) NULL,
        ExpectedValue NVARCHAR(1000) NULL,
        Message NVARCHAR(2000) NOT NULL,
        RecommendedAction NVARCHAR(2000) NULL,
        ErrorType NVARCHAR(100) NULL,
        TechnicalError NVARCHAR(MAX) NULL
    );
    CREATE INDEX IX_ValidationFailure_Validation ON audit.ValidationFailure(ValidationID, FileName);
END;
GO

-- ---------------------------------------------------------
-- Execution history and durations
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_PipelineExecutionHistory
AS
SELECT p.PipelineExecutionID,
       p.PipelineName,
       p.ExecutionStatus,
       p.StartedAt,
       p.CompletedAt,
       DATEDIFF(SECOND, p.StartedAt, COALESCE(p.CompletedAt, SYSUTCDATETIME())) AS DurationSeconds,
       p.MappingsPlanned,
       p.MappingsCompleted,
       p.MappingsFailed,
       p.MappingsBlocked,
       p.TriggeredBy,
       p.HostName,
       p.ConfigChecksum,
       p.ErrorMessage,
       (SELECT COUNT(*) FROM audit.ExecutionError AS e
        WHERE e.PipelineExecutionID = p.PipelineExecutionID AND e.Severity IN ('CRITICAL', 'ERROR')) AS ErrorCount,
       (SELECT COUNT(*) FROM audit.ExecutionError AS e
        WHERE e.PipelineExecutionID = p.PipelineExecutionID AND e.Severity = 'WARNING') AS WarningCount,
       (SELECT COUNT(*) FROM audit.ReconciliationResult AS r
        WHERE r.PipelineExecutionID = p.PipelineExecutionID AND r.CheckStatus = 'FAIL') AS FailedControls
FROM audit.PipelineExecution AS p;
GO

CREATE OR ALTER VIEW audit.vw_MappingExecutionHistory
AS
SELECT a.PipelineExecutionID,
       a.MappingExecutionID,
       m.MappingCode,
       m.LoadLayer,
       CONCAT(m.TargetSchema, '.', m.TargetTable) AS TargetObject,
       s.SourceName,
       h.HandlerCode,
       a.ExecutionStatus,
       a.ErrorCategory,
       a.AttemptCount,
       a.RowsRead,
       a.RowsWritten,
       a.StartedAt,
       a.CompletedAt,
       DATEDIFF(MILLISECOND, a.StartedAt, COALESCE(a.CompletedAt, SYSUTCDATETIME())) / 1000.0 AS DurationSeconds,
       a.OutcomeMessage
FROM audit.MappingExecution AS a
JOIN meta.SourceTargetMapping AS m ON m.MappingID = a.MappingID
JOIN meta.DataSource AS s ON s.SourceID = m.SourceID
JOIN meta.ExecutionHandler AS h ON h.HandlerID = a.HandlerID;
GO

CREATE OR ALTER VIEW audit.vw_ProcessingDuration
AS
WITH Recent AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY MappingCode ORDER BY StartedAt DESC) AS RunsAgo
    FROM audit.vw_MappingExecutionHistory
    WHERE ExecutionStatus = 'COMPLETED'
)
SELECT MappingCode,
       TargetObject,
       COUNT(*) AS CompletedRuns,
       MAX(IIF(RunsAgo = 1, DurationSeconds, NULL)) AS LastDurationSeconds,
       AVG(DurationSeconds) AS AvgDurationSeconds,
       MAX(DurationSeconds) AS MaxDurationSeconds,
       -- Last run took more than twice the average of earlier runs.
       CASE WHEN COUNT(*) >= 3 AND MAX(IIF(RunsAgo = 1, DurationSeconds, NULL))
                 > 2 * AVG(IIF(RunsAgo > 1, DurationSeconds, NULL)) THEN 'SLOW' ELSE 'NORMAL' END AS DurationStatus
FROM Recent
WHERE RunsAgo <= 30
GROUP BY MappingCode, TargetObject;
GO

-- ---------------------------------------------------------
-- Failures (actionable), stalled executions
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_ExecutionErrors
AS
SELECT e.ExecutionErrorID,
       e.PipelineExecutionID,
       e.RecordedAt,
       e.Severity,
       e.Stage,
       e.ErrorCategory,
       e.MappingCode,
       e.SourceName,
       e.SourceFile,
       e.IngestionRunID,
       e.SheetName,
       e.RowNumber,
       e.ColumnName,
       e.RuleCode,
       e.ActualValue,
       e.ExpectedValue,
       e.ErrorMessage,
       e.BusinessImpact,
       e.RecommendedAction,
       ds.DataSteward AS NotifySteward,
       e.TechnicalDetail
FROM audit.ExecutionError AS e
LEFT JOIN meta.DataSource AS ds ON ds.SourceName = e.SourceName;
GO

CREATE OR ALTER VIEW audit.vw_StalledExecutions
AS
SELECT p.PipelineExecutionID,
       p.StartedAt,
       p.HeartbeatAt,
       DATEDIFF(MINUTE, COALESCE(p.HeartbeatAt, p.StartedAt), SYSUTCDATETIME()) AS MinutesSinceHeartbeat,
       p.HostName,
       p.TriggeredBy,
       (SELECT TOP 1 m.MappingCode FROM audit.MappingExecution AS a
        JOIN meta.SourceTargetMapping AS m ON m.MappingID = a.MappingID
        WHERE a.PipelineExecutionID = p.PipelineExecutionID AND a.ExecutionStatus = 'STARTED') AS RunningMapping
FROM audit.PipelineExecution AS p
WHERE p.ExecutionStatus = 'STARTED';
GO

-- ---------------------------------------------------------
-- Reconciliation
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_ReconciliationResults
AS
SELECT r.ReconciliationResultID,
       r.PipelineExecutionID,
       r.CheckedAt,
       COALESCE(r.ControlCode, r.CheckCode) AS ControlCode,
       r.SourceName,
       r.TargetObject,
       r.IngestionRunID,
       r.CheckStatus,
       r.Severity,
       COALESCE(CONVERT(DECIMAL(38, 6), r.ObservedValue), r.ObservedAmount) AS Observed,
       COALESCE(CONVERT(DECIMAL(38, 6), r.ExpectedValue), r.ExpectedAmount) AS Expected,
       r.Tolerance,
       r.Details,
       r.RecommendedAction
FROM audit.ReconciliationResult AS r;
GO

CREATE OR ALTER VIEW audit.vw_LatestReconciliation
AS
SELECT *
FROM (
    SELECT v.*, ROW_NUMBER() OVER (PARTITION BY v.ControlCode ORDER BY v.CheckedAt DESC, v.ReconciliationResultID DESC) AS rn
    FROM audit.vw_ReconciliationResults AS v
    WHERE v.PipelineExecutionID IS NOT NULL
) AS x
WHERE x.rn = 1;
GO

-- ---------------------------------------------------------
-- Data quality
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_DataQualityBySource
AS
WITH Latest AS (
    SELECT o.*, ROW_NUMBER() OVER (PARTITION BY o.SourceName, o.FileName, o.Stage
                                   ORDER BY o.ValidatedAt DESC, o.ValidationOutcomeID DESC) AS rn
    FROM audit.ValidationOutcome AS o
)
SELECT l.SourceName,
       l.Stage,
       COUNT(*) AS FilesValidated,
       SUM(IIF(l.Errors = 0, 1, 0)) AS FilesWithoutErrors,
       SUM(l.RowsChecked) AS RowsChecked,
       SUM(l.Errors) AS Errors,
       SUM(l.Warnings) AS Warnings,
       CONVERT(DECIMAL(5, 2), 100.0 * SUM(IIF(l.Errors = 0, 1, 0)) / COUNT(*)) AS FilePassRatePercent,
       MAX(l.ValidatedAt) AS LastValidatedAt,
       MAX(ds.DataSteward) AS DataSteward
FROM Latest AS l
LEFT JOIN meta.DataSource AS ds ON ds.SourceName = l.SourceName
WHERE l.rn = 1
GROUP BY l.SourceName, l.Stage;
GO

CREATE OR ALTER VIEW audit.vw_DataQualityByDimension
AS
SELECT f.SourceName,
       CASE
           WHEN f.RuleCode = 'REQUIRED_VALUE' THEN 'Completeness'
           WHEN f.RuleCode = 'UNIQUE' THEN 'Uniqueness'
           WHEN f.RuleCode IN ('DATA_TYPE', 'NUMERIC_RANGE', 'VALID_DATE', 'ALLOWED_VALUE', 'PATTERN',
                               'DATE_STORED_AS_TEXT') THEN 'Validity'
           WHEN f.RuleCode = 'REFERENTIAL_INTEGRITY' THEN 'Consistency'
           WHEN f.Stage = 'STRUCTURE_VALIDATION' THEN 'Conformity'
           ELSE 'Accuracy'
       END AS DqDimension,
       f.RuleCode,
       f.FieldName,
       f.Severity,
       COUNT(*) AS Failures,
       COUNT(DISTINCT f.FileName) AS FilesAffected,
       MAX(o.ValidatedAt) AS LastSeenAt
FROM audit.ValidationFailure AS f
LEFT JOIN audit.ValidationOutcome AS o ON o.ValidationID = f.ValidationID AND o.FileName = f.FileName
GROUP BY f.SourceName, f.RuleCode, f.FieldName, f.Severity, f.Stage;
GO

CREATE OR ALTER VIEW audit.vw_ValidationFailureDetail
AS
SELECT f.ValidationFailureID,
       f.SourceName,
       f.FileName,
       f.IngestionRunID,
       f.ValidationID,
       f.SheetName,
       f.RowNumber,
       f.RecordKey,
       f.FieldName AS ColumnName,
       f.RuleCode,
       f.Severity,
       f.ActualValue,
       f.ExpectedValue,
       f.Message,
       br.Definition AS RuleDefinition,
       br.RuleOwner,
       ds.DataSteward,
       f.RecommendedAction
FROM audit.ValidationFailure AS f
LEFT JOIN meta.DataSource AS ds ON ds.SourceName = f.SourceName
LEFT JOIN meta.BusinessRule AS br
       ON br.SourceName = f.SourceName AND br.RuleType = f.RuleCode AND br.FieldName = f.FieldName;
GO

-- ---------------------------------------------------------
-- Freshness and overall health
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_SourceFreshness
AS
WITH LatestBatch AS (
    SELECT b.SourceName, MAX(b.IngestedAt) AS LatestIngestedAt
    FROM audit.PipelineSourceBatch AS b
    WHERE b.Decision IN ('ELIGIBLE', 'BLOCKED', 'HELD')
    GROUP BY b.SourceName
)
SELECT ds.SourceName,
       ds.UpdateFrequency,
       ds.FreshnessThresholdHours,
       lb.LatestIngestedAt,
       DATEDIFF(HOUR, lb.LatestIngestedAt, SYSDATETIME()) AS HoursSinceNewData,
       CASE WHEN ds.FreshnessThresholdHours IS NULL THEN 'NOT_APPLICABLE'
            WHEN lb.LatestIngestedAt IS NULL THEN 'NO_DATA'
            WHEN DATEDIFF(HOUR, lb.LatestIngestedAt, SYSDATETIME()) > ds.FreshnessThresholdHours THEN 'STALE'
            ELSE 'FRESH' END AS FreshnessStatus,
       ds.DataOwner,
       ds.DataSteward
FROM meta.DataSource AS ds
LEFT JOIN LatestBatch AS lb ON lb.SourceName = ds.SourceName
WHERE ds.IsActive = 1;
GO

CREATE OR ALTER VIEW audit.vw_PlatformHealth
AS
WITH LastRun AS (
    SELECT TOP 1 * FROM audit.PipelineExecution
    WHERE ExecutionStatus <> 'STARTED' ORDER BY PipelineExecutionID DESC
)
SELECT 'LAST_PIPELINE_RUN' AS CheckName,
       CASE r.ExecutionStatus WHEN 'COMPLETED' THEN 'OK' WHEN 'PARTIAL' THEN 'WARNING' ELSE 'CRITICAL' END AS HealthStatus,
       CONCAT('Execution ', r.PipelineExecutionID, ' ', r.ExecutionStatus, ' at ',
              CONVERT(VARCHAR(16), r.CompletedAt, 120), ' UTC') AS Detail
FROM LastRun AS r
UNION ALL
SELECT 'STALLED_EXECUTIONS',
       IIF(COUNT(*) = 0, 'OK', 'CRITICAL'),
       CONCAT(COUNT(*), ' execution(s) STARTED without completing')
FROM audit.PipelineExecution WHERE ExecutionStatus = 'STARTED'
UNION ALL
SELECT 'FAILED_CONTROLS_LAST_RUN',
       CASE WHEN SUM(IIF(rr.Severity IN ('CRITICAL', 'ERROR'), 1, 0)) > 0 THEN 'CRITICAL'
            WHEN COUNT(*) > 0 THEN 'WARNING' ELSE 'OK' END,
       CONCAT(COUNT(*), ' failed reconciliation control(s) in the last run')
FROM audit.ReconciliationResult AS rr
WHERE rr.CheckStatus = 'FAIL' AND rr.PipelineExecutionID = (SELECT PipelineExecutionID FROM LastRun)
UNION ALL
SELECT 'STALE_SOURCES',
       IIF(COUNT(*) = 0, 'OK', 'WARNING'),
       CONCAT(COUNT(*), ' source(s) without new data within their freshness threshold')
FROM audit.vw_SourceFreshness WHERE FreshnessStatus IN ('STALE', 'NO_DATA')
UNION ALL
SELECT 'BLOCKED_SOURCE_FILES_LAST_RUN',
       IIF(COUNT(*) = 0, 'OK', 'WARNING'),
       CONCAT(COUNT(*), ' file version(s) blocked or held by the load gate in the last run')
FROM audit.PipelineSourceBatch
WHERE Decision IN ('BLOCKED', 'HELD') AND PipelineExecutionID = (SELECT PipelineExecutionID FROM LastRun);
GO
