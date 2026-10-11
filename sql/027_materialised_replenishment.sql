USE RetailDataPlatform;
GO
SET XACT_ABORT ON;
GO
/*
    027 - Materialised replenishment plan.

    vw_ReplenishmentPlan and vw_SupplierReorderProposal (024) computed
    stock, demand and lead times on every query through stacked views
    (~200 s and 13.8 million logical reads on the full data set). The plan
    is now built once per pipeline run, set-based with temporary tables, by
    mart.usp_BuildReplenishmentPlan (mapping MART_REPLENISHMENT_PLAN), and
    both views read the table. View names and columns are unchanged.

    GRAIN: one row per site (branch or DC) and product at the as-at date.
*/

IF OBJECT_ID(N'mart.ReplenishmentPlan', N'U') IS NULL
BEGIN
    CREATE TABLE mart.ReplenishmentPlan (
        SiteID NVARCHAR(50) NOT NULL,
        SiteName NVARCHAR(255) NOT NULL,
        SiteType NVARCHAR(50) NOT NULL,
        ProductID NVARCHAR(50) NOT NULL,
        ProductName NVARCHAR(255) NOT NULL,
        Category NVARCHAR(255) NOT NULL,
        QuantityOnHand INT NOT NULL,
        StockValue DECIMAL(19, 2) NOT NULL,
        AvgDailyDemand DECIMAL(19, 3) NOT NULL,
        LeadTimeDays DECIMAL(9, 2) NOT NULL,
        ReorderPoint DECIMAL(19, 1) NOT NULL,
        DaysOfCover DECIMAL(19, 1) NULL,
        DaysSinceLastOutflow INT NULL,
        StockStatus VARCHAR(20) NOT NULL,
        AsAtDate DATE NOT NULL,
        PipelineExecutionID BIGINT NOT NULL,
        BuiltAt DATETIME2(7) NOT NULL CONSTRAINT DF_ReplenishmentPlan_BuiltAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_ReplenishmentPlan PRIMARY KEY (SiteID, ProductID)
    );
END;
GO

CREATE OR ALTER PROCEDURE mart.usp_BuildReplenishmentPlan @PipelineExecutionID BIGINT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Rows BIGINT;
    DECLARE @AsAt DATE = COALESCE((SELECT TRY_CONVERT(DATE, SettingValue) FROM meta.Setting
                                   WHERE SettingKey = 'reporting.as_at_date'), CONVERT(DATE, SYSDATETIME()));
    DECLARE @AsAtKey INT = dw.fn_DateKey(@AsAt);
    DECLARE @WindowDays DECIMAL(19, 6) = meta.fn_SettingDecimal('replenishment.demand_window_days', 90);
    DECLARE @SafetyDays DECIMAL(19, 6) = meta.fn_SettingDecimal('replenishment.safety_stock_days', 7);
    DECLARE @DefaultLead DECIMAL(19, 6) = meta.fn_SettingDecimal('replenishment.default_lead_time_days', 7);
    DECLARE @ExcessCover DECIMAL(19, 6) = meta.fn_SettingDecimal('replenishment.excess_cover_days', 60);
    DECLARE @SlowDays DECIMAL(19, 6) = meta.fn_SettingDecimal('replenishment.slow_moving_days', 45);
    DECLARE @WindowStartKey INT = dw.fn_DateKey(DATEADD(DAY, -CONVERT(INT, @WindowDays) + 1, @AsAt));

    BEGIN TRY
        BEGIN TRANSACTION;
        EXEC audit.usp_AcquireLoadLock N'mart.ReplenishmentPlan';

        CREATE TABLE #MergeActions (ActionType NVARCHAR(10), BusinessKey NVARCHAR(200), OldRowHash BINARY(32),
                                    NewRowHash BINARY(32), OldDeleted BIT, NewDeleted BIT);

        -- Stock position, demand and last outflow per site and product (one pass).
        SELECT m.SiteKey, p.ProductID,
               SUM(m.Quantity) AS QuantityOnHand,
               SUM(IIF(m.Quantity < 0 AND m.MovementType IN (N'SALE', N'TRANSFER_OUT')
                       AND m.MovementDateKey >= @WindowStartKey, -m.Quantity, 0)) AS WindowOutflow,
               MAX(IIF(m.Quantity < 0, m.MovementDateKey, NULL)) AS LastOutflowKey
        INTO #Position
        FROM dw.FactInventoryMovement AS m
        JOIN dw.DimProduct AS p ON p.ProductKey = m.ProductKey
        WHERE m.IsDeletedInSource = 0 AND m.MovementDateKey <= @AsAtKey
        GROUP BY m.SiteKey, p.ProductID;

        -- Average supplier lead time per product (goods receipts of its PO lines).
        SELECT l.ProductID, AVG(CONVERT(DECIMAL(9, 2), g.LeadTimeDays)) AS LeadTimeDays
        INTO #LeadTime
        FROM dw.FactPurchaseOrderLine AS l
        JOIN dw.FactGoodsReceipt AS g ON g.POID = l.POID AND g.IsDeletedInSource = 0
        WHERE l.IsDeletedInSource = 0
        GROUP BY l.ProductID;

        SELECT s.SiteID, s.SiteName, s.SiteType, p.ProductID, p.ProductName, p.Category, p.StandardCost,
               pos.QuantityOnHand, pos.WindowOutflow / @WindowDays AS AvgDailyDemand,
               IIF(s.SiteType = N'DISTRIBUTION_CENTRE', ISNULL(lt.LeadTimeDays, @DefaultLead), @DefaultLead) AS LeadTimeDays,
               DATEDIFF(DAY, lo.[Date], @AsAt) AS DaysSinceLastOutflow
        INTO #Plan
        FROM #Position AS pos
        JOIN dw.DimSite AS s ON s.SiteKey = pos.SiteKey
        JOIN dw.DimProduct AS p ON p.ProductID = pos.ProductID AND p.IsCurrent = 1
        LEFT JOIN #LeadTime AS lt ON lt.ProductID = pos.ProductID
        LEFT JOIN dw.DimDate AS lo ON lo.DateKey = pos.LastOutflowKey
        WHERE s.SiteType IN (N'BRANCH', N'DISTRIBUTION_CENTRE');

        DELETE FROM mart.ReplenishmentPlan;

        INSERT mart.ReplenishmentPlan (SiteID, SiteName, SiteType, ProductID, ProductName, Category, QuantityOnHand,
                                       StockValue, AvgDailyDemand, LeadTimeDays, ReorderPoint, DaysOfCover,
                                       DaysSinceLastOutflow, StockStatus, AsAtDate, PipelineExecutionID)
        SELECT SiteID, SiteName, SiteType, ProductID, ProductName, Category, QuantityOnHand,
               CONVERT(DECIMAL(19, 2), QuantityOnHand * StandardCost),
               CONVERT(DECIMAL(19, 3), AvgDailyDemand),
               CONVERT(DECIMAL(9, 2), LeadTimeDays),
               CONVERT(DECIMAL(19, 1), AvgDailyDemand * (LeadTimeDays + @SafetyDays)),
               CONVERT(DECIMAL(19, 1), QuantityOnHand / NULLIF(AvgDailyDemand, 0)),
               DaysSinceLastOutflow,
               CASE WHEN QuantityOnHand < 0 THEN 'NEGATIVE_STOCK'
                    WHEN AvgDailyDemand > 0 AND QuantityOnHand < AvgDailyDemand * (LeadTimeDays + @SafetyDays) THEN 'REORDER'
                    WHEN QuantityOnHand > 0 AND (DaysSinceLastOutflow IS NULL OR DaysSinceLastOutflow > @SlowDays) THEN 'SLOW_MOVING'
                    WHEN AvgDailyDemand > 0 AND QuantityOnHand / AvgDailyDemand > @ExcessCover THEN 'EXCESS'
                    ELSE 'OK' END,
               @AsAt, @PipelineExecutionID
        FROM #Plan;
        SET @Rows = @@ROWCOUNT;

        EXEC audit.usp_CompleteWarehouseExecution @PipelineExecutionID, 'MART_REPLENISHMENT_PLAN',
             N'mart.ReplenishmentPlan', N'inventory_movements', NULL, NULL, @Rows, 0, @Rows;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @Error NVARCHAR(4000) = ERROR_MESSAGE();
        EXEC audit.usp_RecordWarehouseFailure @PipelineExecutionID, 'MART_REPLENISHMENT_PLAN', N'mart.ReplenishmentPlan', N'inventory_movements', @Error;
        THROW;
    END CATCH
END;
GO

CREATE OR ALTER VIEW mart.vw_ReplenishmentPlan AS
SELECT SiteID, SiteName, SiteType, ProductID, ProductName, Category, QuantityOnHand, StockValue, AvgDailyDemand,
       LeadTimeDays, ReorderPoint, DaysOfCover, DaysSinceLastOutflow, StockStatus
FROM mart.ReplenishmentPlan;
GO

-- GRAIN: one row per supplier: what the DC would order now from its
-- preferred suppliers, valued in NAD and checked against the supplier's
-- minimum order value.
CREATE OR ALTER VIEW mart.vw_SupplierReorderProposal AS
WITH Params AS (
    SELECT meta.fn_SettingDecimal('replenishment.safety_stock_days', 7) AS SafetyDays,
           meta.fn_SettingDecimal('replenishment.default_lead_time_days', 7) AS ReviewDays
), Needs AS (
    SELECT r.ProductID,
           CEILING(r.AvgDailyDemand * (r.LeadTimeDays + p.SafetyDays + p.ReviewDays) - r.QuantityOnHand) AS SuggestedQuantity
    FROM mart.ReplenishmentPlan AS r
    CROSS JOIN Params AS p
    WHERE r.SiteType = N'DISTRIBUTION_CENTRE' AND r.StockStatus IN ('REORDER', 'NEGATIVE_STOCK')
)
SELECT s.SupplierID, s.SupplierName, s.MinimumOrderValueNAD,
       COUNT(*) AS ProductsToOrder,
       SUM(n.SuggestedQuantity) AS UnitsToOrder,
       SUM(n.SuggestedQuantity * b.SupplierUnitCost) AS ProposedOrderValue,
       IIF(SUM(n.SuggestedQuantity * b.SupplierUnitCost) >= s.MinimumOrderValueNAD, 'MEETS_MINIMUM',
           'BELOW_MINIMUM_CONSOLIDATE') AS MinimumOrderStatus,
       IIF(SUM(n.SuggestedQuantity * b.SupplierUnitCost) >= s.MinimumOrderValueNAD, 0,
           s.MinimumOrderValueNAD - SUM(n.SuggestedQuantity * b.SupplierUnitCost)) AS ValueShortOfMinimum
FROM Needs AS n
JOIN dw.BridgeProductSupplier AS b ON b.ProductID = n.ProductID AND b.IsPreferredSupplier = 1 AND b.IsDeletedInSource = 0
JOIN dw.DimSupplier AS s ON s.SupplierID = b.SupplierID AND s.IsCurrent = 1
WHERE n.SuggestedQuantity > 0
GROUP BY s.SupplierID, s.SupplierName, s.MinimumOrderValueNAD;
GO
