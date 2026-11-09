"""「資料維護」頁：1. 總表更新與維護　2. 健保資料庫更新與維護　3. 盤點表上傳

這一頁會「寫入」資料庫，所以和查詢頁有幾個不同的習慣：
  * 寫入一律走 db.transaction()：整批成功才 commit，中途出錯就整批 rollback。
  * 檢查「資料是否重複、庫存是多少」時用 db.query()（不經過快取），避免讀到 30 秒前的舊資料。
  * 寫入成功後呼叫 st.cache_data.clear()，其他頁面才會馬上看到新資料。
  * 重要的寫入都要「先預覽、再勾選確認」才會執行。
  * 藥品不刪除，只標記「停用」（歷史異動、管制帳冊都還要對得回藥品）。
app.py 只需要：import maintenance_page，選到「資料維護」時呼叫 maintenance_page.render(load)。
"""
import io
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import db

SUBPAGES = ["總表更新與維護", "健保資料庫更新與維護", "盤點表上傳"]
GRADE = {0: "0（非管制藥）", 1: "第 1 級", 2: "第 2 級", 3: "第 3 級", 4: "第 4 級"}


def _s(v) -> str:
    """資料庫讀出來的值 → 乾淨的字串（空值變空字串，CHAR 欄位的尾端空白去掉）。"""
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _flash():
    """寫入成功後會 rerun 畫面，訊息先存在 session_state，rerun 之後再顯示一次。"""
    msg = st.session_state.pop("mt_flash", None)
    if msg:
        st.success(msg)


def _done(msg: str):
    st.cache_data.clear()                       # 讓其他頁面的 load() 快取失效
    st.session_state["mt_flash"] = msg
    st.rerun()


# ════════════════════════════════════════════════════════════════
# 1. 總表更新與維護（藥品總表）
# ════════════════════════════════════════════════════════════════
def _drug_form(prefix: str, row, makers: pd.DataFrame):
    """畫出藥品欄位的表單內容，回傳各欄位目前的值。row=None 代表新增；否則帶入現有資料。
    widget 的 key 帶上六編碼，切換藥品時欄位會跟著換成新藥品的值。"""
    new = row is None
    g = (lambda k: "" if new else _s(row[k]))
    gi = (lambda k: 0 if new or pd.isna(row[k]) else int(row[k]))
    c1, c2, c3 = st.columns(3)
    code = c1.text_input("六編碼（6 碼，建立後不可改）", value=g("六編碼"), max_chars=6, disabled=not new, key=f"{prefix}_code")
    name = c2.text_input("藥品名稱", value=g("藥品名稱"), max_chars=50, key=f"{prefix}_name")
    barcode = c3.text_input("國際條碼", value=g("國際條碼"), max_chars=20, key=f"{prefix}_bar")
    c4, c5, c6, c7 = st.columns(4)
    nhi = c4.text_input("健保碼（10 碼，自費藥可留空）", value=g("健保碼"), max_chars=10, key=f"{prefix}_nhi")
    safe = c5.number_input("安全量", min_value=0, value=gi("安全量"), step=1, key=f"{prefix}_safe")
    shelf = c6.number_input("安架量", min_value=0, value=gi("安架量"), step=1, key=f"{prefix}_shelf")
    loc = c7.text_input("儲位", value=g("儲位"), max_chars=6, key=f"{prefix}_loc")
    c8, c9 = st.columns([1, 1])
    grade = c8.selectbox("管制級別", list(GRADE), index=0 if new else int(row["管制級別"]),
                         format_func=GRADE.get, key=f"{prefix}_grade")
    off = c9.checkbox("停用（不再出現在庫存與調劑，歷史紀錄保留）", value=False if new else bool(row["是否停用"]), key=f"{prefix}_off")
    st.markdown("**管制藥帳冊需要的資料**（管制級別大於 0 時四項都必填）")
    d1, d2, d3, d4 = st.columns(4)
    ids = [None] + makers["製造商編號"].tolist()
    names = dict(zip(makers["製造商編號"], makers["製造商名稱"]))
    cur_id = None if new or pd.isna(row["製造商編號"]) else int(row["製造商編號"])
    maker = d1.selectbox("製造商", ids, index=ids.index(cur_id) if cur_id in ids else 0,
                         format_func=lambda i: "（未選）" if i is None else names[i], key=f"{prefix}_maker")
    lic = d2.text_input("許可證字號", value=g("許可證字號"), max_chars=30, key=f"{prefix}_lic")
    comp = d3.text_input("成分及含量", value=g("成分含量"), max_chars=100, key=f"{prefix}_comp")
    unit = d4.text_input("最小單位", value=g("最小單位"), max_chars=10, key=f"{prefix}_unit")
    return dict(六編碼=code.strip(), 藥品名稱=name.strip(), 國際條碼=barcode.strip(), 健保碼=nhi.strip() or None,
                安全量=int(safe), 安架量=int(shelf), 儲位=loc.strip(), 管制級別=int(grade), 是否停用=int(off),
                製造商編號=maker, 許可證字號=lic.strip() or None, 成分含量=comp.strip() or None,
                最小單位=unit.strip() or None)


def _validate_drug(v: dict, new: bool) -> list:
    """回傳錯誤訊息清單，空清單代表可以儲存。重複檢查用 db.query（不經快取）。"""
    errs = []
    if new and len(v["六編碼"]) != 6:
        errs.append("六編碼必須剛好 6 碼。")
    if not v["藥品名稱"]:
        errs.append("藥品名稱不可空白。")
    if not v["國際條碼"]:
        errs.append("國際條碼不可空白。")
    if not v["儲位"]:
        errs.append("儲位不可空白。")
    if v["健保碼"] and len(v["健保碼"]) != 10:
        errs.append("健保碼必須剛好 10 碼（或留空）。")
    if v["管制級別"] > 0:
        miss = [label for k, label in (("製造商編號", "製造商"), ("許可證字號", "許可證字號"),
                                      ("成分含量", "成分及含量"), ("最小單位", "最小單位")) if not v[k]]
        if miss:
            errs.append("管制藥必須填：" + "、".join(miss) + "。")
    if errs:
        return errs
    if new and not db.query("SELECT 1 AS x FROM 藥品總表 WHERE RTRIM(六編碼) = ?", (v["六編碼"],)).empty:
        errs.append(f"六編碼 {v['六編碼']} 已經存在。")
    dup = db.query("SELECT 六編碼 FROM 藥品總表 WHERE RTRIM(國際條碼) = ? AND RTRIM(六編碼) <> ?", (v["國際條碼"], v["六編碼"]))
    if not dup.empty:
        errs.append(f"國際條碼已被 {_s(dup.iloc[0, 0])} 使用。")
    if v["健保碼"]:
        dup = db.query("SELECT 六編碼 FROM 藥品總表 WHERE RTRIM(健保碼) = ? AND RTRIM(六編碼) <> ?", (v["健保碼"], v["六編碼"]))
        if not dup.empty:
            errs.append(f"健保碼已被 {_s(dup.iloc[0, 0])} 使用（一個健保碼只能對應一個六編碼）。")
    return errs


def _save_drug(v: dict, new: bool) -> list:
    """寫入藥品總表；成功後回傳提醒訊息清單（不是錯誤，只是提醒）。"""
    cols = ["藥品名稱", "國際條碼", "健保碼", "安全量", "安架量", "儲位", "管制級別", "是否停用",
            "製造商編號", "許可證字號", "成分含量", "最小單位"]
    with db.transaction() as cur:
        if new:
            cur.execute("INSERT INTO 藥品總表 (六編碼, " + ", ".join(cols) + ") VALUES (" + ", ".join(["?"] * (len(cols) + 1)) + ")",
                        (v["六編碼"], *[v[c] for c in cols]))
        else:
            cur.execute("UPDATE 藥品總表 SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE RTRIM(六編碼) = ?",
                        (*[v[c] for c in cols], v["六編碼"]))
        if v["健保碼"]:        # 「目前價格」由健保價格歷史同步，不手動輸入；沒有有效給付價時會是空值
            cur.execute("UPDATE 藥品總表 SET 健保價 = (SELECT p.價格 FROM v_現行健保價 p WHERE p.健保碼 = 藥品總表.健保碼) "
                        "WHERE RTRIM(六編碼) = ?", (v["六編碼"],))
    notes = []
    if v["健保碼"] and db.query("SELECT 1 AS x FROM 健保藥品品項 WHERE RTRIM(健保碼) = ?", (v["健保碼"],)).empty:
        notes.append("注意：健保資料庫裡找不到這個健保碼（可能打錯，或本月檔案已無此品項），替代藥查詢和價格核對會找不到它。")
    if v["是否停用"]:
        stock = db.query("SELECT COALESCE(SUM(現存量), 0) AS n FROM v_批次庫存 WHERE RTRIM(六編碼) = ?", (v["六編碼"],))
        if int(stock.iloc[0, 0]) != 0:
            notes.append(f"注意：這個藥帳上還有 {int(stock.iloc[0, 0])} 的庫存，停用後不會出現在庫存查詢，請先處理庫存。")
    return notes


def _page_master(load):
    st.header("總表更新與維護")
    st.caption("藥品總表的新增與修改。藥品不能刪除，只能「停用」。健保價是由健保價格歷史同步的「目前價格」，這裡不手動改。")
    _flash()
    makers = load("SELECT 製造商編號, 製造商名稱 FROM 製造商表 ORDER BY 製造商名稱")
    drugs = load("SELECT d.六編碼, d.藥品名稱, d.國際條碼, d.健保碼, d.健保價, d.安全量, d.安架量, d.儲位, d.管制級別, d.是否停用, "
                 "d.製造商編號, d.許可證字號, d.成分含量, d.最小單位 FROM 藥品總表 d ORDER BY d.六編碼")
    mode = st.radio("要做什麼", ["修改現有藥品", "新增藥品"], horizontal=True, key="mt_m_mode")

    with st.expander("新增製造商（管制藥帳冊要填製造廠）"):
        m1, m2 = st.columns([3, 1])
        new_maker = m1.text_input("製造商名稱", key="mt_m_newmaker", max_chars=100)
        if m2.button("新增製造商", key="mt_m_addmaker"):
            nm = new_maker.strip()
            if not nm:
                st.error("請輸入製造商名稱。")
            elif not db.query("SELECT 1 AS x FROM 製造商表 WHERE 製造商名稱 = ?", (nm,)).empty:
                st.error("這個製造商已經存在。")
            else:
                with db.transaction() as cur:
                    cur.execute("INSERT INTO 製造商表 (製造商名稱) VALUES (?)", (nm,))
                _done(f"已新增製造商：{nm}")

    if mode == "新增藥品":
        ver = st.session_state.get("mt_mn_ver", 0)         # 新增成功後版本 +1，欄位的 key 就換新，表單回到空白
        with st.form(f"mt_m_form_new_{ver}"):
            v = _drug_form(f"mt_mn{ver}", None, makers)
            go = st.form_submit_button("新增", type="primary")
        if go:
            errs = _validate_drug(v, new=True)
            if errs:
                for e in errs:
                    st.error(e)
            else:
                try:
                    notes = _save_drug(v, new=True)
                except Exception as e:
                    st.error(f"儲存失敗：{e}")
                else:
                    st.session_state["mt_mn_ver"] = ver + 1
                    _done(f"已新增 {v['六編碼']} {v['藥品名稱']}。" + "".join(" " + n for n in notes))
    else:
        show_off = st.checkbox("也列出已停用的藥", value=False, key="mt_m_showoff")
        pool = drugs if show_off else drugs[drugs["是否停用"] == 0]
        if pool.empty:
            st.info("藥品總表是空的。")
            return
        labels = [f"{_s(r.六編碼)}　{r.藥品名稱}" + (f"　健保 {_s(r.健保碼)}" if _s(r.健保碼) else "") +
                  (f"　條碼 {_s(r.國際條碼)}" if _s(r.國際條碼) else "") + ("　【停用】" if r.是否停用 else "")
                  for r in pool.itertuples()]
        pick = st.selectbox("選擇藥品（可輸入六編碼、藥名、健保碼或條碼篩選）", labels, key="mt_m_pick")
        row = pool.iloc[labels.index(pick)]
        code = _s(row["六編碼"])
        st.caption(f"目前健保價：{'—' if pd.isna(row['健保價']) else row['健保價']}（由健保價格歷史同步）")
        with st.form(f"mt_m_form_{code}"):
            v = _drug_form(f"mt_me_{code}", row, makers)
            v["六編碼"] = code
            go = st.form_submit_button("儲存修改", type="primary")
        if go:
            errs = _validate_drug(v, new=False)
            if errs:
                for e in errs:
                    st.error(e)
            else:
                try:
                    notes = _save_drug(v, new=False)
                except Exception as e:
                    st.error(f"儲存失敗：{e}")
                else:
                    _done(f"已更新 {code} {v['藥品名稱']}。" + "".join(" " + n for n in notes))
    with st.expander("藥品總表清單"):
        st.dataframe(drugs.drop(columns=["製造商編號"]), hide_index=True, width="stretch")


# ════════════════════════════════════════════════════════════════
# 2. 健保資料庫更新與維護
# ════════════════════════════════════════════════════════════════
def _guess_dates(paths):
    """檔名像 all1_11511_1.TXT → 民國 115 年 11 月，資料日期預設 115/11/01。"""
    for p in paths:
        m = re.search(r"all1_(\d{3})(\d{2})", Path(p).name, re.I)
        if m:
            return f"{m.group(1)}/{m.group(2)}/01"
    return ""


def _page_nhi(load):
    from import_nhi import SYNC_SQL, roc_to_date, run_import
    from import_nhi_txt import read_txt

    st.header("健保資料庫更新與維護")
    _flash()

    # ── A. 目前狀態 ──
    cnt = load("SELECT (SELECT COUNT(*) FROM 健保藥品品項) AS 品項數, (SELECT COUNT(*) FROM 健保價格歷史) AS 價格列數, "
               "(SELECT MAX(官方資料日期) FROM 健保匯入紀錄) AS 資料日期")
    c1, c2, c3 = st.columns(3)
    c1.metric("健保品項數", int(cnt.iloc[0]["品項數"]))
    c2.metric("價格歷史列數", int(cnt.iloc[0]["價格列數"]))
    d = cnt.iloc[0]["資料日期"]
    c3.metric("目前資料日期", "—" if d is None or pd.isna(d) else str(pd.to_datetime(d).date()))
    log = load("SELECT 匯入編號, 官方資料日期, 匯入時間, 檔名, 檔案筆數, 新增品項數, 價格異動數, 備註 FROM 健保匯入紀錄 ORDER BY 匯入編號 DESC")
    with st.expander("匯入紀錄", expanded=False):
        st.dataframe(log.head(20), hide_index=True, width="stretch")

    # ── B. 匯入新檔 ──
    st.subheader("匯入新的整批檔")
    st.caption("從健保署下載 all1_YYYYM_1.TXT、_2.TXT 放進下面的資料夾（本程式跑在您自己的電腦上，所以直接讀資料夾，不必上傳）。")
    default_dir = str(Path(__file__).resolve().parent.parent)
    folder = st.text_input("檔案所在資料夾", value=default_dir, key="mt_n_folder")
    files = []
    try:
        files = sorted(p for p in Path(folder).glob("*") if p.suffix.lower() == ".txt" and p.is_file())
    except OSError:
        pass
    if not files:
        st.info("這個資料夾裡沒有 .TXT 檔。")
    else:
        names = [p.name for p in files]
        periods = [m.group(1) for n in names if (m := re.match(r"all1_(\d+)_", n, re.I))]
        newest = [n for n in names if periods and n.lower().startswith(f"all1_{max(periods)}_")]
        picked = st.multiselect("要匯入的檔案", names, default=newest, key=f"mt_n_files_{folder}")
        paths = [Path(folder) / n for n in picked]
        ddate = st.text_input("資料日期（民國年，例如 115/11/01）", value=_guess_dates(paths), key=f"mt_n_date_{'|'.join(picked)}")
        if st.button("預覽（不寫入）", disabled=not picked, key="mt_n_preview"):
            try:
                dd = roc_to_date(ddate.replace("/", ".")) if ddate.strip() else None
            except ValueError:
                st.error("資料日期格式要像 115/11/01")
                dd = False
            if dd is not False:
                with st.spinner("讀檔並比對資料庫中…（約 22 萬列，需要一點時間）"):
                    items, prices, problems = read_txt([str(p) for p in paths])
                    conn = db.connect()
                    try:
                        summary = run_import(conn, items, prices, filename=" + ".join(picked), data_date=dd, dry_run=True)
                    finally:
                        conn.close()
                st.session_state["mt_n_prev"] = dict(items=items, prices=prices, problems=problems, summary=summary,
                                                     paths=[str(p) for p in paths], picked=picked, data_date=dd,
                                                     mtimes=[p.stat().st_mtime for p in paths])

    prev = st.session_state.get("mt_n_prev")
    if prev:
        s = prev["summary"]
        stale = any(Path(p).stat().st_mtime != t for p, t in zip(prev["paths"], prev["mtimes"]))
        st.markdown("**預覽結果（尚未寫入）**　" + "、".join(prev["picked"]))
        m = st.columns(4)
        m[0].metric("檔案品項數", s["檔案品項數"])
        m[1].metric("新增品項", s["新增品項"])
        m[2].metric("更新品項", s["更新品項"])
        m[3].metric("價格異動", s["價格異動"])
        m = st.columns(2)
        m[0].metric("新增價格列", s["新增價格列"])
        m[1].metric("更新價格列", s["更新價格列"])
        if s["我的藥價格異動"]:
            st.warning("本藥局藥品的價格異動：")
            st.dataframe(pd.DataFrame(s["我的藥價格異動"], columns=["藥品", "健保碼", "生效起日", "原價格", "新價格"]),
                         hide_index=True, width="stretch")
        if s["重疊健保碼"]:
            st.warning(f"有 {len(s['重疊健保碼'])} 個健保碼的價格期間重疊，請人工確認：{s['重疊健保碼'][:10]}")
        if prev["problems"]:
            st.warning(f"有 {len(prev['problems'])} 列無法解析，已略過（最多顯示 10 列）")
            st.code("\n".join(prev["problems"][:10]))
        if stale:
            st.error("預覽之後檔案又被修改過，請重新預覽。")
        ok = st.checkbox("我已看過預覽，確定寫入資料庫", key="mt_n_ok")
        fast = st.checkbox("使用 fast_executemany 加速（SQL Server；出現型別錯誤就取消勾選）",
                           value=db.mode() == "sqlserver", key="mt_n_fast")
        if st.button("確認匯入", type="primary", disabled=not ok or stale, key="mt_n_go"):
            conn = db.connect()
            try:
                with st.spinner("寫入中…（第一次匯入約需數分鐘，請不要關閉頁面）"):
                    res = run_import(conn, prev["items"], prev["prices"], filename=" + ".join(prev["picked"]),
                                     data_date=prev["data_date"], dry_run=False, fast=fast and db.mode() == "sqlserver",
                                     sync_sql=SYNC_SQL if db.mode() == "sqlserver" else "SELECT 1")
            except Exception as e:
                st.error(f"匯入失敗，已整批還原，資料庫沒有被改動：{e}")
            else:
                st.session_state.pop("mt_n_prev", None)
                _done(f"已匯入：新增品項 {res['新增品項']}、更新品項 {res['更新品項']}、新增價格列 {res['新增價格列']}、價格異動 {res['價格異動']}。")
            finally:
                conn.close()

    # ── C. 資料檢查 ──
    st.subheader("資料檢查")
    miss = db.query("SELECT d.六編碼, d.藥品名稱, d.健保碼 FROM 藥品總表 d LEFT JOIN 健保藥品品項 n ON n.健保碼 = d.健保碼 "
                    "WHERE d.健保碼 IS NOT NULL AND n.健保碼 IS NULL AND d.是否停用 = 0")
    if miss.empty:
        st.success("本藥局藥品的健保碼，在健保資料庫裡都找得到。")
    else:
        st.warning(f"{len(miss)} 個本藥局藥品的健保碼在健保資料庫找不到（打錯、或本月檔案已無此品項）：")
        st.dataframe(miss, hide_index=True, width="stretch")
    try:
        chk = db.query("SELECT 六編碼, 藥品名稱, 健保碼, 帳上健保價, 官方現行價, 核對結果 FROM v_健保價核對 WHERE 核對結果 <> N'一致'")
    except Exception as e:
        chk = None
        st.info(f"尚未建立 v_健保價核對 檢視表，略過價格核對（{e}）")
    if chk is not None:
        if chk.empty:
            st.success("本藥局藥品的帳上健保價，和官方現行價一致。")
        else:
            st.warning(f"{len(chk)} 個藥品的帳上健保價與官方現行價不一致或查無價格：")
            st.dataframe(chk, hide_index=True, width="stretch")
            if st.button("把官方現行價同步到藥品總表的健保價", key="mt_n_sync"):
                try:
                    with db.transaction() as cur:
                        if db.mode() == "sqlserver":
                            cur.execute(SYNC_SQL)
                        else:
                            cur.execute("UPDATE 藥品總表 SET 健保價 = (SELECT p.價格 FROM v_現行健保價 p WHERE p.健保碼 = 藥品總表.健保碼) "
                                        "WHERE 健保碼 IN (SELECT 健保碼 FROM v_現行健保價)")
                except Exception as e:
                    st.error(f"同步失敗：{e}")
                else:
                    _done("已同步藥品總表的健保價。")


# ════════════════════════════════════════════════════════════════
# 3. 盤點表上傳
# ════════════════════════════════════════════════════════════════
STOCK_SQL = ("SELECT b.批次編號, b.六編碼, d.藥品名稱, b.批號, b.效期, d.儲位, d.管制級別, b.現存量 AS 帳面數量 "
             "FROM v_批次庫存 b JOIN 藥品總表 d ON d.六編碼 = b.六編碼")


def _stock(include_zero: bool) -> pd.DataFrame:
    df = db.query(STOCK_SQL + ("" if include_zero else " WHERE b.現存量 <> 0") + " ORDER BY d.儲位, b.六編碼, b.效期")
    df["六編碼"] = df["六編碼"].map(_s)
    df["批號"] = df["批號"].map(_s)
    df["效期"] = pd.to_datetime(df["效期"]).dt.date.astype(str)
    return df


def _template_xlsx(df: pd.DataFrame) -> bytes:
    out = df[["六編碼", "藥品名稱", "批號", "效期", "儲位", "管制級別", "帳面數量"]].copy()
    out["實盤數量"] = None                          # 使用者填；留白代表「這批沒有盤」
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        out.to_excel(w, index=False, sheet_name="盤點表")
        ws = w.sheets["盤點表"]
        for col, width in zip("ABCDEFGH", (10, 28, 14, 12, 8, 10, 10, 10)):
            ws.column_dimensions[col].width = width
    return buf.getvalue()


def _read_count_file(up) -> pd.DataFrame:
    """讀上傳的盤點表（xlsx 或 csv）。六編碼、批號一律當文字讀，避免 Excel 把 000123 變成 123。"""
    name = up.name.lower()
    raw = pd.read_csv(up, dtype=str, encoding="utf-8-sig") if name.endswith(".csv") else pd.read_excel(up, dtype=str)
    raw.columns = [str(c).strip() for c in raw.columns]
    need = ["六編碼", "批號", "實盤數量"]
    if any(c not in raw.columns for c in need):
        raise ValueError(f"檔案缺少欄位：{[c for c in need if c not in raw.columns]}。請用「下載盤點表」產生的格式。")
    df = raw[need].copy()
    df["六編碼"] = df["六編碼"].fillna("").str.strip().map(lambda x: x.zfill(6) if x.isdigit() else x)
    df["批號"] = df["批號"].fillna("").str.strip()
    df["實盤數量"] = df["實盤數量"].fillna("").str.strip()
    return df


def _analyze(counted: pd.DataFrame):
    """把上傳的盤點資料和「現在的帳面庫存」比對。回傳 (差異表, 錯誤清單, 未對應清單, 未盤點清單)。"""
    errs, rows, unmatched = [], [], []
    stock = _stock(include_zero=True)
    key = {(r.六編碼, r.批號): r for r in stock.itertuples()}
    seen = set()
    todo = counted[counted["實盤數量"] != ""]
    for i, r in todo.iterrows():
        line = i + 2                                  # Excel 第幾列（第 1 列是標題）
        k = (r["六編碼"], r["批號"])
        if k in seen:
            errs.append(f"第 {line} 列：{k[0]} 批號 {k[1]} 在檔案中重複。")
            continue
        seen.add(k)
        try:
            qty = float(r["實盤數量"])
        except ValueError:
            errs.append(f"第 {line} 列：實盤數量「{r['實盤數量']}」不是數字。")
            continue
        if qty < 0 or qty != int(qty):
            errs.append(f"第 {line} 列：實盤數量必須是 0 以上的整數（{r['實盤數量']}）。")
            continue
        if k not in key:
            unmatched.append(dict(列=line, 六編碼=k[0], 批號=k[1], 實盤數量=int(qty)))
            continue
        s = key[k]
        rows.append(dict(批次編號=int(s.批次編號), 六編碼=s.六編碼, 藥品名稱=s.藥品名稱, 批號=s.批號, 效期=s.效期,
                         管制級別=int(s.管制級別) if "管制級別" in stock.columns and not pd.isna(s.管制級別) else 0,
                         帳面數量=int(s.帳面數量), 實盤數量=int(qty), 差異=int(qty) - int(s.帳面數量)))
    diff = pd.DataFrame(rows, columns=["批次編號", "六編碼", "藥品名稱", "批號", "效期", "管制級別", "帳面數量", "實盤數量", "差異"])
    diff["類型"] = diff["差異"].map(lambda x: "盤盈" if x > 0 else "盤虧" if x < 0 else "一致")
    untouched = stock[(stock["帳面數量"] > 0) & ~stock.apply(lambda r: (r["六編碼"], r["批號"]) in seen, axis=1)]
    return diff, errs, pd.DataFrame(unmatched), untouched


def _write_counts(changed: pd.DataFrame, who: str, note: str):
    """把有差異的批次寫成庫存異動（盤盈為正、盤虧為負）。整批在同一個交易內，任何一筆失敗就全部還原。"""
    stamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    with db.transaction() as cur:
        for r in changed.itertuples():
            cur.execute("INSERT INTO 庫存異動表 (批次編號, 異動類型, 數量, 異動時間, 備註, 員工編號) VALUES (?, ?, ?, ?, ?, ?)",
                        (int(r.批次編號), r.類型, int(r.差異), stamp, note.strip() or None, who))


def _page_stocktake(load):
    st.header("盤點表上傳")
    st.caption("流程：① 下載盤點表 → ② 現場盤點、填「實盤數量」 → ③ 上傳 → ④ 預覽差異 → ⑤ 確認後寫入盤盈／盤虧。"
               "盤點是「以批次為單位」：同一個藥有幾個批號，就要分別填。留白代表這批沒有盤，不會動它的帳。")
    _flash()

    st.subheader("① 下載盤點表")
    inc0 = st.checkbox("也列出帳面數量為 0 的批次（可能有實物卻沒帳時用）", value=False, key="mt_s_inc0")
    tpl = _stock(inc0)
    st.download_button(f"下載盤點表（{len(tpl)} 個批次）", _template_xlsx(tpl),
                       file_name=f"盤點表_{date.today():%Y%m%d}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="mt_s_dl")

    st.subheader("② 上傳填好的盤點表")
    up = st.file_uploader("盤點表（.xlsx 或 .csv，需有 六編碼、批號、實盤數量 三欄）", type=["xlsx", "csv"], key="mt_s_up")
    if up is None:
        return
    try:
        counted = _read_count_file(up)
    except Exception as e:
        st.error(f"讀取失敗：{e}")
        return
    diff, errs, unmatched, untouched = _analyze(counted)

    m = st.columns(5)
    m[0].metric("已盤批次", len(diff))
    m[1].metric("一致", int((diff["類型"] == "一致").sum()))
    m[2].metric("盤盈", int((diff["類型"] == "盤盈").sum()), f"+{int(diff.loc[diff['差異'] > 0, '差異'].sum())}")
    m[3].metric("盤虧", int((diff["類型"] == "盤虧").sum()), f"{int(diff.loc[diff['差異'] < 0, '差異'].sum())}")
    m[4].metric("未盤（帳上有庫存）", len(untouched))
    if errs:
        st.error(f"檔案有 {len(errs)} 個問題，請修正後重新上傳（有問題時不能寫入）：")
        st.code("\n".join(errs[:20]))
    if not unmatched.empty:
        st.warning("這些六編碼＋批號在系統裡找不到，不會寫入（新批號請先用進貨建立批次）：")
        st.dataframe(unmatched, hide_index=True, width="stretch")
    changed = diff[diff["差異"] != 0]
    ctrl = changed[changed["管制級別"] > 0]
    if not ctrl.empty:
        st.warning(f"其中 {len(ctrl)} 筆是管制藥，寫入後會直接出現在管制藥帳冊（收支原因：盤盈／盤虧），請務必先確認實物。")
    st.markdown("**有差異的批次**")
    if changed.empty:
        st.info("已盤點的批次帳實都一致，沒有需要寫入的差異。")
    else:
        st.dataframe(changed.drop(columns=["批次編號"]), hide_index=True, width="stretch")
    if not untouched.empty:
        with st.expander(f"未盤點、但帳上有庫存的批次（{len(untouched)}）"):
            st.dataframe(untouched.drop(columns=["批次編號"]), hide_index=True, width="stretch")

    st.subheader("③ 確認寫入")
    if changed.empty or errs:
        return
    emps = load("SELECT 員工編號, 員工姓名 FROM 員工 WHERE 是否在職 = 1 ORDER BY 員工編號")
    if emps.empty:
        st.error("沒有在職員工可以當經手人。")
        return
    e1, e2 = st.columns([1, 2])
    who = e1.selectbox("經手人", emps["員工編號"].tolist(), key="mt_s_who",
                       format_func=lambda i: f"{i} {emps.set_index('員工編號').loc[i, '員工姓名']}")
    note = e2.text_input("備註", value=f"盤點 {date.today():%Y-%m-%d}", max_chars=80, key="mt_s_note")
    ok = st.checkbox("我已核對上面的差異，確定寫入庫存異動", key="mt_s_ok")
    if st.button("確認寫入盤盈／盤虧", type="primary", disabled=not ok, key="mt_s_go"):
        # 寫入前再比對一次「現在的帳面數量」：預覽之後若有人調劑或進貨，差異會變，必須重新預覽
        now_diff, _, _, _ = _analyze(counted)
        before = changed.set_index("批次編號")["差異"].to_dict()
        after = now_diff[now_diff["差異"] != 0].set_index("批次編號")["差異"].to_dict()
        if before != after:
            st.error("預覽之後帳面庫存有變動（有人調劑或進貨），差異已經不同，沒有寫入。請重新整理畫面再預覽一次。")
            return
        try:
            _write_counts(changed, who, note)
        except Exception as e:
            st.error(f"寫入失敗，已整批還原，庫存沒有被改動：{e}")
        else:
            _done(f"已寫入 {len(changed)} 筆盤點異動（盤盈 {int((changed['差異'] > 0).sum())}、盤虧 {int((changed['差異'] < 0).sum())}）。")


# ════════════════════════════════════════════════════════════════
def render(load):
    st.sidebar.title("資料維護")
    page = st.sidebar.radio("功能", SUBPAGES, key="mt_sub")
    if page == SUBPAGES[0]:
        _page_master(load)
    elif page == SUBPAGES[1]:
        _page_nhi(load)
    else:
        _page_stocktake(load)
