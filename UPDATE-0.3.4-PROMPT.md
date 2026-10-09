# 更新到 0.3.4 的提示词（已装旧版的用户用）

把下面整段话，连同新版发布包 `feishu-channel-0.3.4.tar.gz`，一起发给你的 Muse：

---

这是 feishu-channel 0.3.4 发布包，请帮我把已装的飞书桥更新到这一版，并补上群权限。要求：

1. 解包覆盖到原来的 feishu-bridge 项目目录，不要删本机文件（config.json、pending.md、inbox.jsonl、handled.json、approvals.json、egress.env 都不在包里，也不要动）。
2. 在项目根目录运行 ./install.sh（新版会对正在运行的服务做 restart 让新代码真正生效），然后运行 ./feishu.sh doctor，结论必须是 OK。
3. 补群权限：运行 ./feishu.sh onboard --update-permissions，把生成的二维码发给我扫码确认。这一步是给已有应用增量补上 im:chat:readonly（用来列群成员/群里其他机器人），保留原凭证，不要重建应用、不要重新 onboard、不要改机器人名称（这个流程改不了名，如实告诉我即可）。
4. 补权限后重启桥（systemctl restart feishu-bridge），再跑一次 ./feishu.sh doctor 确认 OK，并实测一次：在群里 @ 机器人问它「列出群里其他机器人」，应能答出，不再报 99991672。
5. 最后做关页面验收：我关掉 Muse 网页后只用手机飞书发一条消息，仍应先收到即时确认、再收到实质回复；做不到就不算更新完成。
6. 全程不要打印、复制或在聊天里复述 app_secret 等任何凭证。

---

说明：

- 还没装过的新用户不要用这段，用包里的 `AGENT-INSTALL.md` 全新安装；0.3.4 新装时群权限会在扫码创建时随 addons 一并预填，不需要再补。
- 已装用户也可以不换包、只让 Muse 手工补权限，但手工去开发者后台改权限还要创建并发布新版本才生效，容易漏；推荐按上面用 `--update-permissions` 扫码一次补齐。
