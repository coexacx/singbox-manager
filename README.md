# sing-box 整机代理管理器

Debian / systemd 的交互菜单，支持 HTTPS、Shadowsocks、WireGuard、SOCKS5，以及分流 / 全局模式、TUN / TProxy 切换和 Telegram 状态通知。

## 安装与更新

在目标服务器以 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/coexacx/singbox-manager/main/singbox.sh)
```

首次运行选择 **1** 安装，然后填写节点并启用。更新时重新执行同一条命令，再选择 **1**；会保留现有节点资料和运行中的代理。

管理器对自身 Python 进程统一使用 UTF-8，兼容 SSH 会话中的 `latin-1` / ASCII 编码环境；无需修改系统 locale。遇到旧版菜单 17 的 `UnicodeEncodeError` 时，按上述步骤更新即可。

管理脚本与四种模板的资源包均从本 GitHub 仓库下载，资源包使用 SHA256 校验。系统基础依赖通过 Debian APT 安装，sing-box 内核沿用 SagerNet 官方 APT 源。

菜单功能、节点字段和运行机制详见 [使用说明](singbox-manager/README.md)。仓库只包含空白节点模板；节点资料和 Telegram 配置保存在部署服务器的 `/etc/sing-box/manager/` 中。

## 多节点管理

选择 **17** 进入多节点菜单。将配置放入 `/etc/sing-box/manager/nodes.txt`，每个节点前单独写一行数字编号；参见 [编号格式示例](singbox-manager/nodes.example.txt)。支持 WireGuard `.conf`、HTTPS/SOCKS5/SS 链接及单节点 JSON，导入后测试出口并按中文地理位置命名。WireGuard 缺少 DNS/MTU 时默认使用 `1.1.1.1` / `1280`。

可查看、编辑、改名、删除或选择连接节点，也可开启自动连接：先选 HTTP 实测延迟最低的可用节点；连接正常时保持当前节点，只有故障时才切换。每 30 分钟检测全部节点，每轮失败重试 3 次；默认连续 3 轮失败后删除，成功会清零。子菜单 10 可设为 1 轮删除。手动停止代理会关闭自动连接，节点巡检继续运行。

## 主菜单与节点库联动

- 菜单 **2/3/4/14** 按协议选择并编辑节点；**5/6/7/15** 按协议选择连接节点，统一维护节点库和 Telegram 事件。
- 当前节点的修改保存为“待应用”。手动连接并通过复检后才生效；失败时保留原配置、原连接和自动模式。自动巡检、故障切换及 TUN/流量模式切换继续使用已生效配置。
- 编辑其他同协议节点不会覆盖正在运行的协议配置。重复配置会被拒绝；编辑期间若配置被其他操作修改，会要求刷新后重试。
- 多节点列表显示当前节点、存活状态、延迟、出口 IP、失败轮数和待应用修改；每页 8 个，支持中文宽度适配及翻页。
- 连接依次显示切换前检测（最多 60 秒）、服务启动（最多 60 秒）和复检（最多 75 秒）。复检包含入口就绪、HTTPS 连通性、多个服务查询出口 IP。IP 查询限流或失败不会单独判定节点死亡。
- 服务启动或复检失败会恢复原配置、TUN/流量模式、出口检测信息及服务启用状态；恢复失败会明确提示。Telegram 事件发送最多等待 25 秒，失败留待重试。

## Telegram 多节点通知

在菜单 **12** 配置 Bot 和收件人后自动启用。每 30 分钟报告内核状态、本机 IP、代理出口和地理位置，以及当前节点、自动连接开关、全库节点状态、延迟和连续失败轮数。

节点编辑、手动连接、TUN/流量模式切换、手动关闭、自动选择或故障切换、连接失败、失效节点删除、全部节点不可用及恢复会发送专项通知，包含切换前后节点和原因。全部不可用告警只在状态变化时发送，持续故障仍会显示在定时报告中。删除默认需要连续 **3 轮**失败，成功会清零。

事件通知通过物理网卡直连 Telegram，不等待故障代理的 IP 查询；事件内出口信息标明“最近检测”，定时报告会重新查询。通知失败会保存在本机，下一轮检测或节点连接时重试；不影响代理切换。长节点列表自动分条。自动清理导致停机后仍发送监测消息；菜单 **8** 手动关闭代理会先发送关闭事件，再停止通知定时器和自动连接。发送失败的事件会在重新启用通知后重试。

## 仓库结构

- `singbox.sh`：菜单入口与安装器。
- `singbox-manager/`：管理程序、四种节点模板及 systemd 通知定时器。
- `packages/`：经过校验的部署资源包。
- `tools/prepare_release.py`：构建资源包并更新 GitHub 下载地址和 SHA256。

## 维护与发布

修改程序或模板后，在仓库目录执行（将 `OWNER/REPO` 替换为真实仓库名）：

```bash
python3 tools/prepare_release.py --repository coexacx/singbox-manager --branch main
git add singbox.sh singbox-manager packages README.md tools tests .gitignore
git commit -m "Update sing-box manager"
git push origin main
```

构建程序会检查节点占位符、验证 Bash 语法，并生成可重复构建的压缩包。入口与对应资源包必须在同一次提交中推送；保留历史 `packages/singbox-assets-*.tar.gz`，让已经下载的旧版入口仍可使用对应资源包。

不要将 `/etc/sing-box/`、真实节点资料、Telegram Token 或 SSH 登录凭据复制到仓库。`cesnode/` 和原始测试节点文件不属于发布内容。

安装会使用可撤销的 systemd 覆盖配置，明确加载 `/usr/bin/sing-box` 与 `/etc/sing-box/config.json`，避免旧脚本的启动命令、配置目录或启动钩子干扰。原服务文件保留，卸载时移除管理器覆盖并恢复原配置。

选择连接节点成功后，默认开启故障自动切换：每分钟检查当前连接，连续三次 HTTPS 检测失败后重新测量备用节点，按延迟升序逐个尝试，成功后保持该节点。健康节点不会因为出现更快节点而被切走。每分钟检查不累计删除轮数；每 30 分钟全量巡检才累计失败轮数。子菜单 6 可主动关闭自动连接；主菜单 8 关闭代理后不会被自动拉起。
