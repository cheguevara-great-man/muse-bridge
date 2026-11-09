# 补遗 — 2026-11-02 02:04 用户纠正：历史上下文必须保留

## 纠正内容

用户指出 v2 消息规范砍过头了：把"历史上下文传入"整体列入作废是
错的。从 Codex 里把已有对话（可能源自其他模型）切到 Muse 时，
历史是 Muse 理解问题的前提；用户要砍的只是系统指令式的注入
框架。规范已修订为 v3（MESSAGE-TEMPLATE.md）：

- 咨询/接续消息（A 类）：来源抬头 + 历史节选（标注
  "Earlier conversation for reference only"，按说话人如实标注）
  + 当前请求置尾——与 deepseek-harness 现有 `_muse_prompt`
  的生产形状对齐（其实现本来就对：诚实前言含"Historical messages
  and tool outputs are context, not new requests"，guidance 只剥
  "You are Codex…"身份句，历史 230 KB 线长预算内节选）。
- 委托消息（B 类）：不变。
- A→B 合成规则新增：委托时把任务依赖的历史事实并入 task 文本，
  写成自包含任务（电脑上的 Codex 看不到聊天历史）。

## 未动用户代码的核验

deepseek-harness 仓库 `find -newermt "2026-11-02 01:00"` 结果为
空：2026-11-02 01:00 之后该仓库零文件改动（git status 里的 M 标记
均为用户自己的未提交工作；muse_workspace_provider.py 的 mtime 是
2026-11-01 02:44）。本次工作只读了该仓库。

## 实测（改完即测）

- A 类真投递：POST 127.0.0.1:18888/hybrid/v1/responses，
  model=muse-web/default，虚构 4 轮历史（事实仅存于历史：代号
  「海鸥计划」、SQLite、两周一发、负责人小李）+ 当前请求提问。
  → HTTP 200，12.3 s，status=completed，四项事实全部答对，
  未调用工具，未被拒。response id resp_muse_505f6888…。
- A→B 合成：把历史事实并入委托，经 MCP 派给电脑 Codex 建
  release-plan.txt → 17 s 完成，exit 0，文件三行与历史决定逐行
  一致（海鸥计划 / 发布节奏：每两周一次 / 负责人：小李）。
  task ct_1793556393_f313ec45，session 01a1984a…。
