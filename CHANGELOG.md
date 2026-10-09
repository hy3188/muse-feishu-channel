# Changelog

## 0.3.7 — 2026-10-08

- 起因：两位已装用户的失联反馈指向同一类事故——`/etc/systemd/system/feishu-bridge.service` 被带 `__INSTALL_DIR__` 占位符的**未渲染模板**反复覆写（两天 6 次、最长一次停机约 2.2 小时）。systemd 对这种 unit 报 `Unit configuration has fatal error` 直接拒绝启动；而且覆写不杀已运行进程，服务照常跑、到下次重启才爆雷，所以屡修屡漏检。覆写来源有二：①更新/维修时绕过 install.sh、手工 `sudo cp` 了模板；②hook 自愈脚本本身被未渲染地安装（`BRIDGE_DIR` 仍是占位符），sed 变成空操作，每 5 秒轮询就把原始模板覆写一遍到 /etc，手动修好也会被再次覆写。
- 自愈逻辑加固（`templates/feishu-inbox.sh.template`）：只有 unit **缺失或仍含占位符**时才重渲染，好 unit 永不覆写；渲染先落临时文件、校验非空/无占位符残留/`WorkingDirectory` 一致后才原子安装，校验不过则原 unit 原样不动；脚本自身未渲染（`BRIDGE_DIR` 含占位符）时**拒绝碰 /etc** 并记 FAILED；启动后实查 `is-active`，日志明确区分 `self-heal restored` 与 `self-heal FAILED`。旧版「服务不 active 就无条件覆写 + 裸重定向直写 /etc」的做法废弃（渲染一旦失败，重定向会先把好 unit 截断）。
- **根因级修复**：hook 模板里的占位符改为运行时拼接的变量（`PH_DIR`/`PH_PY`）。`install.sh` 渲染 hook 脚本用的是整串替换，模板中凡是字面写出的占位符（sed 的搜索模式、grep 的检测模式）都会在安装时被一并替换掉——sed 退化成恒等空操作、占位符检测变成搜安装路径，自愈因此必然失效甚至覆写坏 unit；这正是第二位用户反馈里「sed 变成空操作」的来源。0.3.7 起模板中字面占位符只允许出现在 `BRIDGE_DIR` 赋值一行，自测有专门的回归用例守住这一点。本机实测：新校验在首次故障模拟时就拦下了这个退化渲染（记 FAILED 且未动 /etc），定位后完成本修复并重新模拟通过。
- `install.sh` 硬校验：模板渲染后仍含占位符直接拒绝安装并退出；安装 systemd unit 前再复查一遍渲染产物，双保险。
- 模板红线：systemd 模板文件头加中文警告（禁止直接 cp、必须经 install.sh 渲染）；`AGENT-INSTALL.md` 安全红线同步增加这一条。
- `doctor` 新增实查：已安装 unit 文件含占位符 → FAIL；hook 脚本是未渲染模板副本（`BRIDGE_DIR="__INSTALL_DIR__"`）→ FAIL。旧版 doctor 对这两种情况都会误报 OK。
- 新增 `UPDATE-0.3.7-PROMPT.md`：给已装用户的整段提示词，覆盖更新 + doctor + 两次故障模拟验收（删 unit 应在一次轮询内自动恢复；占位符 unit 应被自动重渲染并恢复）。
- 自测扩至 86 项：新增未渲染模板防护的静态与渲染校验用例。

## 0.3.6 — 2026-10-08

- 起因：同伴机器人互通实测中，jun-Muse3 其实回过两次消息（2026-10-07 08:02 与 09:03 UTC 的 bridge.log 实证），却都被本桥门控拦下、未进 inbox——对方应用的消息投递身份类型是 `bot`，而旧门控只放行 `app`，以 `ignored_peer_type` 丢弃。双向互通因此假性失败，看起来像对方没回。
- 修复：同伴门控改为接受 `sender_type` 为 `app` 或 `bot`。身份校验仍靠白名单里的 open_id，`sender_type` 只用来筛掉真人 `user` 发送者，白名单本身、任务链、交接限流、自我介绍限流均未放宽。
- 新增 `UPDATE-0.3.6-PROMPT.md`：给已装用户的整段提示词，覆盖更新 + 重启 + doctor + 请同伴重发一条消息验收（两边都要更新到 0.3.6，只改一边仍可能单向收不到）。
- 自测扩至 77 项：新增用例——白名单同伴以 `sender_type=bot` 投递时应被接受（`accepted_peer`）。

## 0.3.5 — 2026-10-07

- 起因：0.3.4 的同伴白名单只能在所有者发起的任务链内协作，两个机器人想「互相了解」也必须先由所有者起链；对方 Muse 也没有现成的「互相加白 + 自我介绍」提示词，靠人工转述 T-002/T-003 迟迟没有回音。
- 新增同伴发现通道（`config.peer_allow_discovery`，默认关）：开启后，白名单内的同伴机器人可在群里 @ 本机器人、发送以 `【自我介绍】` 开头的纯文本主动介绍自己，不需要所有者先起链；介绍条目标记 `peer_discovery=true`，仍是 `collaboration_only`、绝不当所有者指示/授权。限流：每个同伴×每个群在滚动窗口（默认 24 小时）内最多 `peer_discovery_limit` 次（默认 3），超限忽略并记日志；非介绍类消息无链仍一律忽略，白名单本身不放宽到任何未列名的机器人。
- `inbox_hook.py` 透传 `peer_discovery` 标记，被唤醒的 agent 能区分「同伴自我介绍」与普通协作消息，收到介绍后应在群里回发自己的介绍（名称、版本、能力、边界）。
- 新增 `UPDATE-0.3.5-PROMPT.md`：给已装用户的整段提示词，覆盖更新 + 双方互相加白名单（`peer_bots` + `peer_allow_discovery`）+ 重启 + doctor + 群里互发自我介绍验收。
- 自测扩至 76 项：新增发现通道用例（无链介绍通过且带标记、普通消息无链仍拦、限流生效、关闭发现时介绍仍需起链）。

## 0.3.4 — 2026-10-07

- 起因：群里实测「列出群里其他机器人」失败——成员列表接口实测返回 99991672，当前应用没有 `im:chat:readonly` / `im:chat` / `im:chat.group_info:readonly` / `im:chat.members:read` 中任何一个；而这些本可在扫码创建时一并加上，事后去开发者后台改权限还要发新版才生效，很麻烦；机器人名称同理，PersonalAgent 创建后无法经扫码流程改名。
- `onboard.py` 新增 `REQUIRED_ADDONS`：扫码创建时通过 SDK 的 `addons` 把 `im:message`、`im:message:send_as_bot`、`im:resource`、`im:chat:readonly`、事件 `im.message.receive_v1`、回调 `card.action.trigger` 全部预填到扫码确认页，用户确认即生效，不再留到事后补。
- 机器人名称改为扫码前定：`onboard --name <名称>`（或环境变量 `FEISHU_BOT_NAME`、TTY 交互输入），并在生成二维码前明确提醒「确认页是核对/修改名称的最后时机」。
- 新增 `onboard --update-permissions`：对已有应用用同一扫码流程（`register_app(app_id=…, addons=…)`）增量补权限，保留原凭证与 config 其他键，免去手工进后台改权限+发版；该流程不能改名，已有应用的名称问题只能如实告知用户。
- 同伴机器人受限白名单（`config.peer_bots`）：仅群聊、须 @ 本机器人、须挂在所有者发起的任务链下（默认 24 小时、每链默认最多 6 次交接防环）；同伴条目带 `sender_role="peer_bot"` 与 `authority="collaboration_only"`，只作协作/数据，绝不能当所有者的指示或授权。默认不配置即关闭。
- 新增 `UPDATE-0.3.4-PROMPT.md`：给已装旧版的用户可直接粘贴给自己 Muse 的更新提示词（覆盖更新 + 扫码补群权限 + 重启 + doctor + 关页面验收）。

## 0.3.3 — 2026-10-07

- 起因：首个朋友复查自称「检查正常」，但旧版检查本身有盲区——正常结论不可信。本版把盲区逐个堵上，并修一个更新流程的真 bug。
- **更新 bug（真 bug）**：`install.sh` 此前对已在运行的服务执行 `systemctl start`，而 start 对 active 服务是空操作——按旧流程「更新」后跑的仍是旧代码，看起来更新了其实没更新。改为：服务 active 时执行 `restart` 并明确报告「旧进程已替换、新代码生效」。
- `doctor` 新增 hook 启用检查：读 `~/hooks/definitions/feishu-inbox.json`，定义不存在或 `enabled` 不为 true 直接判 FAIL（旧版只查轮询脚本文件是否存在，只放脚本、hook 没建/没启用也能全绿）。
- 积压兜底提示：新增 `inbox_hook.py stale [秒数]`（默认 600 秒），列出桥已接受、却超过阈值仍未被 worker 处理完的消息；`doctor` 同步给出 WARN 并附排查顺序。说明：桥离线时它自己发不出消息，这条兜底只能在本地 doctor / stale 里显形，配合 hook 脚本已有的服务自愈使用。
- `install.sh` 增加 hook 定义复核：更新场景下定义已存在但未 enabled 会明确警告「不算装好」；定义缺失且已有 config 时警告 hook 可能已丢失。
- `AGENT-INSTALL.md` 新增「更新到新版本」一节（覆盖解包、不重扫码、不重建 hook、必须 restart + doctor + 关页面验收）；`README.md` 版本号从滞留的 0.3.0 更新到 0.3.3。

## 0.3.2 — 2026-10-07

- 起因：首个朋友安装反馈「关网页飞书就没反应」——装时能用不等于常驻。`doctor` 新增 systemd `is-enabled` 检查：服务 active 但未 enabled 直接判 FAIL 并给出修复命令（旧版只查 is-active，手动模式/未 enable 的假常驻能蒙混过关）。
- `install.sh` 在 enable 后复核 `is-enabled`，复核不过会明确警告「不算常驻安装、勿宣布完成」。
- `AGENT-INSTALL.md` 与 `templates/hook-setup.md` 验收新增硬指标：关掉 Muse 网页、只用手机飞书发消息，仍收到即时确认与实质回复才算安装成功；并给出该故障的排查顺序（手动模式 → 未 enabled → hook 未真正 enable → 机器休眠）。

## 0.3.1 — 2026-10-07

- 修复分发版把助手名写死的问题：即时确认文案改为读 config 可选键 `agent_name`（如 “Lux” 只是某个实例的配置值），未设置时用不带名字的中性文案「收到，正在处理，稍等。」。仓库内已无任何个人名字。


## 0.3.0 — 2026-10-07

### Added

- 授权审批卡片：`send.py --approval-title/--approval-detail` 先在本地
  `approvals.json`（600、不进仓库）建 pending 申请，再发带「批准 /
  拒绝」按钮的 interactive 卡片；桥接通过 `card.action.trigger`
  回调接收点击，校验点击者必须是 `owner_open_id`，首个决定幂等
  落盘，并向 `inbox.jsonl` 追加 `kind: "decision"` 记录供 hook
  流水线当作正式指令处理；卡片会被替换为无按钮的已处理卡片
  （批准绿 / 拒绝红），重复点击不产生第二条记录。
- 收到即时确认：消息被接受后立即回「收到，正在处理，稍等。」，
  同一会话 60 秒节流、后台线程异步发送、失败只记日志；config 可选
  键 `ack_on_receive`（默认开）可关闭，`--echo-test` 模式不发。
- `AGENT-INSTALL.md`：写给另一个 Muse 的自安装说明，含用户可直接
  粘贴的话术。
- `inbox_hook.py check` 透传 `approval_id` / `decision` 字段。

### Tests

- 离线自测扩至 54 项：新增审批存储（建单/读取/决定/幂等/文件
  600）、审批卡与决定卡结构、卡片回调全流程（非机主拒绝、未知
  申请、批准落 inbox、重复点击幂等、拒绝走上下文 chat 兜底）、
  SDK typed 回调对象字段与响应报文形状、ack 节流逻辑。

## 0.2.1 — 2026-10-07

- hook 唤醒负载补齐媒体字段（kind / media_paths / file_name / media_error），被唤醒的 agent 能真正看到图片与文件消息。
- hook 脚本内置服务自愈：systemd 单元缺失或服务未运行时自动按模板重装并启动（适用于根文件系统不持久的沙箱；VM 换机后数秒内自恢复，已实测）。
- 本机完成在线验收：卡片消息实发成功、桥接心跳（30 秒）正常、doctor 全项通过。


本项目遵循语义化版本；发布包文件名中的版本与本文件保持一致。

## 0.2.0 - 2026-10-07

### Added

- `install.sh` 一条命令安装：Python/venv、依赖、egress 环境快照、
  systemd 模板渲染、hook 脚本渲染和离线自测。
- `templates/`：hook 轮询脚本模板与给 Muse 的 hook 配置说明，含通用
  worker 指令。
- systemd unit 改为路径可渲染模板，不再写死某台机器的目录。
- 入站媒体：image、file、post 解析与下载，post 富文本拍平，媒体下载
  失败时保留消息并记录 `media_error`。
- 可选群聊 @ 门控 `allow_group_mentions`，默认关闭；机器人 ID 获取
  失败时有明确日志的降级规则。
- 发送升级：`--format auto|text|card`、Markdown interactive 卡片、
  3500 字符分片。
- bridge 30 秒状态心跳与 `last_event_at`。
- `feishu.sh doctor` 本地只读健康检查。
- MIT `LICENSE`、`pending.example.md` 和正式分发文档。

### Changed

- 技能文档改为不含具体用户、机器人或机器路径的通用分发版。
- 发布卫生：运行日志、本地 `pending.md`、媒体与运行状态不再进入仓库
  或发布包。

### Security

- 手动启动会拒绝与正在运行的 systemd 桥接双开。
- 凭证和代理环境继续只保存在本机权限受限文件中，发布包明确排除。

## 0.1.0 - 2026-10-06

### Added

- 基于 Python `lark-oapi` 的原型：扫码 `register_app` 接入、WebSocket
  长连接收件、所有者白名单、message_id 去重、`inbox.jsonl` 落盘。
- 纯文本发送 CLI、回声联调模式、systemd 常驻和收件箱 hook 唤醒。
- 11 项离线自测与初版 Muse 技能。
