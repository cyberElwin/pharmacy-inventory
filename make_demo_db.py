"""產生示範用 SQLite 資料庫（虛構資料），檢視表的欄位與 SQL Server 版相同。
執行：python make_demo_db.py
"""
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB = Path(__file__).with_name("demo.db")

SCHEMA = """
CREATE TABLE 藥品總表 (
    六編碼 CHAR(6) PRIMARY KEY, 藥品名稱 TEXT NOT NULL, 安全量 INT NOT NULL,
    國際條碼 CHAR(20) NOT NULL UNIQUE, 健保碼 CHAR(10), 健保價 NUMERIC,
    安架量 INT NOT NULL, 儲位 TEXT NOT NULL, 是否停用 INT NOT NULL DEFAULT 0);
CREATE TABLE 效期表 (
    批次編號 INTEGER PRIMARY KEY AUTOINCREMENT, 六編碼 CHAR(6) NOT NULL,
    效期 DATE NOT NULL, 批號 TEXT NOT NULL, UNIQUE (六編碼, 批號));
CREATE TABLE 庫存異動表 (
    異動編號 INTEGER PRIMARY KEY AUTOINCREMENT, 批次編號 INT NOT NULL,
    異動類型 TEXT NOT NULL, 數量 INT NOT NULL);

CREATE VIEW v_批次庫存 AS
SELECT e.批次編號, e.六編碼, e.批號, e.效期, COALESCE(SUM(t.數量), 0) AS 現存量
FROM 效期表 e LEFT JOIN 庫存異動表 t ON t.批次編號 = e.批次編號
GROUP BY e.批次編號, e.六編碼, e.批號, e.效期;

CREATE VIEW v_藥品庫存 AS
SELECT d.六編碼, d.藥品名稱, d.儲位, d.安全量, d.安架量,
       COALESCE(SUM(b.現存量), 0) AS 總庫存,
       CASE WHEN COALESCE(SUM(b.現存量), 0) < d.安全量 THEN 1 ELSE 0 END AS 需叫貨
FROM 藥品總表 d LEFT JOIN v_批次庫存 b ON b.六編碼 = d.六編碼
WHERE d.是否停用 = 0
GROUP BY d.六編碼, d.藥品名稱, d.儲位, d.安全量, d.安架量;

CREATE VIEW v_近效期 AS
SELECT d.藥品名稱, b.批號, b.效期, b.現存量,
       CAST(julianday(b.效期) - julianday(date('now')) AS INTEGER) AS 剩餘天數
FROM v_批次庫存 b JOIN 藥品總表 d ON d.六編碼 = b.六編碼
WHERE b.現存量 > 0 AND b.效期 <= date('now', '+90 day');
"""

# (六編碼, 名稱, 安全量, 條碼, 安架量, 儲位)
DRUGS = [
    ("A00001", "乙醯胺酚 500mg", 200, "4710000000011", 100, "A-01"),
    ("A00002", "第二代抗組織胺 10mg", 100, "4710000000028", 60, "A-02"),
    ("A00003", "氧化鋅軟膏 20g", 10, "4710000000035", 5, "B-01"),
    ("A00004", "示範止咳糖漿 60ml", 20, "4710000000042", 10, "B-02"),
    ("A00005", "示範胃藥 40mg", 150, "4710000000059", 80, "A-03"),
]
# (六編碼, 距今天數, 批號, 進貨量)
BATCHES = [
    ("A00001", 45, "L2401", 150), ("A00001", 400, "L2503", 300),
    ("A00002", 200, "L2502", 80),
    ("A00003", 60, "L2406", 5),
    ("A00004", -10, "L2308", 12),   # 已過期
    ("A00004", 25, "L2402", 8),
    ("A00005", 20, "L2404", 200),
]


def main():
    DB.unlink(missing_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    today = date.today()
    conn.executemany(
        "INSERT INTO 藥品總表 (六編碼, 藥品名稱, 安全量, 國際條碼, 安架量, 儲位) VALUES (?,?,?,?,?,?)", DRUGS)
    for i, (code, days, lot, qty) in enumerate(BATCHES, start=1):
        conn.execute("INSERT INTO 效期表 (六編碼, 效期, 批號) VALUES (?,?,?)",
                     (code, (today + timedelta(days=days)).isoformat(), lot))
        conn.execute("INSERT INTO 庫存異動表 (批次編號, 異動類型, 數量) VALUES (?, '進貨', ?)", (i, qty))
    conn.commit()
    conn.close()
    print("已建立", DB)


if __name__ == "__main__":
    main()
