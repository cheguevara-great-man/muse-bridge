# pc-agent（${PC_NAME} 无头 agent 入口）

- `agent.js`：2026-11-01 为 ${PC_NAME} 组装的无头入口，替代与 VS Code 绑定的启动文件；从 `C:\Users\${PC_USER}\.muse-bridge\config.json` 读设置、从 `C:\Users\${PC_USER}\.muse\token` 读令牌，日志写 `~\.muse-bridge\agent.log`。
- `package.json`：依赖清单（仅 `ws`）。
- `config.example.json`：配置示例（只有路径与开关，无凭证）。

**内核不在本仓**：`src/muse`、`src/shell`、`src/utils` 是同事插件（muse-bridge 1.2.12）的净化源码，原样复用、未改动，只存在于 ${PC_NAME} 的 `C:\Users\${PC_USER}\.muse-bridge\agent\` 与同事项目目录 `D:\software\AI_Coding\codex\muse-bridge`。重建 agent 需要从那两处之一取回内核，再配上本目录的入口。

启动方式：用户手动双击桌面脚本「启动网络与AI工具」。**禁止**给这台公司 PC 添加任何自启/持久化（登录任务曾有，已由用户禁用）。
