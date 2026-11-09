# Muse Bridge — 全套源码备份

Muse（云端）远程操作用户自有机器的桥系统：控制器 mused、VPS 中继、PC/VPS 两端 agent、Box 管家、Scheme B 的 MCP 服务、Codex 会话附着通道。本仓库是**灾备备份**：任何一台服务器崩了，照下面路径能把系统重建出来。

> ⚠️ **私有仓库，不要公开。** 代码里含有 VPS 地址、主机名等运维细节。真正的凭证（令牌、钥匙）一律不在仓内，见文末「故意不入库的东西」。

## 目录

| 目录 | 是什么 | 跑在哪 |
|---|---|---|
| `controller/` | mused 控制器守护进程 + museapi CLI + 库（microbatch/compress/pool/tls_resume）+ systemd 单元 + CA 公共证书 | Muse 的 VM |
| `relay/` | 中继 `muse-bridge.py`（含 2026-11-09 上线的调用级拆包器）、systemd 单元、nginx 入口配置、装机脚本、全套测试 | VPS ${VPS_IP} |
| `vps-agent/` | VPS 本机 agent（Python 标准库版）、装机/回滚脚本、controllers 合并工具 | VPS |
| `box-steward/` | Simular Box 按需管家（经 VPS 操控云电脑） | VPS |
| `pc-agent/` | ${PC_NAME} 无头 agent 的入口 `agent.js` + 配置示例（内核 `src/` 是同事插件源码，只存在于 PC 本机，未入库） | ${PC_NAME}（公司 PC） |
| `mcp/` | Scheme B 的 MCP 服务（现仅剩 status，代码保留可复活） | VPS |
| `codex-session/` | Codex 会话附着通道：连接器客户端 + 协议文档（MESSAGE-TEMPLATE v3 等） | Muse VM / PC |
| `docs/` | DESIGN.md 协议规格、部署报告、变更日志、A/B 实测汇总 | — |

## 恢复速查

1. **VPS 全新重建**：先按 `relay/install-on-vps.sh` 与 `relay/nginx-muse-bridge.conf` 装中继（独立 nginx 入口，不要动网关的 9443 vhost）；再按 `vps-agent/` 的装机脚本装本机 agent；Box 管家按 `box-steward/install-box-steward.sh`。controllers.json 需按 `relay/controllers.template.json` 的形状重新生成（令牌重新签发、只存哈希）。
2. **Muse VM 重建**：`controller/` 放回 `~/.local/bin/mused`、`~/.local/bin/museapi`、`~/.local/lib/mused/`；systemd 单元原件放 `~/systemd-units/mused.service` 并软链进 /etc；配置按 `controller/config.example.json` 写 `~/.config/mused/config.json`，令牌放 `~/.config/mused/token`（重新签发），CA 证书放 `controller/certs/` 里那张。
3. **${PC_NAME} agent 重建**：需要 PC 上的同事插件内核 `src/`（不在本仓）+ 本仓 `pc-agent/agent.js` 与配置；由用户手动脚本启动，**不要加任何自启**。
4. 改代码先跑测试：`relay/test_relay.py`、`relay/test_batch_demux.py`、`mcp/test_mcp.py`。

## 变量表（公开版已变量化，部署时替换回真值）

本仓库为公开版，真实地址与标识已全部换成变量。部署/恢复时把下面变量替换成真值（真值只在部署者的私有记录里，不入库）：

| 变量 | 含义 |
|---|---|
| `${VPS_IP}` | VPS 的公网 IP |
| `${VPS_SNI}` | 中继入口的 SNI/虚拟主机名（自签 CA 体系内的名字） |
| `${VPS_NAME}` | 中继里 VPS 这一端的目标名（agent 上报的主机名） |
| `${PC_NAME}` | PC 这一端的目标名/主机名 |
| `${PC_USER}` | PC 上的 Windows 用户名（出现在路径里） |

注：`controller/certs/vps-root-ca.pem` 是已签发的 CA 公共证书，无法改字，其 CN 里保留了 PC 代号；它不含 IP、用户名或任何密钥材料。

## 故意不入库的东西（恢复时重新签发/生成）

- 所有令牌文件：mused 的 token、各 agent 的 token、controllers.json 真表（只存哈希也视为敏感）
- Simular Box 钥匙（VPS 上 `/etc/box-steward/key`）
- CA 私钥与服务器私钥（仓内只有 CA 的**公共证书**；私钥遗失就整套重新签发并给各端换证书）
- 运行时配置真文件（仓内是 `*.example.json` / `*.template.json`）
- 日志、缓存、打包 tar 等产物

## 当前状态（2026-11-09）

- A 方案（mused 直连中继）为日常主路线；Remote Desktop Commander 为 fallback；Codex 工作走 `codex-session/` 的会话附着通道。
- 调用级打包已上线：中继可拆 `{requests:[...]}` 混合清单并发分发（详见 `docs/DESIGN.md` 的 2026-11-09 附录）。
- mused 的 microbatch 保持关闭，靠调用方显式发清单。
