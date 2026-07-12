#!/usr/bin/env python3
"""把仓库后端（main.py）打成自包含可执行，供桌面端作为 sidecar 随包分发（P0 本地自包含）。

用法（在 desktop/ 下）：
    npm run build:backend        # = python build/build_backend.py
或直接：
    python desktop/build/build_backend.py [--clean] [--onefile]

产出：desktop/build/backend-dist/backend(.exe)（onedir 默认，更稳）。
electron-builder 的 extraResources 会把 backend-dist/ → 安装包内 resources/backend/。

注意（重量级软依赖）：openai-whisper / torch / easyocr 体积巨大且为「可选」软依赖。
默认 EXCLUDE 之以控包体；若该机型需要本地 ASR/OCR，去掉对应 --exclude 重打或改走在线后端。
打包是「构建期」动作，需在装好 requirements.txt 的同款 Python 环境里跑，且与目标 OS 一致
（Windows 包要在 Windows 上打、mac 包在 mac 上打——PyInstaller 不跨平台交叉编译）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 默认 GBK 控制台无法编码 ✓/✗/⚠ 等状态符 → 打包成功后最后一行 print 会抛
# UnicodeEncodeError 使脚本 exit 1（CI 打包烟测即便构建成功也误报失败）。强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent          # desktop/build
DESKTOP = HERE.parent                            # desktop
REPO = DESKTOP.parent                            # 仓库根
OUT = HERE / "backend-dist"                      # 产出目录（electron-builder extraResources.from）
NAME = "backend"

# 后端运行需要的数据（模板/静态/示例配置）。格式：(源, 包内目标相对路径)
DATAS = [
    (REPO / "src" / "web" / "templates", "src/web/templates"),
    (REPO / "src" / "web" / "static", "src/web/static"),
    # P0-1 A1：桌面随包种子 = 最小配置（无 YOUR_* 占位）。AITR_DESKTOP_MODE 下
    # ConfigManager._ensure_seeded 优先播种它 → 首启只差一个 AI Key（向导写 overlay）。
    # 完整 example 仍随包：供参考 + 非桌面模式回落种子。
    (REPO / "config" / "config.desktop.min.yaml", "config"),
    (REPO / "config" / "config.example.yaml", "config"),
]

# 动态 import 的包，PyInstaller 静态分析抓不全 → 显式 collect。
COLLECT_SUBMODULES = ["src", "uvicorn", "pyrogram", "fastapi"]
COLLECT_ALL = ["uvicorn"]  # uvicorn 的 lifespan/loops/protocols 子模块按字符串加载

# 重量级可选软依赖：默认排除以控包体（缺失时后端对应能力软降级）。
EXCLUDES = ["whisper", "torch", "torchaudio", "easyocr", "cv2", "matplotlib", "tkinter"]


def _sep() -> str:
    return ";" if os.name == "nt" else ":"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="打包前清空产出与缓存")
    ap.add_argument("--onefile", action="store_true", help="单文件模式（更慢、首启解压；默认 onedir 更稳）")
    ap.add_argument("--keep-heavy", action="store_true", help="不排除 whisper/torch 等重依赖")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("✗ 未安装 PyInstaller。请先：pip install pyinstaller", file=sys.stderr)
        return 2

    if args.clean:
        for d in (OUT, HERE / "build", HERE / "__pycache__"):
            shutil.rmtree(d, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        "--distpath", str(OUT),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE),
        "--paths", str(REPO),
        ("--onefile" if args.onefile else "--onedir"),
        "--console",
    ]
    for mod in COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", mod]
    for mod in COLLECT_ALL:
        cmd += ["--collect-all", mod]
    if not args.keep_heavy:
        for mod in EXCLUDES:
            cmd += ["--exclude-module", mod]
    for src, dst in DATAS:
        if Path(src).exists():
            cmd += ["--add-data", f"{src}{_sep()}{dst}"]
        else:
            print(f"  · 跳过不存在的数据：{src}")

    cmd.append(str(REPO / "main.py"))

    print("→ PyInstaller:\n  " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(REPO))
    if proc.returncode != 0:
        print("✗ 打包失败", file=sys.stderr)
        return proc.returncode

    # onedir 模式产出 backend-dist/backend/backend(.exe)；electron-builder 取整个 backend-dist。
    # 这里把 onedir 的内层目录提平，使 resources/backend/backend(.exe) 路径与 launcher 解析一致。
    inner = OUT / NAME
    exe = (NAME + ".exe") if os.name == "nt" else NAME
    if not args.onefile and inner.is_dir():
        for item in inner.iterdir():
            target = OUT / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(inner, ignore_errors=True)

    final = OUT / exe
    print(f"✓ 完成：{final}" if final.exists() else f"⚠ 产出未在预期路径：{OUT}（请检查 PyInstaller 输出）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
