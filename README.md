# sing-box 整机代理管理器

Debian / systemd 的交互菜单，支持 HTTPS、Shadowsocks、WireGuard、SOCKS5，以及分流 / 全局模式、TUN / TProxy 切换和 Telegram 状态通知。

## 安装与更新

在目标服务器以 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/coexacx/singbox-manager/main/singbox.sh)
```

首次运行选择 **1** 安装，然后填写节点并启用。更新时重新执行同一条命令，再选择 **1**；会保留现有节点资料和运行中的代理。

管理脚本与四种模板的资源包均从本 GitHub 仓库下载，资源包使用 SHA256 校验。系统基础依赖通过 Debian APT 安装，sing-box 内核沿用 SagerNet 官方 APT 源。

菜单功能、节点字段和运行机制详见 [使用说明](singbox-manager/README.md)。仓库只包含空白节点模板；节点资料和 Telegram 配置保存在部署服务器的 `/etc/sing-box/manager/` 中。

## 多节点管理

选择 **17** 进入多节点菜单。将配置放入 `/etc/sing-box/manager/nodes.txt`，每个节点前单独写一行数字编号；参见 [编号格式示例](singbox-manager/nodes.example.txt)。支持 WireGuard `.conf`、HTTPS/SOCKS5/SS 链接及单节点 JSON，导入后测试出口并按中文地理位置命名。WireGuard 缺少 DNS/MTU 时默认使用 `1.1.1.1` / `1280`。

可查看、编辑、改名、删除或手动连接节点，也可开启自动连接：先选 HTTP 实测延迟最低的可用节点；连接正常时保持当前节点，只有故障时才切换。每 30 分钟检测全部节点，每轮失败重试 3 次；默认连续 3 轮失败后删除，成功会清零。子菜单 10 可设为 1 轮删除。手动停止代理会关闭自动连接，节点巡检继续运行。

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
