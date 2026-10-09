# feishu-bridge 0.3.7

让 Muse 通过飞书（Feishu）/Lark 与用户对话的开源桥接项目。

它不是飞书官方插件，也不是 Muse 的原生渠道。它的工作方式是：在
用户自己的飞书租户中创建一个 PersonalAgent 自建应用，本机程序通过
官方 SDK 的 WebSocket 长连接接收消息，写入本地收件箱；Muse 被新消息
唤醒后处理，并通过飞书 OpenAPI 把回复发回原对话。

## 功能

- 扫码接入：飞书 App 扫码即可创建应用并回写凭证，无需公网端口、
  Webhook 地址或手工复制 App Secret。
- 私聊：支持文本、图片、文件和 post 富文本；媒体资源可下载到
  `media/`，下载失败不丢消息。
- 群聊 @：默认关闭；显式开启后，只处理白名单用户明确 @ 本智能体的
  群消息。
- 发送：纯文本或 Markdown interactive 卡片；长内容自动分片。
- 授权审批卡片：需要用户拍板时可发带「批准 / 拒绝」按钮的卡片；
  只有所有者本人的点击有效，决定幂等落盘并进入收件箱流水线。
- 收到即时确认：消息被接受后立即回一条「收到，正在处理」，
  每会话 60 秒节流，避免用户对着沉默等待。
- Always-on：systemd 常驻、自动重启、30 秒状态心跳。`doctor` 会实查
  systemd 是否 enabled、hook 定义是否存在且 enabled，并对「已接受却
  超过 10 分钟没处理完」的积压消息给出兜底警告（也可运行
  `.venv/bin/python inbox_hook.py stale` 单独查看）。
- 自动唤醒：Muse hook 每 5 秒检查收件箱，新消息触发 Muse 处理。
- 授权语义：用户在飞书中的指示、确认和决定，与在 WhatsApp 等已连接
  渠道中同等对待；待决事项由 `pending.md` 管理。
- 安全边界：默认只接受应用所有者本人的消息，按 `message_id` 去重，
  凭证只保存在本机权限为 600 的文件中。

## 架构

```text
用户 ──私聊/群 @──► 飞书智能体
                         │
              WebSocket 长连接（仅出站）
                         ▼
systemd: bridge.py ──白名单/去重/媒体下载──► inbox.jsonl
                         │                         │
                         │                         ▼
                         │              feishu-inbox hook（5 秒轮询）
                         │                         │
                         │                         ▼
                         │                    Muse worker
                         │                    处理/查资料/执行
                         │                         │
                         └──── send.py ◄───────────┘
                          text / markdown card / 审批卡
                                   │
                                   ▼
                              回到原飞书对话
```

桥接本身只负责传输、过滤和状态，不生成智能回复。正常模式下也不要
开启 `--echo-test`；回声只用于最初的链路联调。

## 快速开始

### 1. 获取代码并安装

```bash
git clone <你的仓库地址> feishu-bridge
cd feishu-bridge
./install.sh
```

`install.sh` 会：

1. 检查 Python 3.11+，创建 `.venv` 并安装依赖；
2. 如当前环境有代理/CA 变量，把它们快照到权限 600 的 `egress.env`；
3. 从模板渲染 systemd 服务并 enable；已有 `config.json` 时才 start；
4. 把 hook 轮询脚本渲染到 `~/hooks/scripts/feishu-inbox.sh`；
5. 运行全部离线自测。

### 2. 扫码创建飞书应用

```bash
./feishu.sh onboard
```

按终端提示用飞书 App 扫描二维码并确认。成功后，凭证写入
`config.json`（chmod 600），扫码人被设为唯一白名单用户。

如果扫码流程不适用于你的租户，可在开发者后台手工建应用后使用：

```bash
FEISHU_APP_ID=cli_xxx \
FEISHU_APP_SECRET=应用密钥 \
./feishu.sh onboard --manual
```

不要把真实密钥写进 shell 历史、聊天、日志或仓库。

创建本地待决事项文件：

```bash
cp pending.example.md pending.md
```

### 3. 让 Muse 创建 hook

把 `templates/hook-setup.md` 的内容交给你的 Muse。它会按说明创建
并启用 `feishu-inbox` hook。只放好轮询脚本还不够，hook 未创建/未启用
时，桥接能收消息，但 Muse 不会被自动唤醒。

### 4. 检查并启动

```bash
./feishu.sh doctor
./feishu.sh status
systemctl status feishu-bridge
```

没有 systemd 的环境可手动运行 `./feishu.sh start`，但不要在 systemd
服务已经 active 时再手动启动第二个桥接。

### 5. 更新已有安装

已装过旧版时，不要重新扫码、不要重建 hook：

1. 把新版发布包解压覆盖到原项目目录。`config.json`、`pending.md`、
   `inbox.jsonl`、`handled.json` 等本机文件都不在发布包里，不会被覆盖。
2. 运行 `./install.sh`。自 0.3.3 起，它对已在运行的服务执行
   `restart`（旧版用 `start`，对 active 服务是空操作，等于没更新），
   并复核 systemd enabled 与 hook 定义 enabled 状态。
3. 运行 `./feishu.sh doctor`，结论必须是 OK；再重做一次关页面
   持久性验收（关掉 Muse 网页、只用手机飞书发消息，仍有实质回复）。

## 朋友测试清单

分发给朋友试用时，请按以下五条逐项验收：

1. **私聊文本**：给智能体发一句普通问题。预期：消息进入 inbox，
   Muse 在原私聊中给出实质回复，`handled.json` 中该消息最终为
   processed。
2. **私聊图片/文件**：各发一份。预期：inbox 条目 `kind` 正确，
   `media_paths` 指向 `media/` 中的本地文件；文件还应有 `file_name`。
   若只有 `media_error`，按权限矩阵检查媒体权限。
3. **群聊 @（可选）**：先在 `config.json` 开启
   `allow_group_mentions` 并重启服务；把智能体拉进群，由白名单用户
   @ 它提问。预期：被 @ 时处理，未 @ 时不处理，其他成员的消息不处理。
4. **长 Markdown 回复**：让 Muse 回一份带标题、列表、表格或代码块
   的长答案。预期：以卡片发送，超过 3500 字符时顺序分片，不截断。
5. **授权决定**：先在 `pending.md` 放一个无风险的待决问题，再从飞书
   回复明确决定。预期：Muse 把它作为正式决定处理、更新 pending
   状态，并在处理总结中单独标明。也可用下面的审批卡片再验一次：
   发一张测试审批卡，用户点「批准」后卡片应变为绿色「已批准」状态，
   且 inbox 出现一条 `kind: "decision"` 的记录。

## 授权审批卡片

需要用户明确拍板时，除了在 `pending.md` 里记文字问题，还可以发一张
带按钮的审批卡片，让用户点一下就完成授权：

```bash
./feishu.sh send --chat-id oc_xxx \
  --approval-title "发布周报" \
  --approval-detail "把本周周报发布到团队群，内容见附件。"
# 或按人发送：--to-open-id ou_xxx
```

流程：

1. `send.py` 先在本地 `approvals.json` 创建一条 pending 申请
   （`ap_xxxxxxxx`），再把卡片发到目标会话；命令输出 approval id
   与卡片的 message_id。
2. 卡片上有「批准」（primary）和「拒绝」（danger）两个按钮，按钮
   value 携带 `approval_id` 与 `decision`。
3. 用户点击后，飞书通过 `card.action.trigger` 回调把点击送回
   `bridge.py`。桥接校验点击者 open_id 必须等于 `owner_open_id`：
   非所有者点击只会收到 toast「仅机主可操作」，不落任何记录。
4. 第一次有效点击即落盘决定（`approvals.json` 中状态变为
   approved/rejected，并记录决定时间与点击者），同时向
   `inbox.jsonl` 追加一条 `kind: "decision"` 的记录
   （message_id 为 `decision_<approval_id>`，带 `approval_id` 与
   `decision` 字段），于是现有的 hook 唤醒流水线会把决定当作用户
   的正式指令交给 Muse 处理。
5. 卡片本身会被替换为无按钮的已处理卡片（批准为绿色、拒绝为
   红色），并弹出 toast「已记录：批准/拒绝」。重复点击同一申请是
   幂等的：不会产生第二条 inbox 记录，也不会覆盖第一次决定。

安全要点：

- 决定只认所有者本人的点击；卡片被转发给别人点也不会生效。
- 审批状态只保存在本机 `approvals.json`（chmod 600，不进仓库）。
- 审批卡片承载的是业务授权决定；它与文字授权同等对待，但仍不能
  替代产品系统级批准卡（见「安全说明」）。

## 收到即时确认（ack）

消息被桥接接受后，桥接会立即向原会话发一条
「收到，正在处理，稍等。」，让用户知道消息已到达、Muse 正在
被唤醒处理，而不是对着沉默等待。同一会话 60 秒内最多发一条（内存
节流），发送在后台线程异步进行、失败只记日志。在 `config.json`
设置 `"ack_on_receive": false` 可关闭；`--echo-test` 模式下不发
（回声本身就是响应）。

## 权限矩阵

扫码创建时，`onboard.py` 会通过 SDK 的 `addons` 把下表必需项与
`im:chat:readonly` 一并预填到扫码确认页，确认即生效——不要留到事后
补：事后在开发者后台改权限还要**创建并发布新版本**才生效。已有应用
缺权限时，可用 `./feishu.sh onboard --update-permissions` 扫码一次
增量补齐（保留原凭证）。机器人名称同理要在扫码前定好
（`onboard --name <名称>`）：PersonalAgent 名称无法经扫码流程事后
修改，确认页是最后核对时机。租户策略和手工建应用时仍应按下表核对。

### 必需

| 项目 | 用途 | 缺失症状 |
|---|---|---|
| 机器人能力 | 让应用以智能体身份收发消息 | 通道不可见、无法私聊或发送失败 |
| `im:message` | 接收/读取消息事件的基础能力 | 收不到 `im.message.receive_v1` |
| `im:message:send_as_bot` | 以智能体身份发送回复 | inbox 有消息，但回复发送失败 |
| 事件 `im.message.receive_v1`（长连接） | 把新消息推给 `bridge.py` | 服务在线但 inbox 永远不增长 |
| 已发布的应用版本 | 使权限和事件配置生效 | 后台已配置，线上行为仍像没配置 |

### 可选

| 项目 | 启用功能 | 缺失症状 |
|---|---|---|
| `im:resource` | 下载图片、文件和 post 图片 | 消息能落盘，但 `media_paths` 为空并出现 `media_error` |
| `im:chat:readonly` | 读取会话/群信息、列群成员与群内机器人（扫码创建时已随 addons 预填） | 群信息相关调用失败；成员列表接口返回 99991672，列不出群里其他机器人 |
| `config.allow_group_mentions=true` | 群聊 @ 门控 | 群消息一律忽略（这是默认安全行为） |
| Markdown 卡片发送（沿用发送权限） | 标题、表格、代码块等富文本回复 | 可改用 `--format text` 降级为纯文本 |

## 配置项

`config.json` 由 onboard 生成，不要提交到仓库。

| 键 | 必填 | 说明 |
|---|---:|---|
| `app_id` | 是 | 飞书应用 App ID |
| `app_secret` | 是 | 飞书应用密钥，只保存在本地 |
| `domain` | 是 | `feishu` 或 `lark` |
| `owner_open_id` | 是 | 唯一白名单用户，即扫码/所有者的 open_id |
| `allow_group_mentions` | 否 | 默认 `false`；为 `true` 时启用群聊 @ 门控 |
| `ack_on_receive` | 否 | 默认 `true`；设为 `false` 关闭收到即时确认 |
| `agent_name` | 否 | 本实例 Muse 的名字；设置后即时确认文案为「收到，<名字> 正在处理，稍等。」，未设置时用不带名字的中性文案 |
| `peer_bots` | 否 | 同伴机器人受限白名单（对象 `{open_id: 名称}` 或列表）；默认空=关闭。仅群聊 @、所有者发起的任务链内有效，同伴消息只作协作（`collaboration_only`），不能当所有者指示/授权 |
| `peer_max_handovers` | 否 | 每条同伴任务链最多交接次数，默认 `6` |
| `peer_chain_ttl_seconds` | 否 | 同伴任务链有效期秒数，默认 `86400`（24 小时） |
| `peer_allow_discovery` | 否 | 默认 `false`；为 `true` 时，白名单同伴可不经所有者任务链、以群内 @ + 文本前缀 `【自我介绍】` 主动自我介绍（互相了解），仍只作协作、限流 |
| `peer_discovery_limit` | 否 | 每个同伴在每个群、每个窗口内最多自我介绍次数，默认 `3` |
| `peer_discovery_window_seconds` | 否 | 自我介绍限流窗口秒数，默认 `86400`（24 小时） |

本地运行文件：

| 文件 | 说明 |
|---|---|
| `egress.env` | 代理/CA 环境快照，供 systemd 进程使用；权限 600，不提交 |
| `inbox.jsonl` | 已接受消息，每行一条 JSON |
| `state.json` | bridge 的 message_id 去重状态 |
| `handled.json` | hook 的 claimed/processed 状态 |
| `approvals.json` | 审批申请与决定（权限 600，不提交） |
| `status.json` | bridge 心跳与最近事件时间 `last_event_at` |
| `media/` | 下载的图片和文件 |
| `pending.md` | 本地待决事项，不提交真实内容 |

## 日常命令

```bash
./feishu.sh doctor
./feishu.sh status
./feishu.sh send --chat-id oc_xxx --text "普通消息"
./feishu.sh send --chat-id oc_xxx --text "# 标题" --format auto
./feishu.sh send --to-open-id ou_xxx --text "强制纯文本" --format text
./feishu.sh send --chat-id oc_xxx --approval-title "发布周报" --approval-detail "把本周周报发布到团队群"
systemctl restart feishu-bridge
tail -f bridge.log
tail -n 50 inbox.jsonl
```

`send --format auto` 在检测到 Markdown 或文本超过 1200 字符时使用
卡片；`text` 和 `card` 都会按 3500 字符分片。

## 安全说明

- 默认拒绝所有非所有者消息；不要为了方便把白名单改成“任何人”。
- 群聊功能默认关闭。开启后仍同时要求：群聊、所有者发送、明确 @ 本
  智能体。机器人 open_id 获取失败时的降级规则仅要求 mentions 非空，
  日志会明确警告，生产环境应优先修复 bot 信息接口/权限。
- `app_secret`、代理凭证和访问令牌不得进入日志、issue、聊天或发布包。
- 媒体文件来自用户消息，打开和转发前仍应按不可信文件处理。
- 飞书普通文字可以承载用户的日常指示与授权决定，但**不能替代产品
  的系统级批准卡**。这是平台机制，不是本桥接可以绕过的限制。

## 已知限制与路线图

当前限制：

- 这不是 Muse 原生渠道，不会出现在 Muse App 的原生渠道列表中。
- hook 采用 5 秒轮询唤醒，不是逐字流式输出；回复延迟还包括 Muse
  本身的处理时间。
- 一个实例对应一个飞书应用和一个所有者；多账号/多租户路由尚未实现。
- 入站媒体聚焦 image、file、post；音频、视频、贴纸等类型暂不接受。
- 群聊只做 @ 门控，不做群成员级白名单、话题线程路由或群管理。
- 系统级批准卡无法投递到飞书，也不能用飞书文字替代。

路线图（未承诺时间）：

- 审批申请的超时提醒与待决汇总推送；
- 流式/分段更新回复；
- 多应用、多所有者与按会话路由；
- 更多消息类型与媒体预览；
- 可选的 Webhook 接收模式。

## 与其他项目的关系与致谢

本项目的架构模式参考了公开生态中的成熟做法：OpenClaw 的飞书渠道
（包括 `@openclaw/feishu` 及飞书团队相关插件）证明了“自建应用 +
官方 SDK + WebSocket 长连接”的可行性；NousResearch Hermes 的
`gateway/platforms/feishu.py` 展示了同一模式在 Python 中的实现；官方
`lark-oapi` Python SDK 提供 `register_app` 设备码接入、事件长连接和
消息 API。

这里感谢上述项目和维护者提供的设计参考。本仓库代码按 MIT License
发布；引用第三方代码或文本时应同时遵守其各自许可证。

## 开发与自测

```bash
.venv/bin/python tests/selftest.py
.venv/bin/python -m py_compile bridge.py common.py send.py inbox_hook.py onboard.py
bash -n install.sh feishu.sh templates/feishu-inbox.sh.template
```

自测使用样例事件、假 downloader 和离线构造的卡片载荷，不访问网络，
也不需要真实凭证。

## License

MIT，见 `LICENSE`。
