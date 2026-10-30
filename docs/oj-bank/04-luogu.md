# 四、洛谷抓题

> 上级：[oj_bank](README.md) · 上一章：[判题](03-judge.md) · 下一章：[配置](05-config.md)

`/oj luogu P1001` 抓的是一道题的**题面与样例**，不是它的评测数据 —— 洛谷的
测试数据不在页面上，评分需要自己补测试点。

## 4.1 抓到了什么

| 字段 | 来源 |
|------|------|
| 题名 / 难度 / 题号 | `lentille-context` → `data.problem` |
| 题干 | `content.description`（Markdown，含 `$LaTeX$`） |
| 输入格式 / 输出格式 | `content.formatI` / `content.formatO` |
| 注意事项 | `content.hint`（数据范围、提示、样例代码） |
| 输入样例 / 输出样例 | `samples[0]`（全部样例都会写成测试点） |
| 时限 / 内存 | `limits.time[0]`（毫秒）/ `limits.memory[0]`（**KB，换算成 MB**） |

题号支持 `P1001`、`p1001`、`CF1A`、`AT_abc123_a`，也可以直接给题目链接。

## 4.2 为什么必须过 cookie 挑战

洛谷的题目页对**没有 cookie 的 GET** 会先回 302 到同一地址，并下发
`Set-Cookie: C3VK=...`；带上这个 cookie 再请求一次才拿到真正的页面。这是
WAF 的挑战，不是「题目不存在」，也不是按 IP 封禁（无 cookie 的 HEAD 反而回 200）。

所以抓取用的是带 cookie jar 的同一个会话，遇到 302 最多重试两次。
连续失败时优先怀疑网络出口，而不是题号。

## 4.3 抓完为什么还不能评分

抓来的测试点只有**官方样例**（`kind="sample"`），而评分要的是**覆盖边界、特殊结构与较大规模的测试数据**
（`kind="hack"`，最多 10 组）。补测试点有两条路：

```text
# 让 AI 补：说一句「给 #1 生成 10 组测试点」
（模型调 oj_bank_cases，每组带 input / output / note）
```

或者人工维护：直接改 sqlite 并不推荐（格式有约束），更好的做法是让 AI 生成后
用 `/oj cases 1` 检查每组「用途」。

⚠️ 测试点的 `output` 必须准确：评分是逐字节比对（忽略行尾空格与末尾空行）。
拿不准时，先写一份**正确解**，用它对拍验证测试点。

## 4.4 时限与内存

洛谷给的是**单点**时限/内存，直接存成题目的 `time_limit_ms` / `memory_limit_mb`。
判题时每一组都用这个时限（不按数据规模缩放）。

如果发现「洛谷能过、本地 TLE」，先看是不是 `time_limit_ms` 被导入成了秒
（字段单位是毫秒），再看判题机负载（`judge.max_concurrent=1` 时排队）。

## 4.5 重复抓题

题库按 `luogu:<题号>` 去重：

- 已经抓过 → 回「已经在题库里」，不覆盖你的题目 / 测试点；
- 想按洛谷最新题面覆盖：把 `plugins.oj_bank.luogu.overwrite` 设为 `true`，
  或用工具 `oj_bank_luogu` 时传 `overwrite=true`。

覆盖会**重建官方样例测试点**（`kind="sample"`），你补的测试点保留。
