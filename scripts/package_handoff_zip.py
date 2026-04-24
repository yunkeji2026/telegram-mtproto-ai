#!/usr/bin/env python3
"""
将 telegram-mtproto-ai 打成 handoff 压缩包（排除 session、venv、缓存等）。

默认输出：仓库内 _package_out/telegram-mtproto-ai-handoff-YYYYMMDD-HHMMSS.zip
（可用环境变量 HANDOFF_ZIP 覆盖为任意绝对路径。）
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 默认打在 _package_out/（在仓库内、且 walk 时整目录排除，避免把正在写的 zip 再打进去）
_out = os.environ.get("HANDOFF_ZIP", "").strip()
if _out:
    OUT = Path(_out)
else:
    # 时间戳避免与旧任务/杀软占用同名文件冲突
    OUT = (
        ROOT
        / "_package_out"
        / f"telegram-mtproto-ai-handoff-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    )

SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".idea", ".vscode",
    # 运行期生成 / 体积大，handoff 不必带上
    "tmp_messenger_rpa", "tmp_fb_analysis", "tmp_install_check",
    "logs", "htmlcov", "dist", "build", "_package_out",
}
SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".session", ".session-journal",
)
SKIP_NAMES = {
    "code.txt", "2fa_password.txt", ".env", ".env.local",
    # 历史打包产物若在仓库根目录，勿再打进去（曾导致 zip 自包含体积爆炸）
    "telegram-mtproto-ai-handoff.zip",
    "telegram-mtproto-ai-cursor-handoff.zip",
}


def should_skip(rel: Path) -> bool:
    parts = set(rel.parts)
    if parts & SKIP_DIR_NAMES:
        return True
    name = rel.name.lower()
    if name in SKIP_NAMES:
        return True
    # 任意 *handoff*.zip（含 telegram-mtproto-ai-handoff.zip）
    if "handoff" in name and name.endswith(".zip"):
        return True
    for suf in SKIP_SUFFIXES:
        if name.endswith(suf):
            return True
    # 本地模型权重（若存在则跳过，避免包体积爆炸）
    if "whisper" in parts or ("models" in parts and "checkpoints" in parts):
        return True
    return False


def main() -> None:
    os.chdir(ROOT)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out_target = OUT.resolve()
    tmp = Path(
        tempfile.gettempdir(),
    ) / f"telegram-mtproto-ai-handoff-{os.getpid()}.zip"
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    count = 0
    with zipfile.ZipFile(
        tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6
    ) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # prune dirs in-place
            rel_dir = Path(dirpath).relative_to(ROOT)
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIR_NAMES
                and not d.startswith(".")
            ]
            for fn in filenames:
                abs_path = Path(dirpath) / fn
                try:
                    if abs_path.resolve() == out_target:
                        continue
                except OSError:
                    pass
                rel = abs_path.relative_to(ROOT)
                if should_skip(rel):
                    continue
                arcname = Path("telegram-mtproto-ai") / rel
                zf.write(abs_path, arcname.as_posix())
                count += 1
    shutil.move(str(tmp), str(OUT))
    with zipfile.ZipFile(OUT, "r") as zr:
        corrupt = zr.testzip()
    if corrupt is not None:
        OUT.unlink(missing_ok=True)
        raise RuntimeError(f"zip 校验失败: 首个损坏成员 {corrupt!r}")
    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"Wrote {OUT} ({count} files, {size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
