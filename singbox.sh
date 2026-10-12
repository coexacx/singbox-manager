#!/usr/bin/env bash
set -Eeuo pipefail
# Chinese menus, node names and child processes must share one encoding.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
ASSET_URL=https://raw.githubusercontent.com/coexacx/singbox-manager/main/packages/singbox-assets-402d054f7919e1cb.tar.gz
ASSET_SHA256=402d054f7919e1cbfcd8e3d31d03f05d638c4fd721e61820a4d5780a79c7c8a0
LIB=/usr/local/lib/singbox-manager
STATE=/var/lib/singbox-manager
CONFIG_DIR=/etc/sing-box
CONFIG=/etc/sing-box/config.json
PROFILES=/etc/sing-box/manager
MARKER=/var/lib/singbox-manager/installed

say() { printf '%s\n' "$*"; }
error() { printf '错误：%s\n' "$*" >&2; }
remove_file() { [[ ! -e $1 ]] || unlink -- "$1"; }
require_install() {
  [[ -e $MARKER ]] || { error '请先选择 1 安装。'; return 1; }
  [[ -f $LIB/API_VERSION && $(cat "$LIB/API_VERSION") == 6 ]] || { error '管理器需要更新，请先选择 1；现有节点资料会保留。'; return 1; }
}
locked_action() {
  ( flock -w 5 9 || { error '节点检测或切换正在进行，请稍后重试。'; return 1; }; "$@"; ) 9>/run/lock/singbox-manager.lock
}
package_installed() { dpkg-query -s sing-box 2>/dev/null | grep -q '^Status: install ok installed$'; }
current_tun_mode() { [[ -f $STATE/tun_mode && $(cat "$STATE/tun_mode") == off ]] && printf off || printf on; }
current_traffic_mode() { [[ -f $STATE/traffic_mode && $(cat "$STATE/traffic_mode") == global ]] && printf global || printf split; }

check_host() {
  [[ $EUID -eq 0 ]] || { error '请使用 root 执行。'; exit 1; }
  [[ -d /run/systemd/system ]] || { error '需要 systemd 系统。'; exit 1; }
  [[ -r /etc/os-release ]] || { error '无法识别系统。'; exit 1; }
  . /etc/os-release
  [[ $ID == debian || " $ID_LIKE " == *' debian '* ]] || { error '目前只支持 Debian 系系统。'; exit 1; }
  [[ -t 0 ]] || { [[ -r /dev/tty ]] && exec </dev/tty || { error '请在交互终端运行。'; exit 1; }; }
}

install_manager() {
  local archive old_installed old_active old_enabled had_config repo_created key_created
  export DEBIAN_FRONTEND=noninteractive
  if [[ ! -e $MARKER ]]; then
    old_installed=0; package_installed && old_installed=1
    old_active=0; systemctl is-active --quiet sing-box.service && old_active=1
    old_enabled=0; systemctl is-enabled --quiet sing-box.service && old_enabled=1
    had_config=0; [[ -e $CONFIG ]] && had_config=1
    repo_created=0; [[ ! -e /etc/apt/sources.list.d/sagernet.sources ]] && repo_created=1
    key_created=0; [[ ! -e /etc/apt/keyrings/sagernet.asc ]] && key_created=1
  fi
  install -d -m 0700 "$STATE"
  if [[ ! -f $STATE/nft_was_installed ]]; then
    if dpkg-query -s nftables 2>/dev/null | grep -q '^Status: install ok installed$'; then
      printf '1\n' >"$STATE/nft_was_installed"
    else
      printf '0\n' >"$STATE/nft_was_installed"
    fi
    chmod 0600 "$STATE/nft_was_installed"
  fi
  local dependencies=() package
  for package in ca-certificates curl python3 iproute2 nftables iputils-ping; do
    if ! dpkg-query -s "$package" 2>/dev/null | grep -q '^Status: install ok installed$'; then dependencies+=("$package"); fi
  done
  if [[ ${#dependencies[@]} -gt 0 ]]; then
    apt-get update && apt-get install -y --no-upgrade "${dependencies[@]}" || { error '依赖安装失败，请检查 APT 源后重试。'; return 1; }
  fi
  if [[ ! -x /usr/bin/sing-box ]] || ! dpkg --compare-versions "$(/usr/bin/sing-box version | awk 'NR==1 {print $3}')" ge 1.14.0; then
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://sing-box.app/gpg.key -o /etc/apt/keyrings/sagernet.asc
    chmod 0644 /etc/apt/keyrings/sagernet.asc
    cat >/etc/apt/sources.list.d/sagernet.sources <<'EOF'
Types: deb
URIs: https://deb.sagernet.org/
Suites: *
Components: *
Enabled: yes
Signed-By: /etc/apt/keyrings/sagernet.asc
EOF
    apt-get update && apt-get install -y sing-box || { error 'sing-box 安装失败，请检查 APT 源。'; return 1; }
  fi
  command -v sing-box >/dev/null || { error 'sing-box 安装失败。'; return 1; }
  dpkg --compare-versions "$(sing-box version | awk 'NR==1 {print $3}')" ge 1.14.0 || { error 'sing-box 版本低于 1.14。'; return 1; }
  getent group sing-box >/dev/null || { error 'sing-box 服务用户组不存在。'; return 1; }
  archive=$(mktemp /tmp/singbox-assets.XXXXXXXX.tar.gz)
  if ! curl -fL --retry 2 -o "$archive" "$ASSET_URL"; then
    remove_file "$archive"; error '部署资源下载失败。'; return 1
  fi
  if [[ $(sha256sum "$archive" | awk '{print $1}') != "$ASSET_SHA256" ]]; then
    remove_file "$archive"; error '部署资源 SHA256 校验失败。'; return 1
  fi
  install -d -m 0755 "$LIB"
  if ! tar -xzf "$archive" -C "$LIB" --strip-components=1; then
    remove_file "$archive"; error '部署资源解压失败。'; return 1
  fi
  remove_file "$archive"
  install -d -m 0700 "$STATE"
  install -d -m 0750 "$CONFIG_DIR"; chown root:sing-box "$CONFIG_DIR"
  install -d -m 0750 "$PROFILES"; chown root:sing-box "$PROFILES"
  install -m 0644 "$LIB/systemd/singbox-manager-telegram.service" /etc/systemd/system/singbox-manager-telegram.service
  install -m 0644 "$LIB/systemd/singbox-manager-telegram.timer" /etc/systemd/system/singbox-manager-telegram.timer
  install -m 0644 "$LIB/systemd/singbox-manager-nodes.service" /etc/systemd/system/singbox-manager-nodes.service
  install -m 0644 "$LIB/systemd/singbox-manager-nodes.timer" /etc/systemd/system/singbox-manager-nodes.timer
  install -m 0644 "$LIB/systemd/singbox-manager-watch.service" /etc/systemd/system/singbox-manager-watch.service
  install -m 0644 "$LIB/systemd/singbox-manager-watch.timer" /etc/systemd/system/singbox-manager-watch.timer
  install -d -m 0755 /etc/systemd/system/sing-box.service.d
  if [[ ! -f $STATE/dropin_recorded ]]; then
    if [[ -e /etc/systemd/system/sing-box.service.d/99-split-manager.conf ]]; then
      cp -p /etc/systemd/system/sing-box.service.d/99-split-manager.conf "$STATE/original.dropin"
    fi
    touch "$STATE/dropin_recorded"
  fi
  install -d -o sing-box -g sing-box -m 0750 /var/lib/sing-box
  install -m 0644 "$LIB/systemd/99-split-manager.conf" /etc/systemd/system/sing-box.service.d/99-split-manager.conf
  systemctl daemon-reload
  if [[ ! -e $MARKER ]]; then
    if [[ $had_config -eq 1 ]]; then cp -p -- "$CONFIG" "$STATE/original.config"; fi
    printf 'old_installed=%s\nold_active=%s\nold_enabled=%s\nhad_config=%s\nrepo_created=%s\nkey_created=%s\n' \
      "$old_installed" "$old_active" "$old_enabled" "$had_config" "$repo_created" "$key_created" >"$STATE/original.state"
    chmod 0600 "$STATE/original.state"
    touch "$MARKER"; chmod 0600 "$MARKER"
  fi
  for protocol in https shadowsocks wg socks5; do
    if [[ ! -f $PROFILES/$protocol.json ]]; then install -m 0600 "$LIB/nodes/$protocol.json" "$PROFILES/$protocol.json"; fi
  done
  if [[ ! -e $PROFILES/nodes.txt ]]; then
    install -m 0600 "$LIB/nodes.example.txt" "$PROFILES/nodes.txt"
  fi
  python3 "$LIB/probe_policy.py" init
  python3 "$LIB/node_manager.py" init
  systemctl enable --now singbox-manager-nodes.timer >/dev/null
  systemctl enable --now singbox-manager-watch.timer >/dev/null
  if [[ $(sed -n 's/^old_installed=//p' "$STATE/original.state") == 0 && ! -e $STATE/active ]]; then
    systemctl disable --now sing-box.service 2>/dev/null || true
  fi
  if [[ -s $STATE/active ]] && systemctl is-active --quiet sing-box.service; then
    say '管理器已更新；现有节点配置和运行中的代理保持不变。'
  else
    say '安装完成。请用菜单 2、3、4、14 填写节点，再选择 5、6、7、15 启用。'
  fi
}

edit_profile() {
  require_install || return 1
  python3 "$LIB/node_manager.py" edit-protocol "$1"
}

telegram_ready() {
  [[ -f $LIB/telegram_monitor.py ]] && python3 "$LIB/telegram_monitor.py" ready
}

start_telegram_monitor() {
  telegram_ready || return 0
  if ! timeout 10s systemctl enable --now singbox-manager-telegram.timer >/dev/null; then
    error 'Telegram 定时器启动失败；代理仍在运行。'
    return 0
  fi
  [[ ${1:-} == defer ]] && return 0
  if ! timeout -k 3s 45s systemctl start singbox-manager-telegram.service; then
    error 'Telegram 首条通知发送失败；代理仍在运行。请检查 Bot 配置和 journalctl 日志。'
  fi
}

configure_telegram() {
  require_install || return 1
  python3 "$LIB/telegram_monitor.py" setup || return 1
  if telegram_ready; then
    if systemctl is-active --quiet sing-box.service; then
      say '代理正在运行，立即发送首条监测通知…'
      start_telegram_monitor
    else
      say '下次成功启用代理时会发送首条通知，并启动每 30 分钟监测。'
    fi
  else
    systemctl disable --now singbox-manager-telegram.timer 2>/dev/null || true
  fi
}

restore_previous() {
  local backup=$1 was_active=$2 was_enabled=$3 item failed=0
  say '正在恢复切换前的配置与服务状态…'
  timeout -k 3s 25s systemctl stop sing-box.service || failed=1
  if [[ -f $backup/config ]]; then cp -p -- "$backup/config" "$CONFIG" || failed=1
  else remove_file "$CONFIG" || failed=1; fi
  for item in active tun_mode traffic_mode exit_ip probe-result.json; do
    if [[ -f $backup/$item ]]; then cp -p -- "$backup/$item" "$STATE/$item" || failed=1
    else remove_file "$STATE/$item" || failed=1; fi
  done
  if [[ $was_enabled == 1 ]]; then timeout 10s systemctl enable sing-box.service >/dev/null || failed=1
  else timeout 10s systemctl disable sing-box.service 2>/dev/null || failed=1; fi
  if [[ $was_active == 1 ]]; then
    timeout -k 3s 60s systemctl start sing-box.service || failed=1
    systemctl is-active --quiet sing-box.service || failed=1
  fi
  [[ $failed == 0 ]]
}

enable_profile() {
  require_install || return 1
  python3 "$LIB/probe_policy.py" activate "$1" "${2:-$(current_tun_mode)}" "${3:-$(current_traffic_mode)}" "${4:-$PROFILES/$1.json}" "${5:-}"
}

apply_profile() (
  local protocol=$1 mode=${2:-$(current_tun_mode)} traffic=${3:-$(current_traffic_mode)} candidate backup item
  local node_file=${4:-$PROFILES/$protocol.json} armed=0 was_active=0 was_enabled=0
  require_install || return 1
  [[ -f $PROFILES/$protocol.json && -f $LIB/templates/$protocol.json ]] || { error '此协议模板尚未安装，请先选择 1 更新管理器。'; return 1; }
  backup=$(mktemp -d "$STATE/.switch.XXXXXXXX") || return 1
  candidate=$backup/candidate
  activation_cleanup() {
    local result=$?
    trap - EXIT INT TERM
    if [[ $armed == 1 ]]; then
      if restore_previous "$backup" "$was_active" "$was_enabled"; then
        error '切换未完成，已恢复先前配置与服务状态。'
      else
        error '切换失败，恢复服务也未完成；请检查 systemctl status sing-box 与服务日志。'
      fi
      [[ $result != 0 ]] || result=1
    fi
    python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$backup"
    exit "$result"
  }
  trap activation_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  if ! python3 "$LIB/render.py" "$node_file" "$LIB/templates" "$candidate" "$mode" "$traffic"; then
    error '节点信息不完整或格式错误。WireGuard 默认 MTU 为 1280。'; return 1
  fi
  if ! timeout -k 3s 12s sing-box check -c "$candidate"; then
    error 'sing-box 配置校验失败或超时。'; return 1
  fi
  say '[1/3] 切换前检测节点连通性（最多 60 秒）…'
  local probe_status=0
  timeout -k 3s 60s python3 "$LIB/probe.py" --preflight "$candidate" "$node_file" || probe_status=$?
  if [[ $probe_status != 0 ]]; then
    error '节点未通过实际验证，保持现有配置。'; return "$probe_status"
  fi
  systemctl is-active --quiet sing-box.service && was_active=1
  systemctl is-enabled --quiet sing-box.service && was_enabled=1
  if [[ -f $CONFIG ]]; then cp -p -- "$CONFIG" "$backup/config" || return 1; fi
  for item in active tun_mode traffic_mode exit_ip probe-result.json; do
    if [[ -f $STATE/$item ]]; then cp -p -- "$STATE/$item" "$backup/$item" || return 1; fi
  done
  armed=1
  printf '%s\n' "$mode" >"$STATE/tun_mode" || return 1
  printf '%s\n' "$traffic" >"$STATE/traffic_mode" || return 1
  chmod 0600 "$STATE/tun_mode" "$STATE/traffic_mode" || return 1
  chown root:sing-box "$candidate" && chmod 0640 "$candidate" && mv -f -- "$candidate" "$CONFIG" || return 1
  say '[2/3] 启动代理服务（最多 60 秒）…'
  timeout 10s systemctl enable sing-box.service >/dev/null || return 1
  if [[ $was_active == 1 ]]; then
    timeout -k 3s 60s systemctl restart sing-box.service || return 75
  else
    timeout -k 3s 60s systemctl start sing-box.service || return 75
  fi
  if command -v resolvectl >/dev/null; then timeout 3s resolvectl flush-caches 2>/dev/null || true; fi
  if command -v nscd >/dev/null; then timeout 3s nscd -i hosts 2>/dev/null || true; fi
  say '[3/3] 启用后复检服务和节点（最多 75 秒）…'
  systemctl is-active --quiet sing-box.service || { error '本机服务启动失败，暂不判断节点失效。'; return 75; }
  probe_status=0
  timeout -k 3s 75s python3 "$LIB/probe.py" --active "$protocol" || probe_status=$?
  if [[ $probe_status != 0 ]]; then
    error '启用后复检未通过。'; return "$probe_status"
  fi
  printf '%s\n' "$protocol" >"$STATE/active" && chmod 0600 "$STATE/active" || return 1
  armed=0
  if [[ $traffic == global ]]; then
    say "已启用 $protocol（TUN $mode · 全局代理）；保留 SSH 管理连接直连。"
  else
    say "已启用 $protocol（TUN $mode · 分流代理）；其他未命中规则的流量直连。"
  fi
  start_telegram_monitor "${5:-}"
)

manual_enable() {
  require_install || return 1
  python3 "$LIB/node_manager.py" connect-protocol "$1"
}

manual_disable() {
  require_install || return 1
  python3 "$LIB/node_manager.py" stop
}

toggle_tun() {
  local old_mode new_mode protocol
  require_install || return 1
  old_mode=$(current_tun_mode)
  if [[ $old_mode == on ]]; then new_mode=off; else new_mode=on; fi
  if [[ -s $STATE/active ]] && systemctl is-active --quiet sing-box.service; then
    protocol=$(cat "$STATE/active")
    say "正在将 TUN 切换为 $new_mode，并复检 $protocol 节点…"
    if enable_profile "$protocol" "$new_mode" "$(current_traffic_mode)" "$PROFILES/$protocol.json" defer; then
      python3 "$LIB/node_manager.py" mode-ok "主菜单切换 TUN 为 $new_mode"
    else
      python3 "$LIB/node_manager.py" mode-failed 'TUN 切换失败，已保留原模式和连接'
      return 1
    fi
  else
    printf '%s\n' "$new_mode" >"$STATE/tun_mode"; chmod 0600 "$STATE/tun_mode"
    say "TUN 已设为 $new_mode；下次启用代理时生效。"
  fi
}

toggle_traffic_mode() {
  local new_mode protocol label
  require_install || return 1
  if [[ $(current_traffic_mode) == split ]]; then new_mode=global; label=全局; else new_mode=split; label=分流; fi
  if [[ -s $STATE/active ]] && systemctl is-active --quiet sing-box.service; then
    protocol=$(cat "$STATE/active")
    say "正在切换到${label}模式，并复检 $protocol 节点…"
    if enable_profile "$protocol" "$(current_tun_mode)" "$new_mode" "$PROFILES/$protocol.json" defer; then
      python3 "$LIB/node_manager.py" mode-ok "主菜单切换为${label}模式"
    else
      python3 "$LIB/node_manager.py" mode-failed '流量模式切换失败，已保留原模式和连接'
      return 1
    fi
  else
    printf '%s\n' "$new_mode" >"$STATE/traffic_mode"; chmod 0600 "$STATE/traffic_mode"
    say "已设为${label}模式；下次启用代理时生效。"
  fi
}

disable_proxy() {
  require_install || return 1
  if [[ ${1:-} != keep-monitor ]]; then
    systemctl disable --now singbox-manager-telegram.timer 2>/dev/null || true
  fi
  timeout -k 3s 30s systemctl disable --now sing-box.service || return 1
  if command -v resolvectl >/dev/null; then resolvectl flush-caches 2>/dev/null || true; fi
  remove_file "$STATE/active"
  remove_file "$STATE/exit_ip"
  remove_file "$STATE/probe-result.json"
  say '代理已关闭。'
}

detect_proxy() {
  local protocol
  require_install || return 1
  if [[ ! -s $STATE/active ]] || ! systemctl is-active --quiet sing-box.service; then
    error '当前没有运行中的代理，请先选择 5、6、7 或 15 启用。'
    return 1
  fi
  protocol=$(cat "$STATE/active")
  case "$protocol" in
    https|shadowsocks|wg|socks5) ;;
    *) error '当前协议状态无效。'; return 1 ;;
  esac
  say "正在检测 $protocol 代理，请稍候…"
  timeout -k 3s 75s python3 "$LIB/probe.py" --active "$protocol"
}

uninstall_manager() {
  local old_installed old_active old_enabled had_config repo_created key_created
  require_install || return 1
  . "$STATE/original.state"
  systemctl disable --now singbox-manager-nodes.timer 2>/dev/null || true
  systemctl stop singbox-manager-nodes.service 2>/dev/null || true
  systemctl disable --now singbox-manager-watch.timer 2>/dev/null || true
  systemctl stop singbox-manager-watch.service 2>/dev/null || true
  remove_file /etc/systemd/system/singbox-manager-watch.timer
  remove_file /etc/systemd/system/singbox-manager-watch.service
  remove_file /etc/systemd/system/singbox-manager-nodes.timer
  remove_file /etc/systemd/system/singbox-manager-nodes.service
  systemctl disable --now singbox-manager-telegram.timer 2>/dev/null || true
  systemctl stop singbox-manager-telegram.service 2>/dev/null || true
  remove_file /etc/systemd/system/singbox-manager-telegram.timer
  remove_file /etc/systemd/system/singbox-manager-telegram.service
  systemctl disable --now sing-box.service 2>/dev/null || true
  python3 "$LIB/transparent.py" stop 2>/dev/null || true
  if [[ -f $STATE/original.dropin ]]; then
    cp -p "$STATE/original.dropin" /etc/systemd/system/sing-box.service.d/99-split-manager.conf
  else
    remove_file /etc/systemd/system/sing-box.service.d/99-split-manager.conf
  fi
  systemctl daemon-reload
  remove_file "$CONFIG"
  if [[ $old_installed == 0 ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get purge -y sing-box
    if [[ $repo_created == 1 ]]; then remove_file /etc/apt/sources.list.d/sagernet.sources; fi
    if [[ $key_created == 1 ]]; then remove_file /etc/apt/keyrings/sagernet.asc; fi
  fi
  if [[ $had_config == 1 && -f $STATE/original.config ]]; then
    install -d -m 0755 "$CONFIG_DIR"
    cp -p -- "$STATE/original.config" "$CONFIG"
  fi
  if [[ $old_installed == 1 && $old_active == 1 ]]; then
    if [[ $old_enabled == 1 ]]; then systemctl enable sing-box.service >/dev/null; fi
    systemctl start sing-box.service || true
  elif [[ $old_installed == 1 && $old_enabled == 1 ]]; then systemctl enable sing-box.service >/dev/null; fi
  if [[ -f $STATE/nft_was_installed && $(cat "$STATE/nft_was_installed") == 0 ]]; then
    apt-get purge -y nftables
  fi
  for protocol in https shadowsocks wg socks5; do remove_file "/var/lib/sing-box/split-$protocol-cache.db"; done
  remove_file /var/lib/sing-box/split-fakeip-cache.db
  remove_file /var/lib/sing-box/split-rule-cache.db
  python3 - "$LIB" "$PROFILES" "$STATE" /var/lib/singbox-manager-budget <<'PY'
import pathlib, shutil, sys
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
PY
  say '管理器、节点资料和由其安装的 sing-box 已清除；原有配置如有则已恢复。'
}

show_menu() {
  local active=未启用 status=未安装 exit_ip=— version=未安装 tun=开启 traffic=分流 telegram=未配置 accent='' green='' dim='' reset=''
  if command -v sing-box >/dev/null; then version=$(sing-box version | awk 'NR==1 {print $3}'); fi
  if [[ $(current_tun_mode) == off ]]; then tun='关闭 · 透明代理'; else tun='开启 · TUN'; fi
  if [[ $(current_traffic_mode) == global ]]; then traffic=全局; fi
  if telegram_ready; then telegram=已配置; elif [[ -f $PROFILES/telegram.json ]]; then telegram=待绑定; fi
  if [[ -e $MARKER ]]; then status=已停止; fi
  if [[ -f $STATE/active ]] && systemctl is-active --quiet sing-box.service; then
    active=$(cat "$STATE/active")
    status=运行中
    if [[ -s $STATE/exit_ip ]]; then exit_ip=$(cat "$STATE/exit_ip"); fi
  fi
  if [[ -t 1 && -z ${NO_COLOR:-} ]]; then
    accent=$'\033[1;36m'; green=$'\033[1;32m'; dim=$'\033[2m'; reset=$'\033[0m'
  fi
  printf '\n%b╭──────────────────────────────────────────────╮%b\n' "$accent" "$reset"
  printf '%b│       sing-box · 全机智能分流管理              │%b\n' "$accent" "$reset"
  printf '%b╰──────────────────────────────────────────────╯%b\n' "$accent" "$reset"
  printf '  %b版本%b %-13s %bTUN%b %s\n' "$dim" "$reset" "$version" "$dim" "$reset" "$tun"
  printf '  %b服务%b %-8s  %b协议%b %-13s\n' "$dim" "$reset" "$status" "$dim" "$reset" "$active"
  printf '  %b流量模式%b %s\n' "$dim" "$reset" "$traffic"
  printf '  %b代理出口 IP%b %b%s%b\n' "$dim" "$reset" "$green" "$exit_ip" "$reset"
  printf '  %bTelegram 监测%b %s\n' "$dim" "$reset" "$telegram"
  if [[ -f $LIB/node_manager.py && -e $MARKER ]]; then
    printf '  多节点 %s\n' "$(python3 "$LIB/node_manager.py" summary 2>/dev/null || printf '未初始化')"
  fi
  printf '%b  ── 安装与节点 ──────────────────────────────%b\n' "$dim" "$reset"
  printf '   %b01%b  安装 / 更新 sing-box 与四种分流模板\n' "$accent" "$reset"
  printf '   %b02%b  编辑 HTTPS 代理节点\n' "$accent" "$reset"
  printf '   %b03%b  编辑 Shadowsocks 节点\n' "$accent" "$reset"
  printf '   %b04%b  编辑 WireGuard 节点与 MTU\n' "$accent" "$reset"
  printf '   %b14%b  编辑 SOCKS5 代理节点\n' "$accent" "$reset"
  printf '%b  ── 选择节点与连接 ──────────────────────────%b\n' "$dim" "$reset"
  printf '   %b05%b  选择 / 连接 HTTPS 节点\n' "$accent" "$reset"
  printf '   %b06%b  选择 / 连接 Shadowsocks 节点\n' "$accent" "$reset"
  printf '   %b07%b  选择 / 连接 WireGuard 节点\n' "$accent" "$reset"
  printf '   %b15%b  选择 / 连接 SOCKS5 节点\n' "$accent" "$reset"
  printf '   %b08%b  关闭代理\n' "$accent" "$reset"
  printf '   %b09%b  卸载并清除管理器配置\n' "$accent" "$reset"
  printf '%b  ── 诊断与退出 ──────────────────────────────%b\n' "$dim" "$reset"
  printf '   %b10%b  检测当前代理并获取出口 IP\n' "$accent" "$reset"
  printf '   %b11%b  开启 / 关闭 TUN（自动切换透明代理）\n' "$accent" "$reset"
  printf '   %b12%b  配置 Telegram 监测 / 节点事件通知\n' "$accent" "$reset"
  printf '   %b16%b  切换分流 / 全局模式\n' "$accent" "$reset"
  printf '   %b17%b  多节点管理 / 批量转换 / 自动连接\n' "$accent" "$reset"
  printf '   %b13%b  退出\n\n' "$accent" "$reset"
}

main() {
  local choice
  if [[ ${1:-} == --apply-profile ]]; then
    [[ $EUID -eq 0 && ${SBM_POLICY_MANAGED:-} == 1 && $# -eq 6 ]] || return 1
    shift
    apply_profile "$@"
    return $?
  elif [[ ${1:-} == --activate-node ]]; then
    [[ $EUID -eq 0 && $# -eq 2 ]] || return 1
    local protocol
    protocol=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol"])' "$2")
    enable_profile "$protocol" "$(current_tun_mode)" "$(current_traffic_mode)" "$2" defer
    return $?
  elif [[ ${1:-} == --stop-node ]]; then
    [[ $EUID -eq 0 ]] || return 1
    disable_proxy keep-monitor
    return $?
  elif [[ ${1:-} == --start-node-monitor ]]; then
    [[ $EUID -eq 0 ]] || return 1
    start_telegram_monitor defer
    return $?
  elif [[ ${1:-} == --stop-node-monitor ]]; then
    [[ $EUID -eq 0 ]] || return 1
    timeout 10s systemctl disable --now singbox-manager-telegram.timer
    return $?
  fi
  check_host
  while :; do
    show_menu
    read -r -p '请选择功能 [1-17] › ' choice
    case "$choice" in
      1) locked_action install_manager || true ;;
      2) edit_profile https || true ;;
      3) edit_profile shadowsocks || true ;;
      4) edit_profile wg || true ;;
      5) manual_enable https || true ;;
      6) manual_enable shadowsocks || true ;;
      7) manual_enable wg || true ;;
      8) manual_disable || true ;;
      9) locked_action uninstall_manager || true ;;
      10) locked_action detect_proxy || true ;;
      11) locked_action toggle_tun || true ;;
      12) locked_action configure_telegram || true ;;
      13) say '已退出。'; return 0 ;;
      14) edit_profile socks5 || true ;;
      15) manual_enable socks5 || true ;;
      16) locked_action toggle_traffic_mode || true ;;
      17) if require_install; then python3 "$LIB/node_manager.py" menu || true; fi ;;
      *) error '请输入 1–17。' ;;
    esac
    read -r -p '按回车继续…' _
  done
}
main "$@"
