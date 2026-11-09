# Codex–Muse 项目全量变更清单（2026-11-01 03:43 委托 → 2026-11-02 11:23 现行状态）

范围：从用户最初委托部署 Muse Bridge 起，Muse 在本项目中做过的
全部新建与修改。按阶段列出，每项标注现行状态。未列出的即未动过；
deepseek-harness 仓库全程只读（以文件修改时间核验，零写入）。

## 阶段一：Remote Desktop Commander 连接器（2026-11-01 上午）

- 新建 VM 侧自定义连接器 `~/workspace/custom-connectors/custom.remote-desktop-commander/`
（CONNECTOR.md + bin/client.py），OAuth 接入 Desktop Commander 云端，
作为最初进入 ${PC_NAME} 的通道。全天按需扩过其工具白名单（读/进程 →
后加写文件类）。现为备用通道（有月度额度）。

## 阶段二：VPS 上的身份与入口（2026-11-01）

- 新建私有根 CA：VPS `/root/muse-bridge-ca/`（私钥从未离开 VPS）；
服务器证书（CN `${VPS_SNI}`）置于 `/etc/muse-bridge/tls/`。
- 新建 `/etc/muse-bridge/controllers.json`（三份令牌的哈希：控制器 /
agent / MCP 服务；明文分别只存于 VM mused 配置、PC 的.muse\token、
VPS 的 /etc/muse-bridge/mcp-bridge-token）。
- nginx 新增独立 443 vhost（SNI `${VPS_SNI}`）+ 默认 444 兜底块；
原有 9443 vhost 未动。

## 阶段三：中继服务（2026-11-01）

- 新写 Python 中继（约 940 行，母本 `~/workspace/muse-bridge-deploy/relay/muse-bridge.py`），
部署为 VPS systemd `muse-bridge.service`（`/opt/muse-bridge/`），
仅监听 127.0.0.1:8471，经上述 nginx 入口对外。

## 阶段四：方案 A（2026-11-01）

- VM：装入同事工具链 mused/museapi/musepc/musepcd（`~/.local/bin` +
`~/.local/lib/mused`，配置在 `~/.config/mused/`，microbatch/压缩关闭）；
新写 C 语言快速垫片 `~/.local/bin/muse-a`（源码在部署目录 shims/）。
- PC：新建独立 Node agent 于 `C:\Users\${PC_USER}\.muse-bridge\agent\`
（同事插件核心代码原样 + 新写 agent.js（含 SNI/DNS 修正）+ config.json），
以当前用户登录计划任务 `Muse Bridge Agent` 运行。
- CA 副本分发至 VM 与 PC 的.muse-bridge 目录。
- 状态变更（2026-11-02，用户经其 Codex 执行，非 Muse 变更）：上述
  登录计划任务已禁用（原任务有备份），agent 改由用户桌面脚本
  "启动网络与AI工具"手动启动（防重复启动）；当时运行中的 agent
  进程未中断。重启后 agent 不自启，需用户双击该脚本。

## 阶段五：方案 B 初版（2026-11-01 部署 → 同日晚用户决定停用）

- 新写 MCP 服务（含完整 OAuth 授权服务器）`muse-mcp.py`，部署为 VPS
systemd `muse-mcp.service`（`/opt/muse-mcp/`，127.0.0.1:8472），
经 443 vhost 的 /mcp 等 location 暴露；当时的工具面是裸 shell/文件六件套。
- VM 新建连接器 `custom.muse-bridge`（client.py + 自适应连接池守护
daemon.py + C 垫片 muse-b + OAuth 客户端状态 `~/.config/muse-mcp-client/`）。
- 2026-11-01 晚用户决定停用：服务 stop+disable（代码与状态保留，未删）。

## 阶段六：性能测量（2026-11-01 晚）

- H2/HY2 多轮对照基准（脚本与原始数据在部署目录 tools/ 与 ab/）。
- 唯一系统改动：终止隔离的备份测试实例 18892（经用户当场批准）；
生产 H2（18889）进程与脚本未动。

## 阶段七：Codex 链路第一版（2026-11-02 01:38 委托）

- PC 新建 `C:\Users\${PC_USER}\.muse-bridge\codex-tasks\runner.py`
（以 codex exec 派任务的运行器）+ 其 workspace 与测试任务目录。
- VPS 的 muse-mcp 重新启用，工具面换成 codex_task_* 五件套
（01:46 部署；该版本备份为 `.bak-codextools-20261102`）。
- VM 连接器白名单与 CONNECTOR.md 同步更新；产出 STUDY.md（只读研究）、
MESSAGE-TEMPLATE.md（消息规范文档）、FINAL-REPORT-20261102.md。

## 阶段八：HY2 包装优化（2026-11-02 凌晨）

- `browser-gateway-main\tools\hy2-local.cjs` 替换为 opt-v1（在用户原
源码上加：45 秒穿隧道探活保活、启动热身、HTTP 转发连接池复用、
端口可传参；守护规则逐字保留），原文件备份为同目录
`hy2-local.cjs.bak-20261102`；生产进程对以相同命令行重启一次。
- 隔离测试实例（18893/18894 及其状态目录）测后已删除。
- 补记（2026-11-02 12:46，用户指示对齐 H2 的业务闲置规则）：生产已
  升级为 opt-v2——探活只在最近一小时有真实流量时进行（探活不算使用），
  满一小时无真实流量即停探活、隧道自然休眠，监听与客户端进程保持
  运行；隔离验证（5 秒探活/20 秒闲置限）与生产验证均通过（10 MB
  1.81 秒）。回滚链：`.bak-20261102` 为优化前原版，
  `.bak-20261102-opt1` 为 opt-v1。

## 阶段九：执行模型纠正与重建（2026-11-02 上午，用户纠正后）

- 撤下 codex_task_*：VPS 服务已重新部署为仅 `status`（10:47 生效并
验证 list-tools）；runner 不再被任何组件调用（文件留档于 PC）。
- 连接器 client.py 新增本地工具 `pc_tools` / `pc_call`：原样加载用户
已安装的 push 技能（codex_push_persistent_20261021_v2）的 exchange
客户端，把单件工具调用送进活着的 Codex 会话同步执行；白名单收紧
为仅 `status` 走 VPS 服务。CONNECTOR.md 已同步改写。
- 消息规范修至 v3（历史恢复为"标注参考 + 当前请求置尾"）；补遗报告
ADDENDUM-20261102-history.md；记忆文件多处按纠正更新。
- 只读验收：用户 benchmark 隔离验收跑通一轮（测试会话 + 3 个 run
记录留在 v2 技能目录、PC Temp 两个测试 json——均为测试产物）。

## 从未动过的（点名）

- deepseek-harness 全部代码与文档（扩展、native-host、fanvpn_bridge、
_muse_prompt / _muse_context、docs）；
- 同事的 muse-bridge 源码目录、用户的 Codex 配置 config.toml；
- 生产 H2 脚本与进程、9443 vhost、VPS 上 browser-gateway / gost /
sing-box / Hysteria2 服务端、muse-tool-relay（仅读过其 /health）；
- VPS 的 sshd / 防火墙 / fail2ban 配置；PC 的 Falcon EDR 与公司策略。
