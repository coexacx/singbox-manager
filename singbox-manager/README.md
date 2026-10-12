# sing-box 整机分流菜单

入口脚本与部署资源均由 GitHub 仓库提供。资源包文件名包含内容摘要，安装时校验完整 SHA256。

在 Debian 服务器上以 root 运行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/coexacx/singbox-manager/main/singbox.sh)
```

菜单 1 安装 sing-box 和 HTTPS、Shadowsocks、WireGuard、SOCKS5 四种模板。已有安装再次选择 1 会更新管理器并补齐新增模板，保留节点资料和当前代理。菜单 2–4 分别编辑 HTTPS、Shadowsocks、WireGuard；菜单 14 编辑 SOCKS5。密码和私钥输入时不回显。

菜单 5–7、15 分别启用 HTTPS、Shadowsocks、WireGuard、SOCKS5。启用前检查必填项和 sing-box 配置，再通过临时本地 SOCKS 监听依次查询 ipinfo.io、ipwho.is 和 api.ipify.org；启用后等待 TUN 接口或透明规则及检测入口就绪，再复检并显示代理出口 IP。单个服务限流、超时或响应无效时会自动切换。等待超时或检测失败时恢复先前服务和配置。WireGuard 的 MTU 范围为 576–9000，未设置时默认 1280。启用时会让当前 SSH 客户端地址和 SSH 服务端口绕开 TUN，以保护管理连接。

SOCKS5 的服务器地址和端口为必填项；用户名、密码需同时填写或同时留空。在菜单 14 的认证项输入 `-` 可清空该项，直接回车保留原值。默认仅代理 TCP，匹配代理规则的 UDP 流量会被拒绝，以便支持回退的应用改用 TCP；只有上游支持 SOCKS5 UDP ASSOCIATE 时才开启 `udp_enabled`。启用检测验证 TCP 连接，不能代表上游支持 UDP。SOCKS5 模板沿用 HTTPS 模板的全部目标站点规则。

## 多节点与自动连接（菜单 17）

将节点集中放入 `/etc/sing-box/manager/nodes.txt`，每个节点前使用独立数字行 `1`、`2`、`3` 分隔；安装时会放入不含真实凭据的格式示例。也可以在导入提示中指定其他文件路径。支持标准 WireGuard `[Interface]` / `[Peer]` 配置、`https://`、`socks5://`、`ss://` 链接、`用户名:密码@地址:端口`（SOCKS5），以及单节点 sing-box JSON / 本管理器节点 JSON。Shadowsocks 支持 SIP002 和旧式完整 Base64 链接，暂不支持插件。

WireGuard 缺少 DNS 时使用 `1.1.1.1`，缺少 MTU 时使用 `1280`；已填写值保留。保留多个 IPv4/IPv6 地址、AllowedIPs、可选预共享密钥和保活时间。每个编号只放一个 Peer；文件中的注释不会作为配置执行。

导入后会转换、校验、测试并查询真实出口 IP，按“中文国家-城市-协议-唯一编号”命名。地理服务限流时会切换服务，仍无法查询时显示“位置待查询”，以后巡检重试；不会把未查询到地理位置的可用节点判死。重复导入相同配置会跳过，失败节点保留待复测。旧版已填写的四种节点资料会自动加入节点库。

子菜单：1 导入，2 查看（密码/私钥隐藏），3 编辑，4 手动连接，5 开启自动连接，6 关闭自动连接，7 立即检测，8 删除，9 改名，10 设置失败删除轮数，0 返回。节点编辑保存后需重新连接才能应用到运行配置；连接失败会恢复先前配置。选择主菜单单协议启用项或关闭代理，会退出自动连接模式。仅关闭自动连接时当前代理保持运行。

自动连接首次检测所有节点，按成功 HTTP 请求的实测耗时选最小者；这不是 ICMP ping 值。连接成功后保持该节点，不因另一节点变快而切换。当前节点死亡、运行进程停止或连接失败时，按延迟次序尝试下一个可用节点；没有可用节点时下一轮继续重试。TUN 和 TProxy、分流和全局模式均使用同一套切换与回滚流程。

`singbox-manager-nodes.timer` 每半小时调用一次巡检，两个独立 HTTP 204 地址用于存活判断；失败重试 3 次。默认连续 3 轮失败才从节点库删除（通常约 90 分钟），成功清零。子菜单 10 可设置为 1 轮（该轮重试 3 次失败就删除），或其他轮数。手动检测不增加删除计数。手动关闭代理不会重新启动代理，但仍继续检测节点库；卸载会清理节点库、导入文件和定时器。节点库文件 `node-store.json` 权限为 0600，不能上传到仓库。

```bash
systemctl status singbox-manager-nodes.timer
journalctl -u singbox-manager-nodes.service -e
```

菜单 8 停用代理。菜单 9 清除本管理器安装的配置、节点资料及其安装的 sing-box 软件包；若安装前已有 sing-box，恢复原配置和服务状态。菜单 10 重新检测当前代理并显示出口 IP。菜单 11 开关 TUN，菜单 12 配置 Telegram Bot 监测，菜单 13 退出。菜单 16 切换分流 / 全局模式。菜单顶部显示 sing-box 版本、TUN 状态、运行协议、流量模式、Telegram 状态和最近检测到的出口 IP。

默认采用分流模式：模板目标站点走代理，其余直连。全局模式将普通 TCP / UDP 流量交给所选代理，并保留 SSH、代理自身建连和 Telegram 告警等必要直连；上游仅支持 TCP 时，UDP 仍会被拒绝。四种协议均支持 TUN 和 TProxy 下的模式切换，切换失败时回滚；停用代理时修改模式会在下次启用时生效。Telegram 通知也会显示当前分流 / 全局模式。

Telegram 配置保存在 `/etc/sing-box/manager/telegram.json`，只有 root 与 sing-box 服务用户可读。个人用户名需要对应的数字 chat_id；若 Bot 已设置 webhook，Telegram 无法使用 getUpdates 自动解析 chat_id，请在菜单中额外填写数字 ID。配置完成后，代理成功启用时发送首条通知，此后 systemd timer 每 30 分钟发送内核运行状态、服务器网卡 IP、代理出口 IP 与可获取的 IP 地理信息。代理检测失败仍会发送异常通知。菜单 8 停止定时通知；菜单 9 删除配置与定时器。

默认开启 TUN。关闭 TUN 后使用 nftables TProxy 透明接管本机 TCP、UDP 和 DNS 流量，仍按所选协议模板分流；SSH 服务端口直连。切换时会复检当前节点，失败则恢复先前配置。安装器会安装 nftables 并创建 sing-box 的 systemd 启停钩子，使透明规则随服务启动和停止。卸载时清除这些规则和钩子。

节点资料仅 root 可读，位置为 `/etc/sing-box/manager/https.json`、`shadowsocks.json`、`wg.json`、`socks5.json`。当前启用的完整配置为 `/etc/sing-box/config.json`。查看状态和日志：

运行中的 sing-box 会在 `127.0.0.1:18081` 提供仅本机可访问的 SOCKS 检测入口；菜单在切换后和手动检测时通过此入口依次查询三个独立的 IP 服务。

```bash
systemctl status sing-box.service
journalctl -u sing-box.service -e
```

默认直连 DNS 为 `1.1.1.1`，可在对应节点资料中修改。WireGuard 默认隧道 DNS 是原模板中的 Surfshark 地址；使用其他提供商时请在菜单 4 修改。分流规则来自原始三个模板，目标站点走选定节点，其他流量直连。程序自带加密 DNS 或隐藏域名时，匹配取决于 IP 规则与协议嗅探。
