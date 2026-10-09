# 更新到 0.3.7 的提示词（已装旧版的用户用）

把下面整段话，连同新版发布包 `feishu-channel-0.3.7.tar.gz`，一起发给你的 Muse。

---

这是 feishu-channel 0.3.7 发布包，请帮我把已装的飞书桥更新到这一版。这一版修的是已在两台用户机器上真实发生过的失联事故，不用改任何配置、不用重新扫码：

背景：旧版的 systemd unit 文件会被带 `__INSTALL_DIR__` 占位符的未渲染模板反复覆写——来源可能是更新/维修时绕过 install.sh 手工 cp 了模板，也可能是 hook 自愈脚本本身被未渲染地安装（BRIDGE_DIR 仍是占位符），旧自愈逻辑「服务不 active 就无条件重渲染覆写、还用裸重定向直写 /etc」，渲染一旦失效就会每 5 秒把原始模板覆写一遍，手动修好也会被再次覆写。systemd 对占位符 unit 报 `Unit configuration has fatal error` 拒绝启动，而覆写不杀已运行进程，所以总是等下次重启才爆雷、屡修屡漏检，最长一次失联约 2.2 小时。0.3.7 的改法：自愈只在 unit 缺失或仍含占位符时才重渲染、渲染产物先校验再原子安装、脚本自身未渲染时拒绝碰 /etc、启动后实查 is-active 并区分 restored / FAILED 日志；install.sh 渲染后仍含占位符直接拒绝安装；doctor 新增对 unit 占位符与未渲染 hook 脚本的 FAIL 检查。

要求：

1. 解包覆盖到原来的 feishu-bridge 项目目录，不要删本机文件（config.json、pending.md、inbox.jsonl、handled.json、approvals.json、egress.env 都不在包里，也不要动）。
2. 在项目根目录运行 ./install.sh（会重新渲染 systemd unit 与 hook 脚本、restart 服务并跑自测），必须看到 SELFTEST OK；然后运行 ./feishu.sh doctor，结论必须是 OK，且新增的两项检查（unit 已正确渲染、hook 脚本非未渲染副本）都应是 OK。
3. 红线：以后任何时候都不要手工 cp `systemd/feishu-bridge.service.template` 到 /etc，也不要手工 cp `templates/feishu-inbox.sh.template` 到 ~/hooks/scripts/——这两个都是模板，只能由 install.sh 渲染安装。模板文件头已经写了这条警告。
4. 故障模拟验收（两项都要做，做完确认服务恢复 active）：
   a. `sudo systemctl stop feishu-bridge` 并删掉 /etc 下的 unit 文件，观察一次 hook 轮询（约 5 秒）内自愈是否把 unit 正确重渲染、服务是否恢复 active；
   b. `systemctl kill -s TERM feishu-bridge`（或正常 stop 后 start），确认 15 秒左右新进程起来、WS 重连、doctor 仍 OK。
5. 不要改 config.json 里的任何键；这版不需要改配置，也不要放宽 owner / peer_bots 白名单。
6. 最后告诉我结果：版本号、doctor 结论、两次故障模拟的实际恢复时间；做不到的步骤如实说，不要假装完成。

---

说明：

- 还没装过的新用户不要用这段，用包里的 `AGENT-INSTALL.md` 全新安装（新装即带此修复）。
- 若 doctor 报「unit 文件仍含未渲染占位符」FAIL：不要手工改 /etc 下的文件，在项目根目录重跑 ./install.sh 即可修复，然后复查 doctor。
