#!/usr/bin/env bash
# Linux/macOS 启动盲抢（前台运行，输出同时写入 run.log）。
# Windows 不用此脚本，直接：.venv\Scripts\python.exe grabbing.py
# 后台运行：nohup ./run.sh >/dev/null 2>&1 &  或用 tmux/screen。
set -e
cd "$(dirname "$0")"
exec ./.venv/bin/python grabbing.py --log run.log "$@"
