# VPS 管家（agent）搭建报告 — 2026-11-08

## 先说结论（大白话版）

- **东西全做好了，但最后一步“装到 VPS 上”这趟没装成**，卡在一个安全关卡上，不是技术问题：我这台机器按平台规定**不能直接 SSH 连你的 VPS**（以前所有 VPS 操作都是借你公司电脑 ${PC_NAME} 中转 SSH 的）。而今晚 ${PC_NAME} 是关着的（桥的状态里它不在线），这条路走不通。
- 还有第二道关卡等你拍板：**VPS 的 SSH 主机密钥在 11 月 3 日之后被人整体换过**，新指纹是 `SHA256:0p4zrIddcG2+O2uU1xWdgNQUuAEWor/JansyJxXTyqw`（11 月 5 日就发现并报给你了）。按你的规矩，在你确认“这把新钥匙是正当更换的”之前，谁都不能把新钥匙写进信任名单——哪怕 ${PC_NAME} 开机了，SSH 也会先卡在这一步。
- **能提前干的我全干完了**：VPS 用的 agent 已经写好，并在我这边用一个一模一样的模拟中继**完整跑通验证**（上线、执行命令、读写改文件、批量命令全过，额外延迟约 3 毫秒，可以忽略）。控制器注册的增删脚本也演练过、能精确还原。所以只差临门一脚：**等 ${PC_NAME} 在线 + 你确认上面那个指纹**，我借道过去跑一条安装命令就完事；或者你自己在 VPS 控制台粘贴一条命令也行（安装包在下面，命令见“部署方式”）。
- **云电脑（Box）那部分（第二阶段）软件也备好了**，但有个必须你本人做的动作：Box 的 API 密钥在我这边是**托管的假钥匙占位符**（平台在出口时才换成真钥匙），我这边根本不存在能拷走的真钥匙文件。所以 VPS 上的管家要用 Box，你得把真钥匙（在 box.simular.ai 的 keys 页，最好给 VPS 单独签一把）亲手放进 VPS 的 `/etc/box-steward/key`（权限 600）。放好之后，按需连接的逻辑已经写完：平时不连、不用不占；一有 Box 任务就从 VPS 把会话焐热；连续 60 分钟没人用，自动断开退出。
- **实测数字（今晚测的，方法见下文）**：现在从我这儿单步操作 Box（查一次状态）中位数 **1.16 秒**；把 6 步活打包成一个热会话连续跑，总耗时从约 **6.94 秒降到 2.73 秒，省约 61%**——慢的大头确实在“每步都重新建会话”，不在 Box 本身。等 VPS 管家上线后还会再省掉一段网络绕路，到时候我再测一组对比数给你。

## Phase 1 — VPS 上的 agent：已备好，未部署（访问通道阻塞）

### 为什么没部署成（确切失败点）

| 路径 | 状态 | 依据 |
|---|---|---|
| 本 VM 直连 SSH 到 VPS | 平台出口守卫禁用（探针返回 “Outbound SSH is turned off…”），且站立规矩禁止绕行 | 部署记录 2026-11-01 / 2026-11-04 |
| 经 ${PC_NAME} 中转 SSH | ${PC_NAME} 离线：2026-11-08 ~18:59 CST `museapi GET /status` 返回 HTTP 200、中继健康，但 `online: []` | 本趟实测 |
| 就算 ${PC_NAME} 在线 | VPS 主机密钥 2026-11-03 后被整体更换，新 ED25519 指纹 `SHA256:0p4zrIddcG2+O2uU1xWdgNQUuAEWor/JansyJxXTyqw`，用户尚未确认，known_hosts 未更新，严格校验会拒绝 | 记忆 2026-11-05 |

本趟**没有**尝试任何绕行（不改 known_hosts、不走网关隧道——那条路 2026-11-02 已测并按你的决定废弃删除）。

### 已构建并验证的部署包

位置：[bundle/](sandbox://workspace/vps-agent-build/bundle/)（[说明 README](sandbox://workspace/vps-agent-build/bundle/README.md)）

- **agent 本体** [vps-agent.py](sandbox://workspace/vps-agent-build/bundle/vps-agent.py)：插件内核源码只存在于 ${PC_NAME}，本 VM 只有入口包装，所以按任务预案用 Python 标准库（VPS 自带 3.11，零新依赖）按 DESIGN.md A.4/A.6 协议规格重写了一个最小 agent：hello 注册、15 秒心跳应答、断线退避重连（2s ×1.5 封顶 30s，401/403 直接退出不硬重试）、`cmd`/`batch` 及读/写/改/列表/信息/ glob / grep 文件操作、结果形状与现网完全一致（含 deflate 字段解码；zstd 返回干净的解压错误而不是挂死；7z 操作明确返回不支持）。它装在 VPS 上后**直连本机回环的中继**（ws://127.0.0.1:8471），不走 TLS/nginx/SNI，少一整段网络。
- **注册方式**：新令牌在 VPS 本地生成，明文只留在 VPS 的 `/etc/muse-bridge-agent/token`（0600），中继的 controllers.json 里只登记它的 SHA-256，新端点名 **${VPS_NAME}**；同时把现有 mused 控制器条目的 pcs 从 `["${PC_NAME}"]` 扩为 `["${PC_NAME}","${VPS_NAME}"]`（agent 侧条目永不扩权）。中继按 mtime 热加载该文件——**不用重启中继、不用动 nginx**。
- **隔离与限额**：独立目录 `/opt/muse-bridge-agent/` + `/etc/muse-bridge-agent/`，独立 systemd 单元 `muse-bridge-agent-vps`，`CPUQuota=50%`、`MemoryMax=256M`、`TasksMax=64`、`Nice=5`；不修改任何现有单元与服务（网关、HY2 Gecko、sing-box、gost、9443 vhost、muse-bridge.service 全部原样）。以 root 运行（管家本职就是管这台机器），暴露面由令牌作用域（该令牌只能以 ${VPS_NAME} 名义上线）与上述限额约束，此取舍已在 bundle README 中写明。
- **回滚**：`rollback-on-vps.sh` 一条命令——停用并删除单元 → 从 controllers.json 精确摘除 ${VPS_NAME} 端点与追加的 pcs（先备份）→ 删除两个目录。${PC_NAME} 端点与中继不受影响。

### 本地验证证据（替代性验收，非 VPS 实测）

在**本 VM** 上用现网同款中继源码拉起一个临时中继（127.0.0.1:8479）+ 上述 agent，全程走真实协议（[harness-results.txt](sandbox://workspace/vps-agent-build/test/harness-results.txt)）：

- 状态查询显示 `online: ["${VPS_NAME}"]`，客户端标识 `muse-bridge-vps/1.0`；
- `cmd` 真实执行并返回正确输出（hostname/uname/echo）；
- 文件 write/read 哈希一致、edit 替换成功、list/info 正确、batch 两条命令齐全；
- 往返延迟约 **3 ms**（回环，[harness-latency.txt](sandbox://workspace/vps-agent-build/test/harness-latency.txt)）——agent 本身不引入可感知的协议开销，真实延迟将由网络段决定；
- controllers 合并脚本在仿生产结构的临时文件上演练：install 只新增端点并扩控制器条目，rollback 精确恢复原始三条目（带时间戳备份）。

### 部署方式（通道恢复后，约 5 分钟）

1. 把 `bundle/` 拷到 VPS（如 `/root/vps-agent-bundle/`，经 ${PC_NAME} 的 scp，或用户在 VPS 控制台手动放）；
2. 在 VPS 上以 root 跑 `./install-on-vps.sh`（会自验：回环状态必须列出 ${VPS_NAME} 在线）；
3. 回本 VM 验：`museapi GET /status` 应列出 ${VPS_NAME}，向目标 `${VPS_NAME}` 发一条只读命令（如 `hostname; uname -a`）核对；
4. 随后按同一脚本装 `box-steward`（见 Phase 2），并补测 Phase 3 的 (b)(c)(d)。

## Phase 2 — VPS 上的 Box 按需客户端：软件备好，等用户放钥匙

- **钥匙结论（如实报告）**：本 VM 的客户端（custom.simular-box-key）从不接触真钥匙——它把 `hsurr:*` 占位符放进请求头，由本机 authd/Sentinel 在出口时换成真凭据（代码在 `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`，已读码确认）。VM 上**不存在可拷贝的真钥匙文件**，所以“从这边把钥匙传过去”这条路不存在。**需要用户做的唯一动作**：在 box.simular.ai 的 keys 页给 VPS 单独签一把钥匙（可独立吊销），亲手写进 VPS 的 `/etc/box-steward/key`（单行、0600）。安装脚本的抬头写了完整步骤，且在钥匙缺位时拒绝启动任何东西。
- **已备软件**（[box-steward/boxsteward.py](sandbox://workspace/vps-agent-build/bundle/box-steward/boxsteward.py)，标准库）：按需会话管家——平时无进程、无连接；第一次调用时拉起一个小管家进程，从 VPS 打开一条 MCP 会话焐热，后续调用经本机 unix 套接字复用（支持一次提交多步的 `batch`）；**连续 60 分钟无调用，管家自动断开并退出**；`stop` 可立即拆掉。工具白名单与 VM 侧连接器一致。回滚：`stop` 后删除 `/opt/box-steward`、`/etc/box-steward`、`/run/box-steward` 即可。
- **未做的验证**：因钥匙与 VPS 通道双双缺位，本趟没有从 VPS 发起任何 Box 调用；也没有为凑数去唤醒 Box（正在跑的独立休眠测试未受打扰，本趟对 Box 只有 box_status 级别的轻状态读取，见 Phase 3 方法说明）。

## Phase 3 — 实测数据（2026-11-08 晚，本 VM 出发）

方法：(a) 每样本 = 一个全新 client.py 进程（进程启动 + MCP 初始化 + 1 次 box_status），7 个样本，Box 状态全程 `running`（休眠测试已将其唤醒）；(c) 打包臂 = 同一个进程、同一条热会话内连续 6 次 box_status。原始数据：[phase3-results.json](sandbox://workspace/vps-agent-build/test/phase3-results.json)，脚本：[phase3_measure.py](sandbox://workspace/vps-agent-build/test/phase3_measure.py)。

| 指标 | 结果 |
|---|---|
| (a) Box 单步（直连，逐条起进程） | 中位数 **1.157 s**（样本 0.97–1.54 s，n=7） |
| (c) 打包：一次性初始化 | 0.597 s（只付一次） |
| (c) 打包：热会话内单步 | 中位数 **0.319 s**（n=6） |
| (c) 6 步总耗时：逐条 vs 打包 | ≈6.94 s vs **2.73 s** → **省 ≈4.2 s（≈61%）**，且步数越多省得越多（每步约省 0.84 s 的建会话开销） |
| (b) Box 单步（经 VPS） | **未测**——Phase 1/2 未部署，无此路径 |
| (d) A 通道基线：museapi 状态往返 | 中位数 **0.145 s**（n=5）；在线列表为空 |
| (d) A 通道到 ${VPS_NAME} / ${PC_NAME} 的命令延迟 | **未测**——${VPS_NAME} 未上线、${PC_NAME} 离线。参考值：2026-11-01 实测 A 全链路 echo 到 ${PC_NAME} P50 为 424 ms |

解读（与用户核心诉求对齐）：贵的主要是**每步重建会话**，不是 Box 算力，也不是单步网络。打包/热会话在现有直连路径上就能省约六成；VPS 管家上线后，常驻热会话 + 少一跳，预计单步还能再降，但**以实测为准**——部署完成后我会补齐 (b)(c) 的 VPS 臂和 (d) 的端点延迟，与本表对账。

## 风险与未决事项

1. **主机密钥确认（需要用户）**：指纹 `SHA256:0p4zrIddcG2+O2uU1xWdgNQUuAEWor/JansyJxXTyqw` 是否为正当更换（11-04 晚迁移期间动过 VPS，很可能是那时换的，但必须由用户点头才更新信任）。
2. **部署通道（需要用户）**：${PC_NAME} 开机并手动启动其桥 agent 后，本趟即可借道完成 Phase 1 部署；或用户在 VPS 控制台自跑安装命令。
3. **Box 钥匙（需要用户）**：单独签发一把放进 `/etc/box-steward/key` 后，Phase 2 才能验活。
4. 未验证项如实标注：VPS 上的真实安装、VPS→Box 的真实延迟、双端点下控制器的定向调用，均待通道恢复后补验；本地 harness 通过只证明协议与代码正确，不等于 VPS 已部署。

## Phase 2 software install — 已装好，未激活（2026-11-08 19:12 CST 补记）

Phase 1 上线后（用户于 19:06–19:11 CST 亲手在 VPS 控制台跑完 `install-on-vps.sh` 并确认主机密钥为本人更换；本 VM 经 A 通道核验 `${VPS_NAME}` 在线、命令可达），box-steward 软件本体已通过 A 通道装到 VPS。**本次只装文件、没有启动任何东西、没有对 Simular 发起任何接触**（未调 box_ensure/box_wake/box_status，正在跑的独立休眠观察未受扰）。

### 装了什么、装在哪

| 内容 | 位置 | 说明 |
|---|---|---|
| 管家本体 | `/opt/box-steward/boxsteward.py`（0755） | 用 bundle 自带安装脚本 `install-box-steward.sh` 从 VPS 上的 `/root/bundle/box-steward/` 安装；装后文件 SHA-256 与 bundle 源文件一致（`8692d3e2…cd4ab5`），`py_compile` 通过 |
| 配置目录 | `/etc/box-steward/`（0700） | 已建，**空的**——钥匙文件不存在（见下） |
| systemd 单元 | 无 | bundle 设计即无单元：管家是「首调拉起、闲置 60 分钟自退」的按需进程，不常驻；`systemctl list-unit-files` 确认无 box-steward 单元 |
| 运行态目录 | `/run/box-steward/` | 仅有 `steward.log`（拒绝启动的日志，见下），无 socket、无 pid 文件 |

未改动任何现有服务：安装脚本只新建上述两个目录与一个文件；复查 `muse-bridge`、`muse-bridge-agent-vps` 均为 active。

### 无钥匙拒绝证据（实测）

- 安装前核查：`/etc/box-steward/key` 不存在（`KEY_ABSENT`），本次未创建、未伪造任何钥匙。
- `boxsteward.py status`（纯本地）：`{"keeper_alive": false, "socket": false, "idle_limit_seconds": 3600}`。
- 试调一次 `boxsteward.py call --name box_status`：退出码 1，stderr 为 `error: keeper exited early (rc=2)`；日志 `/run/box-steward/steward.log` 记录 `key file /etc/box-steward/key unreadable — keeper not started`。按代码路径，keeper 是在**读钥匙这一步就退出**、尚未构造 MCP 客户端、更未发起 initialize，所以这次试调对 Simular 是零接触。
- 试调后复查：`keeper_alive: false`，进程表中无 boxsteward 进程，无任何常驻连接。

### 激活只剩一步（用户亲手做）

1. 在 box.simular.ai 的 keys 页给 VPS 单独签一把钥匙（可独立吊销）；
2. 在 VPS 上把钥匙作为单行写进 `/etc/box-steward/key` 并设权限 0600，例如：`install -m 600 /dev/null /etc/box-steward/key` 后用编辑器粘贴那一行；
3. 验活（放钥匙之后再做，本次未做）：`/opt/box-steward/boxsteward.py call --name box_status` —— 首次调用会从 VPS 焐热会话；`status` 看管家状态，闲置 60 分钟自动退出，或 `stop` 立即拆掉。
