USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    030 - Demand-forecasting feature table (gold ML feature table).

    GRAIN: one row per site, product (business key, all SCD2 versions
    combined) and week (Monday start), for every site and product that
    has ever sold, from the week of the first sale to the week after the
    last complete week. Weeks without sales are included (0 units), so
    the series are dense, as forecasting models expect.

    POINT-IN-TIME CORRECT: every feature uses only data from before the
    week starts (lags, rolling windows, price, stock on hand). The label,
    TargetQuantity, is the week's own sales. Rows whose week lies wholly
    within the loaded data are TRAINING rows; the last week is the
    INFERENCE row (its label, where present, is the partial week so far).

    Rebuilt each run by gold.usp_BuildFeatureDemandWeekly (mapping
    GOLD_FEATURE_DEMAND_WEEKLY). Models and notebooks read
    presentation.vw_DemandForecastFeatures.
*/

IF OBJECT_ID(N'gold.FeatureDemandWeekly', N'U') IS NULL
BEGIN
    CREATE TABLE gold.FeatureDemandWeekly (
        WeekStartDateKey INT NOT NULL CONSTRAINT FK_FeatureDemandWeekly_Date REFERENCES gold.DimDate(DateKey),
        SiteKey INT NOT NULL CONSTRAINT FK_FeatureDemandWeekly_Site REFERENCES gold.DimSite(SiteKey),
        ProductID NVARCHAR(50) NOT NULL,
        ProductKey INT NOT NULL CONSTRAINT FK_FeatureDemandWeekly_Product REFERENCES gold.DimProduct(ProductKey),
        Category NVARCHAR(255) NOT NULL,
        IsDCStocked BIT NOT NULL,
        WeekOfYear TINYINT NOT NULL,
        MonthNumber TINYINT NOT NULL,
        QuarterNumber TINYINT NOT NULL,
        HistoryWeeks INT NOT NULL,
        QtyLag1W INT NULL,
        QtyLag2W INT NULL,
        QtyLag4W INT NULL,
        QtyRolling4WAvg DECIMAL(19, 4) NULL,
        QtyRolling13WAvg DECIMAL(19, 4) NULL,
        QtyRolling4WStdDev DECIMAL(19, 4) NULL,
        AvgUnitPrice4W DECIMAL(19, 4) NULL,
        WeeksSinceLastSale INT NULL,
        StockOnHandStart INT NOT NULL,
        StockCoverWeeks DECIMAL(19, 4) NULL,
        TargetQuantity INT NULL,
        IsLabelComplete BIT NOT NULL,
        DatasetSplit VARCHAR(10) NOT NULL,
        PipelineExecutionID BIGINT NOT NULL,
        BuiltAt DATETIME2(7) NOT NULL CONSTRAINT DF_FeatureDemandWeekly_BuiltAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_FeatureDemandWeekly PRIMARY KEY (WeekStartDateKey, SiteKey, ProductID),
        CONSTRAINT CK_FeatureDemandWeekly_Split CHECK (DatasetSplit IN ('TRAINING', 'INFERENCE'))
    );
END;
GO

CREATE OR ALTER PROCEDURE gold.usp_BuildFeatureDemandWeekly @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Rows BIGINT;
    DECLARE @FirstWeek DATE, @LastWeek DATE, @LastSaleDate DATE;

    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'gold.FeatureDemandWeekly';

        -- Read by audit.usp_CompleteWarehouseExecution (empty for a full rebuild).
        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        -- Sales and stock movements per site, product (business key) and week.
        SELECT s.SiteKey, p.ProductID, DATEADD(DAY, 1 - d.DayOfWeekNumber, d.[Date]) AS WeekStart,
               SUM(s.Quantity) AS Quantity, SUM(s.LineNetAmount) AS NetAmount
        INTO #WeeklySales
        FROM gold.FactSalesLine AS s
        JOIN gold.DimDate AS d ON d.DateKey = s.SaleDateKey
        JOIN gold.DimProduct AS p ON p.ProductKey = s.ProductKey
        WHERE s.IsDeletedInSource = 0
        GROUP BY s.SiteKey, p.ProductID, DATEADD(DAY, 1 - d.DayOfWeekNumber, d.[Date]);

        SELECT m.SiteKey, p.ProductID, DATEADD(DAY, 1 - d.DayOfWeekNumber, d.[Date]) AS WeekStart,
               SUM(m.Quantity) AS NetQuantity
        INTO #WeeklyStock
        FROM gold.FactInventoryMovement AS m
        JOIN gold.DimDate AS d ON d.DateKey = m.MovementDateKey
        JOIN gold.DimProduct AS p ON p.ProductKey = m.ProductKey
        WHERE m.IsDeletedInSource = 0
        GROUP BY m.SiteKey, p.ProductID, DATEADD(DAY, 1 - d.DayOfWeekNumber, d.[Date]);

        SELECT @LastSaleDate = MAX(d.[Date])
        FROM gold.FactSalesLine AS s JOIN gold.DimDate AS d ON d.DateKey = s.SaleDateKey
        WHERE s.IsDeletedInSource = 0;

        SELECT @FirstWeek = MIN(WeekStart) FROM #WeeklySales;
        -- The forecast week: the week holding the day after the last sale (1900-01-01 was a Monday).
        SET @LastWeek = DATEADD(DAY, -(DATEDIFF(DAY, '19000101', DATEADD(DAY, 1, @LastSaleDate)) % 7),
                                DATEADD(DAY, 1, @LastSaleDate));

        -- Dense grid: every site and product that has sold, for every week.
        SELECT DATEADD(WEEK, n.Number, @FirstWeek) AS WeekStart, n.Number AS WeekIndex
        INTO #Weeks
        FROM (SELECT TOP (DATEDIFF(DAY, @FirstWeek, @LastWeek) / 7 + 1)
                     ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS Number
              FROM gold.DimDate) AS n
        WHERE @FirstWeek IS NOT NULL;

        SELECT g.SiteKey, g.ProductID, w.WeekStart, w.WeekIndex,
               ISNULL(s.Quantity, 0) AS Quantity, ISNULL(s.NetAmount, 0) AS NetAmount,
               ISNULL(k.NetQuantity, 0) AS NetStockMovement
        INTO #Grid
        FROM (SELECT DISTINCT SiteKey, ProductID FROM #WeeklySales) AS g
        CROSS JOIN #Weeks AS w
        LEFT JOIN #WeeklySales AS s ON s.SiteKey = g.SiteKey AND s.ProductID = g.ProductID AND s.WeekStart = w.WeekStart
        LEFT JOIN #WeeklyStock AS k ON k.SiteKey = g.SiteKey AND k.ProductID = g.ProductID AND k.WeekStart = w.WeekStart;

        -- Stock moved before the grid starts counts towards the first week's opening stock.
        SELECT SiteKey, ProductID, SUM(NetQuantity) AS OpeningStock
        INTO #OpeningStock
        FROM #WeeklyStock WHERE WeekStart < @FirstWeek
        GROUP BY SiteKey, ProductID;

        DELETE FROM gold.FeatureDemandWeekly;

        ;WITH Features AS (
            SELECT g.*,
                   LAG(g.Quantity, 1) OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex) AS QtyLag1W,
                   LAG(g.Quantity, 2) OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex) AS QtyLag2W,
                   LAG(g.Quantity, 4) OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex) AS QtyLag4W,
                   AVG(CONVERT(DECIMAL(19, 4), g.Quantity))
                       OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS Avg4,
                   AVG(CONVERT(DECIMAL(19, 4), g.Quantity))
                       OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN 13 PRECEDING AND 1 PRECEDING) AS Avg13,
                   STDEVP(CONVERT(DECIMAL(19, 4), g.Quantity))
                       OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS Std4,
                   SUM(g.NetAmount) OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS Amount4,
                   SUM(g.Quantity) OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS Units4,
                   -- -1 = no earlier sale (NULLs in an aggregate raise warning 8153).
                   MAX(IIF(g.Quantity > 0, g.WeekIndex, -1))
                       OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS LastSaleWeekIndex,
                   SUM(g.NetStockMovement)
                       OVER (PARTITION BY g.SiteKey, g.ProductID ORDER BY g.WeekIndex ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS StockBefore
            FROM #Grid AS g
        )
        INSERT gold.FeatureDemandWeekly (
            WeekStartDateKey, SiteKey, ProductID, ProductKey, Category, IsDCStocked, WeekOfYear, MonthNumber,
            QuarterNumber, HistoryWeeks, QtyLag1W, QtyLag2W, QtyLag4W, QtyRolling4WAvg, QtyRolling13WAvg,
            QtyRolling4WStdDev, AvgUnitPrice4W, WeeksSinceLastSale, StockOnHandStart, StockCoverWeeks,
            TargetQuantity, IsLabelComplete, DatasetSplit, PipelineExecutionID)
        SELECT gold.fn_DateKey(f.WeekStart), f.SiteKey, f.ProductID, p.ProductKey, p.Category, p.IsDCStocked,
               d.IsoWeek, d.MonthNumber, d.QuarterNumber,
               f.WeekIndex,
               f.QtyLag1W, f.QtyLag2W, f.QtyLag4W,
               IIF(f.WeekIndex >= 4, f.Avg4, NULL),
               IIF(f.WeekIndex >= 13, f.Avg13, NULL),
               IIF(f.WeekIndex >= 4, f.Std4, NULL),
               CONVERT(DECIMAL(19, 4), f.Amount4 / NULLIF(f.Units4, 0)),
               f.WeekIndex - NULLIF(f.LastSaleWeekIndex, -1),
               ISNULL(o.OpeningStock, 0) + ISNULL(f.StockBefore, 0),
               CONVERT(DECIMAL(19, 4), (ISNULL(o.OpeningStock, 0) + ISNULL(f.StockBefore, 0))
                                       / NULLIF(IIF(f.WeekIndex >= 4, f.Avg4, NULL), 0)),
               IIF(f.WeekStart > @LastSaleDate, NULL, f.Quantity),
               IIF(DATEADD(DAY, 6, f.WeekStart) <= @LastSaleDate, 1, 0),
               IIF(DATEADD(DAY, 6, f.WeekStart) <= @LastSaleDate, 'TRAINING', 'INFERENCE'),
               @PipelineExecutionID
        FROM Features AS f
        JOIN gold.DimDate AS d ON d.[Date] = f.WeekStart
        JOIN gold.DimProduct AS p ON p.ProductID = f.ProductID AND p.IsCurrent = 1
        LEFT JOIN #OpeningStock AS o ON o.SiteKey = f.SiteKey AND o.ProductID = f.ProductID;
        SET @Rows = @@ROWCOUNT;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'GOLD_FEATURE_DEMAND_WEEKLY',
             N'gold.FeatureDemandWeekly', N'sales_lines', NULL, NULL, @Rows, 0, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'GOLD_FEATURE_DEMAND_WEEKLY',
             N'gold.FeatureDemandWeekly', N'sales_lines', @Error;
        THROW;
    END CATCH
END;
GO

-- Read by models and notebooks: features, label and split with readable site and product.
CREATE OR ALTER VIEW presentation.vw_DemandForecastFeatures AS
SELECT d.[Date] AS WeekStartDate, s.SiteID, s.SiteName, f.ProductID, p.ProductName, f.Category, f.IsDCStocked,
       f.WeekOfYear, f.MonthNumber, f.QuarterNumber, f.HistoryWeeks, f.QtyLag1W, f.QtyLag2W, f.QtyLag4W,
       f.QtyRolling4WAvg, f.QtyRolling13WAvg, f.QtyRolling4WStdDev, f.AvgUnitPrice4W, f.WeeksSinceLastSale,
       f.StockOnHandStart, f.StockCoverWeeks, f.TargetQuantity, f.IsLabelComplete, f.DatasetSplit
FROM gold.FeatureDemandWeekly AS f
JOIN gold.DimDate AS d ON d.DateKey = f.WeekStartDateKey
JOIN gold.DimSite AS s ON s.SiteKey = f.SiteKey
JOIN gold.DimProduct AS p ON p.ProductKey = f.ProductKey;
GO
