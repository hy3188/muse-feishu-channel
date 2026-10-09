# AGENT-INSTALL.md — 给 Muse 的自安装说明

这份文件是写给 **Muse（AI 助手）** 的：你的用户把
`feishu-channel` 发布包交给你，请你按下面步骤为他/她装好这个飞书
渠道，并完成验收。不要跳步；每一步都以命令的实际输出为准，不要
凭假设宣布成功。

## 前置条件

- 用户有一个飞书（Feishu）或 Lark 的**组织/租户**账号（个人注册后
  自建的团队也可以），并能在手机上用飞书 App **扫码**确认。
- 当前机器是 Linux + Python 3.11+；有 systemd 时走常驻服务模式，
  没有时走手动模式。
- 用户本人在场：扫码和点审批卡片都只能由用户本人完成。

## 安装步骤

1. **解包**

   把发布包解压到 `~/workspace/feishu-bridge`，进入该目录。下文所有
   命令都在这个项目根目录执行。

2. **一条命令安装**

   ```bash
   ./install.sh
   ```

   它会建 `.venv` 装依赖、把当前代理/CA 环境快照到 `egress.env`
   （权限 600）、渲染并 enable systemd 服务（有 `config.json` 时才
   start）、把 hook 轮询脚本渲染到 `~/hooks/scripts/feishu-inbox.sh`，
   最后跑一遍离线自测。自测必须显示 `SELFTEST OK`，否则先排障，
   不要继续。

3. **创建 hook**

   打开 `templates/hook-setup.md`，用你的 hook 管理能力创建并启用
   hook：id 为 `feishu-inbox`、轮询 5 秒、脚本
   `~/hooks/scripts/feishu-inbox.sh`、worker 指令用该文件给出的全文。
   先 dry-run（无新消息应返回 silent），再 enable。只放好轮询脚本
   不算完成——hook 没建，消息只会躺在收件箱里没人处理。

4. **扫码接入**

   ```bash
   ./feishu.sh onboard
   ```

   把生成的 `qr.png` 展示给用户，请用户用飞书 App 扫码并确认。
   成功后凭证写入 `config.json`（600），扫码人自动成为唯一白名单
   用户。用户没扫之前，不要声称已接入。

   扫码前先和用户定好机器人名称（`./feishu.sh onboard --name <名称>`）：
   名称只在创建确认页预填，PersonalAgent 创建后无法经扫码流程改名；
   所需权限（含列群成员/机器人用的 `im:chat:readonly`）会随 addons
   一并预填到确认页，请用户确认前核对名称与权限。已有应用缺权限时
   不要重建应用，改用 `./feishu.sh onboard --update-permissions`
   扫码增量补齐。

5. **启动与健康检查**

   ```bash
   systemctl start feishu-bridge   # 无 systemd 时：./feishu.sh start
   ./feishu.sh doctor
   ./feishu.sh status
   ```

   `doctor` 的结论必须是 OK。再把仓库的技能文件
   `skill/feishu-channel/SKILL.md` 安装到你的技能目录（若你的环境
   支持），并复制一份待决清单模板：`cp pending.example.md pending.md`。

6. **验收（两项都要做）**

   - 测试文本：`./feishu.sh send --to-open-id <用户 open_id> --text "飞书渠道已接通"`，
     再请用户在飞书回一句话，确认 hook 唤醒的你在原对话给出实质
     回复、`handled.json` 中该消息进入 processed。
   - 测试审批卡片：
     `./feishu.sh send --to-open-id <用户 open_id> --approval-title "安装验收" --approval-detail "这是一张测试审批卡片，点批准即完成验收"`，
     请用户点「批准」。预期：卡片变绿并显示「已批准：安装验收」，
     `inbox.jsonl` 出现 `kind: "decision"` 记录，你被唤醒后在飞书
     回复确认。
   - **关页面持久性验收（硬指标，前两项都过也不许跳过）**：这是
     第一位朋友安装时翻车的那一关——装时能用、关掉网页就没反应。
     做法：(1) 先跑 `systemctl is-enabled feishu-bridge` 与
     `systemctl is-active feishu-bridge`，必须分别为 `enabled` /
     `active`（`doctor` 自 0.3.2 起会把未 enabled 判为 FAIL）；
     (2) 请用户**关掉 Muse 网页、不要重开**，然后只用手机飞书
     再发一条消息；预期在没有任何页面打开的情况下，仍先收到即时
     确认、随后收到实质回复。做不到就不算安装成功，不得宣布完成。
     排查顺序：桥是不是手动模式在跑（`./feishu.sh start` 起的进程
     随会话结束而死）→ systemd 是否 enabled → hook 是否真的已
     enable（只有脚本文件躺在 `~/hooks/scripts/` 不算；自 0.3.3 起
     doctor 会读 `~/hooks/definitions/feishu-inbox.json` 实查 enabled，
     但这一关的端到端实测仍然不许跳过）→ 机器是否休眠/关机。

## 更新到新版本（已装过旧版的用户）

用户已经装过旧版、只是升级时，按下面做，**不要**重走扫码和建 hook：

1. 把新版发布包解压覆盖到原项目目录。本机文件（`config.json`、
   `pending.md`、`inbox.jsonl`、`handled.json`、`approvals.json`）
   都不在发布包里，不会被覆盖，也不要删。
2. 在项目根目录运行 `./install.sh`。0.3.3 的 install.sh 对已在运行
   的服务执行 `restart` 使新代码生效（旧版是 `start`，对 active 的
   服务是空操作——用旧版流程「更新」过的人其实从未真正更新成功，
   必须用新版重跑一次），并复核 systemd enabled 与 hook 定义状态。
3. 运行 `./feishu.sh doctor`，结论必须是 OK；若出现「超过 10 分钟
   未处理」的积压警告，先运行 `.venv/bin/python inbox_hook.py stale`
   看是哪条消息卡住，再决定是否让用户重发。
4. 更新到 0.3.4 时还要给已有应用补群权限：运行
   `./feishu.sh onboard --update-permissions` 让用户扫码一次，增量
   补上 `im:chat:readonly`（保留原凭证、不重建应用、不能改名），
   之后重启桥并复测群里能列出其他机器人。给用户的现成话术见包内
   `UPDATE-0.3.4-PROMPT.md`。
5. 更新到 0.3.5 做机器人互通时：按用户给的对方机器人 open_id 写
   入 `config.peer_bots`，并设 `peer_allow_discovery=true`，重启后
   在群里 @ 对方发一条以 `【自我介绍】` 开头的介绍；对方的介绍同
   样会以 `peer_discovery=true` 的同伴条目进来。现成话术见包内
   `UPDATE-0.3.5-PROMPT.md`。
6. 更新到 0.3.6 时无需改任何 config：这版只修同伴门控——真实同伴
   应用的消息可能以 `sender_type=bot` 投递，旧版只认 `app` 会把白
   名单同伴的消息以 `ignored_peer_type` 拦掉。更新后请同伴在群里
   @ 本机器人重发一条消息验收能进 inbox；互通是双向的，两边都要
   更新到 0.3.6。现成话术见包内 `UPDATE-0.3.6-PROMPT.md`。
7. 更新到 0.3.7 时同样无需改任何 config：这版修未渲染模板反复覆写
   systemd unit 导致的数小时级失联（自愈只在 unit 缺失/含占位符时
   才重渲染、渲染先校验再装、install.sh 占位符硬校验、doctor 新增
   unit/hook 未渲染检查）。更新后照 `UPDATE-0.3.7-PROMPT.md` 做两
   次故障模拟验收（删 unit 应在一次轮询内自动恢复、杀进程应快速
   重连），再跑 doctor 确认全 OK。
8. 重做关页面持久性验收（硬指标，同上）：关掉 Muse 网页、只用
   手机飞书发一条消息，仍收到即时确认与实质回复，才算更新完成。

## 安全红线（不可协商）

- 凭证只存项目根目录 `config.json`；**永远不要打印、引用、复制或
  在聊天里复述 `app_secret`**。状态输出是脱敏的，按原样转述即可。
- `egress.env` 是 Muse 沙箱的代理/CA 环境快照（含代理凭证），和
  `approvals.json`、`inbox.jsonl` 一样只属于本机：不要提交仓库、
  不要发给任何人、不要放进发布包。
- 白名单只认 `owner_open_id` 本人，不要为任何人放宽；群聊
  `allow_group_mentions` 默认关闭，用户没明确要求就不要开。
- 审批卡片只有机主本人的点击有效，但这不改变一条边界：飞书里的
  文字与按钮决定都不能替代 Muse 产品的系统级批准卡，遇到那种
  批准要如实向用户说明。
- 不要在 systemd 服务已 active 时再 `./feishu.sh start` 手动起
  第二个桥接（脚本会拒绝，也不要绕过）。
- **模板文件只能经 install.sh 渲染安装，永远不要手工 cp**：
  `systemd/feishu-bridge.service.template` 不能直接复制到
  `/etc/systemd/system/`，`templates/feishu-inbox.sh.template` 不能
  直接复制到 `~/hooks/scripts/`。未渲染的占位符 unit 会让 systemd
  拒绝启动桥，且覆写不杀已运行进程、等重启才爆雷——0.3.7 之前这在
  用户机器上两天内发生过 6 次、最长失联约 2.2 小时。修 unit/hook
  的唯一正确做法是在项目根目录重跑 `./install.sh`。

## 用户可直接粘贴的话术

用户把发布包交给自己的 Muse 时，可以这样说：

> 这是 feishu-channel 的发布包，请你读包里的 AGENT-INSTALL.md，
> 按里面的步骤帮我装好飞书渠道：先解包跑 install.sh，再按
> templates/hook-setup.md 建好 feishu-inbox hook，然后运行
> ./feishu.sh onboard 把二维码发给我扫码。装完用 doctor 检查，
> 并发一条测试消息和一张测试审批卡片给我验收。凭证不要打印，
> 也不要发到任何地方。
