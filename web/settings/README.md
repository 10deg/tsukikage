# 网页设置文件目录

网页右上「设置文件」一栏把**页面上所有参数**导出成这里的一份 `.conf`，也可以在这里
放别人给的设置文件，再从网页上选一份加载。

- 文件就是 JSON（UTF-8，缩进 2 空格），`settings` 字段里是**控件的 id → 值**：
  文本框与下拉框存字符串，复选框存 `true`/`false`。
- 文件名由网页上的「文件名」决定；服务端只保留字母、数字、中文、空格与 ` ._-()[]（）【】`，
  路径分隔符一律丢弃，所以名字不可能写到这个目录之外。
- 用命令行启动时可以用 `--settings-dir` 指向别处：

  ```bash
  ./.venv/bin/python web/server.py --port 8765 --settings-dir ~/moon-settings
  ```

一份例子（节选）：

```json
{
  "schema": "moonlib-web-settings/1",
  "saved_utc": "2026-10-11T05:20:11+00:00",
  "settings": {
    "when": "2026-09-26T20:00",
    "tz": "8",
    "lat": "30",
    "lon": "120",
    "out-w": "1600",
    "annotate": true,
    "label-language": "zh",
    "label-density": "dense",
    "label-font": "18",
    "class-basin": true,
    "class-rima": false,
    "label-shadow": false,
    "disk-px": "3000",
    "disk-format": "png",
    "image-rotation": "0",
    "image-mirror": "none",
    "noise-poisson": "0.35",
    "noise-gaussian": "0.2",
    "back-focus": "1.05"
  }
}
```

载入时只应用文件里出现的字段，缺的保持当前值——所以一份"只改标注"的小文件也能用。

地貌分类的开关是 `class-` 加分类键（`class-basin`、`class-mare`、`class-crater`、
`class-rima` …），键的完整列表见 `moonlib.annotations.FEATURE_CLASSES`，也可以在命令行看：

```bash
./.venv/bin/python tools/preview_annotations.py --list-classes
```
