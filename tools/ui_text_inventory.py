#!/usr/bin/env python3
"""Build a reviewable inventory of the web client's visible strings.

The page's wording lives in two places -- ``index.html`` for labels, tooltips
and option names, and ``static/app.js`` for the status line and the strings the
script writes at run time.  This tool reads both and writes:

* ``docs/10-界面文本清单.md`` -- a sectioned Markdown table for reading;
* ``docs/ui-text-inventory.csv`` -- the same rows for a spreadsheet;
* ``docs/ui-text-review.html`` -- a self-contained filterable page.

It is deliberately a *reader*: editing one wording should stay a one-line edit
in the source, not in a generated copy.  Re-run this after changing the UI.
"""

from __future__ import annotations

import csv
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web" / "static"
DOCS = ROOT / "docs"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["Node"]):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: List[object] = []

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    def classes(self) -> List[str]:
        return self.attrs.get("class", "").split()

    def elements(self) -> Iterable["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.elements()

    def text(self) -> str:
        chunks: List[str] = []
        for child in self.children:
            if isinstance(child, str):
                chunks.append(child)
            elif child.tag not in ("script", "style", "option", "select", "input"):
                chunks.append(child.text())
        return " ".join("".join(chunks).split())


class Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {key: (value if value is not None else "") for key, value in attrs},
                    self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {key: (value if value is not None else "") for key, value in attrs},
                    self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].children.append(data)


def load_html() -> Node:
    builder = Builder()
    builder.feed((STATIC / "index.html").read_text())
    return builder.root


SECTION_TAGS = {"h1", "h2", "h3", "summary"}


def own_text(node: Node) -> str:
    """Only the literal text directly inside *node*, not its controls' labels."""
    chunks = [child for child in node.children if isinstance(child, str)]
    return " ".join("".join(chunks).split())


def heading_label(node: Node) -> str:
    if node.tag == "figcaption":
        for element in node.elements():
            if "view-title" in element.classes():
                return own_text(element) or element.text()
        tabs = [element.text() for element in node.elements()
                if "tab" in element.classes()]
        if tabs:
            return " / ".join(tabs)
    if node.tag == "h2":
        for element in node.elements():
            if "zh" in element.classes():
                return element.text()
    return own_text(node) or node.text()


def section_for(node: Node) -> str:
    """Walk backwards through document order to the nearest section heading."""
    chain: List[Node] = []
    for element in ALL:
        if element is node:
            break
        if element.tag in SECTION_TAGS:
            chain.append(element)
        elif (element.tag == "figcaption"
              and element.parent is not None
              and "view" in element.parent.classes()):
            chain.append(element)
        elif element.tag == "div" and "sky-heading" in element.classes():
            chain.append(element)
    if not chain:
        return "未分组"
    heading = chain[-1]
    label = heading_label(heading)
    if heading.tag == "figcaption":
        label = label or "图窗"
    return label or heading.tag


def control_rows(root: Node) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for element in root.elements():
        if element.tag == "label":
            controls = [child for child in element.elements()
                        if child.tag in ("input", "select", "textarea")]
            if not controls:
                continue
            control = controls[0]
            kind = control.tag
            if kind == "input":
                kind = control.attrs.get("type", "text")
            options = []
            if control.tag == "select":
                options = [option.text() or option.attrs.get("value", "")
                           for option in control.elements() if option.tag == "option"]
            label = element.text()
            title = element.attrs.get("title", "")
            if not title:
                title = control.attrs.get("title", "")
            rows.append({
                "section": section_for(element),
                "kind": f"label/{kind}",
                "id": control.id,
                "text": label,
                "title": title,
                "options": " | ".join(options),
            })
        elif element.tag == "button" and element.id:
            rows.append({
                "section": section_for(element),
                "kind": "button",
                "id": element.id,
                "text": element.text(),
                "title": element.attrs.get("title", ""),
                "options": "",
            })
        elif element.tag in ("p", "span", "div", "output", "div") and element.id:
            if element.tag == "div" and not element.classes():
                continue
            label = element.text()
            if not label:
                continue
            if element.tag == "output":
                rows.append({
                    "section": section_for(element),
                    "kind": "output",
                    "id": element.id,
                    "text": label,
                    "title": element.attrs.get("title", ""),
                    "options": "",
                })
            elif "hint" in element.classes() or "view-foot" in element.classes() \
                    or "status" in element.classes() or "coord" in element.classes():
                rows.append({
                    "section": section_for(element),
                    "kind": "readout",
                    "id": element.id,
                    "text": label,
                    "title": element.attrs.get("title", ""),
                    "options": "",
                })
    seen = {row["id"] for row in rows}
    for element in root.elements():
        if element.tag not in ("input", "select", "textarea") or not element.id:
            continue
        if element.id in seen:
            continue
        ancestor = element.parent
        row_node = None
        while ancestor is not None:
            if {"slider-row", "param-row"} & set(ancestor.classes()):
                row_node = ancestor
                break
            ancestor = ancestor.parent
        if row_node is None:
            continue
        label_node = next((node for node in row_node.elements()
                           if "slider-label" in node.classes()), None)
        text = label_node.text() if label_node else ""
        title = (row_node.attrs.get("title", "")
                 or (label_node.attrs.get("title", "") if label_node else "")
                 or element.attrs.get("title", ""))
        kind = element.tag if element.tag != "input" else element.attrs.get("type", "text")
        options = ""
        if element.tag == "select":
            options = " | ".join(option.text() or option.attrs.get("value", "")
                                 for option in element.elements() if option.tag == "option")
        rows.append({
            "section": section_for(element),
            "kind": f"control/{kind}",
            "id": element.id,
            "text": text,
            "title": title,
            "options": options,
        })
        seen.add(element.id)
    return rows


#: Wording we would change if the reader agrees.  Kept here, not in the HTML,
#: so a suggestion cannot quietly become a change before it is reviewed.
SUGGESTIONS = {
    "out-kmpx-preset": "档位里的「数据源上限」现已自动带产品名（如 SLDEM 上限）；确认是否保留产品名",
    "seeing": "默认值改为 0.80″",
    "sharpen-strength": "上限 5；确认 0–5 的刻度是否要改成非线性",
    "blur-strength": "范围已收为 0–1",
    "label-shadow": "“暗面”与提示里的“夜面”建议统一为“夜面”",
    "label-refresh-font": "“刷新字体”只改字号，建议叫“适配字号”",
    "preview-samples": "与移入模拟设置的“每像素采样”重名，建议区分“预览采样”",
    "curve-nudge-toggle": "点开后是时间微调，确认“时间步长”是否比“时间步进”更准确",
    "curve-white-toggle": "点按后按钮变“黑色”，提示里已说明；确认这个反义按钮是否直观",
    "experiment-open": "“实验功能”较泛，可考虑“月面太阳”或“月面光照”",
    "sharpen-method": "“高斯锐化 / 小波锐化”两个名字是否保留",
    "diffraction": "“衍射环”实际控制环的能量占比，是否改“衍射鬼影”",
    "curve-series-point-color": "与上一行“颜色”重名，建议“点色”或“数据点颜色”",
}

STATUS_RE = re.compile(r"setStatus\(\s*(['`\"])(.*?)\1", re.S)
TITLE_RE = re.compile(r"\.title\s*=\s*(['`\"])(.*?)\1", re.S)


def js_strings(path: Path) -> List[Dict[str, str]]:
    source = path.read_text()
    rows: List[Dict[str, str]] = []
    for match in STATUS_RE.finditer(source):
        text = " ".join(match.group(2).split())
        if not text or "\n" in text:
            continue
        line = source.count("\n", 0, match.start()) + 1
        rows.append({"kind": "状态栏", "line": str(line), "text": text})
    return rows


def write_markdown(rows: List[Dict[str, str]], statuses: List[Dict[str, str]]) -> None:
    lines = [
        "# 网页客户端文本清单（供逐项审阅）",
        "",
        "这份清单由 `tools/ui_text_inventory.py` 从 `web/static/index.html` 与",
        "`web/static/app.js` 生成，按界面分区列出当前实际显示的文本。",
        "",
        "用法：在“ID / 位置”一列找到要改的控件，把“建议”或你的新文案告诉我；",
        "不要直接改本文件（下次生成会覆盖）。",
        "",
        "> 行号会随代码移动，ID 和控件类型才是稳定的定位方式。",
        "",
    ]
    grouped: Dict[str, List[Dict[str, str]]] = {}
    order: List[str] = []
    for row in rows:
        if row["section"] not in grouped:
            grouped[row["section"]] = []
            order.append(row["section"])
        grouped[row["section"]].append(row)
    for section in order:
        lines += ["", f"## {section}", "",
                  "| ID / 位置 | 类型 | 当前文本 | 悬停提示 / 说明 | 选项 | 建议 |",
                  "|---|---|---|---|---|---|"]
        for row in grouped[section]:
            text = row["text"].replace("|", "\\|")
            title = row["title"].replace("|", "\\|")
            options = row["options"].replace("|", "\\|")
            suggestion = SUGGESTIONS.get(row["id"], "").replace("|", "\\|")
            lines.append(f"| `{row['id']}` | {row['kind']} | {text} | {title} | {options} | {suggestion} |")
    lines += ["", "## 状态栏与运行时文案（app.js）", "",
              "| 行 | 当前文本 | 建议 |", "|---|---|---|"]
    for row in statuses:
        text = row["text"].replace("|", "\\|")
        lines.append(f"| {row['line']} | {text} |  |")
    lines.append("")
    (DOCS / "10-界面文本清单.md").write_text("\n".join(lines))


def write_csv(rows: List[Dict[str, str]],
              statuses: List[Dict[str, str]]) -> None:
    with (DOCS / "ui-text-inventory.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "kind", "id", "text",
                                                    "title", "options"])
        writer.writeheader()
        writer.writerows(rows)
        for row in statuses:
            writer.writerow({"section": "状态栏", "kind": "status",
                             "id": f"app.js:{row['line']}", "text": row["text"],
                             "title": "", "options": ""})


def write_html(rows: List[Dict[str, str]], statuses: List[Dict[str, str]]) -> None:
    data = json.dumps(rows, ensure_ascii=False)
    status_data = json.dumps(statuses, ensure_ascii=False)
    template = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>网页客户端文本清单</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#0e1116;color:#dfe4ee;
font:13px -apple-system,"PingFang SC",sans-serif}header{position:sticky;top:0;
background:#151a21;padding:10px 16px;border-bottom:1px solid #2a303a;z-index:2}
h1{font-size:16px;margin:0 0 6px}input{width:320px;max-width:50vw;padding:4px 7px;
background:#0b0e13;color:#dfe4ee;border:1px solid #343b47;border-radius:4px}
table{width:100%;border-collapse:collapse}th,td{padding:4px 8px;
border-bottom:1px solid #222832;vertical-align:top;text-align:left}th{position:sticky;
top:64px;background:#151a21;color:#98a0b0;font-weight:500}tr.section td{background:#1a2029;
color:#ffd666;font-weight:600}code{color:#9db8f5}.options{color:#8b93a3;max-width:380px}
.title{color:#8b93a3;max-width:460px}.muted{color:#6f7788}
</style></head><body><header><h1>网页客户端文本清单</h1>
<input id="q" placeholder="过滤：ID、文本或提示…" autofocus>
<span class="muted" id="count"></span></header>
<table><thead><tr><th>ID</th><th>类型</th><th>当前文本</th>
<th>悬停提示 / 说明</th><th>选项</th></tr></thead><tbody id="rows"></tbody></table>
<h2 style="margin:24px 16px 8px">状态栏与运行时文案</h2>
<table><thead><tr><th>行</th><th>当前文本</th></tr></thead>
<tbody id="status"></tbody></table>
<script>
const rows = __ROWS__, statuses = __STATUS__;
const body = document.getElementById('rows'), status = document.getElementById('status');
let section = null;
function render(filter){
  body.textContent='';
  let shown=0;
  for(const row of rows){
    const hay=(row.section+' '+row.id+' '+row.text+' '+row.title+' '+row.options).toLowerCase();
    if(filter && !hay.includes(filter)) continue;
    if(row.section!==section){
      section=row.section;
      const tr=document.createElement('tr'); tr.className='section';
      const td=document.createElement('td'); td.colSpan=5; td.textContent=row.section;
      tr.appendChild(td); body.appendChild(tr);
    }
    const tr=document.createElement('tr');
    for(const [value, cls] of [[row.id,''], [row.kind,'muted'], [row.text,''],
                               [row.title,'title'], [row.options,'options']]){
      const td=document.createElement('td'); td.className=cls; td.textContent=value;
      tr.appendChild(td);
    }
    body.appendChild(tr); shown++;
  }
  status.textContent='';
  for(const row of statuses){
    const hay=(row.text+' '+row.line).toLowerCase();
    if(filter && !hay.includes(filter)) continue;
    const tr=document.createElement('tr');
    const a=document.createElement('td'); a.textContent=row.line;
    const b=document.createElement('td'); b.textContent=row.text;
    tr.append(a,b); status.appendChild(tr);
  }
  document.getElementById('count').textContent=` · ${shown} 条`;
}
document.getElementById('q').addEventListener('input', e => render(e.target.value.toLowerCase()));
render('');
</script></body></html>"""
    template = template.replace("__ROWS__", data).replace("__STATUS__", status_data)
    (DOCS / "ui-text-review.html").write_text(template)


def main() -> int:
    root = load_html()
    global ALL
    ALL = list(root.elements())
    rows = control_rows(root)
    statuses = js_strings(STATIC / "app.js")
    # Belt and braces: the generated files are for reading, not for execution.
    DOCS.mkdir(exist_ok=True)
    write_markdown(rows, statuses)
    write_csv(rows, statuses)
    write_html(rows, statuses)
    print(f"wrote {len(rows)} controls, {len(statuses)} status strings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
