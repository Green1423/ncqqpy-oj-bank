# 六、出图

> 上级：[oj_bank](README.md) · 上一章：[配置](05-config.md) · 下一章：[代码结构](07-structure.md)

`/oj show <编号>`、`/oj set show <组名>` 的回复是**图片**：题面在 QQ 里读长文很
难受，一张图更接近「题目卡片」。

## 6.1 渲染链路

```text
题目字段（Markdown）
   ↓ markdown.py：全量 HTML 转义 + 结构化
自包含 HTML（无外链；KaTeX 用插件自带的那份）
   ↓ Chromium 第一趟 --dump-dom：页面量高，写进 title（H=1234）
   ↓ Chromium 第二趟 --screenshot --window-size=W,H
PNG（<data_dir>/renders/）
```

两趟是必须的：`--window-size` 就是最终图像尺寸，页面里的 `window.resizeTo()`
在无头模式下被忽略。一趟只能拿到固定高度，不是底部一大片空白就是内容被截断。

## 6.2 图上有什么

| 区块 | 来源 |
|------|------|
| 标题 + 编号 + 徽章（来源 / 难度 / 时限 / 内存 / 测试点数） | `Problem` 元数据 |
| 题目描述 | `statement` |
| 输入格式 / 输出格式 | `input_format` / `output_format` |
| 输入样例 / 输出样例 | `sample_input` / `sample_output`（等宽、保留空白） |
| 注意事项 | `note`（**只剩数据范围**：提示/题解/广告/样例代码在入库时就被过滤掉了） |
| 页脚 | 生成时间 |

空字段整段省略 —— 没有注意事项就不会出现一个空标题。

## 6.3 公式、代码块、表格

- 公式用 **KaTeX**，资源在插件目录 `katex/`（`katex.min.css` + `katex.min.js` +
  fonts）。**不依赖 CDN**：截图时 Chromium 带 `--disable-background-networking`，
  页面里也没有任何外链；
- Markdown 支持：标题、段落、列表、引用、表格、围栏代码块、行内代码、粗斜体、
  删除线、分隔线、行内/块级公式；
- 块级公式**不要求 `$$` 独占一行**：洛谷题面常见 `$$\def…` 开头、`\end{array}$$`
  收尾的写法，收尾后的同一行还有正文也会接着解析；
- 行尾的反斜杠按 Markdown「硬换行」处理（去掉记号，用换行呈现）；
- 题面里的图**不会**下载，显示成 `[图片：alt]`；
- 链接只显示文字、图片显示成 `[图片：alt]` —— 题面里的图不会被下载，也不会
  在截图时去联网（不可信题面不能变成 SSRF 的入口）。

## 6.4 宽度、高度与清理

- `render.width`：图片宽度（默认 760）。太宽在手机上看不方便，700–820 比较合适；
- `render.max_height`：最大高度（默认 8000）。超过会被截断 —— 洛谷题的「注意事项」
  常带十几个语言的样例代码，很容易顶到上限；
- `render.keep_files`：目录里只留最近 N 张，旧的自动删（防止写满磁盘）；
- 图片文件名带毫秒时间戳，同一道题多次出图不会互相覆盖。

## 6.5 失败时

| 现象 | 原因 |
|------|------|
| `❌ 出图失败：找不到 Chromium…` | 没装 Chromium；`apt install chromium-browser`，或在 `render.chromium` 写绝对路径 |
| `❌ 出图失败：出图超时` | 机器太慢或题面超长；调大 `render.timeout` |
| 图片底部被截断 | 调大 `render.max_height`，或把 `render.virtual_time_budget` 调大（字体没就绪就截图会量矮） |
| 公式显示成源码 | `katex/` 资源缺失（发布时漏拷）；看日志里的 `[oj_bank]` 告警 |

出图失败**不影响**判题与题库：`/oj list`、`/oj submit` 照常。
