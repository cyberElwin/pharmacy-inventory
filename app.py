"""藥局庫存管理（Streamlit 原型）
啟動：streamlit run app.py
"""
from datetime import date, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import db
import nhi_page
import print_ledger
import maintenance_page

st.set_page_config(page_title="藥局庫存", layout="wide")  # 必須是第一個 Streamlit 指令


# ── load()：定義在 app.py（不是 db.py）──────────────────────────────
# db.py 裡真正連資料庫的函式叫 query()；load() 只是在 app.py 多包一層，
# 目的是加上 Streamlit 的快取（@st.cache_data 是 Streamlit 的功能，
# 所以放在 app.py，讓 db.py 只管資料庫、不必認識 Streamlit）。
#   ttl=30：同一組 (sql, params) 的結果暫存 30 秒，所以在 SSMS 剛新增的資料，
#           網頁最多可能晚 30 秒才看到。之後做寫入頁時，寫入成功後要呼叫
#           st.cache_data.clear()，畫面才會馬上更新。
#


@st.cache_data(ttl=30)
def load(sql: str, params: tuple = ()):
    return db.query(sql, params)
# ── 參數說明 ──────────────────────────────────────────────────────
# sql: str      第一格要放「SQL 語法的文字」。Python 不懂 SQL，是把它當成字串
#               交給 pyodbc，再由 SQL Server 執行，所以要用引號包起來。
#               ": str" 是型別提示，只是給人和編輯器看的註解，Python 不會強制檢查。
# params: tuple = ()   要填進 SQL 裡「?」位置的值，依序對應、數量要一樣。
#               預設 () 代表沒有要填的值（沒有 ? 的查詢可以不傳）。
#               可以想成填空題：sql 是題目，params 是答案。
#               傳遞路徑：load(sql, params) → db.query(sql, params)
#                         → cursor.execute(sql, params)（這裡才真正交給資料庫）

@st.cache_data(ttl=10)
def connection_status():
    return db.check_connection()


if db.mode() == "demo":
    st.sidebar.warning("示範模式：目前顯示的是虛構資料")
else:
    ok, reason = connection_status()
    if ok:
        st.sidebar.success("已連線 SQL Server")
    else:
        st.sidebar.error("無法連線 SQL Server")
        st.sidebar.caption(reason)


SEARCH_COLS = ["藥品名稱", "六編碼", "健保碼", "國際條碼"]      # 搜尋時會比對的欄位（資料表裡有哪欄就比哪欄）
SEARCH_LABEL = "搜尋（藥名／六編碼／健保碼／國際條碼）"
SEARCH_HINT = "可直接掃描條碼；用空白隔開可輸入多個關鍵字"


def search_filter(df, text, cols=SEARCH_COLS):
    """同時比對藥名、六編碼、健保碼、國際條碼，其中任一欄含有關鍵字就留下。
    用空白隔開多個關鍵字時，每個關鍵字都要找得到（AND），例如「胃藥 40mg」或「胃藥 DEMO000005」。"""
    terms = text.split()
    use = [c for c in cols if c in df.columns]
    if df.empty or not terms or not use:
        return df
    # CHAR 欄位會有尾端空白、健保碼可能是空值，先整理成乾淨的文字再比對
    hay = df[use].fillna("").astype(str).apply(lambda col: col.str.strip())
    keep = pd.Series(True, index=df.index)
    for t in terms:
        keep &= hay.apply(lambda col: col.str.contains(t, case=False, regex=False)).any(axis=1)
    return df[keep]


def expiry_status(days: int) -> str:
    if days < 0:
        return "已過期"
    if days <= 30:
        return "30 天內"
    return "31–90 天"


def color_rows(row):
    color = {"已過期": "#f7c1c1", "30 天內": "#faeeda"}.get(row["狀態"], "")
    style = f"background-color: {color}; color: #2c2c2a" if color else ""
    return [style] * len(row)


def period_label(ts, unit: str):
    """把時間欄位轉成統計期間的標籤，例如 2026 / 2026-Q4 / 2026-11 / 2026-11-06。"""
    if unit == "年":
        return ts.dt.strftime("%Y")
    if unit == "季":
        return ts.dt.year.astype(str) + "-Q" + ts.dt.quarter.astype(str)
    if unit == "月":
        return ts.dt.strftime("%Y-%m")
    return ts.dt.strftime("%Y-%m-%d")


#還有異動,缺貨紀錄與替代藥功能
select_function = st.selectbox("選擇使用類型", ["調劑", "出入庫", "健保藥品查詢", "藥局庫存查詢", "資料維護"], index=3, key="mode_select")

if select_function=="藥局庫存查詢":
    st.sidebar.title("藥局庫存查詢")

    page = st.sidebar.radio("功能", ["效期提醒", "出入庫查詢", "庫存水位", "管制藥總表", "管制藥帳冊"])
    try:
        if page == "效期提醒":
            st.header("效期提醒")
            df = load("SELECT 六編碼, 藥品名稱, 健保碼, 國際條碼, 批號, 效期, 現存量, 剩餘天數 FROM v_近效期 ORDER BY 效期")
            df["狀態"] = df["剩餘天數"].apply(expiry_status)

            c1, c2, c3 = st.columns(3)
            c1.metric("已過期（仍有庫存）", int((df["狀態"] == "已過期").sum()))
            c2.metric("30 天內到期", int((df["狀態"] == "30 天內").sum()))
            c3.metric("31–90 天到期", int((df["狀態"] == "31–90 天").sum()))

            f1, f2 = st.columns([1, 2])
            days = f1.slider("顯示幾天內到期", 0, 90, 90, key="exp_days")
            text = f2.text_input(SEARCH_LABEL, key="exp_search", placeholder=SEARCH_HINT)
            view = search_filter(df[df["剩餘天數"] <= days], text)

            if view.empty:
                st.info("沒有符合條件的批次")
            else:
                st.dataframe(view.style.apply(color_rows, axis=1), hide_index=True, width="stretch")
            st.caption("紅色：已過期但帳上仍有庫存，請盡快處理；橘色：30 天內到期。")
        elif page == "出入庫查詢":
            st.header("出入庫查詢")
            # 入庫、出庫的歸類規則寫在 SQL 檢視表 v_異動明細 的「出入庫」欄：
            #   入庫＝進貨、調入；出庫＝調劑、調出、報廢、退貨；盤盈／盤虧是盤點調整，不算出入庫。
            # 管制藥與一般藥品放在一起查。
            q1, q2, q3 = st.columns([1, 1, 1])
            kind = q1.selectbox("查詢內容", ["入庫查詢", "出庫查詢", "總查詢"], key="io_kind")
            unit = q2.selectbox("統計單位", ["年", "季", "月", "日"], index=2, key="io_unit")
            today = date.today()
            rng = q3.date_input("日期區間", (today - timedelta(days=180), today), key="io_range")
            text = st.text_input(SEARCH_LABEL, key="io_search", placeholder=SEARCH_HINT)

            if len(rng) != 2:            # 日期區間還沒選完整（只點了起日）
                st.info("請選擇完整的起訖日期")
            else:
                start, end = rng
                # 日期用字串（ISO 格式）傳入，SQL Server 與 SQLite 都能比較；結束日 +1 天，才會包含結束日整天
                raw = load(
                    "SELECT 異動時間, 異動類型, 出入庫, 數量, 六編碼, 藥品名稱, 健保碼, 國際條碼 FROM v_異動明細 "
                    "WHERE 異動時間 >= ? AND 異動時間 < ?",
                    (start.isoformat(), (end + timedelta(days=1)).isoformat()),
                )
                raw = search_filter(raw, text)
                if raw.empty:
                    st.info("這個期間沒有符合條件的異動")
                else:
                    raw = raw.copy()
                    raw["異動時間"] = pd.to_datetime(raw["異動時間"])
                    raw["期間"] = period_label(raw["異動時間"], unit)

                    if kind in ("入庫查詢", "出庫查詢"):
                        direction = "入庫" if kind == "入庫查詢" else "出庫"
                        part = raw[raw["出入庫"] == direction].copy()
                        # 可以只看某幾種類型（例如出庫只看「調劑」）。key 帶上方向，切換入庫／出庫時選項不會互相干擾
                        types = sorted(part["異動類型"].unique())
                        picked = st.multiselect("異動類型", types, default=types, key=f"io_types_{direction}")
                        part = part[part["異動類型"].isin(picked)]
                        part["數量"] = part["數量"].abs()          # 出庫在資料庫是負數，顯示成正的量
                        if part.empty:
                            st.info(f"這個期間沒有符合條件的{direction}紀錄")
                        else:
                            m1, m2 = st.columns(2)
                            m1.metric(f"{direction}合計", int(part["數量"].sum()))
                            m2.metric("品項數", part["六編碼"].nunique())
                            by_type = part.groupby("異動類型", as_index=False)["數量"].sum().rename(columns={"數量": f"{direction}量"})
                            st.dataframe(by_type, hide_index=True, width="stretch")
                            st.bar_chart(part.groupby("期間")["數量"].sum())
                            # 橫軸是期間、列是藥品，最右邊是合計
                            pv = part.pivot_table(index=["六編碼", "藥品名稱"], columns="期間", values="數量",
                                                  aggfunc="sum", fill_value=0)
                            pv["合計"] = pv.sum(axis=1)
                            pv = pv.sort_values("合計", ascending=False).reset_index()
                            pv.columns.name = None
                            st.dataframe(pv, hide_index=True, width="stretch")
                    else:
                        # 總查詢：每個「期間 × 藥品」一列，入庫量、出庫量、盤點調整、淨變動並排
                        raw["入庫量"] = raw["數量"].where(raw["出入庫"] == "入庫", 0)
                        raw["出庫量"] = (-raw["數量"]).where(raw["出入庫"] == "出庫", 0)
                        raw["盤點調整"] = raw["數量"].where(raw["出入庫"] == "盤點調整", 0)
                        tot = (raw.groupby(["期間", "六編碼", "藥品名稱"], as_index=False)[["入庫量", "出庫量", "盤點調整"]]
                               .sum().sort_values(["期間", "六編碼"]))
                        tot["淨變動"] = tot["入庫量"] - tot["出庫量"] + tot["盤點調整"]
                        m1, m2, m3 = st.columns(3)
                        m1.metric("入庫合計", int(tot["入庫量"].sum()))
                        m2.metric("出庫合計", int(tot["出庫量"].sum()))
                        m3.metric("淨變動", int(tot["淨變動"].sum()))
                        st.bar_chart(raw.groupby("期間")[["入庫量", "出庫量"]].sum())
                        st.dataframe(tot, hide_index=True, width="stretch")
                    st.caption("入庫 = 進貨、調入；出庫 = 調劑、調出、報廢、退貨；盤盈、盤虧是盤點調整，只出現在總查詢。"
                               "管制藥與一般藥品一起統計。")
                    if unit == "日" and (end - start).days > 62:
                        st.caption("提醒：日期區間超過兩個月又用「日」統計，欄位會很多，建議改用月或季。")

        elif page == "庫存水位":
            st.header("庫存水位")
            df = load(
                "SELECT 六編碼, 藥品名稱, 健保碼, 國際條碼, 儲位, 安全量, 安架量, 總庫存, 可用庫存, 近效庫存, 過期庫存, 需叫貨 "
                "FROM v_藥品庫存 ORDER BY 六編碼"
            )
            # 缺口、需叫貨都以「可用庫存」（未過期）為準，過期的藥不能調劑，不算庫存
            df["缺口"] = (df["安全量"] - df["可用庫存"]).clip(lower=0)
            df["狀態"] = df["需叫貨"].map({1: "需叫貨", 0: "正常"})

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("品項數", len(df))
            c2.metric("需叫貨品項", int(df["需叫貨"].sum()))
            c3.metric("有近效庫存品項", int((df["近效庫存"] > 0).sum()))
            c4.metric("有過期庫存品項", int((df["過期庫存"] > 0).sum()))

            f1, f2, f3 = st.columns([1, 1, 2])
            only_low = f1.checkbox("只看需叫貨", value=False, key="stock_low")
            only_exp = f2.checkbox("只看有近效／過期", value=False, key="stock_exp")
            text = f3.text_input(SEARCH_LABEL, key="stock_search", placeholder=SEARCH_HINT)
            base = df
            if only_low:
                base = base[base["需叫貨"] == 1]
            if only_exp:
                base = base[(base["近效庫存"] > 0) | (base["過期庫存"] > 0)]
            view = search_filter(base, text)

            show = view[["六編碼", "藥品名稱", "健保碼", "國際條碼", "儲位", "總庫存", "可用庫存", "近效庫存", "過期庫存",
                         "安全量", "缺口", "安架量", "狀態"]]
            if show.empty:
                st.info("沒有符合條件的品項")
            else:
                st.dataframe(show, hide_index=True, width="stretch")
            st.caption(
                "總庫存 = 全部批次；可用庫存 = 未過期的量（調劑時能領出的量）；"
                "近效庫存 = 可用庫存中 90 天內到期的部分（已包含在可用庫存內，不要重複相加）；"
                "過期庫存 = 已過期但帳上仍有的量。缺口 = 安全量 − 可用庫存。安架量是架上固定要補回的標準量，目前僅供參考。"
            )

        elif page == "管制藥總表":
            st.header("管制藥總表")
            text = st.text_input(SEARCH_LABEL, key="ctrl_search", placeholder=SEARCH_HINT)
            drugs = search_filter(
                load("SELECT 六編碼, 健保碼, 國際條碼, 藥品名稱, 管制級別, 儲位 FROM v_管制藥清單 ORDER BY 六編碼"), text)
            if drugs.empty:
                st.info("沒有符合條件的管制藥")
            else:
                st.dataframe(drugs, hide_index=True, width="stretch")
            st.subheader("管制藥批號管理")
            lots = search_filter(
                load("SELECT 六編碼, 健保碼, 國際條碼, 藥品名稱, 管制級別, 儲位, 批號, 效期, 現存量 FROM v_管制藥批次 ORDER BY 六編碼"), text)
            if lots.empty:
                st.info("沒有符合條件的管制藥批次")
            else:
                st.dataframe(lots, hide_index=True, width="stretch")

        elif page == "管制藥帳冊":
            st.header("管制藥帳冊")
            drugs = load("SELECT 六編碼, 藥品名稱, 健保碼, 國際條碼 FROM v_管制藥清單 ORDER BY 六編碼")
            if drugs.empty:
                st.info("目前沒有管制藥")
            # .empty 是 pandas DataFrame 的「屬性」（不是函式，不加括號）：
            # 表格沒有任何一列時為 True，有資料時為 False。先判斷有沒有資料，再決定顯示提示或表格，避免畫出空表格。
            else:
                # 1) 選藥品：把「六編碼＋全形空格＋品名」串成好讀的選項文字（.str.strip() 是因為 CHAR(6) 欄位可能帶尾端空白）
                # 選項文字也帶上健保碼、國際條碼：點開下拉選單後直接打字，就能用這些編碼篩出藥品
                def drug_label(r):
                    parts = [str(r["六編碼"]).strip(), r["藥品名稱"]]
                    if pd.notna(r["健保碼"]) and str(r["健保碼"]).strip():
                        parts.append("健保 " + str(r["健保碼"]).strip())
                    if pd.notna(r["國際條碼"]) and str(r["國際條碼"]).strip():
                        parts.append("條碼 " + str(r["國際條碼"]).strip())
                    return "　".join(parts)

                labels = drugs.apply(drug_label, axis=1).tolist()
                choice = st.selectbox("選擇藥品（可輸入藥名、六編碼、健保碼或條碼篩選）", labels, key="ledger_drug")
                # 畫面顯示好讀的文字，查詢用精確的六編碼：labels.index(choice) 找出選到第幾個，再從 drugs 取同一位置的六編碼
                code = drugs["六編碼"].iloc[labels.index(choice)]

                # 2) 只查選定藥品的帳冊。結存數量在檢視表裡是依每個六編碼分別累計，所以單獨篩一種藥，結存仍然正確。同一天內用異動編號決定先後。
                #    計算規則（結存、備註、收支原因）都在 v_管制帳冊，Python 只負責選藥、查詢、排版。
                led = load(
                    "SELECT 品名, 管制藥品成分及含量, 藥品許可證字號, 管制級別, 製造廠名稱, 最小單位, "
                    "日期, 收支原因, 收入數量, 收入藥品批號, 支出數量, 結存數量, 備註, 經手人 "
                    "FROM v_管制帳冊 WHERE 六編碼 = ? ORDER BY 日期, 異動編號",
                    (code,),
                )
                # 3) 表頭（對應紙本帳冊上方）：品名、成分、許可證字號等每列都相同，取第一列即可。沒有異動時 led 是空的，沒有第一列，直接 iloc[0] 會報錯，所以先用 not led.empty 檢查，沒有就設為 None。
                h = led.iloc[0] if not led.empty else None
                # ── 為什麼用 ? 參數，不用 f-string 拼字串 ─────────────────────────
                #   不寫：f"... WHERE 六編碼 = '{code}'";要寫：  "... WHERE 六編碼 = ?", (code,)
                #   1) 引號問題：值若含單引號（如 O'Brien），拼出來的 SQL 會語法錯誤。
                #   2) SQL 注入：使用者若輸入 x' OR '1'='1，拼字串會變成永遠成立的條件而撈出全部資料，更糟的輸入還可能夾帶 DROP TABLE。
                #   用 ? 時，整串輸入只會被當成「一個要比對的值」，不會被當成 SQL 指令執行。
                #   注意：
                #   - (code,) 結尾的逗號不能省，沒有逗號只是加括號的變數，不是 tuple。
                #   - ? 只能代替「值」，不能代替表名或欄位名（FROM ? / ORDER BY ? 不行）。
                #   - pyodbc 和 SQLite 都用 ?，所以同一份 SQL 在兩種資料庫都能跑。
                # 小工具：有資料就回傳該欄位，沒資料回傳「—」，避免對 None 取值報錯
                def f(col):
                    return h[col] if h is not None else "—"

                name = f("品名") if h is not None else drugs.loc[drugs["六編碼"] == code, "藥品名稱"].iloc[0]
                # st.columns(3) 把畫面分成三欄；f"..." 是 f-string，會把 {} 內的變數換成值
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**品名**　{name}")
                c2.markdown(f"**成分及含量**　{f('管制藥品成分及含量')}")
                c3.markdown(f"**許可證字號**　{f('藥品許可證字號')}")
                c4, c5, c6 = st.columns(3)
                c4.markdown(f"**管制級別**　第 {f('管制級別')} 級")
                c5.markdown(f"**製造廠**　{f('製造廠名稱')}")
                c6.markdown(f"**最小單位**　{f('最小單位')}")

                # 4) 帳冊表格：只挑要顯示的欄位（表頭資料已在上方，不重複）。 hide_index=True 隱藏 pandas 列號；width="stretch" 撐滿寬度。
                if led.empty:
                    st.info("這個藥品目前沒有異動紀錄")
                else:
                    cols = ["日期", "收支原因", "收入數量", "收入藥品批號", "支出數量", "結存數量", "備註"]
                    # 空值顯示成空白：用 Styler 的 na_rep（只改顯示，數字欄位仍是數字、靠右對齊）。
                    # 不要用 fillna("")：會讓數字欄位混進文字，Streamlit 轉表格時報 Arrow 錯誤。
                    st.dataframe(led[cols].style.format(na_rep=""), hide_index=True, width="stretch")
                    st.caption("結存數量由異動紀錄依時間順序累計計算，不是另外存放的數字。")

                    # 5) 列印預覽：print_ledger.render() 產生仿紙本格式的 HTML（A4 橫式、每頁 16 行），
                    #    預覽視窗裡的「列印」按鈕會呼叫瀏覽器的列印功能（可存成 PDF）。
                    #    表頭欄位取自 h（帳冊第一列）；表格欄位取自 led。
                    if st.toggle("列印預覽", key="ledger_preview"):
                        page_html = print_ledger.render(h.to_dict(), led)   # 內容皆已 escape
                        if hasattr(st, "iframe"):      # 新版 Streamlit：components.html 已被 st.iframe 取代
                            st.iframe(page_html, height=760)
                        else:                          # 舊版相容
                            components.html(page_html, height=760, scrolling=True)


    except Exception as e:  # 連線或查詢失敗時，顯示可讀的訊息而不是整頁錯誤
        st.error(f"讀取資料失敗：{e}")

elif select_function=="健保藥品查詢":
    try:
        nhi_page.render(load)
    except Exception as e:
        st.error(f"讀取資料失敗：{e}")

elif select_function=="資料維護":
    try:
        maintenance_page.render(load)
    except Exception as e:
        st.error(f"讀取資料失敗：{e}")

else:
    st.header(select_function)
    st.info("這個功能還在開發中。")