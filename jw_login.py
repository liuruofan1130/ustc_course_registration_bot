#!/usr/bin/env python3
# encoding=utf8
"""USTC 教务系统登录（Playwright + storage_state）。Windows / Linux 通用。

登录态用 auth.json（Playwright storage_state，纯 JSON，含 cookie + localStorage，
跨平台可靠）。本机 `python grabbing.py --login` 生成，拷到别的机器即可。不依赖
.browser_data（Chromium profile），跨平台更稳。

无图形界面的机器（如 Linux 服务器）无法人工登录：请先在有浏览器的机器 --login
生成 auth.json，再拷过来。
"""
import os
import re
import time

BASE = "https://jw.ustc.edu.cn"
HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_JSON = os.path.join(HERE, "auth.json")


def open_context(headless=False, storage_state=None):
    """启动浏览器 context，返回 (playwright, context)。

    用 launch() + new_context(storage_state=...) 而非 launch_persistent_context()，
    因为后者不接受 storage_state 参数。登录态完全由 auth.json 承载（跨平台）。
    """
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    kwargs = dict(viewport={"width": 1280, "height": 800}, locale="zh-CN")
    if storage_state:
        kwargs["storage_state"] = storage_state
    context = browser.new_context(**kwargs)
    return pw, context


def save_auth(context):
    """导出登录态到 auth.json（cookie + localStorage）。返回是否成功。"""
    try:
        context.storage_state(path=AUTH_JSON)
        return True
    except Exception as e:
        print("导出 auth.json 失败:", e)
        return False


def _looks_like_login(url):
    u = (url or "").lower()
    return ("passport.ustc.edu.cn" in u or "id.ustc.edu.cn" in u or "/login" in u)


def _quick_sid(url):
    """从选课相关 URL 尽量取出 studentId；取不到返回 None（仅 --login 场景用）。"""
    m = re.search(r"/for-std/course-select/(?:turns/)?(\d+)", url or "")
    return m.group(1) if m else None


def ensure_login(context, headless=False, login_timeout_s=900,
                 forced_sid=None, forced_tid=None, need_turn=True):
    """确保已登录；未登录则等待人工登录。

    need_turn=False 时（如 --login 仅生成 auth.json）只验证登录，不解析选课轮次，
    返回 (sid 或 None, None)。
    """
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(BASE + "/for-std/course-select", wait_until="domcontentloaded")

    if _looks_like_login(page.url):
        if headless:
            raise RuntimeError(
                "未登录且处于 headless 模式，无法人工登录。\n"
                "请在有图形界面的机器运行 `python grabbing.py --login` 生成 auth.json，\n"
                "再把 auth.json 拷到本项目下，然后以 headless 运行。")
        print("=" * 60)
        print("请在弹出的浏览器窗口中完成 USTC 统一身份认证登录。")
        print("（账号密码 / 微信扫码均可）。登录成功后脚本会自动继续。")
        print("=" * 60)
        deadline = time.time() + login_timeout_s
        while _looks_like_login(page.url):
            if time.time() > deadline:
                raise TimeoutError("登录超时（15 分钟），请重新运行。")
            page.wait_for_timeout(2000)
        print("检测到登录成功，继续...\n")

    if not need_turn:
        return _quick_sid(page.url), None
    return resolve_student_turn(page, forced_sid, forced_tid)


def resolve_student_turn(page, forced_sid=None, forced_tid=None):
    """解析 studentId / turnId。forced 值优先。"""
    page.goto(BASE + "/for-std/course-select", wait_until="domcontentloaded")
    url = page.url

    sid, tid = None, None
    m = re.search(r"/for-std/course-select/(\d+)/turn/(\d+)", url)
    if m:
        sid, tid = m.group(1), m.group(2)
    else:
        m2 = re.search(r"/for-std/course-select/turns/(\d+)", url)
        if m2:
            sid = m2.group(1)

    sid = forced_sid or sid
    tid = forced_tid or tid
    if not tid and sid:
        tid = _extract_turn_id(page)

    if not sid:
        raise RuntimeError(
            f"无法解析学号 studentId：{url}\n请在 config.json 显式设置 student_id。")
    if not tid:
        raise RuntimeError(
            f"无法确定选课轮次 turnId（当前页：{url}）。\n"
            "请在 config.json 显式设置 turn_id（从选课页 URL .../turn/{turnId}/select 取）。")
    return sid, tid


def _extract_turn_id(page):
    """从 turns 列表页找第一个指向 /turn/{id} 的链接，返回 turnId。"""
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))")
    except Exception:
        return None
    for h in hrefs:
        if not h:
            continue
        m = re.search(r"/turn/(\d+)", h)
        if m:
            return m.group(1)
    return None
