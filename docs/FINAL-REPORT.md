# Muse Bridge 迁移部署 — 完成报告（2026-11-01)

## 交付物总览

| 组件 | 位置 | 形态 |
|---|---|---|
| VPS 中继 relay | VPS `/opt/muse-bridge/muse-bridge.py`，systemd `muse-bridge`，127.0.0.1:8471 | 按技术书规格重写的 Python 实现（原源码不在交付包内），940 行，本地 24 项合同测试通过 |
| 公网入口 | VPS nginx 443，SNI `${VPS_SNI}`，独立 vhost + 私有 CA | 未动 9443 browser-gateway 与任何现有服务；其他 SNI 一律 444 |
| PC 执行端 agent | ${PC_NAME} `C:\Users\${PC_USER}\.muse-bridge\agent\`，计划任务 `Muse Bridge Agent`（登录自启，用户级） | 插件 1.2.12 内核原样复用 + 新写的无头入口；shellPool 关（Git Bash 语义），工作区 `D:\software\AI_Coding\codex` |
| 方案 A 客户端 | 本 VM `~/.local/bin/{mused,museapi,musepc,musepcd}` + `~/.local/lib/mused/` | 上-boarding 包原文件，逐个 SHA-256 校验后安装；无 systemd user bus，用 setsid/nohup，musepc 自愈 |
| 方案 B MCP 服务 | VPS `/opt/muse-mcp/muse-mcp.py`，systemd `muse-mcp`，127.0.0.1:8472，nginx 同 vhost 五条 location | Streamable HTTP MCP + 完整 OAuth 2.0（动态注册、授权码+PKCE、刷新轮换、所有者口令批准），27 项本地测试通过 |
| 方案 B 连接器 | 本 VM `~/workspace/custom-connectors/custom.muse-bridge/` | 自定义连接器 + 本地客户端；自管 OAuth（原因见下） |

身份材料全部新生成、互不复用：私有根 CA（VPS 保管私钥）、服务器证书（SAN 含名称/通配/IP/nip.io）、三个桥 token（mused 侧、agent 侧、MCP 服务侧，controllers.json 只存哈希、按 muse/pcs 隔离权限）。

## 验证结果（均为真实执行）

- 方案 A：museapi GET /status → ${PC_NAME} online；museapi /cmd、musepc 命令在 ${PC_NAME} Git Bash 真实执行并返回。
- 方案 B：VPS 回环完整 OAuth 流程（register 201 → authorize 302 → token 200）后，tools/call 的 run_command 在 ${PC_NAME} 真实执行返回；公网发现文档与 /mcp 401 门槛经 PC 与 VM 两侧验证。
- 连接器客户端：首次实测全通 — list-tools、status、run_command 均经连接器在 ${PC_NAME} 真实执行（完整自管 OAuth 流程在一条保活连接上 0.6 s 内完成）。

## A/B 性能（2026-11-01 夜，详表见 ab/results_summary.md）

- 命令 echo P50：A 全链路（本 VM 出发）424 ms ≈ 技术书原系统基线 420 ms；B 公网全程（本 VM 出发，经连接器）361 ms，快约 15%；B 服务端段（VPS 回环）280 ms。A 的额外开销在 mused 池与逐调用进程，B 的单连接保活占优。
- 文件 10 KiB 写/读 P50：A 345/305 ms；B 段内 214/170 ms。
- 并发：A 25 并发 15.4 req/s、B 段内 21.4 req/s，均零失败（原系统基线：HTTPS 链 11.6 req/s @128 并发）。
- 长命令：20 s 命令全程保持并正确返回（20.7 s）。
- 恢复：agent 进程重启后 6.7 s 重新上线；mused 被运行时重启杀掉后，musepc 下一次调用 13 s 内自愈拉起。
- 稳定性：全部探针零失败。后台任务/断线重复执行：中继按请求 id 配对结果、断线快速失败（fail-fast 合同测试覆盖）；mused 侧带 idem_key 去重（微批处理保持关闭，与中继 /batch 语义不兼容）。

结论：两条链路性能同量级，B 的服务端开销更低且是标准 MCP/OAuth 形态，适合长期用连接器直连；A 的优势是现成的 mused 池化与自愈，适合脚本化批量调用。建议日常以 B 为主、A 为备。

## 与原计划的偏差（均已当场判断并记录）

1. Windows 执行端做成独立无头 agent 而非 VS Code 插件（用户拍板）。
2. 中继为按规格重写（原源码缺失），非搬运。
3. 桥 token 拆成三份分侧保管（原设计共用一份），token 明文从未进对话。
4. agent 以 SNI 名称连接并在进程内解析到 VPS IP（用 IP 直连会被 nginx 444；改 hosts 需管理员权限，故进程内解决）。
5. 方案 B 连接器未走 Muse 托管 OAuth：私有 .local 名称 + 私有 CA 无法被托管同意页解析/信任。改为连接器在本 VM 内自管 OAuth，所有者批准口令只存本 VM（600），VPS 只存其哈希。若今后想要托管流程，两条路：给 VPS 配公有域名+受信证书（现无域名，网关证书仅 IP SAN），或把桥 CA 装进你的浏览器信任库（certutil -user 免管理员，但属安全敏感操作，留给你决定）。
6. 本 VM 无 systemd user bus，mused 以 nohup 方式运行，运行时重启后靠 musepc 自愈（已实测）。

## 今晚踩到的环境特性

- 对这台 VPS 的新建连接（${PC_NAME} 的 SSH、本 VM 经代理的新 TLS）会在高连接量后成波次被黑洞：首包挂起、已建立的持久连接（mused 池、agent WSS）不受影响，静默 10–30 分钟后自愈。对照实验证明本 VM 出口代理本身健康（连其他主机 TLS 0.12 s）。运维含义：保持连接常温、批量操作、别用新建连接风暴打它。

## 回滚

- B：disable muse-mcp + 还原 nginx vhost 备份 + controllers.json 中 MCP 条目 enabled:false。
- 中继：disable muse-bridge + 删除 443 vhost 文件 + reload nginx。
- PC：禁用/删除计划任务 Muse Bridge Agent，删 C:\Users\${PC_USER}\.muse-bridge 与 .Muse\token。
- 本 VM：停 mused/musepcd，删 ~/.local/bin/muse* 与 ~/.config/mused。
以上互不影响，且不触碰 browser-gateway、muse-tool-relay、Codex Bridge、Desktop Commander。
