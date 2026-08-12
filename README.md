# 中国科学技术大学（中科大）选课抢课（Windows / Linux 通用）

监控一门课的已选人数，发现有人退课时自动选课。适配 2026 新版教务系统。**同一份代码在 Windows 和 Linux 都能直接跑。**

## ⚠️ 风险提示
自动抢课可能触发教务系统风控，导致账号受限，**后果自负**。建议：间隔 ≥ 60 秒。

## 核心逻辑

每轮先 POST `std-count` 查「已选人数」——模拟在浏览器里刷新看人数。
- **已满** → 什么都不做，等下一轮；
- **有空位**（`stdCount < 容量`）→ 才 POST `add-request` → `add-drop-response` 真正选课。

叠加三道保护：**随机间隔**、**活跃时段**、**失败熔断**。

## 工作原理

1. `python grabbing.py --login` 在有浏览器的机器登录一次，生成 `auth.json`（cookie）。
2. 运行时用浏览器请求通道（自动带 cookie）调用教务接口。
3. 监控循环：`std-count` 查人数 → 有空位时 `add-request`/`add-drop-response` 选课 → 成功退出。

> 注：`std-count` 接口只返回「已选人数」，不含「容量上限」；容量从 `addable-lessons` 取或手动填 `limit_count`。

## 快速开始

### 1. 装环境（一次性）

**Windows**：
```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
```
**Linux / macOS**：
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

### 2. 登录（在有浏览器的机器上，生成 auth.json）
```bat
.venv\Scripts\python grabbing.py --login      :: Windows
.venv/bin/python grabbing.py --login          # Linux/macOS
```

### 3. 配置 `config.json`
填 `student_id`、`turn_id`、`target_lesson_id`，以及 **`limit_count`（满课人数 = 容量上限）**。`mode` 保持 `spam`。
> 「满课人数」怎么填：脚本调 `std-count` 只能拿到「已选人数」，拿不到容量上限；所以需要手动填。打开选课页看那门课显示的容量（如 `容量 178`），把数字填进 `limit_count`。脚本在「已选人数 < 满课人数」时才抢。

### 4. 运行
```bat
.venv\Scripts\python grabbing.py
.venv/bin/python grabbing.py
```
看到 `std-count 探测：...` 和每轮 `已满 x/y` 就是正常；出现 `有空位！` 即触发选课。

## 在 Linux 服务器上跑（无图形界面）
1. 本机 `--login` 生成 `auth.json`。
2. `scp -r ustc_grab_classes/ user@server:/path/...`
3. 服务器装环境（同上 Linux 命令）。
4. `.venv/bin/python grabbing.py`（默认 headless）。

服务器完全独立运行；本机关机/删本地文件不影响。`auth.json` 拷过去即可。

## `config.json` 字段

| 字段 | 说明 |
|---|---|
| `student_id` | 学生关联 id；留空自动解析（从选课页 URL 取）|
| `turn_id` | 选课轮次 id，**每学期变** |
| `target_lesson_id` | 目标课 lessonId（如 `123456`，这个ID号不是课堂号，需要在F12的数据包里看）|
| `target_course_name` | 课程名模糊匹配（兜底）|
| `limit_count` | **必填**：目标课「满课人数」(容量上限)。脚本在「已选人数 < 满课人数」时才抢（`std-count` 只返回人数，容量需手填）|
| `interval_seconds` | 查询基准间隔（秒），建议 ≥ 60 |
| `jitter_seconds` | 随机抖动（秒），实际间隔 = interval ± jitter |
| `active_hours` | 活跃时段 `HH:MM-HH:MM`，支持跨天；默认 `6:30-1:00`（即 1:00–6:30 暂停）。空 = 全天 |
| `max_errors` | 连续异常熔断阈值，达到即停止 |
| `mode` | `spam` 监控到空位就抢（默认）；`monitor` 仅提醒；`grab` 同 spam |
| `notify_webhook_url` | 可选，事件时 GET 该 URL |
| `headless` | 一般不用设，默认按平台自动判断 |
| `heart_beat` | 在非活跃时段的查询基准间隔，主要目的是保活 |

## 命令一览
```bash
grabbing.py --login            # 登录并生成 auth.json（需图形界面）
grabbing.py --list             # 列出可选课（查 lessonId / 容量）
grabbing.py                    # 监控抢课（按 config.json）
grabbing.py --lesson 123456 -t 90 --log run.log
grabbing.py --headless         # 强制无头
```

## 文件说明
| 文件 | 作用 |
|---|---|
| `grabbing.py` | 主程序（监控式抢课 + 登录失效检测 + 跨平台 headless）|
| `jw_login.py` | 登录模块（Playwright + auth.json）|
| `config.json` | 运行配置 |
| `auth.json` | 登录态（cookie），**敏感，勿提交**，已 gitignore |
| `requirements.txt` | 依赖 |
| `run.sh` | Linux/macOS 启动便捷脚本 |

## 常见问题
- **`std-count 探测` 显示 `解析人数=None`**：响应结构与脚本假设不符。把探测输出的「原始片段」贴出来，对照调整 `_parse_std_count`。
- **一直「已满 x/y」**：正常，等退课。
- **`登录态已过期`**：回本机 `--login` 刷新 `auth.json` 再传一次。
- **被风控/账号受限**：立即停止，降频或改手动；联系教务说明。
- **非选课时段**：解析不到 turnId；选课开放后再跑，或填 `turn_id`。
