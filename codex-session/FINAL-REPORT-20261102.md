# 夜间工作报告 — 2026-11-02 01:38 委托（两项任务）

## 任务一：Codex → Muse → MCP → 电脑上的 Codex（已完成并实测）

### 做成了什么

新链路按你定的三段式落地，第三段（执行后端）已换成电脑上的 Codex：

```
你的 Codex 会话 →(browser-ai-bridge，已有)→ Muse 对话
  → MCP 工具 codex_task_start → VPS muse-mcp → 中继 → 桥 agent
  → codex-tasks\runner.py → 本机 codex.exe exec（workspace-write 沙箱）
```

- **MCP 服务**（VPS `muse-mcp.service`，01:09 时按你的决定停用，现已按
  本次委托重新启用）：工具面整体替换为 5 个——`status`、
  `codex_task_start / status / result / cancel`。旧的裸 shell/文件工具
  已从服务里删除。备份：`/opt/muse-mcp/muse-mcp.py.bak-20261102`，
  回滚 = 恢复备份或 `systemctl disable --now muse-mcp`。
- **电脑侧运行器** `C:\Users\${PC_USER}\.muse-bridge\codex-tasks\runner.py`：
  每个任务一个 detached 监督进程 + 一个任务目录（prompt/events/
  last_message/exit/meta 齐全）；直接启动 npm 包里的原生 codex.exe
  （不能经 cmd.exe 垫片，会随控制台父进程一起死——实测踩过）；
  新任务 `-s workspace-write`，续接用 `codex exec resume <session>`
  （0.142 的 resume 不接受 `-s`，已按实测修正）；同一工作目录只许
  一个在跑的任务；session_id 与 token 用量从 `--json` 事件流解析。
- **研究吸收**：子代理只读通读了 deepseek-harness 的 MUSE_WEB /
  MUSE_PC_RPC / mailbox / 会话连续性实现，报告在同目录 STUDY.md
  （618 行，带文件行号）。结论是新流程可删掉旧脚手架的几乎全部
  （见下），保留的硬教训都已落进实现：异步 task-id 形状（分钟级
  任务 + 不重放原则决定了它必须异步）、结果只走工具回传、
  出错不盲目重派。

### 实测证据

| 测试 | 结果 |
|---|---|
| 运行器直测：建 hello.txt | 23 s 完成，exit 0，内容精确 |
| 续接同一 Codex 会话追加一行 | 同一 session_id，exit 0，成功 |
| 全链路 MCP：start→status→result 建 summary.txt | 21 s 完成，session `01a1983a…`，用量 input 23039 / output 306，文件内容与要求逐项相符 |

### 新消息模板（已定稿：同目录 MESSAGE-TEMPLATE.md）

旧提示词整套作废。新消息 = 一行如实来源抬头
`[Relayed from my Codex session on ${PC_NAME}, at my direction]`
+ 任务原文（一字不改）+ 可选参数块（workdir / model / session /
constraints）。连续性由对话上下文和 Codex 会话 resume 承载，不再
转发历史、不再有工具目录/邮箱协议/伪装系统指令。

## 任务二：网络优化（已完成，可回滚）

### 先说物理地板（没有动、也动不了的部分）

- 电脑↔VPS RTT ≈167 ms 决定了小请求的下限；四条路径热态 p50
  今晚复测全部在 367–383 ms（新进程口径）/ 169 ms（连接复用口径），
  已在地板上。唯一数量级杠杆仍是换更近的 VPS，你没授权，未动。
- 生产 H2（18889，原进程未重启）与 9443 站点全程未碰。

### 找到的真问题与修复：HY2 冷启动爬坡

- 根因：客户端配置 `lazy: true`（按需才建 QUIC 会话）+ 包装层从不
  主动发流量探活（`/ready` 只在被问时才测，缓存 60 s）。闲置后第一
  笔大流量要现付会话建立 + BBR 爬坡——三晚数据里的 4.5 / 12 / 17
  Mbps 级灾难值都是这么来的。
- 修复（隔离副本先行，对照基准后才上生产）：包装层增加
  ① 每 45 s 强制穿隧道探活保活；② 启动 3 s 后先热身一发；
  ③ 普通 HTTP 转发改 keep-alive 连接池（原为每请求新建连接）；
  ④ 端口参数化（仅为隔离测试，生产调用方式不变）。其余守护规则
  （CONNECT 白名单、凭据拒绝、失败不重放等）逐字保留。
- 对照基准（BULK×8 / CONC×6 / WARM×20，四路轮转）：

  | 路径 | 批量均值 | 批量最差 | 批量序列形态 |
  |---|---|---|---|
  | 生产包装（改前） | 32.6 Mbps | **17.0** | 17→39 逐轮爬坡 |
  | 生产裸客户端 | 30.5 Mbps | **4.8** | 4.8→39 逐轮爬坡 |
  | 优化包装（隔离） | 36.3 Mbps | **35.2** | 第一轮起全程平坦 |
  | 优化侧裸客户端 | 36.8 Mbps | **34.0** | 平坦（被包装的探活顺带保活） |

  稳态上限四路一致（≈36–38 Mbps）；并发与小请求中位数无变化
  （本已在地板）。优化版赢的正是全部最差情形。
- 已上生产：`tools\hy2-local.cjs` 已替换（原文件备份
  `hy2-local.cjs.bak-20261102`，回滚 = 拷回 + 重启这一对进程）；
  用与原先完全相同的命令行重启包装+客户端。验证：日志带
  `variant:opt-v1`；重启后第一笔 10 MiB 下载即 2.25 s（≈37 Mbps，
  无爬坡），连续三笔 2.25/2.26/2.31 s；`/ready` 正常。
- 隔离测试实例（18893/18894）已关闭，其状态目录（含配置副本）
  已删除。

### 其余检查

- 18889 /ready：ok（healthySessions 5，未受影响）。
- Muse 通道 A（mused）：存活；延迟在物理地板（回声 p50 ≈358 ms）。
- 最终监听面核对：8000/8008/18888（你的 bridge）、18889（H2）、
  18890/18891（HY2 优化版）齐全，无多余残留。

## 遗留说明

- 每次委托任务在 PC 上 Codex 的 token 用量不小（简单任务约 23k
  input，其中约一半 cached）——这是 Codex 脚手架自身的上下文
  成本，与链路无关，status/result 里每次都会如实带回。
- MCP 服务的 OAuth、连接器、守护进程均未改动，仅工具面替换；
  CONNECTOR.md 已同步更新。
