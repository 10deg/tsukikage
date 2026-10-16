# 在本仓库工作的约定

本文件给自动化编码助手（以及人类贡献者）看：这些是本仓库的既定做法。

## 按改动范围选测试，不要每次都跑全套

**只改排版/样式**（`web/static/style.css` 的间距、对齐、尺寸，或 `index.html` 的结构微调）
时，跑三个 Node 自检就够了：

```bash
node web/test_dom.js && node web/test_page.js && node web/test_projection.js
```

它们读 CSS 与 HTML 做接线检查，并且真正驱动页面——排版改动能破坏的东西它们都覆盖。
**全套 pytest 约 7 分钟，而且不读 CSS**，对这类改动不会多出任何信息。

其余情况按改动范围决定：

| 改动 | 跑什么 |
|---|---|
| `web/static/app.js` 的行为 | 三个 Node 自检（同上） |
| `src/moonlib/`、`web/server.py`、`tools/` | `pytest`；改动小就指定文件或 `-k` |
| 发布前，或动到渲染/数值 | 整套 `./.venv/bin/python -m pytest tests -q` |

## 版本号

`src/moonlib/_version.py` 与 `pyproject.toml` 两处必须一致；网页面板右上角显示的就是它。
