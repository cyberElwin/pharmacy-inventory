"""匯入健保署「整批下載」的固定寬度文字檔（all1_YYYYM_1.TXT、_2.TXT；Big5/cp950，每筆 1859 位元組）。
沿用 import_nhi.py 的比對與寫入邏輯：只新增與更新，不刪除；同一健保碼不同期間的價格各存一列。

用法（PowerShell，先設好 PHARMACY_ODBC，與 run.bat 相同）：
    python import_nhi_txt.py all1_11511_1.TXT all1_11511_2.TXT --data-date 115/11/01 --dry-run   # 先預覽
    python import_nhi_txt.py all1_11511_1.TXT all1_11511_2.TXT --data-date 115/11/01             # 正式匯入
    加 --fast 可用 pyodbc 的 fast_executemany 加速（首次匯入約 20 萬列時建議；若出現型別錯誤就拿掉）

欄位位置（位元組，從 0 起算）是我從樣本檔推斷的，未對照官方說明；若官方改版，請先 --dry-run 檢查「無法解析」數量。
"""
import argparse
import os
import sys
from datetime import date

import pandas as pd

from import_nhi import ITEM_COLS, clean, find_overlaps, print_summary, roc_to_date, run_import

REC_LEN = 1859
# 欄位：(起, 迄) 位元組
F = dict(flag=(14, 15), code=(17, 27), price=(28, 37), start=(38, 45), end=(46, 53),
         eng=(54, 127), spec_amt=(176, 183), spec_unit=(183, 193), form=(358, 372), cat=(767, 768),
         zh=(771, 830), group=(900, 1025), vendor=(1806, 1849), atc=(1849, 1857))
# 成分區塊（名稱起, 含量起, 單位起, 單位迄）：第 1 個成分 + 後續成分；含量靠右對齊於單位起點之前
ING = [(236, 296, 306, 316), (1200, 1260, 1270, 1280), (1322, 1382, 1391, 1401),
       (1443, 1503, 1512, 1522), (1564, 1624, 1633, 1643), (1685, 1745, 1754, 1764)]


def seg(b: bytes, a: int, z: int) -> str | None:
    s = b[a:z].decode("cp950", errors="replace").strip()
    return s or None


def roc7(s):
    """'1040401' → date；'9991231' → None（迄今）；格式不對丟 ValueError。"""
    if s is None or len(s) != 7 or not s.isdigit():
        raise ValueError(s)
    if s == "9991231":
        return None
    return roc_to_date(f"{int(s[:3])}.{s[3:5]}.{s[5:7]}")


def parse_line(b: bytes):
    code = seg(b, *F["code"])
    start = roc7(seg(b, *F["start"]))
    if start is None:
        raise ValueError("起日")
    end = roc7(seg(b, *F["end"]))
    ptxt = seg(b, *F["price"])
    price = None if ptxt in (None, "-") else float(ptxt)      # '-' = 官方沒有價格：品項照匯入，不寫價格列
    if not code or len(code) != 10 or (price is not None and price < 0):
        raise ValueError("代碼/價格")
    names, amts = [], []
    for ns, ae, us, ue in ING:
        n = seg(b, ns, ae)
        if not n:
            continue
        names.append(n)
        amt, unit = seg(b, ae, us), seg(b, us, ue)
        amts.append(" ".join(x for x in (amt, unit) if x) or "")
    spec = " ".join(x for x in (seg(b, *F["spec_amt"]), seg(b, *F["spec_unit"])) if x) or None
    flag = seg(b, *F["flag"])
    item = dict(英文名稱=seg(b, *F["eng"]), 中文名稱=seg(b, *F["zh"]),
                成分名稱="; ".join(names) or None, 成分含量="; ".join(amts) if any(amts) else None,
                規格量=spec, 單複方={"Y": "複方", "N": "單方"}.get(flag, flag), 藥商=seg(b, *F["vendor"]),
                劑型=seg(b, *F["form"]), 藥品分類=seg(b, *F["cat"]),   # 藥品分類：原樣存代碼，意義未確認
                分類分組名稱=seg(b, *F["group"]), ATC代碼=seg(b, *F["atc"]), 給付規定章節=None, 備註=None)
    return code, start, end, price, item


def read_txt(paths):
    """逐行讀（不一次載入整檔），回傳 (品項 DataFrame, 價格 DataFrame, 問題列清單)。"""
    price_rows, items, problems, no_price = [], {}, [], 0
    for path in paths:
        with open(path, "rb") as f:
            for n, line in enumerate(f, 1):
                line = line.rstrip(b"\r\n")
                if not line.strip():
                    continue
                if len(line) != REC_LEN:
                    problems.append(f"{os.path.basename(path)} 第 {n} 行：長度 {len(line)}（應為 {REC_LEN}）")
                    continue
                try:
                    code, start, end, price, item = parse_line(line)
                except (ValueError, TypeError) as e:
                    problems.append(f"{os.path.basename(path)} 第 {n} 行：無法解析（{e}）")
                    continue
                if price is None:
                    no_price += 1
                else:
                    price_rows.append((code, start, end, price))
                if code not in items or start >= items[code][0]:      # 品項資料取「起日最新」那列
                    items[code] = (start, item)
    if not price_rows:
        raise SystemExit("沒有任何可匯入的資料列，請確認檔案。")
    prices = (pd.DataFrame(price_rows, columns=["健保碼", "生效起日", "生效迄日", "價格"])
              .drop_duplicates(["健保碼", "生效起日"], keep="last"))
    print(f"價格欄為「-」（無價格，只匯入品項、不寫價格列）：{no_price} 列")
    items_df = pd.DataFrame([{"健保碼": c, **v[1]} for c, v in items.items()])[["健保碼"] + ITEM_COLS]
    return items_df, prices, problems


def main():
    ap = argparse.ArgumentParser(description="匯入健保署整批下載的固定寬度文字檔")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--data-date", help="資料日期（民國年，例如 115/11/01）；記在匯入紀錄")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fast", action="store_true", help="使用 fast_executemany 加速")
    a = ap.parse_args()
    data_date = None
    if a.data_date:
        try:
            data_date = roc_to_date(a.data_date.replace("/", "."))
        except ValueError:
            sys.exit("--data-date 格式要像 115/11/01")
    items, prices, problems = read_txt(a.files)
    zero = int((prices["價格"] == 0).sum())
    print(f"讀入 {len(prices)} 列價格、{len(items)} 個品項；其中價格 0.00（視為無給付價）{zero} 列")
    cs = os.environ.get("PHARMACY_ODBC")
    if not cs:
        sys.exit("請先設定環境變數 PHARMACY_ODBC（內容與 run.bat 相同）")
    import pyodbc
    conn = pyodbc.connect(cs)
    s = run_import(conn, items, prices, filename=" + ".join(os.path.basename(f) for f in a.files),
                   data_date=data_date, dry_run=a.dry_run, fast=a.fast)
    print_summary(s, problems, a.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
