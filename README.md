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

## 测速、自动连接与清理（管理接口 6）

- 导入和批量测速只测服务器：HTTPS、Shadowsocks、SOCKS5 使用 TCP 握手，WireGuard 使用 ICMP ping，不发送代理认证、TLS 或出口 IP 请求。
- 自动连接按 TCPing/Ping 延迟升序尝试；端口可达不代表代理账号有效，选中的节点还必须通过真实 HTTPS 连通性验证。实际失败立即删除并尝试下一节点；成功后保持连接，不因其他节点变快而切换。
- 备用节点分散轮询，每次最多检测一个到期节点，轮询之间至少 60 秒；同一节点默认至少间隔 1800 秒，节点多时依序排队。子菜单 10 调整复测间隔。手动全库测速与导入依次执行，节点间至少 2 秒，5 分钟内复用测速结果。
- 备用节点 TCP 握手明确失败立即清除，不再等待三轮。WireGuard 没有 ICMP 回复只代表延迟未知，保留节点并排在已测得延迟的候选之后。当前连接由真实代理检测确认故障，避免仅凭一次新 TCP 握手失败中断仍可用的连接。
- 自动模式每分钟调度当前连接检查，成功结果缓存 120 秒。每次先请求一个 HTTPS 204 地址，失败才使用第二地址复核；两个地址均连接失败即删除并切换，不累计失败轮数。服务进程停止时尝试恢复，不单凭进程状态删除节点。
- 连接成功后查询代理出口 IP，并按可获得的中文地理位置命名。IP 服务依次尝试 ipinfo.io、ipwho.is、api.ipify.org，总预算 15 秒；地理查询失败不会把可用代理删除。旧节点名称保留，旧 HTTP 耗时不再参与排序。
- 频率限制仅针对本服务器：真实连接尝试间隔至少 15 秒，真实检测每小时预算 180 个请求额度（连接预留 8，健康检测 2，IP 查询 3）；TCPing/Ping 每小时最多 600 次节点测量。配置在 `/etc/sing-box/manager/probe-policy.json`。这些是本机默认策略，并非供应商承诺的安全频率。
- 本机检测额度用完、上游明确 429 限流、检测网站返回非预期响应、TUN/服务未就绪或本机直连路由错误时延后检测，不作为失效删除。上游 429 暂停本机真实检测一小时；节点认证失败则删除该节点，继续尝试后续候选。
- 运行中节点的新配置实际失败时丢弃待应用修改，保留原有可用配置。手动停用代理会关闭自动连接；备用服务器巡检仍继续。

## Telegram 通知

菜单 12 配置 Bot Token 与个人数字 chat_id。使用 HTTPS Bot API `sendMessage`，绑定物理网卡发送；每 30 分钟报告内核、本机 IP、当前节点、出口 IP、地理位置及 TCPing/Ping 测速结果。

连接、故障切换、立即删除、编辑、模式切换、停止、全部不可用/恢复和检测延后均有专项通知。事件发送前提交节点状态，最多等待 25 秒，失败入队重试。事件使用已有出口信息；定时通知复用 30 分钟内的 IP 查询缓存，查询暂不可用时标明旧采样时间，不将 IP 查询失败单独判定为代理故障。

## 主菜单与节点库联动

- 菜单 **2/3/4/14** 按协议选择并编辑节点；**5/6/7/15** 按协议选择连接节点，统一维护节点库和 Telegram 事件。
- 当前节点的修改保存为“待应用”。手动连接并通过复检后才生效；失败时丢弃修改，保留原配置、原连接和自动模式。自动巡检、故障切换及 TUN/流量模式切换继续使用已生效配置。
- 编辑其他同协议节点不会覆盖正在运行的协议配置。重复配置会被拒绝；编辑期间若配置被其他操作修改，会要求刷新后重试。
- 多节点列表显示当前节点、代理验证状态、服务器可达性、TCPing/Ping 延迟、出口 IP和待应用修改；每页 8 个，支持中文宽度适配及翻页。
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
