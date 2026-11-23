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
    "back-focus": "1.05",
    "parallel": "4",
    "sky-span": "6",
    "sky-label-step": "30"
  }
}
```

载入时只应用文件里出现的字段，缺的保持当前值——所以一份"只改标注"的小文件也能用。

地貌分类的开关是 `class-` 加分类键（`class-basin`、`class-mare`、`class-crater`、
`class-rima` …），键的完整列表见 `moonlib.annotations.FEATURE_CLASSES`，也可以在命令行看：

```bash
./.venv/bin/python tools/preview_annotations.py --list-classes
```

## Clair-Obscur 事件表

内置表在 `src/moonlib/data/clair_obscur.csv`；这里的 `clair_obscur_user.csv` 用来放
自定义或修正的行，列名与内置表相同。网页事件栏的「刷新」会重新读取两份表；同名事件以
用户表为准。
没有锚点、只由余经度决定的条目（如 Cassini's Moon Maiden、Aries' Hoofprint）沿用
参考表写法：`余经度` 填最佳值，`起始太阳高度` / `结束太阳高度` 两列填余经度窗口。
有锚点的条目则按列名填太阳高度窗口。

`default.conf` 在项目根目录，是页面启动时统一应用的默认值；网页上的「存为默认」会覆盖它。
这里 `web/settings/` 下的 `.conf` 仍是可命名的个人设置文件。

根目录 `default.conf` 里的 `"when": "now"` 表示每次打开页面读取电脑当前时间；把它改成具体
日期时间则固定为那个时刻。网页上的「存为默认」会保留 `now`（若本次没有手动改过时间）。
