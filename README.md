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

选择 **17** 进入多节点管理。将配置放入 `/etc/sing-box/manager/nodes.txt`，每个节点前单独写一行数字编号；参见 [编号格式示例](singbox-manager/nodes.example.txt)。支持 WireGuard `.conf`、HTTPS/SOCKS5/SS 链接及单节点 JSON。WireGuard 缺少 DNS/MTU 时默认使用 `1.1.1.1` / `1280`。

## 顺序连接与故障恢复（管理接口 7）

- 导入仅解析、转换和校验配置，不进行 TCPing、ping、代理认证或 IP 查询。备用节点不执行定时存活检测；升级会停用并移除旧的备用巡检定时器。
- 自动连接按节点列表顺序尝试。手动选择从指定节点开始；当前连接确认失效后，从它的下一项继续，列表末尾会回到首项。实际连接失败立即删除，成功即停止尝试并保持该节点。
- 仅检测当前连接，默认间隔 180 秒，子菜单 10 可调整（至少 60 秒）。每分钟调度，实际检测受上述间隔约束。使用 HTTPS 204 地址，失败才用第二地址复核；同时确认本机直连可用，避免断网时误删整个节点库。
- 全部节点实际失败后，停止并禁用 sing-box，清理并核验本管理器的 TUN、nftables 和策略路由，恢复直连，发送 Telegram 专项通知。自动连接暂停，等待导入并选择新节点。清理失败会明确通知并继续重试清理；未送达通知会重试。
- 连接成功后查询出口 IP，并按可获得的中文地理位置命名。IP 查询依次尝试 ipinfo.io、ipwho.is、api.ipify.org，总预算 15 秒。查询失败不删除可用节点。
- 频率限制仅针对本服务器：真实连接尝试至少间隔 15 秒，每小时默认预算 180 个请求额度（连接预留 8，健康检测 2，IP 查询 3）。配置在 `/etc/sing-box/manager/probe-policy.json`；不协调其他服务器。
- 本机额度不足、上游明确 429、检测网站异常、本机网络异常、TUN 或服务未就绪均暂缓检测并保留节点。供应商 429 触发本机一小时冷却。节点认证失败则删除该节点，继续下一个。
- 运行中节点的新配置失败时丢弃待应用修改，保留原配置和自动连接设置。手动关闭代理会关闭自动连接。故障告警使用已有出口信息，不再触发备用节点查询。

## Telegram 通知

菜单 12 配置 Bot Token 与个人数字 chat_id。使用 HTTPS Bot API `sendMessage`，绑定物理网卡发送；每 30 分钟报告内核、本机 IP、当前节点、出口 IP、地理位置及节点状态。

连接、故障切换、立即删除、编辑、模式切换、停止、全部失败恢复直连/清理失败和检测延后均有专项通知。事件发送前提交节点状态，最多等待 25 秒，失败入队重试。事件使用已有出口信息；定时通知复用 30 分钟内的 IP 查询缓存，查询暂不可用时标明旧采样时间，不将 IP 查询失败单独判定为代理故障。

异常告警发送成功后自动调用 Telegram `pinChatMessage` 置顶；连接故障、故障切换、全部失效、恢复直连失败及检测暂缓都会置顶。正常定时报告和手动关闭通知不置顶。长消息只置顶包含事件摘要的第一条，不取消既有置顶。置顶失败会记录原因，已送达的告警不会因此重复发送。个人聊天直接调用；群组需要 Bot 有置顶权限，频道需要相应管理员权限。参见 [Telegram Bot API](https://core.telegram.org/bots/api#pinchatmessage)。

## 主菜单与节点库联动

- 菜单 **2/3/4/14** 按协议选择并编辑节点；**5/6/7/15** 按协议选择连接节点，统一维护节点库和 Telegram 事件。
- 当前节点的修改保存为“待应用”。手动连接并通过复检后才生效；失败时丢弃修改，保留原配置、原连接和自动模式。自动巡检、故障切换及 TUN/流量模式切换继续使用已生效配置。
- 编辑其他同协议节点不会覆盖正在运行的协议配置。重复配置会被拒绝；编辑期间若配置被其他操作修改，会要求刷新后重试。
- 多节点列表显示当前节点、代理验证状态、出口 IP 和待应用修改；每页 8 个，支持中文宽度适配及翻页。
- 连接依次显示切换前检测（最多 60 秒）、服务启动（最多 60 秒）和复检（最多 75 秒）。复检包含入口就绪、HTTPS 连通性、多个服务查询出口 IP。IP 查询限流或失败不会单独判定节点死亡。
- 服务启动或复检失败会恢复原配置、TUN/流量模式、出口检测信息及服务启用状态；恢复失败会明确提示。Telegram 事件发送最多等待 25 秒，失败留待重试。

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
