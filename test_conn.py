"""測試 Python 能不能連上 SQL Server 的「藥局庫存」資料庫。
執行：python test_conn.py
"""
import pyodbc

print("可用的 SQL Server 驅動：", [d for d in pyodbc.drivers() if "SQL Server" in d])

cs = (
    r"DRIVER={ODBC Driver 18 for SQL Server};"
    r"SERVER=localhost\SQLEXPRESS;"
    r"DATABASE=藥局庫存;"
    r"Trusted_Connection=yes;TrustServerCertificate=yes;"
)
conn = pyodbc.connect(cs)
count = conn.cursor().execute("SELECT COUNT(*) FROM v_藥品庫存").fetchone()[0]
print(f"連線成功，v_藥品庫存 共 {count} 筆")
conn.close()
