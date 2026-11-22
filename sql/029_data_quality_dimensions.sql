USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    029 - Data-quality dimensions.

    Every check the platform runs proves one of eight dimensions:
    FRESHNESS, VOLUME, SCHEMA, COMPLETENESS, DUPLICATE, ACCURACY,
    CONSISTENCY and ANOMALY (config/pipeline.yaml, quality_dimensions).
    The taxonomy is synchronised by scripts/sync_metadata.py:

        meta.QualityDimension   the eight dimensions
        meta.QualityCheckType   validation checks and reconciliation
                                handlers, each with its dimension
        meta.BusinessRule       DqDimension now holds the same codes

    audit.DataAnomaly keeps every value an ANOMALY control flagged, so each
    one can be investigated (the control's result row holds the count).

    Views
        audit.vw_QualityCheckCoverage      checks configured per mapping and
                                           dimension (COVERED / NOT_COVERED)
        audit.vw_QualityResultsByDimension last run's reconciliation results
                                           per dimension
        audit.vw_DataQualityByDimension    validation failures per dimension
                                           (replaces the earlier DAMA mapping)
        audit.vw_DataAnomalies             anomalies flagged in the last run
*/

IF OBJECT_ID(N'meta.QualityDimension', N'U') IS NULL
BEGIN
    CREATE TABLE meta.QualityDimension (
        DimensionCode VARCHAR(30) NOT NULL CONSTRAINT PK_QualityDimension PRIMARY KEY,
        Description NVARCHAR(500) NOT NULL,
        DisplayOrder INT NOT NULL,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_QualityDimension_UpdatedAt DEFAULT SYSUTCDATETIME()
    );
END;
GO

IF OBJECT_ID(N'meta.QualityCheckType', N'U') IS NULL
BEGIN
    CREATE TABLE meta.QualityCheckType (
        CheckType VARCHAR(120) NOT NULL CONSTRAINT PK_QualityCheckType PRIMARY KEY,
        CheckStage VARCHAR(30) NOT NULL,
        DimensionCode VARCHAR(30) NOT NULL
            CONSTRAINT FK_QualityCheckType_Dimension REFERENCES meta.QualityDimension(DimensionCode),
        Description NVARCHAR(500) NULL,
        IsActive BIT NOT NULL CONSTRAINT DF_QualityCheckType_Active DEFAULT 1,
        UpdatedAt DATETIME2(7) NOT NULL CONSTRAINT DF_QualityCheckType_UpdatedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT CK_QualityCheckType_Stage CHECK (CheckStage IN ('VALIDATION', 'RECONCILIATION'))
    );
END;
GO

IF OBJECT_ID(N'audit.DataAnomaly', N'U') IS NULL
BEGIN
    CREATE TABLE audit.DataAnomaly (
        DataAnomalyID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DataAnomaly PRIMARY KEY,
        PipelineExecutionID BIGINT NOT NULL,
        ControlCode VARCHAR(120) NOT NULL,
        TargetObject NVARCHAR(256) NOT NULL,
        MeasureName NVARCHAR(128) NOT NULL,
        GroupLabel NVARCHAR(100) NULL,          -- for example the branch; NULL when not grouped
        DateKey INT NOT NULL,
        ObservedValue DECIMAL(19, 4) NOT NULL,
        BaselineMedian DECIMAL(19, 4) NOT NULL,
        Score DECIMAL(9, 2) NOT NULL,           -- robust z-score
        Threshold DECIMAL(9, 2) NOT NULL,
        DetectedAt DATETIME2(7) NOT NULL CONSTRAINT DF_DataAnomaly_DetectedAt DEFAULT SYSUTCDATETIME()
    );
    CREATE INDEX IX_DataAnomaly_Execution ON audit.DataAnomaly(PipelineExecutionID, ControlCode);
END;
GO

-- ---------------------------------------------------------
-- Coverage: which dimensions each mapping's checks prove
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_QualityCheckCoverage
AS
WITH Checks AS (
    SELECT c.MappingID, q.DimensionCode, 1 AS Controls, 0 AS ValidationRules
    FROM meta.ReconciliationControl AS c
    JOIN meta.ExecutionHandler AS h ON h.HandlerID = c.HandlerID
    JOIN meta.QualityCheckType AS q ON q.CheckType = h.HandlerCode
    WHERE c.IsEnabled = 1
    UNION ALL
    SELECT m.MappingID, b.DqDimension, 0, 1
    FROM meta.BusinessRule AS b
    JOIN meta.DataSource AS s ON s.SourceName = b.SourceName
    JOIN meta.SourceTargetMapping AS m ON m.SourceID = s.SourceID AND m.LoadLayer = 'SILVER'
    WHERE b.IsActive = 1 AND b.DqDimension IS NOT NULL
)
SELECT m.MappingCode,
       m.LoadLayer,
       s.SourceName,
       CONCAT(m.TargetSchema, '.', m.TargetTable) AS TargetObject,
       d.DimensionCode,
       d.DisplayOrder,
       ISNULL(SUM(c.Controls), 0) AS ReconciliationControls,
       ISNULL(SUM(c.ValidationRules), 0) AS ValidationRules,
       IIF(COUNT(c.MappingID) > 0, 'COVERED', 'NOT_COVERED') AS CoverageStatus
FROM meta.SourceTargetMapping AS m
JOIN meta.DataSource AS s ON s.SourceID = m.SourceID
CROSS JOIN meta.QualityDimension AS d
LEFT JOIN Checks AS c ON c.MappingID = m.MappingID AND c.DimensionCode = d.DimensionCode
WHERE m.IsEnabled = 1
GROUP BY m.MappingCode, m.LoadLayer, s.SourceName, m.TargetSchema, m.TargetTable, d.DimensionCode, d.DisplayOrder;
GO

-- ---------------------------------------------------------
-- Results per dimension (last completed pipeline run)
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_QualityResultsByDimension
AS
WITH LastRun AS (
    SELECT TOP 1 PipelineExecutionID FROM audit.PipelineExecution
    WHERE ExecutionStatus <> 'STARTED' ORDER BY PipelineExecutionID DESC
), Results AS (
    SELECT r.CheckStatus, r.Severity, q.DimensionCode
    FROM audit.ReconciliationResult AS r
    JOIN LastRun AS l ON l.PipelineExecutionID = r.PipelineExecutionID
    -- Supplier checks record ControlCode.CheckCode under their control.
    JOIN meta.ReconciliationControl AS c
      ON c.ControlCode = r.ControlCode OR r.ControlCode LIKE c.ControlCode + '.%'
    JOIN meta.ExecutionHandler AS h ON h.HandlerID = c.HandlerID
    JOIN meta.QualityCheckType AS q ON q.CheckType = h.HandlerCode
)
SELECT d.DimensionCode,
       d.Description,
       (SELECT PipelineExecutionID FROM LastRun) AS PipelineExecutionID,
       COUNT(r.CheckStatus) AS ChecksRun,
       SUM(IIF(r.CheckStatus = 'PASS', 1, 0)) AS Passed,
       SUM(IIF(r.CheckStatus = 'FAIL', 1, 0)) AS Failed,
       SUM(IIF(r.CheckStatus = 'FAIL' AND r.Severity IN ('CRITICAL', 'ERROR'), 1, 0)) AS FailedBlocking,
       CASE WHEN COUNT(r.CheckStatus) = 0 THEN 'NOT_RUN'
            WHEN SUM(IIF(r.CheckStatus = 'FAIL' AND r.Severity IN ('CRITICAL', 'ERROR'), 1, 0)) > 0 THEN 'FAIL'
            WHEN SUM(IIF(r.CheckStatus = 'FAIL', 1, 0)) > 0 THEN 'WARNING'
            ELSE 'PASS' END AS DimensionStatus
FROM meta.QualityDimension AS d
LEFT JOIN Results AS r ON r.DimensionCode = d.DimensionCode
GROUP BY d.DimensionCode, d.Description, d.DisplayOrder;
GO

-- ---------------------------------------------------------
-- Validation failures per dimension
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_DataQualityByDimension
AS
SELECT f.SourceName,
       COALESCE(q.DimensionCode, b.DqDimension, t.DqDimension, 'UNCLASSIFIED') AS DqDimension,
       f.RuleCode,
       f.FieldName,
       f.Severity,
       COUNT(*) AS Failures,
       COUNT(DISTINCT f.FileName) AS FilesAffected,
       MAX(o.ValidatedAt) AS LastSeenAt
FROM audit.ValidationFailure AS f
LEFT JOIN audit.ValidationOutcome AS o ON o.ValidationID = f.ValidationID AND o.FileName = f.FileName
LEFT JOIN meta.QualityCheckType AS q ON q.CheckType = f.RuleCode AND q.CheckStage = 'VALIDATION'
-- Named business rules carry their own dimension; a group balance also checks the file total.
LEFT JOIN meta.BusinessRule AS b
       ON b.SourceName = f.SourceName AND b.RuleType = f.RuleCode AND ISNULL(b.FieldName, '') = ISNULL(f.FieldName, '')
LEFT JOIN meta.BusinessRule AS t
       ON t.SourceName = f.SourceName AND t.RuleType + '_FILE_TOTAL' = f.RuleCode
GROUP BY f.SourceName, COALESCE(q.DimensionCode, b.DqDimension, t.DqDimension, 'UNCLASSIFIED'), f.RuleCode,
         f.FieldName, f.Severity;
GO

-- ---------------------------------------------------------
-- Anomalies flagged in the last run
-- ---------------------------------------------------------
CREATE OR ALTER VIEW audit.vw_DataAnomalies
AS
SELECT a.PipelineExecutionID,
       a.ControlCode,
       a.TargetObject,
       a.MeasureName,
       a.GroupLabel,
       d.[Date] AS AnomalyDate,
       a.ObservedValue,
       a.BaselineMedian,
       a.ObservedValue - a.BaselineMedian AS Deviation,
       a.Score,
       a.Threshold,
       a.DetectedAt
FROM audit.DataAnomaly AS a
LEFT JOIN gold.DimDate AS d ON d.DateKey = a.DateKey
WHERE a.PipelineExecutionID = (SELECT TOP 1 PipelineExecutionID FROM audit.PipelineExecution
                               WHERE ExecutionStatus <> 'STARTED' ORDER BY PipelineExecutionID DESC);
GO
