-- 藥品庫存專案（SQL Server / SQL Server Express）
-- 用 SSMS 開啟後整份執行即可。範例資料皆為虛構。
-- 重複執行會先清掉舊物件。（注意：會連同資料一起清空，已有真實資料時不要整份重跑）
 
IF DB_ID(N'藥局庫存') IS NULL CREATE DATABASE 藥局庫存;
GO
USE 藥局庫存;
GO
 
-- 清理舊物件（順序：檢視/程序 → 子表 → 父表）
DROP PROCEDURE IF EXISTS dbo.sp_調劑;
DROP PROCEDURE IF EXISTS dbo.usp_同步目前健保價;
DROP VIEW IF EXISTS dbo.v_本藥局成分;
DROP VIEW IF EXISTS dbo.v_健保價核對;
DROP VIEW IF EXISTS dbo.v_現行健保價;
DROP VIEW IF EXISTS dbo.v_異動明細;
DROP VIEW IF EXISTS dbo.v_管制帳冊;
DROP VIEW IF EXISTS dbo.v_管制藥批次;
DROP VIEW IF EXISTS dbo.v_管制藥清單;
DROP VIEW IF EXISTS dbo.v_近效期;
DROP VIEW IF EXISTS dbo.v_藥品庫存;
DROP VIEW IF EXISTS dbo.v_批次庫存;
DROP TABLE IF EXISTS dbo.健保匯入紀錄;
DROP TABLE IF EXISTS dbo.健保價格歷史;
DROP TABLE IF EXISTS dbo.健保藥品品項;
DROP TABLE IF EXISTS dbo.庫存異動表;
DROP TABLE IF EXISTS dbo.效期表;
DROP TABLE IF EXISTS dbo.員工;
DROP TABLE IF EXISTS dbo.廠商表;
DROP TABLE IF EXISTS dbo.藥品總表;
DROP TABLE IF EXISTS dbo.製造商表;
GO
 
-- 1. 員工
CREATE TABLE dbo.員工 (
    員工編號  VARCHAR(10)   NOT NULL PRIMARY KEY,
    員工姓名  NVARCHAR(20)  NOT NULL,
    職稱      NVARCHAR(10)  NOT NULL,
    是否在職  BIT           NOT NULL DEFAULT 1      -- 離職只標記，不刪除，舊紀錄才對得回人
);
 
-- 2. 廠商表（供應商：向誰進貨）
CREATE TABLE dbo.廠商表 (
    廠商編號  INT IDENTITY(1,1) PRIMARY KEY,
    廠商代號  VARCHAR(10)   NOT NULL UNIQUE,
    廠商名稱  NVARCHAR(50)  NOT NULL,
    連絡電話  VARCHAR(20)   NULL
);
 
-- 3. 製造商表（藥品由誰製造；與供應商是不同的角色）
CREATE TABLE dbo.製造商表 (
    製造商編號  INT IDENTITY(1,1) PRIMARY KEY,
    製造商名稱  NVARCHAR(100) NOT NULL UNIQUE
);
 
-- 4. 藥品總表
CREATE TABLE dbo.藥品總表 (
    六編碼      CHAR(6)       NOT NULL PRIMARY KEY,
    藥品名稱    NVARCHAR(50)  NOT NULL,
    安全量      INT           NOT NULL DEFAULT 0 CHECK (安全量 >= 0),  -- 低於此量要叫貨
    國際條碼    CHAR(20)      NOT NULL UNIQUE,
    健保碼      CHAR(10)      NULL,                                    -- 自費/成藥沒有健保碼
    健保價      DECIMAL(10,2) NULL CHECK (健保價 >= 0),   -- 「目前價格」：由健保價格歷史同步（usp_同步目前健保價），不要手動改；歷史請看健保價格歷史
    安架量      INT           NOT NULL DEFAULT 0 CHECK (安架量 >= 0),  -- 架上固定補回的標準量
    儲位        VARCHAR(6)    NOT NULL,
    管制級別    TINYINT       NOT NULL DEFAULT 0
                CONSTRAINT CK_藥品_管制級別 CHECK (管制級別 BETWEEN 0 AND 4),  -- 0 = 非管制藥，1~4 = 管制級別
    是否停用    BIT           NOT NULL DEFAULT 0,
    -- 以下為列印管制藥帳冊需要的資料（許可證字號不是健保碼，兩者不可互相取代，也不要設 UNIQUE）
    製造商編號  INT           NULL CONSTRAINT FK_藥品_製造商 REFERENCES dbo.製造商表(製造商編號),
    許可證字號  NVARCHAR(30)  NULL,
    成分含量    NVARCHAR(100) NULL,
    最小單位    NVARCHAR(10)  NULL,
    -- 管制藥一定要填齊帳冊需要的欄位，由資料庫擋掉漏填
    CONSTRAINT CK_管制藥_帳冊欄位 CHECK (
        管制級別 = 0 OR (製造商編號 IS NOT NULL AND 許可證字號 IS NOT NULL
                         AND 成分含量 IS NOT NULL AND 最小單位 IS NOT NULL))
);
-- 健保碼可為空，但有值時不可重複（一般 UNIQUE 在 SQL Server 只允許一個 NULL，所以用篩選索引）
CREATE UNIQUE INDEX UX_藥品_健保碼 ON dbo.藥品總表(健保碼) WHERE 健保碼 IS NOT NULL;
 
-- 5. 效期表（批次）
CREATE TABLE dbo.效期表 (
    批次編號  INT IDENTITY(1,1) PRIMARY KEY,
    六編碼    CHAR(6)      NOT NULL REFERENCES dbo.藥品總表(六編碼),
    效期      DATE         NOT NULL,
    批號      VARCHAR(20)  NOT NULL,
    廠商編號  INT          NOT NULL REFERENCES dbo.廠商表(廠商編號),
    CONSTRAINT UQ_效期_藥品批號 UNIQUE (六編碼, 批號)
);
CREATE INDEX IX_效期_效期 ON dbo.效期表(效期);
 
-- 6. 庫存異動表（只記變動，現存量由加總算出）
CREATE TABLE dbo.庫存異動表 (
    異動編號  INT IDENTITY(1,1) PRIMARY KEY,
    異動類型  NVARCHAR(10)  NOT NULL
              CONSTRAINT CK_異動_類型 CHECK (異動類型 IN (N'進貨', N'調入', N'調劑', N'調出', N'報廢', N'退貨', N'盤盈', N'盤虧')),
    數量      INT           NOT NULL,              -- 入庫為正、出庫為負
    異動時間  DATETIME2(0)  NOT NULL DEFAULT SYSDATETIME(),
    備註      NVARCHAR(100) NULL,
    批次編號  INT           NOT NULL REFERENCES dbo.效期表(批次編號),
    員工編號  VARCHAR(10)   NOT NULL REFERENCES dbo.員工(員工編號),
    CONSTRAINT CK_異動_正負 CHECK (
        (異動類型 IN (N'進貨', N'調入', N'盤盈') AND 數量 > 0) OR
        (異動類型 IN (N'調劑', N'調出', N'報廢', N'退貨', N'盤虧') AND 數量 < 0)
    )
);
CREATE INDEX IX_異動_批次 ON dbo.庫存異動表(批次編號);
 
-- 7. 健保藥品品項（官方「健保用藥品項」的基本資料，一個健保碼一列）
--    欄位對應官方下載的 Excel（價格、起迄兩欄另存到健保價格歷史）。
--    不建立到藥品總表的外鍵：官方資料包含全台所有品項，藥品總表只有本藥局經營的藥。
CREATE TABLE dbo.健保藥品品項 (
    健保碼          CHAR(10)       NOT NULL PRIMARY KEY,   -- 官方「藥品代碼」
    英文名稱        NVARCHAR(300)  NULL,
    中文名稱        NVARCHAR(300)  NULL,
    成分名稱        NVARCHAR(500)  NULL,
    成分含量        NVARCHAR(200)  NULL,
    規格量          NVARCHAR(50)   NULL,
    單複方          NVARCHAR(20)   NULL,
    藥商            NVARCHAR(200)  NULL,
    劑型            NVARCHAR(50)   NULL,
    藥品分類        NVARCHAR(50)   NULL,
    分類分組名稱    NVARCHAR(500)  NULL,
    ATC代碼         VARCHAR(20)    NULL,
    給付規定章節    NVARCHAR(100)  NULL,
    備註            NVARCHAR(500)  NULL,
    最後匯入時間    DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME()
);
CREATE INDEX IX_健保品項_中文名稱 ON dbo.健保藥品品項(中文名稱);
CREATE INDEX IX_健保品項_ATC ON dbo.健保藥品品項(ATC代碼);
 
-- 8. 健保價格歷史（價格有生效期間，不覆蓋舊資料，才能查「某一天當時的價格」與比對漲跌）
CREATE TABLE dbo.健保價格歷史 (
    健保碼    CHAR(10)       NOT NULL,
    生效起日  DATE           NOT NULL,                     -- 官方為民國年，匯入時轉成西元
    生效迄日  DATE           NULL,                         -- NULL = 迄今仍有效
    價格      DECIMAL(10,2)  NOT NULL CONSTRAINT CK_價格_非負 CHECK (價格 >= 0),
    CONSTRAINT PK_健保價格歷史 PRIMARY KEY (健保碼, 生效起日),
    CONSTRAINT CK_價格_期間 CHECK (生效迄日 IS NULL OR 生效迄日 >= 生效起日)
    -- 同一健保碼的生效期間不應重疊：資料表不易用約束擋住，由匯入腳本檢查
);
 
-- 9. 健保匯入紀錄（每次匯入官方檔案留一筆，用來追蹤資料是否過期、這次改了什麼）
CREATE TABLE dbo.健保匯入紀錄 (
    匯入編號        INT IDENTITY(1,1) PRIMARY KEY,
    官方資料日期    DATE           NULL,                   -- 官方頁面標示的「資料更新時間」，轉成西元
    匯入時間        DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
    檔名            NVARCHAR(200)  NULL,
    檔案筆數        INT            NULL,
    新增品項數      INT            NULL,
    價格異動數      INT            NULL,
    備註            NVARCHAR(500)  NULL
);
GO
 
-- 檢視 1：每個批次的現存量
CREATE VIEW dbo.v_批次庫存 AS
SELECT  e.批次編號, e.六編碼, e.批號, e.效期,
        COALESCE(SUM(t.數量), 0) AS 現存量
FROM    dbo.效期表 e
LEFT JOIN dbo.庫存異動表 t ON t.批次編號 = e.批次編號
GROUP BY e.批次編號, e.六編碼, e.批號, e.效期;
GO
 
-- 檢視 2：每個藥品的總庫存、可用庫存（未過期）、近效庫存、過期庫存與是否需叫貨
CREATE OR ALTER VIEW dbo.v_藥品庫存 AS
SELECT  d.六編碼, d.藥品名稱, d.健保碼, d.國際條碼, d.儲位, d.安全量, d.安架量,
        COALESCE(SUM(b.現存量), 0) AS 總庫存,                                  -- 全部批次（含已過期）
        COALESCE(SUM(CASE WHEN b.效期 >= CAST(GETDATE() AS DATE)
                          THEN b.現存量 END), 0) AS 可用庫存,                    -- 未過期，可以調劑的量（sp_調劑也是這個口徑）
        COALESCE(SUM(CASE WHEN b.效期 >= CAST(GETDATE() AS DATE)
                           AND b.效期 <= DATEADD(DAY, 90, CAST(GETDATE() AS DATE))
                          THEN b.現存量 END), 0) AS 近效庫存,                    -- 可用庫存中，90 天內到期的部分（已包含在可用庫存內）
        COALESCE(SUM(CASE WHEN b.效期 <  CAST(GETDATE() AS DATE)
                          THEN b.現存量 END), 0) AS 過期庫存,                    -- 已過期但帳上仍有的量
        CASE WHEN COALESCE(SUM(CASE WHEN b.效期 >= CAST(GETDATE() AS DATE)
                                    THEN b.現存量 END), 0) < d.安全量
             THEN 1 ELSE 0 END AS 需叫貨                                        -- 以「可用庫存」判斷，過期的不算
FROM    dbo.藥品總表 d
LEFT JOIN dbo.v_批次庫存 b ON b.六編碼 = d.六編碼
WHERE   d.是否停用 = 0
GROUP BY d.六編碼, d.藥品名稱, d.健保碼, d.國際條碼, d.儲位, d.安全量, d.安架量;
GO
 
-- 檢視 3：90 天內到期（或已過期）且仍有庫存的批次（排序請在查詢時加 ORDER BY）
CREATE OR ALTER VIEW dbo.v_近效期 AS
SELECT  d.六編碼, d.藥品名稱, d.健保碼, d.國際條碼, b.批號, b.效期, b.現存量,
        DATEDIFF(DAY, CAST(GETDATE() AS DATE), b.效期) AS 剩餘天數
FROM    dbo.v_批次庫存 b
JOIN    dbo.藥品總表 d ON d.六編碼 = b.六編碼
WHERE   b.現存量 > 0
  AND   b.效期 <= DATEADD(DAY, 90, CAST(GETDATE() AS DATE));
GO
 
-- 檢視 4：管制藥資訊（一個藥品一列）
CREATE OR ALTER VIEW dbo.v_管制藥清單 AS
SELECT  六編碼, [健保碼], 國際條碼, 藥品名稱, 管制級別, 儲位
FROM    dbo.藥品總表
WHERE   管制級別 > 0 AND 是否停用 = 0;
GO
 
-- 檢視 5：管制藥庫存表（一個藥品的一個有庫存批次一列；沒有批次的管制藥仍會列出，批次欄位為空）
CREATE OR ALTER VIEW dbo.v_管制藥批次 AS
SELECT  d.六編碼, d.[健保碼], d.國際條碼, d.藥品名稱, d.管制級別, d.儲位,
        b.批號, b.效期, b.現存量
FROM    dbo.藥品總表 d
LEFT JOIN dbo.v_批次庫存 b ON b.六編碼 = d.六編碼
                          AND b.現存量 > 0
WHERE   d.管制級別 > 0 AND d.是否停用 = 0;
GO
 
-- 檢視 6：管制藥帳冊（一筆異動一列，欄位依紙本帳冊順序；結存數量依時間順序累加算出，不會漏寫）
-- 注意：不篩選「是否停用」，已停用藥品的歷史紀錄仍要保留在帳冊中。排序請在查詢時加 ORDER BY 六編碼, 日期, 異動編號。
CREATE OR ALTER VIEW dbo.v_管制帳冊 AS
SELECT  d.六編碼,
        d.藥品名稱                AS 品名,
        d.成分含量                AS 管制藥品成分及含量,
        d.許可證字號              AS 藥品許可證字號,
        d.管制級別,
        m.製造商名稱              AS 製造廠名稱,
        d.最小單位,
        t.異動編號,
        CAST(t.異動時間 AS DATE)   AS 日期,          -- 只取日期；時間仍在備註欄（調劑）
        t.異動類型                AS 收支原因,
        CASE WHEN t.數量 > 0 THEN t.數量  END AS 收入數量,
        CASE WHEN t.數量 > 0 THEN e.批號  END AS 收入藥品批號,
        CASE WHEN t.數量 < 0 THEN -t.數量 END AS 支出數量,
        SUM(t.數量) OVER (PARTITION BY d.六編碼
                          ORDER BY t.異動時間, t.異動編號
                          ROWS UNBOUNDED PRECEDING) AS 結存數量,
        -- 帳冊備註：進貨顯示購買來源、調劑顯示調劑時間（取自異動時間，不另外存一份），人工備註接在後面
        NULLIF(LTRIM(RTRIM(CONCAT(
            CASE t.異動類型
                 WHEN N'進貨' THEN v.廠商名稱
                 WHEN N'調劑' THEN CONVERT(VARCHAR(5), t.異動時間, 108)   -- 只顯示 hh:mm，日期已在「日期」欄
            END, N' ', t.備註))), N'') AS 備註,
        s.員工姓名                AS 經手人
FROM    dbo.庫存異動表 t
JOIN    dbo.效期表   e ON e.批次編號 = t.批次編號
JOIN    dbo.藥品總表 d ON d.六編碼   = e.六編碼
JOIN    dbo.員工     s ON s.員工編號 = t.員工編號
LEFT JOIN dbo.製造商表 m ON m.製造商編號 = d.製造商編號
LEFT JOIN dbo.廠商表   v ON v.廠商編號   = e.廠商編號
WHERE   d.管制級別 > 0;
GO
 
-- 異動明細：一筆異動一列（出入庫查詢的資料來源；管制藥與一般藥品放在一起，不篩選停用藥品，歷史紀錄要保留）
-- 出入庫的歸類規則寫在這裡：入庫＝進貨、調入；出庫＝調劑、調出、報廢、退貨；盤盈／盤虧是盤點調整，不算出入庫
CREATE OR ALTER VIEW dbo.v_異動明細 AS
SELECT  t.異動編號, t.異動時間, t.異動類型,
        CASE WHEN t.異動類型 IN (N'進貨', N'調入')                    THEN N'入庫'
             WHEN t.異動類型 IN (N'調劑', N'調出', N'報廢', N'退貨')  THEN N'出庫'
             ELSE N'盤點調整' END AS 出入庫,
        t.數量,
        e.六編碼, d.藥品名稱, d.健保碼, d.國際條碼, e.批號, s.員工姓名 AS 經手人
FROM    dbo.庫存異動表 t
JOIN    dbo.效期表   e ON e.批次編號 = t.批次編號
JOIN    dbo.藥品總表 d ON d.六編碼   = e.六編碼
JOIN    dbo.員工     s ON s.員工編號 = t.員工編號;
GO
 
-- 健保現行價：每個健保碼「今天有效」的價格（一碼一列；萬一期間重疊，取生效起日最新的一筆）
-- 價格 0.00 視為「無給付價」：歷史表照存，但不算現行價，避免把藥局帳上的健保價洗成 0
CREATE OR ALTER VIEW dbo.v_現行健保價 AS
SELECT  健保碼, 價格, 生效起日
FROM   (SELECT 健保碼, 價格, 生效起日,
               ROW_NUMBER() OVER (PARTITION BY 健保碼 ORDER BY 生效起日 DESC) AS rn
        FROM   dbo.健保價格歷史
        WHERE  價格 > 0
          AND  生效起日 <= CAST(GETDATE() AS DATE)
          AND (生效迄日 IS NULL OR 生效迄日 >= CAST(GETDATE() AS DATE))) x
WHERE   rn = 1;
GO
 
-- 健保價核對：本藥局藥品帳上的健保價 vs 官方現行價（只列有健保碼的藥）
-- 官方現行價為空時：今天有效的價格是 0.00 → 「官方無給付價(0.00)」；否則 → 「官方無資料」
CREATE OR ALTER VIEW dbo.v_健保價核對 AS
SELECT  d.六編碼, d.藥品名稱, d.健保碼,
        d.健保價 AS 帳上健保價,
        p.價格   AS 官方現行價,
        CASE WHEN p.價格 IS NOT NULL AND (d.健保價 IS NULL OR d.健保價 <> p.價格) THEN N'價格不同'
             WHEN p.價格 IS NOT NULL THEN N'一致'
             WHEN EXISTS (SELECT 1 FROM dbo.健保價格歷史 h
                          WHERE h.健保碼 = d.健保碼 AND h.價格 = 0
                            AND h.生效起日 <= CAST(GETDATE() AS DATE)
                            AND (h.生效迄日 IS NULL OR h.生效迄日 >= CAST(GETDATE() AS DATE)))
                  THEN N'官方無給付價(0.00)'
             ELSE N'官方無資料' END AS 核對結果
FROM    dbo.藥品總表 d
LEFT JOIN dbo.v_現行健保價 p ON p.健保碼 = d.健保碼
WHERE   d.健保碼 IS NOT NULL AND d.是否停用 = 0;
GO
 
-- 本藥局藥品 × 健保成分資料：每個「有健保碼、且在健保資料裡找得到」的本藥局藥品一列（替代藥品查詢頁使用）
-- 沒有健保碼、或健保資料找不到的藥不會出現；成分相同／ATC 相同的比對規則由頁面依基準藥決定。
CREATE OR ALTER VIEW dbo.v_本藥局成分 AS
SELECT  d.六編碼, d.藥品名稱, d.健保碼, d.儲位, d.管制級別,
        n.成分名稱, n.成分含量, n.劑型, n.ATC代碼,
        s.可用庫存, s.總庫存
FROM    dbo.藥品總表 d
JOIN    dbo.健保藥品品項 n ON n.健保碼 = d.健保碼
JOIN    dbo.v_藥品庫存 s   ON s.六編碼 = d.六編碼
WHERE   d.是否停用 = 0;
GO
 
-- 同步「目前價格」：把藥品總表.健保價 更新成健保價格歷史中今天有效的價格（價格 0.00 不算，見 v_現行健保價）。
-- 匯入官方檔案後會自動執行；價格生效日（通常是 1 號）到了、又沒有重新匯入時，也可以手動執行一次。
CREATE OR ALTER PROCEDURE dbo.usp_同步目前健保價
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE d
    SET    d.健保價 = p.價格
    FROM   dbo.藥品總表 d
    JOIN   dbo.v_現行健保價 p ON p.健保碼 = d.健保碼
    WHERE  d.健保價 IS NULL OR d.健保價 <> p.價格;
    SELECT @@ROWCOUNT AS 更新筆數;
END;
GO
 
-- 預存程序：調劑（先到期先出 FEFO，整筆在同一個交易內完成）
CREATE OR ALTER PROCEDURE dbo.sp_調劑
    @六編碼   CHAR(6),
    @數量     INT,
    @員工編號 VARCHAR(10)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;   -- 避免兩人同時調劑同一藥品而超扣
 
    -- 前置檢查（先在交易外擋掉明顯的錯誤）
    IF @數量 IS NULL OR @數量 <= 0
        THROW 50001, N'調劑數量必須大於 0', 1;
 
    IF NOT EXISTS (SELECT 1 FROM dbo.員工 WHERE 員工編號 = @員工編號 AND 是否在職 = 1)
        THROW 50003, N'員工不存在或已離職', 1;
 
    IF NOT EXISTS (SELECT 1 FROM dbo.藥品總表 WHERE 六編碼 = @六編碼 AND 是否停用 = 0)
        THROW 50004, N'找不到此藥品，或藥品已停用', 1;
 
    BEGIN TRAN;
 
    DECLARE @可用 INT = (
        SELECT COALESCE(SUM(現存量), 0)
        FROM dbo.v_批次庫存
        WHERE 六編碼 = @六編碼 AND 現存量 > 0 AND 效期 >= CAST(GETDATE() AS DATE));
 
    IF @可用 < @數量
    BEGIN
        ROLLBACK;
        THROW 50002, N'可用庫存不足（已排除過期批次）', 1;
    END
 
    ;WITH 可用批次 AS (
        SELECT 批次編號, 現存量,
               SUM(現存量) OVER (ORDER BY 效期, 批次編號 ROWS UNBOUNDED PRECEDING) AS 累計
        FROM dbo.v_批次庫存
        WHERE 六編碼 = @六編碼 AND 現存量 > 0 AND 效期 >= CAST(GETDATE() AS DATE))
    INSERT INTO dbo.庫存異動表 (批次編號, 異動類型, 數量, 員工編號)
    SELECT 批次編號, N'調劑',
           -(CASE WHEN 累計 <= @數量 THEN 現存量 ELSE @數量 - (累計 - 現存量) END),
           @員工編號
    FROM 可用批次
    WHERE 累計 - 現存量 < @數量;
 
    COMMIT;
END;
GO