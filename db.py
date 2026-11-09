"""資料連線層：同一份 app.py 可連 SQL Server，或用內建的示範資料庫（SQLite）。

切換方式（環境變數）：
  PHARMACY_DB=sqlserver   連 SQL Server（需同時設定 PHARMACY_ODBC）
  未設定 / demo           使用 demo.db 的虛構資料

PHARMACY_ODBC 範例（Windows 驗證、本機 SQL Express）：
  DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\\SQLEXPRESS;
  DATABASE=藥局庫存;Trusted_Connection=yes;TrustServerCertificate=yes;
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

DEMO_DB = Path(__file__).with_name("demo.db")


def mode() -> str:
    return "sqlserver" if os.environ.get("PHARMACY_DB", "demo").lower() == "sqlserver" else "demo"


def _connect():
    if mode() == "sqlserver":
        import pyodbc  # 只有連 SQL Server 才需要安裝

        cs = os.environ.get("PHARMACY_ODBC")
        if not cs:
            raise RuntimeError("已選擇 sqlserver 模式，但沒有設定環境變數 PHARMACY_ODBC")
        return pyodbc.connect(cs)

    if not DEMO_DB.exists():
        raise RuntimeError("找不到 demo.db，請先執行：python make_demo_db.py")
    return sqlite3.connect(DEMO_DB)


def check_connection() -> tuple[bool, str]:
    """實際連一次資料庫，回傳（是否成功, 失敗時的原因）。"""
    try:
        conn = _connect()
        try:
            conn.cursor().execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True, ""
    except Exception as e:
        return False, str(e)


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """執行唯讀查詢並回傳 DataFrame。SQL 一律用 ? 參數，不要自己拼字串。"""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
        return pd.DataFrame(rows, columns=cols)
    finally:
        conn.close()


def connect():
    """回傳一條資料庫連線（用完要自己 close）。給需要自己管理交易的程式用，例如健保檔匯入。"""
    return _connect()


@contextmanager
def transaction():
    """寫入資料用：with db.transaction() as cur: cur.execute(...)。
    區塊內全部成功才一起 commit；中途任何一步出錯，整批 rollback，資料庫維持原狀。
    SQL 一律用 ? 參數，不要拼字串。"""
    conn = _connect()
    try:
        yield conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
