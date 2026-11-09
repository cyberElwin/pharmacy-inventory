"""「健保藥品查詢」頁：查的是本機資料庫裡已匯入的健保署資料（健保藥品品項、健保價格歷史），不是即時連官方網站。
資料多久更新一次，取決於你多久匯入一次新的整批檔（import_nhi_txt.py）。
app.py 只需要：import nhi_page，然後在頁面選單加一項，呼叫 nhi_page.render(load)。
"""
from datetime import date

import pandas as pd
import streamlit as st

NHI_URL = "https://info.nhi.gov.tw/INAE3000/INAE3000S01"      # 健保署「藥品查詢」官方頁面
PAGE_SIZES = [10, 20, 50, 100]                                 # 「每頁顯示筆數」選項
STALE_DAYS = 35                                                # 官方每月更新；超過約一個月就提醒


def _like(term: str) -> str:
    """把使用者輸入的 % _ \\ 當一般文字（搭配 ESCAPE '\\'）。"""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _freshness(load):
    """顯示目前資料庫裡健保資料的日期；太久沒匯入就提醒。"""
    log = load("SELECT MAX(官方資料日期) AS 資料日期, MAX(匯入時間) AS 匯入時間 FROM 健保匯入紀錄")
    d = log.iloc[0]["資料日期"] if not log.empty else None
    if d is None or pd.isna(d):
        st.warning("資料庫裡還沒有健保匯入紀錄（或沒填資料日期）。請先執行 import_nhi_txt.py。")
        return
    d = pd.to_datetime(d).date()
    age = (date.today() - d).days
    st.caption(f"健保資料日期：{d}（{age} 天前）。價格以今天有效的期間計算。")
    if age > STALE_DAYS:
        st.warning(f"這份健保資料已超過 {STALE_DAYS} 天沒更新，價格可能已調整。"
                   "請到健保署下載最新整批檔，再用 import_nhi_txt.py 匯入。")


# 各輸入格的 key（統一前綴 nhi_，避免和其他頁面的元件撞名）
TEXT_KEYS = ["nhi_code", "nhi_name", "nhi_ing", "nhi_vendor", "nhi_form", "nhi_atc"]
MODE_PAID, MODE_HIST = "只顯示給付中資料", "顯示歷次異動詳細資料"


def _clear():
    """「清除」按鈕的 callback：在畫面重新執行之前，把所有輸入格與查詢結果歸零。"""
    for k in TEXT_KEYS:
        st.session_state[k] = ""
    st.session_state["nhi_mode"] = MODE_PAID
    st.session_state["nhi_size"] = PAGE_SIZES[0]
    st.session_state["nhi_page_no"] = 1
    st.session_state["nhi_q"] = None


def _conditions(q):
    """把各格輸入變成 WHERE 條件。每一格內用空白隔開的關鍵字都要符合；不同格之間也是「且」。"""
    where, params = [], []
    # (輸入格, 比對的欄位們)：欄位之間是「或」，例如藥名同時比對中文與英文名稱
    spec = [("nhi_code", ["i.健保碼"]), ("nhi_name", ["i.中文名稱", "i.英文名稱"]),
            ("nhi_ing", ["i.成分名稱"]), ("nhi_vendor", ["i.藥商"]),
            ("nhi_form", ["i.劑型"]), ("nhi_atc", ["i.ATC代碼"])]
    for key, cols in spec:
        for t in q[key].split():
            where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")")
            params += [_like(t)] * len(cols)
    return where, params

# ── 替代藥品查詢（只列本藥局有六編碼的藥）─────────────────────────
ALT_KEYS = ["alt_ing", "alt_id"]


def _alt_clear():
    for k in ALT_KEYS:
        st.session_state[k] = ""
    st.session_state["alt_q"] = None


def _alt_source(load, ident: str):
    """用健保碼、六編碼或國際條碼找出「基準藥」。先查本藥局的藥品總表（三種編碼都可以），
    找不到再直接查健保藥品品項的健保碼（基準藥可以是本藥局沒有的藥）。回傳 DataFrame，查不到是空的。"""
    cols = "i.健保碼, i.中文名稱, i.成分名稱, i.成分含量, i.劑型, i.ATC代碼, d.六編碼, d.藥品名稱"
    mine = load(f"SELECT {cols} FROM 藥品總表 d JOIN 健保藥品品項 i ON i.健保碼 = d.健保碼 "
                "WHERE RTRIM(d.六編碼) = ? OR RTRIM(d.健保碼) = ? OR RTRIM(d.國際條碼) = ?", (ident, ident, ident))
    if not mine.empty:
        return mine
    return load(f"SELECT {cols} FROM 健保藥品品項 i LEFT JOIN 藥品總表 d ON d.健保碼 = i.健保碼 "
                "WHERE RTRIM(i.健保碼) = ?", (ident,))

def render(load):
    st.sidebar.title("健保藥品查詢")
    page = st.sidebar.radio("功能", ["健保資料庫查詢", "替代藥品查詢", "多品項比較", "缺藥紀錄"], key="nhi_sub")
    if "nhi_code" not in st.session_state:        # 輸入格的 key 不存在 = 剛進入這一頁（或剛被清掉）
        st.session_state["nhi_q"] = None
    _freshness(load)
    st.link_button("到健保署官方查詢", NHI_URL)
    if page == "替代藥品查詢":
        st.header("替代藥品查詢")
        st.caption("只列本藥局有登錄（有六編碼）的藥。排序：成分相同在最前，ATC 相同次之。"
                       "兩格擇一即可：成分名稱，或健保碼／六編碼／國際條碼（以該藥為基準）；兩格都填時，會再用成分名稱篩選。")
        if "alt_ing" not in st.session_state:                 # 剛進入這一頁：清掉舊的查詢
            st.session_state["alt_q"] = None
        with st.form("alt_form_box"):
            st.text_input("成分名稱", key="alt_ing", placeholder="請輸入部分成分名稱，例如 acetaminophen")
            st.text_input("健保碼／六編碼／國際條碼", key="alt_id", placeholder="可直接掃描條碼；需完整輸入")
            b1, b2, _ = st.columns([1, 1, 3])
            go = b1.form_submit_button("查詢", type="primary", width="stretch")
            b2.form_submit_button("清除", width="stretch", on_click=_alt_clear)
        if go:
            st.session_state["alt_q"] = {k: st.session_state[k].strip() for k in ALT_KEYS}
        q = st.session_state.get("alt_q")
        if not q:
            st.info("請輸入條件後按「查詢」。")
            return
        ing_text, ident = q["alt_ing"], q["alt_id"]
        if not ing_text and not ident:
            st.info("兩格至少要填一格。")
            return
        
        # 本藥局全部有健保成分資料的藥（幾百到幾千列，量小，條件判斷在 pandas 做）
        mine = load("SELECT 六編碼, 藥品名稱, 健保碼, 成分名稱, 成分含量, 劑型, ATC代碼, 可用庫存, 總庫存, 儲位, 管制級別 "
                    "FROM v_本藥局成分").copy()
        for c in ("六編碼", "健保碼"):
            mine[c] = mine[c].astype(str).str.strip()
        
        def contains_all(series, text):                         # 成分格：空白隔開的關鍵字都要符合
            ok = pd.Series(True, index=series.index)
            for t in text.split():
                ok &= series.fillna("").str.contains(t, case=False, regex=False)
            return ok
        
        src = None
        if ident:
            found = _alt_source(load, ident)
            if found.empty:
                st.warning(f"找不到編碼「{ident}」。請確認是完整的健保碼、六編碼或國際條碼。")
                return
            src = found.iloc[0]
            if not src["成分名稱"] and not src["ATC代碼"]:
                st.warning(f"{src['中文名稱'] or ident} 在健保資料裡沒有成分名稱與 ATC 代碼，無法比對替代藥。")
                return
            st.success(f"基準藥：{src['中文名稱'] or src['藥品名稱']}（健保碼 {str(src['健保碼']).strip()}）"
                        f"　成分：{src['成分名稱'] or '—'}　含量：{src['成分含量'] or '—'}　劑型：{src['劑型'] or '—'}"
                        f"　ATC：{src['ATC代碼'] or '—'}")
            same_ing = (mine["成分名稱"] == src["成分名稱"]) if src["成分名稱"] else pd.Series(False, index=mine.index)
            same_atc = (mine["ATC代碼"] == src["ATC代碼"]) if src["ATC代碼"] else pd.Series(False, index=mine.index)
            mine["比對"] = same_ing.map({True: "成分相同"}).fillna(same_atc.map({True: "ATC相同"}))
            if ing_text:
                mine = mine[contains_all(mine["成分名稱"], ing_text)]
        else:
            hit = contains_all(mine["成分名稱"], ing_text)       # 成分名稱符合輸入的藥 = 成分相同組
            atcs = set(mine.loc[hit, "ATC代碼"].dropna())          # 這些藥的 ATC，沒有成分符合但 ATC 相同的列為次要
            mine["比對"] = hit.map({True: "成分相同"}).fillna(mine["ATC代碼"].isin(atcs).map({True: "ATC相同"}))
        df = mine[mine["比對"].notna()].copy()                    # 沒有比對上的（含沒有六編碼的藥，本來就不在檢視表）不顯示
        if df.empty:
            st.info("本藥局沒有成分相同或 ATC 相同的藥（沒有六編碼的藥視同未建檔，不列出）。")
            return
        
        f1, f2, _ = st.columns(3)
        only_ing = f1.checkbox("只顯示成分相同（隱藏 ATC 相同）", value=False, key="alt_ing_only")
        only_stock = f2.checkbox("只顯示有可用庫存", value=False, key="alt_stock")
        if only_ing:
            df = df[df["比對"] == "成分相同"]
        if only_stock:
            df = df[df["可用庫存"] > 0]
        if df.empty:
            st.info("勾選的條件下沒有符合的藥品，可取消部分勾選。")
            return
        
        df["備註"] = ""
        if src is not None:
            df.loc[df["健保碼"] == str(src["健保碼"]).strip(), "備註"] = "◎ 基準藥"
        # 排序：成分相同 → ATC相同；同層級內基準藥在前，再來有可用庫存的、最後依藥名
        df["_t"] = df["比對"].map({"成分相同": 0, "ATC相同": 1})
        df["_b"] = (df["備註"] == "").astype(int)
        df["_s"] = (df["可用庫存"].fillna(0) <= 0).astype(int)
        df = df.sort_values(["_t", "_b", "_s", "藥品名稱"]).drop(columns=["_t", "_b", "_s"])
        st.write(f"符合 {len(df)} 筆")
        show = df[["備註", "比對", "六編碼", "藥品名稱", "健保碼", "成分名稱", "成分含量", "劑型", "ATC代碼",
                       "可用庫存", "總庫存", "儲位", "管制級別"]]
        st.dataframe(show, hide_index=True, width="stretch")
        st.caption("成分相同 = 健保成分名稱完全相同；ATC相同 = 成分寫法不同但 ATC 代碼相同。可用庫存是未過期的量。"
                       "成分相同不代表可以互換：含量與劑型都列在表格裡，不替您篩選，請藥師自行判斷；管制級別大於 0 的是管制藥。")
        return
    if page != "健保資料庫查詢":
        st.header(page)
        st.info("這個功能還在開發中。")
        return
    if page == "健保資料庫查詢":
        st.header("健保資料庫查詢")
    # ── 查詢表單：放在 st.form 裡，按「查詢」（或在輸入格按 Enter）才會一次送出 ──
        with st.form("nhi_form_box"):
            st.radio("健保目前給付", [MODE_PAID, MODE_HIST], horizontal=True, key="nhi_mode")
            st.text_input("藥品代碼", key="nhi_code", placeholder="請輸入部分藥品代碼（健保碼）")
            st.text_input("藥品名稱(中英文)", key="nhi_name", placeholder="請輸入部分中英文名稱")
            st.text_input("成分名稱", key="nhi_ing", placeholder="請輸入部分成分名稱")
            st.selectbox("每頁顯示筆數", PAGE_SIZES, key="nhi_size", format_func=lambda n: f"{n}筆")
            with st.expander("進階查詢"):
                a1, a2, a3 = st.columns(3)
                a1.text_input("藥商", key="nhi_vendor", placeholder="部分藥商名稱")
                a2.text_input("劑型", key="nhi_form", placeholder="例如 錠、膠囊、注射")
                a3.text_input("ATC代碼", key="nhi_atc", placeholder="例如 N05AA01")
            b1, b2, _ = st.columns([1, 1, 3])
            go = b1.form_submit_button("查詢", type="primary", width="stretch")
            b2.form_submit_button("清除", width="stretch", on_click=_clear)

        if go:                                                  # 按下「查詢」：記住這次的查詢條件，頁碼回到第 1 頁
            st.session_state["nhi_q"] = {k: st.session_state[k].strip() for k in TEXT_KEYS}
            st.session_state["nhi_q"]["mode"] = st.session_state["nhi_mode"]
            st.session_state["nhi_page_no"] = 1
        q = st.session_state.get("nhi_q")
        if not q:
            st.info("請輸入條件後按「查詢」。資料庫裡的健保品項很多，所以沒有條件時不列出全部。")
            return
        if not any(q[k] for k in TEXT_KEYS):
            st.info("至少要填一格條件。")
            return

        where, params = _conditions(q)
        hist_mode = q["mode"] == MODE_HIST
        if hist_mode:        # 歷次異動：每個「品項 × 價格期間」一列；沒有價格列的品項也要列出，所以用 LEFT JOIN
            sql = ("SELECT i.健保碼, i.中文名稱, i.英文名稱, i.成分名稱, i.成分含量, i.劑型, i.藥商, i.ATC代碼, "
                "h.價格, h.生效起日, h.生效迄日 "
                "FROM 健保藥品品項 i LEFT JOIN 健保價格歷史 h ON h.健保碼 = i.健保碼 "
                "WHERE " + " AND ".join(where or ["1=1"]) + " ORDER BY i.中文名稱, i.健保碼, h.生效起日 DESC")
        else:                # 給付中：只列「今天有有效價格」的品項（INNER JOIN 現行價檢視表）
            sql = ("SELECT i.健保碼, i.中文名稱, i.英文名稱, i.成分名稱, i.成分含量, i.劑型, i.藥商, i.ATC代碼, "
                "p.價格 AS 目前價格, p.生效起日 AS 價格生效日 "
                "FROM 健保藥品品項 i JOIN v_現行健保價 p ON p.健保碼 = i.健保碼 "
                "WHERE " + " AND ".join(where or ["1=1"]) + " ORDER BY i.中文名稱")
        df = load(sql, tuple(params))
        if df.empty:
            st.info("沒有符合的品項")
            return
        if hist_mode:
            df["生效迄日"] = df["生效迄日"].where(df["生效迄日"].notna(), "迄今")

        # ── 分頁：用 session_state 記住目前頁碼 ──
        size = int(st.session_state["nhi_size"])
        pages = max(1, -(-len(df) // size))                     # 無條件進位的除法
        st.session_state["nhi_page_no"] = min(max(1, int(st.session_state.get("nhi_page_no", 1))), pages)
        t1, t2 = st.columns([3, 1])
        t1.write(f"符合 {len(df)} 筆　點選一列可看價格歷史與詳細資料")
        page_no = t2.number_input(f"頁碼（共 {pages} 頁）", 1, pages, key="nhi_page_no")
        shown = df.iloc[(page_no - 1) * size: page_no * size]
        fmt = {"價格": "{:.2f}"} if hist_mode else {"目前價格": "{:.2f}"}
        ev = st.dataframe(shown.style.format(fmt, na_rep=""), hide_index=True, width="stretch",
                        on_select="rerun", selection_mode="single-row", key="nhi_table")
        rows = ev.selection.rows if ev and ev.selection else []
        if not rows:
            return
        item = shown.iloc[rows[0]]
        code = str(item["健保碼"]).strip()
        st.subheader(f"{item['中文名稱'] or ''}（{code}）")

        hist = load("SELECT 生效起日, 生效迄日, 價格 FROM 健保價格歷史 WHERE 健保碼 = ? ORDER BY 生效起日 DESC", (code,))
        left, right = st.columns(2)
        with left:
            st.markdown("**價格歷史**")
            if hist.empty:
                st.info("官方檔沒有這個品項的價格（價格欄為「-」）。")
            else:
                hist["生效迄日"] = hist["生效迄日"].where(hist["生效迄日"].notna(), "迄今")
                hist["說明"] = hist["價格"].apply(lambda v: "無給付價（0.00）" if v == 0 else "")
                st.dataframe(hist.style.format({"價格": "{:.2f}"}), hide_index=True, width="stretch")
        with right:
            st.markdown("**品項資料**")
            full = load("SELECT 英文名稱, 成分名稱, 成分含量, 規格量, 單複方, 劑型, 藥商, 藥品分類, 分類分組名稱, ATC代碼 "
                        "FROM 健保藥品品項 WHERE 健保碼 = ?", (code,))
            if not full.empty:
                detail = full.iloc[0].fillna("").astype(str)
                st.dataframe(pd.DataFrame({"欄位": detail.index, "內容": detail.values}), hide_index=True, width="stretch")
            mine = load("SELECT 六編碼, 藥品名稱, 健保價 FROM 藥品總表 WHERE 健保碼 = ?", (code,))
            if mine.empty:
                st.caption("這個健保碼不在本藥局的藥品總表。")
            else:
                st.success("本藥局有這個藥：" + "、".join(f"{r.藥品名稱}（{r.六編碼}）帳上健保價 {r.健保價}" for r in mine.itertuples()))
