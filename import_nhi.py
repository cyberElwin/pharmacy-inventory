"""匯入健保署「健保用藥品項」下載的 Excel，更新健保藥品品項、健保價格歷史、健保匯入紀錄，
並把藥品總表的「目前價格」（健保價）同步成今天有效的價格。

用法（PowerShell，先設好連線環境變數 PHARMACY_ODBC，與 run.bat 相同）：
    python import_nhi.py 下載的檔案.xlsx --data-date 115/10/27 --dry-run     # 先預覽，不寫入
    python import_nhi.py 下載的檔案.xlsx --data-date 115/10/27               # 確認後正式匯入

--data-date：健保署頁面上標示的「資料更新時間」（民國年），會記在匯入紀錄，用來判斷資料是否過期。
重要：腳本只「新增與更新」，不會刪除或把任何品項標成停止給付，所以部分查詢的檔案也不會弄壞資料。
"""
import argparse
import os
import re
import sys
from datetime import date, datetime

import pandas as pd

# 資料庫欄位 → Excel 標題的開頭文字（用開頭比對，避免全形／半形括號、截斷造成對不上）
HEADERS = {
    "健保碼": "藥品代碼", "英文名稱": "藥品名稱(英", "中文名稱": "藥品名稱(中",
    "成分名稱": "成分名稱", "成分含量": "成分含量", "規格量": "規格量", "單複方": "單複方",
    "價格": "價格", "起迄": "起迄", "藥商": "藥商", "劑型": "劑型", "藥品分類": "藥品分類",
    "分類分組名稱": "分類分組名", "ATC代碼": "ATC代碼", "給付規定章節": "給付規定章", "備註": "備註",
}
ITEM_COLS = ["英文名稱", "中文名稱", "成分名稱", "成分含量", "規格量", "單複方", "藥商", "劑型",
             "藥品分類", "分類分組名稱", "ATC代碼", "給付規定章節", "備註"]
SYNC_SQL = "EXEC dbo.usp_同步目前健保價"          # SQL Server 上同步「目前價格」的預存程序


def roc_to_date(text: str) -> date:
    """民國年 '103.07.01' → date(2014, 7, 1)。"""
    y, m, d = (int(x) for x in text.strip().split("."))
    return date(y + 1911, m, d)


def parse_period(text):
    """'103.07.01-迄今' / '100.12.01-110.12.31' → (起日, 迄日或 None)。格式不對回傳 None。"""
    if text is None or str(text).strip() == "" or str(text) == "nan":
        return None
    t = re.sub(r"\s+", "", str(text)).replace("－", "-").replace("~", "-").replace("至", "-")
    m = re.fullmatch(r"(\d{2,3}\.\d{2}\.\d{2})-?(\d{2,3}\.\d{2}\.\d{2}|迄今)?", t)
    if not m:
        return None
    try:
        start = roc_to_date(m.group(1))
        end = roc_to_date(m.group(2)) if m.group(2) and m.group(2) != "迄今" else None
    except ValueError:
        return None
    return start, end


def clean(v):
    """空白、nan 一律視為空值（None）；其餘去掉前後空白。"""
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" or s.lower() == "nan" else s


def read_excel(path: str):
    """讀官方 Excel，回傳 (品項 DataFrame, 價格 DataFrame, 問題列清單)。"""
    raw = pd.read_excel(path, dtype=str)
    norm = {c: str(c).replace("（", "(").replace("）", ")").replace(" ", "") for c in raw.columns}
    col_of, missing = {}, []
    for key, prefix in HEADERS.items():
        hit = [c for c, n in norm.items() if n.startswith(prefix)]
        if hit:
            col_of[key] = hit[0]
        elif key in ("健保碼", "價格", "起迄"):
            missing.append(prefix)
    if missing:
        raise SystemExit(f"Excel 缺少必要欄位：{missing}。目前的欄位：{list(raw.columns)}")
    df = pd.DataFrame({k: raw[c] for k, c in col_of.items()})
    for c in ITEM_COLS:
        if c not in df.columns:
            df[c] = None
    problems, rows = [], []
    for i, r in df.iterrows():
        code, period = clean(r["健保碼"]), parse_period(r["起迄"])
        try:
            price = float(clean(r["價格"]))
        except (TypeError, ValueError):
            price = None
        if not code or len(code) != 10 or period is None or price is None or price < 0:
            problems.append(f"Excel 第 {i + 2} 列：代碼={code} 價格={r['價格']} 起迄={r['起迄']}")
            continue
        rows.append({"健保碼": code, "生效起日": period[0], "生效迄日": period[1], "價格": price,
                     **{c: clean(r[c]) for c in ITEM_COLS}})
    all_rows = pd.DataFrame(rows)
    if all_rows.empty:
        raise SystemExit("沒有任何可匯入的資料列，請確認檔案。")
    prices = (all_rows[["健保碼", "生效起日", "生效迄日", "價格"]]
              .drop_duplicates(["健保碼", "生效起日"], keep="last"))
    # 品項基本資料：同一健保碼取「生效起日最新」的那一列
    items = (all_rows.sort_values("生效起日").drop_duplicates("健保碼", keep="last")[["健保碼"] + ITEM_COLS])
    return items, prices, problems


def find_overlaps(prices: pd.DataFrame):
    """同一健保碼的生效期間不應重疊，回傳有問題的健保碼。"""
    bad = []
    for code, g in prices.sort_values("生效起日").groupby("健保碼"):
        prev_end = None
        for _, r in g.iterrows():
            if prev_end is not None and r["生效起日"] <= prev_end:
                bad.append(code)
                break
            prev_end = r["生效迄日"] if r["生效迄日"] is not None else date.max
    return bad


def fetch(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)


def d2s(d):
    return None if d is None else d.isoformat()


def to_date(x):
    """資料庫讀回的日期（date、字串或空值）統一成 date；空值回傳 None。"""
    if x is None or (isinstance(x, float) and x != x) or str(x) in ("None", "NaT", "nan", ""):
        return None
    return x if isinstance(x, date) and not isinstance(x, datetime) else date.fromisoformat(str(x)[:10])


def run_import(conn, items, prices, *, filename, data_date, dry_run, sync_sql=SYNC_SQL, fast=False):
    cur = conn.cursor()
    if fast:
        cur.fast_executemany = True                          # 只有 pyodbc 有此屬性；大量匯入時可加速
    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    # ---- 品項：新增 / 有變動才更新 ----
    old_items = fetch(cur, "SELECT 健保碼, " + ", ".join(ITEM_COLS) + " FROM 健保藥品品項")
    old_map = {r["健保碼"].strip(): {c: clean(r[c]) for c in ITEM_COLS} for _, r in old_items.iterrows()}
    new_items, upd_items = [], []
    for _, r in items.iterrows():
        vals = {c: clean(r[c]) for c in ITEM_COLS}
        code = r["健保碼"]
        if code not in old_map:
            new_items.append((code, *[vals[c] for c in ITEM_COLS], now))
        elif old_map[code] != vals:
            upd_items.append((*[vals[c] for c in ITEM_COLS], now, code))

    # ---- 價格歷史：以 (健保碼, 生效起日) 為鍵 ----
    old_p = fetch(cur, "SELECT 健保碼, 生效起日, 生效迄日, 價格 FROM 健保價格歷史")
    old_p["生效起日"] = old_p["生效起日"].map(to_date)
    old_p["生效迄日"] = old_p["生效迄日"].map(to_date)
    old_pm = {(r["健保碼"].strip(), d2s(r["生效起日"])): (d2s(r["生效迄日"]), float(r["價格"]))
              for _, r in old_p.iterrows()}
    new_p, upd_p, price_changes = [], [], []
    for _, r in prices.iterrows():
        key = (r["健保碼"], d2s(r["生效起日"]))
        end, price = d2s(r["生效迄日"]), float(r["價格"])
        if key not in old_pm:
            new_p.append((r["健保碼"], key[1], end, price))
        elif old_pm[key] != (end, price):
            if old_pm[key][1] != price:
                price_changes.append((r["健保碼"], key[1], old_pm[key][1], price))
            upd_p.append((end, price, r["健保碼"], key[1]))

    # ---- 重疊檢查（合併舊資料後）----
    old_p["健保碼"] = old_p["健保碼"].str.strip()
    merged = pd.concat([old_p, prices]).drop_duplicates(["健保碼", "生效起日"], keep="last")
    overlaps = find_overlaps(merged[merged["健保碼"].isin(prices["健保碼"])])

    # ---- 你的藥品（藥品總表）有哪些價格變動 ----
    mine = fetch(cur, "SELECT 健保碼, 藥品名稱 FROM 藥品總表 WHERE 健保碼 IS NOT NULL")
    mine_map = {r["健保碼"].strip(): r["藥品名稱"] for _, r in mine.iterrows()}
    my_changes = [(mine_map[c], c, s, o, n) for c, s, o, n in price_changes if c in mine_map]

    summary = dict(檔案品項數=len(items), 新增品項=len(new_items), 更新品項=len(upd_items),
                   新增價格列=len(new_p), 更新價格列=len(upd_p), 價格異動=len(price_changes),
                   重疊健保碼=overlaps, 我的藥價格異動=my_changes)
    if dry_run:
        return summary

    try:
        def many(sql, rows):                                   # fast_executemany 不接受空清單
            if rows:
                cur.executemany(sql, rows)
        many("INSERT INTO 健保藥品品項 (健保碼, " + ", ".join(ITEM_COLS) + ", 最後匯入時間) VALUES ("
                        + ", ".join(["?"] * (len(ITEM_COLS) + 2)) + ")", new_items)
        many("UPDATE 健保藥品品項 SET " + ", ".join(f"{c} = ?" for c in ITEM_COLS)
                        + ", 最後匯入時間 = ? WHERE 健保碼 = ?", upd_items)
        many("INSERT INTO 健保價格歷史 (健保碼, 生效起日, 生效迄日, 價格) VALUES (?, ?, ?, ?)", new_p)
        many("UPDATE 健保價格歷史 SET 生效迄日 = ?, 價格 = ? WHERE 健保碼 = ? AND 生效起日 = ?", upd_p)
        cur.execute(sync_sql)                                   # 「目前價格」同步到藥品總表.健保價
        cur.execute("INSERT INTO 健保匯入紀錄 (官方資料日期, 匯入時間, 檔名, 檔案筆數, 新增品項數, 價格異動數, 備註) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (d2s(data_date), now, os.path.basename(filename), len(prices), len(new_items), len(price_changes),
                     f"價格列新增{len(new_p)}/更新{len(upd_p)}" + (f"；期間重疊：{overlaps}" if overlaps else "")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return summary


def print_summary(s, problems, dry_run):
    print("【預覽，尚未寫入資料庫】" if dry_run else "【已匯入】")
    for k in ("檔案品項數", "新增品項", "更新品項", "新增價格列", "更新價格列", "價格異動"):
        print(f"  {k}：{s[k]}")
    if s["我的藥價格異動"]:
        print("  你的藥品價格異動：")
        for name, code, start, old, new in s["我的藥價格異動"]:
            print(f"    {name}（{code}）{start} 起：{old} → {new}")
    if s["重疊健保碼"]:
        print(f"  ⚠ 這些健保碼的生效期間有重疊，請人工確認：{s['重疊健保碼'][:20]}")
    if problems:
        print(f"  ⚠ 有 {len(problems)} 列無法解析，已略過（最多顯示 10 列）：")
        for p in problems[:10]:
            print("    ", p)


def main():
    ap = argparse.ArgumentParser(description="匯入健保署下載的藥品品項 Excel")
    ap.add_argument("file")
    ap.add_argument("--data-date", help="官方頁面標示的資料更新時間（民國年，例如 115/10/27）")
    ap.add_argument("--dry-run", action="store_true", help="只預覽結果，不寫入資料庫")
    a = ap.parse_args()
    data_date = None
    if a.data_date:
        try:
            data_date = roc_to_date(a.data_date.replace("/", "."))
        except ValueError:
            sys.exit("--data-date 格式要像 115/10/27")
    items, prices, problems = read_excel(a.file)
    cs = os.environ.get("PHARMACY_ODBC")
    if not cs:
        sys.exit("請先設定環境變數 PHARMACY_ODBC（內容與 run.bat 相同）")
    import pyodbc
    conn = pyodbc.connect(cs)
    s = run_import(conn, items, prices, filename=a.file, data_date=data_date, dry_run=a.dry_run)
    print_summary(s, problems, a.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
