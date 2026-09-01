#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
英语单元字帖生成器
==================

把「单元知识专题」里的词汇 / 短语例句 / 短文 / 默写题，排版成可直接打印的 A4 字帖。

产出：
  1. 自包含 HTML（浏览器打开即可 Ctrl+P 打印）
  2. A4 PDF（由 Chrome headless 导出，可直接送印）

用法：
    python3 generate_copybook.py --data data/unit-1-teenage-life.sample.json
    python3 generate_copybook.py --data data/xxx.json --no-pdf

设计要点（踩过的坑，别改回去）：
  * 四线三格用「真实 DOM 边框 + 伪元素」画，不用 background-image。
    浏览器打印默认不输出背景图，用 gradient 画格线会打出一片白纸。
  * 范字基线用 flex baseline + 零宽撑高块（.sp）定位，不靠 line-height 硬凑。
    .sp 高度 = 行高 × 2/3，其 baseline 就是底边，位置与字体无关。
  * 分页在 Python 侧按 mm 预算预先算好，每个 .page 固定 297mm 高，
    不依赖浏览器的分页算法，避免词条被拦腰截断。
  * 词条信息拆两行：第一行「编号 + 单词 / 中文 + 复选框」，第二行「释义 / 说明」，
    避免单行过长互相挤压。
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 版面常量 (mm)

PAGE_W, PAGE_H = 210.0, 297.0
PAD_T, PAD_R, PAD_B, PAD_L = 16.0, 15.0, 14.0, 20.0
CONTENT_H = PAGE_H - PAD_T - PAD_B          # 267
CONTENT_SAFE = CONTENT_H - 8.0               # 分页安全余量，避免估算误差导致裁切
CONTENT_W = PAGE_W - PAD_L - PAD_R          # 175
ROW_INSET = 2.5                             # 范字左边距
AVAIL_W = CONTENT_W - ROW_INSET * 2         # 170

HEAD_H = 15.0
SEC_TITLE_H = 10.0

# 词条信息两行
L1_H = 4.9
L2_LINE_H = 4.0
RECALL_Q_H = 4.8
ITEM_GAP = 3.6
RECALL_GAP = 2.4

COL_GAP = 6.0       # 双栏之间的水平间距

# 短文块
PLABEL_H = 4.6
PLABEL_GAP = 1.2
PBLOCK_GAP = 3.2
PHEAD_H = 5.0        # 短文标题行

CHAR_W = 0.52       # Comic Sans 平均字宽 ≈ 0.52em，用于估算一行放得下几个
SPACE_W = 0.28      # 空格字宽 ≈ 0.28em
TRACE_GAP_SPACES = 5

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# ---------------------------------------------------------------- 宽度估算

def est_width(text: str, font: float) -> float:
    """英文宽度估算：非空格 0.52em，空格 0.28em。"""
    n = len(text)
    sp = text.count(" ")
    return (n - sp) * CHAR_W * font + sp * SPACE_W * font


def est_mixed(text: str, font: float) -> float:
    """中英混排宽度估算：CJK 按 1.0em，ASCII 按 0.52em。"""
    w = 0.0
    for ch in text:
        if ord(ch) > 0x2E80:        # CJK 及全角标点
            w += 1.00 * font
        elif ch == " ":
            w += SPACE_W * font
        else:
            w += CHAR_W * font
    return w


def col_avail(sec: dict) -> float:
    """按栏数算出一栏内可用于书写的净宽度。"""
    cols = max(1, int(sec.get("cols", 1)))
    gap = COL_GAP if cols > 1 else 0.0
    return (CONTENT_W - (cols - 1) * gap) / cols - ROW_INSET * 2


def fit_font(text: str, base: float, avail: float = AVAIL_W) -> float:
    """一行放不下就等比缩字号，最小 2.6mm。"""
    w = est_width(text, base)
    if w <= avail or w == 0:
        return base
    return max(2.6, base * avail / w)


def wrap_text(text: str, font: float, avail: float = AVAIL_W) -> list[str]:
    """按可用宽度贪心折行，用于短文分排。"""
    lines: list[str] = []
    cur = ""
    for w in text.split():
        cand = (cur + " " + w).strip()
        if not cur or est_width(cand, font) <= avail:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_trace_text(text: str, font: float, cap: int,
                     avail: float = AVAIL_W) -> tuple[str, float]:
    """先按期望遍数反推字号，再校验一栏是否放得下。"""
    gap_w = TRACE_GAP_SPACES * SPACE_W * font
    need = cap * est_width(text, font) + (cap - 1) * gap_w
    f = font if need <= avail else max(2.8, font * avail / need)
    body = (" " * TRACE_GAP_SPACES).join([text] * cap)
    return body, fit_font(body, f, avail)


def info_lines(item: dict, sec: dict, avail: float) -> int:
    """第二行（释义 + 说明）预计占几行。留 8% 余量防估算误差。"""
    mode = sec.get("mode", "word")
    parts: list[tuple[str, float]] = []
    if mode == "word":
        if item.get("cn"):
            parts.append((item["cn"], 2.9))
        if item.get("note"):
            parts.append((item["note"], 2.5))
    elif mode == "sentence":
        if item.get("head"):
            parts.append((item["head"], 3.0))
        if item.get("note"):
            parts.append((item["note"], 2.5))
    if not parts:
        return 1
    total = sum(est_mixed(t, f) for t, f in parts) + 1.4 * (len(parts) - 1)
    return max(1, -(-total // (avail * 0.92)))


# ---------------------------------------------------------------- 高度预算

def item_height(item: dict, sec: dict) -> float:
    mode = sec.get("mode", "word")
    row_h = float(item.get("row_h", sec.get("row_h", 8)))
    avail = col_avail(sec)
    if mode == "recall":
        return RECALL_Q_H + row_h + RECALL_GAP
    if mode == "passage":
        font = float(item.get("font", sec.get("font", 4.4)))
        n = len(wrap_text(item.get("text", ""), font, avail))
        head_h = PHEAD_H if item.get("head") else 0.0
        return (head_h + 2 * (PLABEL_H + PLABEL_GAP) + PBLOCK_GAP
                + 2 * n * row_h + ITEM_GAP)
    blanks = int(item.get("blanks", sec.get("blanks", 1)))
    info = L1_H + info_lines(item, sec, avail) * L2_LINE_H
    return info + (1 + blanks) * row_h + ITEM_GAP


def group_items(sec: dict) -> list[tuple[list[dict], float]]:
    """按栏数把词条分组，一组占一行；行高取组内最大。"""
    cols = max(1, int(sec.get("cols", 1)))
    items = sec["items"]
    if cols <= 1:
        return [([it], item_height(it, sec)) for it in items]
    return [(items[i:i + cols],
             max(item_height(x, sec) for x in items[i:i + cols]))
            for i in range(0, len(items), cols)]


def paginate(data: dict) -> list[list[tuple]]:
    """把 sections 摊平后按 mm 预算装箱，返回每页的 block 列表。"""
    pages: list[list] = []
    cur: list = []
    used = HEAD_H

    def flush():
        nonlocal cur, used
        if cur:
            pages.append(cur)
        cur, used = [], HEAD_H

    for sec in data["sections"]:
        groups = group_items(sec)
        if not groups:
            continue
        if used + SEC_TITLE_H + groups[0][1] > CONTENT_SAFE:
            flush()
        cur.append(("title", sec, False))
        used += SEC_TITLE_H
        for grp, h in groups:
            if used + h > CONTENT_SAFE:
                flush()
                cur.append(("title", sec, True))
                used += SEC_TITLE_H
            cur.append(("items", grp, sec))
            used += h
    flush()
    return pages


# ---------------------------------------------------------------- 片段渲染

def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def chk(n: int) -> str:
    return f'<span class="chk">{"".join("<i></i>" for _ in range(n))}</span>'


def row(inner: str, cls: str = "") -> str:
    return f'<div class="row {cls}"><i class="sp"></i>{inner}</div>'


def grid_style(row_h: float) -> str:
    return f"--rh:{row_h:.2f}mm;--bh:{row_h * 2 / 3:.2f}mm"


def render_word_item(item: dict, sec: dict, no: int) -> str:
    """第一行：编号 + 单词 + 词性 + 复选框；第二行：中文释义 + 说明。"""
    avail = col_avail(sec)
    row_h = float(item.get("row_h", sec.get("row_h", 8)))
    font = float(item.get("font", sec.get("font", 5.4)))
    cap = int(item.get("repeat_cap", sec.get("repeat_cap", 2)))
    blanks = int(item.get("blanks", sec.get("blanks", 2)))

    trace_src = item.get("trace") or item.get("head", "")
    trace_txt, trace_font = build_trace_text(trace_src, font, cap, avail)

    l1 = [f'<span class="no">{no:02d}</span>',
          f'<span class="hw">{esc(item.get("head", ""))}</span>']
    if item.get("pos"):
        l1.append(f'<span class="pos">{esc(item["pos"])}</span>')
    l1.append('<span class="grow"></span>' + chk(3))

    l2 = []
    if item.get("cn"):
        l2.append(f'<span class="cn2">{esc(item["cn"])}</span>')
    if item.get("note"):
        l2.append(f'<span class="note">{esc(item["note"])}</span>')

    rows = [row(f'<span class="txt" style="font-size:{trace_font:.2f}mm">'
                f'{esc(trace_txt)}</span>', "tr")]
    rows.append(row("") * blanks)

    return (f'<div class="item">'
            f'<div class="l1">{"".join(l1)}</div>'
            f'<div class="l2">{"".join(l2)}</div>'
            f'<div class="grid" style="{grid_style(row_h)}">{"".join(rows)}</div>'
            f'</div>')


def render_sentence_item(item: dict, sec: dict, no: int) -> str:
    """第一行：编号 + 中文释义 + 复选框；第二行：短语本体 + 固定搭配。"""
    avail = col_avail(sec)
    row_h = float(item.get("row_h", sec.get("row_h", 9)))
    font = float(item.get("font", sec.get("font", 4.4)))
    blanks = int(item.get("blanks", sec.get("blanks", 1)))

    trace_txt = item.get("trace", "")
    trace_font = fit_font(trace_txt, font, avail)

    l1 = [f'<span class="no">{no:02d}</span>',
          f'<span class="cn1">{esc(item.get("cn", ""))}</span>',
          '<span class="grow"></span>' + chk(3)]

    l2 = [f'<span class="ph">{esc(item.get("head", ""))}</span>']
    if item.get("note"):
        l2.append(f'<span class="note">{esc(item["note"])}</span>')

    rows = [row(f'<span class="txt" style="font-size:{trace_font:.2f}mm">'
                f'{esc(trace_txt)}</span>', "tr")]
    rows.append(row("") * blanks)

    return (f'<div class="item">'
            f'<div class="l1">{"".join(l1)}</div>'
            f'<div class="l2">{"".join(l2)}</div>'
            f'<div class="grid" style="{grid_style(row_h)}">{"".join(rows)}</div>'
            f'</div>')


def render_passage_item(item: dict, sec: dict, no: int) -> str:
    """短文：先整段描红，再整段临写。"""
    avail = col_avail(sec)
    row_h = float(item.get("row_h", sec.get("row_h", 8)))
    font = float(item.get("font", sec.get("font", 4.4)))
    lines = wrap_text(item.get("text", ""), font, avail)

    g1 = "".join(row(f'<span class="txt" style="font-size:{font:.2f}mm">'
                     f'{esc(l)}</span>', "tr") for l in lines)
    g2 = "".join(row("") for _ in lines)
    st = grid_style(row_h)
    head = (f'<div class="phead2">{esc(item["head"])}</div>'
            if item.get("head") else "")

    return (f'<div class="item">{head}'
            f'<div class="plabel">① 描红</div>'
            f'<div class="grid" style="{st}">{g1}</div>'
            f'<div class="plabel">② 临写</div>'
            f'<div class="grid" style="{st}">{g2}</div>'
            f'</div>')


def render_recall_item(item: dict, sec: dict, no: int) -> str:
    row_h = float(item.get("row_h", sec.get("row_h", 8)))
    q = [f'<span class="no">{no:02d}</span>',
         f'<span class="prompt">{esc(item.get("prompt", ""))}</span>']
    if item.get("hint"):
        q.append(f'<span class="hint">{esc(item["hint"])}</span>')
    q.append('<span class="grow"></span>' + chk(1))
    return (f'<div class="item"><div class="recall-q">{"".join(q)}</div>'
            f'<div class="grid" style="{grid_style(row_h)}">{row("")}</div></div>')


def render_title(sec: dict, carry: bool) -> str:
    suffix = "（续）" if carry else ""
    bits = [esc(sec.get("desc", ""))]
    if sec.get("minutes"):
        bits.append(f"约 {int(sec['minutes'])} 分钟")
    desc = f"<em>{'　·　'.join(b for b in bits if b)}</em>"
    legend = ('<span class="legend">□ 拼写　□ 释义　□ 搭配</span>'
              if sec.get("mode") in ("word", "sentence") else "")
    return (f'<div class="sec-t">{esc(sec["key"])}　{esc(sec["title"])}{suffix}'
            f'{desc}{legend}</div>')


def render_page(data: dict, blocks: list, pno: int, total: int) -> str:
    body = [
        f'<div class="phead">'
        f'<div class="ph-l"><b>{esc(data["unit"])}</b>'
        f'<span>{esc(data.get("book", ""))}　·　{esc(data.get("scope", ""))}</span></div>'
        f'<div class="ph-r">第 ______ 轮　　日期 __________　　自评 ______</div>'
        f'</div>'
    ]
    counters: dict[str, int] = {}
    renderer = {
        "word": render_word_item,
        "sentence": render_sentence_item,
        "passage": render_passage_item,
        "recall": render_recall_item,
    }
    for kind, a, b in blocks:
        if kind == "title":
            body.append(render_title(a, b))
        else:
            grp, sec = a, b
            fn = renderer.get(sec.get("mode", "word"), render_word_item)
            inner = []
            for item in grp:
                counters[sec["key"]] = counters.get(sec["key"], 0) + 1
                inner.append(fn(item, sec, counters[sec["key"]]))
            html_items = "".join(inner)
            body.append(f'<div class="cols">{html_items}</div>'
                        if len(grp) > 1 else html_items)
    body.append(
        f'<div class="pfoot"><span>{esc(data.get("footer", data["unit"]))}</span>'
        f'<span>第 {pno} 页 / 共 {total} 页</span></div>'
    )
    return f'<section class="page">{"".join(body)}</section>'


# ---------------------------------------------------------------- 使用说明页

GUIDE_PAGE = """
<section class="page guide">
  <div class="phead">
    <div class="ph-l"><b>{unit}</b><span>{book}　·　{scope}</span></div>
    <div class="ph-r">姓名 __________　　班级 __________</div>
  </div>

  <h1>怎么用这份字帖</h1>
  <p class="sub">一整份约 <b>{total_min} 分钟</b>一次写完。练字只是载体，目标是「字形 + 拼写 + 搭配」一起记住。</p>

  <h2>一、三段式，一段都不能跳</h2>
  <div class="steps">
    <div class="step"><b>① 描红</b><span>照着灰色范字描。慢，看清楚每个字母占哪几格：上伸字母顶到第一线，x 高度字母占满中格，下伸字母落到第四线。</span></div>
    <div class="step"><b>② 临写</b><span>空格里自己写，写完回头和上一行比一比，看哪个字母歪了、哪个格子没占满。</span></div>
    <div class="step"><b>③ 默写</b><span>翻到最后的「默写自检」，只看中文提示写出来。写不出来的，当场回前面重新描一遍。</span></div>
  </div>

  <h2>二、右侧三个小方框是自检，不是装饰</h2>
  <ul>
    <li><b>拼写</b>：合上本子能默写出来，才打勾。</li>
    <li><b>释义</b>：看到英文能立刻说出中文，且能说清词性。</li>
    <li><b>搭配</b>：能说出它的固定伙伴（比如 responsible 后面一定跟 for）。</li>
  </ul>
  <p>三个都勾上才算过。只勾前两个的，第二天优先重写。</p>

  <h2>三、节奏</h2>
  <ul>
    <li>一整份 {total_min} 分钟一次写完，中间不刷手机、不翻书。</li>
    <li>同一份隔 2 天、再隔 7 天各回来一次，比连写十遍管用得多。</li>
    <li>短文板块先整段描红、再整段临写，注意词与词之间的间距，别连成一团。</li>
  </ul>

  <h2>四、练习记录</h2>
  <table class="log">
    <tr><th>轮次</th><th>日期</th><th>用时</th><th>拼写过关</th><th>释义过关</th><th>搭配过关</th><th>需重做</th></tr>
    {log_rows}
  </table>

  <div class="demo">
    <div class="sec-t">范字示意<em>第一线 / 第二线 / 基线（深色）/ 第四线</em></div>
    <div class="grid" style="--rh:9.00mm;--bh:6.00mm">
      <div class="row tr"><i class="sp"></i><span class="txt" style="font-size:5.60mm">{sample}</span></div>
      <div class="row"><i class="sp"></i></div>
    </div>
  </div>

  <div class="pfoot"><span>{footer}</span><span>第 1 页 / 共 {total} 页</span></div>
</section>
"""


def render_guide(data: dict, total: int) -> str:
    total_min = sum(int(s.get("minutes", 0)) for s in data["sections"])
    log_rows = "".join(
        "<tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>"
        for _ in range(6)
    )
    return GUIDE_PAGE.format(
        unit=esc(data["unit"]),
        book=esc(data.get("book", "")),
        scope=esc(data.get("scope", "")),
        footer=esc(data.get("footer", data["unit"])),
        total=total,
        total_min=total_min,
        log_rows=log_rows,
        sample="challenge　fluent　responsible",
    )


# ---------------------------------------------------------------- 样式

CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --ink: #1A1A1A;
  --ink2: #3A3A36;
  --ink3: #6E6E68;
  --trace: #C9CFD6;
  --line: #CBD3DA;
  --base: #93A0AD;
  --accent: #1F4E6B;
  --hand: __HAND_FONT__;
}

html { background: #E9E9E6; }
body {
  font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: var(--ink); background: #E9E9E6;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}

.page {
  width: 210mm; height: 297mm;
  padding: 16mm 15mm 14mm 20mm;
  background: #fff;
  margin: 0 auto 10mm;
  position: relative; overflow: hidden;
  break-after: page; page-break-after: always;
}
.page:last-child { break-after: auto; page-break-after: auto; }

@media print {
  html, body { background: #fff; }
  .page { margin: 0; box-shadow: none; }
}

.phead {
  height: 15mm; display: flex; align-items: flex-start; justify-content: space-between;
  border-bottom: 0.8pt solid #C4C4BE;
}
.ph-l b { font-size: 4.4mm; letter-spacing: .01em; font-weight: 600; }
.ph-l span { display: block; font-size: 2.7mm; color: #7C7C76; margin-top: 1.2mm; }
.ph-r { font-size: 2.7mm; color: #7C7C76; text-align: right; white-space: nowrap; }

.pfoot {
  position: absolute; left: 20mm; right: 15mm; bottom: 6mm;
  display: flex; justify-content: space-between;
  font-size: 2.5mm; color: #A2A29C;
  border-top: 0.4pt solid #E0E0DA; padding-top: 1.4mm;
}

.sec-t {
  font-size: 3.3mm; font-weight: 600; color: var(--accent);
  margin-bottom: 2.6mm; display: flex; align-items: baseline; gap: 2mm;
}
.sec-t em { font-style: normal; font-size: 2.5mm; font-weight: 400; color: #8A8A85; }
.sec-t .legend { margin-left: auto; font-size: 2.4mm; font-weight: 400; color: #9A9A95; }

.item { break-inside: avoid; page-break-inside: avoid; }

.cols { display: flex; flex-wrap: wrap; column-gap: 6mm; }
.cols > .item { width: calc(50% - 3mm); }

.l1, .l2, .recall-q { display: flex; align-items: baseline; gap: 1.4mm; }
.l1 { height: 4.9mm; flex-wrap: nowrap; overflow: hidden; }
.l2 { min-height: 4.0mm; flex-wrap: wrap; line-height: 4.0mm; }
.recall-q { height: 4.8mm; }

.no { font-size: 2.6mm; color: #B0B0AA; font-family: ui-monospace, Menlo, monospace; }
.hw { font-size: 3.9mm; font-weight: 600; }
.pos { font-size: 2.7mm; color: var(--ink3); }
.cn1 { font-size: 3.0mm; color: var(--ink2); font-weight: 500; }
.cn2 { font-size: 2.9mm; color: var(--ink2); }
.ph { font-size: 3.0mm; color: var(--accent); font-weight: 600;
      font-family: var(--hand); }
.note { font-size: 2.5mm; color: #8A8A85; }
.prompt { font-size: 3.0mm; color: var(--ink2); font-weight: 500; }
.hint { font-size: 2.6mm; color: #9A9A95; font-family: ui-monospace, Menlo, monospace; }
.grow { flex: 1; }
.chk { display: flex; gap: 1.4mm; }
.chk i { display: block; width: 2.6mm; height: 2.6mm; border: 0.4pt solid #B9B9B3; }

.plabel { font-size: 2.7mm; font-weight: 600; color: var(--accent);
          margin-bottom: 1.2mm; }
.grid + .plabel { margin-top: 3.2mm; }
.phead2 { font-size: 3.0mm; font-weight: 600; color: var(--ink2);
          height: 5.0mm; line-height: 5.0mm; }

.grid { border-bottom: 0.35pt solid var(--line); }
.row {
  position: relative; height: var(--rh, 8mm);
  border-top: 0.35pt solid var(--line);
  display: flex; align-items: baseline; overflow: hidden;
}
.row::before {
  content: ""; position: absolute; left: 0; right: 0; top: 33.333%;
  border-top: 0.35pt solid var(--line);
}
.row::after {
  content: ""; position: absolute; left: 0; right: 0; top: 66.667%;
  border-top: 0.6pt solid var(--base);
}
.sp { display: inline-block; width: 0; height: var(--bh, 5.34mm); }
.txt {
  padding-left: 2.5mm; white-space: pre;
  font-family: var(--hand); color: var(--ink);
}
.row.tr .txt { color: var(--trace); }

.guide h1 { font-size: 6mm; font-weight: 600; margin-top: 6mm; }
.guide .sub { font-size: 3mm; color: #7C7C76; margin-top: 2mm; }
.guide h2 { font-size: 3.6mm; font-weight: 600; color: var(--accent); margin: 7mm 0 3mm; }
.guide p { font-size: 3mm; line-height: 1.9; color: var(--ink2); }
.guide ul { padding-left: 5mm; margin-top: 1mm; }
.guide li { font-size: 3mm; line-height: 1.9; color: var(--ink2); }
.guide .steps { display: flex; gap: 3mm; margin: 3mm 0 1mm; }
.guide .step { flex: 1; border: 0.5pt solid #D8D8D2; padding: 3mm; }
.guide .step b { display: block; font-size: 3.2mm; color: var(--accent); margin-bottom: 1.5mm; }
.guide .step span { display: block; font-size: 2.6mm; color: var(--ink3); line-height: 1.75; }
.guide .demo { margin-top: 8mm; }
table.log { width: 100%; border-collapse: collapse; margin-top: 3mm; }
table.log th, table.log td {
  border: 0.4pt solid #C4C4BE; height: 7.5mm;
  text-align: center; font-size: 2.8mm;
}
table.log th { background: #F4F4F0; font-weight: 600; }
"""


# ---------------------------------------------------------------- 主流程

def build_html(data: dict) -> str:
    pages = paginate(data)
    total = len(pages) + 1
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        f'<title>{esc(data["unit"])} · 字帖</title>',
        f'<style>{CSS.replace("__HAND_FONT__", data.get("hand_font", ""))}</style>',
        "</head><body>",
        render_guide(data, total),
    ]
    for i, blocks in enumerate(pages, start=2):
        parts.append(render_page(data, blocks, i, total))
    parts.append("</body></html>")
    return "".join(parts)


def export_pdf(html_path: Path, pdf_path: Path) -> bool:
    cmd = [
        CHROME, "--headless", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--virtual-time-budget=4000",
        f"--print-to-pdf={pdf_path}", f"file://{html_path}",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("  ! 未找到 Chrome，跳过 PDF 导出", file=sys.stderr)
        return False
    return pdf_path.exists() and pdf_path.stat().st_size > 1000


def main() -> int:
    ap = argparse.ArgumentParser(description="英语单元字帖生成器")
    ap.add_argument("--data", required=True, help="内容数据 JSON 路径")
    ap.add_argument("--out-dir", default=None, help="输出目录，默认 <数据文件上级>/<单元名>")
    ap.add_argument("--no-pdf", action="store_true", help="只生成 HTML，不导出 PDF")
    args = ap.parse_args()

    data_path = Path(args.data).expanduser().resolve()
    data = json.loads(data_path.read_text(encoding="utf-8"))

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        out_dir = data_path.parent.parent / data["unit"]
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = paginate(data)
    total = len(pages) + 1
    total_min = sum(int(s.get("minutes", 0)) for s in data["sections"])

    print(f"单元：{data['unit']}")
    print(f"预计用时：{total_min} 分钟（整份一次写完）")
    print(f"内容页 {len(pages)} 页 + 说明页 1 页 = {total} 页")
    for i, blocks in enumerate(pages, start=2):
        used = HEAD_H
        for k, a, b in blocks:
            if k == "title":
                used += SEC_TITLE_H
            else:
                used += max(item_height(x, b) for x in a)
        marks = "".join("T" if k == "title" else "." for k, _, _ in blocks)
        print(f"  P{i}: 占用 {used:6.1f}mm / {CONTENT_H:.0f}mm   {marks}")

    html_path = out_dir / f"{data['unit']}-字帖.html"
    html_path.write_text(build_html(data), encoding="utf-8")
    print(f"\nHTML  -> {html_path}")

    if not args.no_pdf:
        pdf_path = out_dir / f"{data['unit']}-字帖.pdf"
        if export_pdf(html_path, pdf_path):
            print(f"PDF   -> {pdf_path}  ({pdf_path.stat().st_size / 1024:.0f} KB)")
        else:
            print("PDF   -> 导出失败", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
