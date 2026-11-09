"""管制藥品收支結存簿冊：產生仿紙本格式的 HTML（A4 橫式，每頁 16 行），含列印按鈕。

用法（app.py）：
    import streamlit.components.v1 as components
    html = print_ledger.render(header, led)      # header 是 dict，led 是帳冊 DataFrame
    components.html(html, height=760, scrolling=True)
"""
from html import escape

ROWS_PER_PAGE = 16

# 資料欄：(欄位名稱, 右邊界 %)；0% 起算，依紙本帳冊的欄寬比例
DATA_COLS = [("日期", 9.6), ("收支原因", 19.2), ("收入數量", 29.5), ("收入藥品批號", 41.3),
             ("支出數量", 50.8), ("結存數量", 61.9), ("備註", 100.0)]

# 表頭列用到的額外分界（讓第 2、3 列的欄位位置貼近紙本）
EXTRA = [32.8, 47.7, 70.0, 83.3]
BOUNDS = sorted({0.0, *[b for _, b in DATA_COLS], *EXTRA})   # 全部分界線 → 共用的欄格


def _span(x0: float, x1: float) -> int:
    """x0~x1 這段橫跨幾個欄格（用來設定 colspan）。"""
    return sum(1 for a, b in zip(BOUNDS, BOUNDS[1:]) if a >= x0 - 1e-6 and b <= x1 + 1e-6)


def _cell(x0, x1, text="", cls=""):
    return f'<td colspan="{_span(x0, x1)}" class="{cls}">{escape(str(text))}</td>'


def _fmt(v) -> str:
    """空值顯示為空白；整數不要帶 .0。"""
    if v is None:
        return ""
    try:
        if v != v:            # NaN
            return ""
    except Exception:
        pass
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _page(header: dict, rows: list, page_no: int, pages: int) -> str:
    h = lambda k: _fmt(header.get(k, ""))
    colgroup = "".join(f'<col style="width:{b - a:.2f}%">' for a, b in zip(BOUNDS, BOUNDS[1:]))
    top = [
        # 標題列：整列只有一個格子，標題靠左、說明靠右（同一格內，所以中間不會有格線）
        f'<tr class="title"><td colspan="{_span(0, 100)}" class="ttlrow">'
        '<span class="ttl"><b>管制藥品收支結存簿冊</b></span>'
        '<span class="sub">（<b>醫療機構、藥局</b>、醫藥教育研究試驗機構、獸醫診療機構、畜牧獸醫機構）</span>'
        '</td></tr>',
        "<tr>" + _cell(0, 9.6, "藥品名稱", "lb") + _cell(9.6, 32.8, h("品名"))
        + _cell(32.8, 47.7, "管制藥品成分及含量", "lb s") + _cell(47.7, 70, h("管制藥品成分及含量"))
        + _cell(70, 83.3, "藥品許可證字號", "lb") + _cell(83.3, 100, h("藥品許可證字號")) + "</tr>",
        "<tr>" + _cell(0, 9.6, "管制級別", "lb") + _cell(9.6, 19.2, h("管制級別"))
        + _cell(19.2, 29.5, "製造廠名稱", "lb") + _cell(29.5, 70, h("製造廠名稱"))
        + _cell(70, 83.3, "最　小　單　位", "lb") + _cell(83.3, 100, h("最小單位")) + "</tr>",
        "<tr>" + "".join(
            _cell(a, b, name, "lb")
            for name, a, b in zip([n for n, _ in DATA_COLS], [0] + [b for _, b in DATA_COLS[:-1]],
                                  [b for _, b in DATA_COLS])) + "</tr>",
    ]
    body = []
    for r in rows + [None] * (ROWS_PER_PAGE - len(rows)):          # 不足 16 行補空白列
        cells = []
        for (name, b), a in zip(DATA_COLS, [0] + [b for _, b in DATA_COLS[:-1]]):
            num = name in ("收入數量", "支出數量", "結存數量")
            cells.append(_cell(a, b, "" if r is None else _fmt(r.get(name)), "num" if num else ""))
        body.append('<tr class="dr">' + "".join(cells) + "</tr>")
    foot = f'<div class="pg">第 {page_no} / {pages} 頁</div>'
    return f'<div class="page"><table><colgroup>{colgroup}</colgroup>{"".join(top)}{"".join(body)}</table>{foot}</div>'


CSS = """
@page { size: A4 landscape; margin: 10mm; }
* { box-sizing: border-box; }
/* 字型依序比對「每一個字」：英文、數字先用 Times New Roman；Times New Roman 沒有的字（中文、全形標點）自動換用標楷體 */
body { margin: 0; font-family: "Times New Roman", "DFKai-SB", "標楷體", serif; color: #000; background: #eee; }
.bar { position: sticky; top: 0; background: #fff; padding: 8px 12px; border-bottom: 1px solid #ccc; z-index: 1; }
.bar button { font-size: 15px; padding: 6px 18px; cursor: pointer; }
.bar span { margin-left: 12px; color: #666; font-size: 13px; }
.page { width: 277mm; background: #fff; margin: 12px auto; padding: 0; page-break-after: always; }
.page:last-child { page-break-after: auto; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
td { border: 1px solid #000; height: 8.6mm; padding: 0 5px; font-size: 13px; overflow: hidden; white-space: nowrap; }
td.lb { text-align: center; }
td.s { font-size: 11px; }
td.num { text-align: right; padding-right: 8px; }
tr.title td { height: 9.5mm; }
.ttl { font-size: 19px; font-weight: bold; }
.sub { float: right; font-size: 12px; line-height: 9.5mm; }
td.ttlrow { padding-left: 14px; }
.pg { text-align: right; font-size: 11px; color: #555; margin-top: 3px; }
@media print {
  body { background: #fff; }
  .bar { display: none; }
  .page { margin: 0; width: auto; }
}
"""


def render(header: dict, led) -> str:
    """header：表頭欄位 dict；led：帳冊 DataFrame（欄位含日期、收支原因…、備註）。"""
    records = led.to_dict("records") if led is not None and len(led) else []
    chunks = [records[i:i + ROWS_PER_PAGE] for i in range(0, len(records), ROWS_PER_PAGE)] or [[]]
    pages = "".join(_page(header, c, i + 1, len(chunks)) for i, c in enumerate(chunks))
    bar = ('<div class="bar"><button onclick="window.print()">🖨 列印</button>'
           f'<span>共 {len(chunks)} 頁（A4 橫式，每頁 {ROWS_PER_PAGE} 行）。列印時請選 A4、橫向、縮放 100%。</span></div>')
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{bar}{pages}</body></html>"
