# ustc 盲抢选课（Windows / Linux 通用）

自动盲抢一门课：每隔 N 秒尝试选课，有人退课空位即命中并退出。适配 2026 新版教务系统（统一认证 `id.ustc.edu.cn`）。**同一份代码在 Windows 和 Linux 都能直接跑。**


## ⚠️ 请勿滥用
仅用于正当补选。间隔建议 ≥ 30 秒。脚本开发者对滥用后果不负责任。


## 它能做什么

- **盲抢（推荐，默认）**：指定 `lessonId`，每 N 秒自动尝试选课，成功即退出。
- 列出本轮可选课（自动翻页），方便查 `lessonId`。
- **跨平台 + 跨机器**：登录态存 `auth.json`（纯 JSON cookie），本机登录一次，拷到 Linux 服务器即可用。
- **headless 自动判断**：Windows/macOS 默认有头；Linux 无 `$DISPLAY` 默认无头。无需手动配置。

## 工作原理

1. `python grabbing.py --login` 在**有浏览器的机器**上登录一次，生成 `auth.json`（Playwright `storage_state`，跨平台可靠）。
2. 运行时用浏览器的请求通道（自动带 cookie）调用教务接口。
3. **盲抢** = `add-request`（拿 `requestId`）→ `add-drop-response`（确认）。满员返回 `success=false, "教学班人数已满"`；空位返回 `success=true`，脚本退出。

## 为什么是「盲抢」

新系统 `addable-lessons` 只返回容量 `limitCount`，**不返回已选人数 `stdCount`**，无法判断是否满/是否退课。故直接反复尝试选课，靠系统返回的成功/失败判定。（`monitor`/`grab` 模式因取不到人数，基本无效。）


## 快速开始

### 1. 装环境（一次性）

**Windows**（PowerShell / cmd）：
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

```bash
# Windows
.venv\Scripts\python grabbing.py --login
# Linux/macOS
.venv/bin/python grabbing.py --login
```

弹出浏览器 → 完成 USTC 统一身份认证登录 → 自动生成 `./auth.json`。

### 3. 配置 `config.json`

至少设 `target_lesson_id`（要抢的课）、`student_id`、`turn_id`，`mode` 保持 `spam`。

### 4. 运行

```bash
# Windows
.venv\Scripts\python grabbing.py
# Linux/macOS
.venv/bin/python grabbing.py        # 或 ./run.sh
```

看到 `选课: ok=False | 教学班人数已满` 就是正常等退课；某次 `ok=True` 即抢到，自动退出。

---

## 在 Linux 服务器上跑（无图形界面）

服务器没有浏览器界面，无法在那里登录。流程：

1. 按「快速开始」在**本机**（Windows/Mac）`--login` 生成 `auth.json`。
2. 把项目（含 `auth.json`）传到服务器：
   ```bash
   scp -r ustc_grab_classes/ user@server:/path/to/ustc_grab_classes
   ```
3. 服务器装环境（同上 Linux 命令；若报缺系统库见下文 FAQ）。
4. 服务器运行（默认 headless）：
   ```bash
   cd /path/to/ustc_grab_classes
   .venv/bin/python grabbing.py        # 或 ./run.sh
   ```

**服务器是否依赖本机？** 不依赖。`auth.json` 拷过去后服务器完全独立运行——本机关机、关浏览器、删本地文件都不影响服务器。脚本持续运行时服务器会续期 cookie，长期挂着一般不过期；万一失效，脚本会提示，回本机重 `--login` 刷新 `auth.json` 再传一次即可。

---

## `config.json` 字段

| 字段 | 说明 |
|---|---|
| `student_id` | 学生关联 id；留空自动解析（从选课页 URL 取）|
| `turn_id` | 选课轮次 id，**每学期变**；从选课页 URL `.../turn/{id}/select` 取 |
| `target_lesson_id` | 目标课 lessonId（如 `123456`）|
| `target_course_name` | 课程名模糊匹配（兜底）|
| `interval_seconds` | 尝试间隔（秒），最小 5，建议 ≥ 30 |
| `mode` | `spam` 盲抢（推荐，默认）；`monitor`/`grab` 依赖已选人数（基本无效）|
| `notify_webhook_url` | 可选，事件时 GET 该 URL |
| `headless` | **一般不用设**：默认按平台自动判断。需强制时填 `true`/`false` |

## 命令一览

```bash
grabbing.py --login            # 登录并生成 auth.json（需图形界面）
grabbing.py --list             # 列出可选课（查/确认 lessonId）
grabbing.py                    # 盲抢（按 config.json）
grabbing.py --lesson 123456 -m spam -t 30 --log run.log
grabbing.py --headless         # 强制无头
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `grabbing.py` | 主程序（盲抢 + 登录失效检测 + 跨平台 headless）|
| `jw_login.py` | 登录模块（Playwright + auth.json）|
| `config.json` | 运行配置 |
| `auth.json` | 登录态（cookie），**敏感，勿提交**，已 gitignore |
| `requirements.txt` | 依赖 |
| `run.sh` | Linux/macOS 启动便捷脚本（Windows 不用）|
| `grabbing_legacy.py` / `monitoring*.py` | 旧版失效脚本，存档 |

## 常见问题

- **Windows 上 `grabbing.py` 弹出浏览器窗口**：正常，默认非 headless。不想看窗口可 `--headless`。
- **Linux 服务器报缺系统库（`libnss3` 等）**：`playwright install` 不装系统库。免 sudo 方案：用 conda（用户级）装这些库；或让管理员 `apt install` 一次。
- **`登录失败：未登录且处于 headless`**：服务器上没有有效 `auth.json`。先在本机 `--login` 生成 `auth.json`，拷到服务器。
- **一直「教学班人数已满」**：正常，满员等退课。
- **非选课时段**：提示解析不到 turnId；选课开放后再跑，或 `config.json` 显式填 `turn_id`。
