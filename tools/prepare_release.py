#!/usr/bin/env python3
"""Build a reproducible asset bundle and point the installer to GitHub."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ("https", "shadowsocks", "wg", "socks5")
PRIVATE_FIELDS = {
    "username", "password", "private_key", "pre_shared_key", "peer_public_key",
    "local_address", "peer_address", "tls_server_name",
}


def validate_sources():
    for protocol in PROTOCOLS:
        path = ROOT / "singbox-manager" / "nodes" / f"{protocol}.json"
        node = json.loads(path.read_text())
        for key, value in node.items():
            if key in PRIVATE_FIELDS or key == "server":
                if value and not str(value).startswith("REPLACE_ME_"):
                    raise ValueError(f"{path.relative_to(ROOT)}: {key} 必须使用空值或占位符")
    for path in (ROOT / "singbox-manager" / "templates").glob("*.json"):
        template = json.loads(path.read_text())
        for outbound in template.get("outbounds", []):
            if outbound.get("type") not in ("http", "shadowsocks", "socks"):
                continue
            for key in ("server", "username", "password"):
                value = outbound.get(key)
                if value and not str(value).startswith("REPLACE_ME_"):
                    raise ValueError(f"{path.name}: {key} 必须使用空值或占位符")
        for endpoint in template.get("endpoints", []):
            for value in [endpoint.get("private_key"), *[
                peer.get(key) for peer in endpoint.get("peers", [])
                for key in ("address", "public_key", "pre_shared_key")
            ]]:
                if value and not str(value).startswith("REPLACE_ME_"):
                    raise ValueError(f"{path.name}: WireGuard 节点字段必须使用占位符")
    for path in (ROOT / "singbox-manager").rglob("*"):
        if path.is_symlink():
            raise ValueError(f"不打包符号链接：{path.relative_to(ROOT)}")


def build_archive():
    content = io.BytesIO()
    with gzip.GzipFile(fileobj=content, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            folder = ROOT / "singbox-manager"
            for path in [folder, *sorted(folder.rglob("*"))]:
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                member = archive.gettarinfo(str(path), str(path.relative_to(ROOT)))
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = "root"
                member.mode = 0o755 if path.is_dir() else 0o644
                if path.is_file():
                    with path.open("rb") as stream:
                        archive.addfile(member, stream)
                else:
                    archive.addfile(member)
    return content.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub OWNER/REPO")
    parser.add_argument("--branch", default="main", help="发布分支（默认 main）")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository 必须为 OWNER/REPO")
    if args.repository.rsplit("/", 1)[1] in (".", ".."):
        parser.error("仓库名称无效")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.branch):
        parser.error("branch 只能包含英文字母、数字、下划线、点和连字符")
    validate_sources()
    subprocess.run(["bash", "-n", str(ROOT / "singbox.sh")], check=True)
    base = f"https://raw.githubusercontent.com/{args.repository}/{args.branch}"
    for path in (ROOT / "README.md", ROOT / "singbox-manager" / "README.md"):
        text = path.read_text()
        text = re.sub(
            r"https://(?:pay\.vistart\.art/singbox\.sh|raw\.githubusercontent\.com/[^\s)]+/singbox\.sh)",
            f"{base}/singbox.sh", text,
        )
        text = text.replace("https://github.com/OWNER/REPO", f"https://github.com/{args.repository}")
        text = text.replace(
            "> 发布准备阶段：`OWNER/REPO` 将在指定目标仓库后自动替换为真实地址。\n\n", ""
        )
        text = text.replace("--repository OWNER/REPO", f"--repository {args.repository}")
        path.write_text(text)
    payload = build_archive()
    digest = hashlib.sha256(payload).hexdigest()
    name = f"singbox-assets-{digest[:16]}.tar.gz"
    target = ROOT / "packages" / name
    target.parent.mkdir(exist_ok=True)
    if target.exists() and target.read_bytes() != payload:
        raise ValueError("同名资源包内容不同；停止发布")
    target.write_bytes(payload)
    entry = ROOT / "singbox.sh"
    text = entry.read_text()
    text, url_count = re.subn(r"^ASSET_URL=.*$", f"ASSET_URL={base}/packages/{name}", text, flags=re.M)
    text, sha_count = re.subn(r"^ASSET_SHA256=.*$", f"ASSET_SHA256={digest}", text, flags=re.M)
    if url_count != 1 or sha_count != 1:
        raise ValueError("入口脚本缺少唯一的下载地址和 SHA256 字段")
    entry.write_text(text)
    entry.chmod(0o755)
    subprocess.run(["bash", "-n", str(entry)], check=True)
    for path in (entry, ROOT / "README.md", ROOT / "singbox-manager" / "README.md"):
        if "pay.vistart.art" in path.read_text():
            raise ValueError(f"旧站点地址未完全替换：{path.relative_to(ROOT)}")
    print(f"资源包：packages/{name}")
    print(f"SHA256：{digest}")
    print(f"推送后安装：bash <(curl -fsSL {base}/singbox.sh)")


if __name__ == "__main__":
    main()
