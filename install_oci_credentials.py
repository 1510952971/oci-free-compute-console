#!/usr/bin/env python3
"""Install an OCI config download and private key in the standard location."""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: install_oci_credentials.py CONFIG_FILE PRIVATE_KEY", file=sys.stderr)
        return 2
    source_config = Path(sys.argv[1]).expanduser()
    source_key = Path(sys.argv[2]).expanduser()
    key_text = source_key.read_text(encoding="utf-8")
    if "PRIVATE KEY" not in key_text:
        raise ValueError("The selected key file is not a PEM private key")

    raw_config = source_config.read_text(encoding="utf-8", errors="replace")
    if raw_config.lstrip().startswith("{\\rtf"):
        converted = subprocess.run(
            ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(source_config)],
            check=True,
            capture_output=True,
            text=True,
        )
        raw_config = converted.stdout
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(raw_config)
    except configparser.MissingSectionHeaderError as error:
        raise ValueError(
            "配置文件必须包含 [DEFAULT]。请下载 OCI 的配置文件，或把配置预览保存为纯文本/RTF"
        ) from error
    if "DEFAULT" not in parser:
        raise ValueError("The selected file is not an OCI SDK configuration file")
    required = ("user", "fingerprint", "tenancy", "region")
    missing = [key for key in required if not parser["DEFAULT"].get(key)]
    if missing:
        raise ValueError("OCI configuration is missing: " + ", ".join(missing))

    target_dir = Path.home() / ".oci"
    target_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target_dir, 0o700)
    target_key = target_dir / "oci_api_key.pem"
    shutil.copyfile(source_key, target_key)
    os.chmod(target_key, 0o600)
    parser["DEFAULT"]["key_file"] = str(target_key)
    target_config = target_dir / "config"
    with target_config.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    os.chmod(target_config, 0o600)
    print(f"Installed OCI credentials in {target_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, configparser.Error, subprocess.SubprocessError) as error:
        print(f"安装失败：{error}", file=sys.stderr)
        raise SystemExit(2)
