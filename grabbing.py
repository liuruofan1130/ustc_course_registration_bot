#!/usr/bin/env python3
# encoding=utf8
"""USTC 教务系统：盲抢选课（Windows / Linux 通用）。

接口契约（全部为 application/x-www-form-urlencoded，来自真实抓包）：
  GET  /for-std/course-select                     -> 302 到 .../{sid}/turn/{tid}/select
  POST /ws/for-std/course-select/addable-lessons   body: turnId, studentId
  POST /ws/for-std/course-select/add-request       body: studentAssoc, lessonAssoc,
                                                      courseSelectTurnAssoc, scheduleGroupAssoc, virtualCost
                                                  -> 返回 requestId
  POST /ws/for-std/course-select/add-drop-response body: studentId, requestId   -> 确认选课

真实响应：{success, errorMessage:{textZh,textEn,text}, requestId, ...}
  满员时 success=false, errorMessage.textZh="教学班人数已满"。

模式：
  spam    : 盲抢——不检查容量，每隔 N 秒直接尝试选课，成功即退出。推荐。
  monitor : 仅提醒（依赖 stdCount，当前系统取不到，效果有限）
  grab    : 监控到空位才抢（同上）

跨平台：
  - Windows / macOS：直接 `python grabbing.py`，默认非 headless（本机有浏览器）。
  - Linux 服务器（无 $DISPLAY）：默认 headless，登录态来自 auth.json（见下）。
  headless 默认值由 _default_headless() 按平台自动判断；config.json 显式设了则以配置为准，
  命令行 --headless / --login 优先级最高。

登录态（auth.json）：
  - `python grabbing.py --login` 在「有浏览器的机器」上执行，登录后生成 ./auth.json
    （Playwright storage_state，纯 JSON 含 cookie，跨平台可靠）。
  - Linux 服务器无图形界面，无法在此登录：在本机 --login 生成 auth.json，拷到服务器即可。
  - 脚本持续运行时服务器会续期 cookie；长期挂着一般不会过期。

用法：
  python grabbing.py --login            # 本机：登录并生成 auth.json（需图形界面）
  python grabbing.py --list             # 列出可选课（用于查 lessonId）
  python grabbing.py                    # 按 config.json 盲抢
  python grabbing.py --lesson 123456 -m spam -t 30 --log run.log
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlencode

import jw_login

BASE = "https://jw.ustc.edu.cn"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# add-request 正常返回的 requestId 形如 UUID v1
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _default_headless():
    """默认是否无头：Windows/macOS 有桌面→False；Linux 无 $DISPLAY→True。"""
    if sys.platform in ("win32", "darwin"):
        return False
    return not os.environ.get("DISPLAY")


class LoginExpired(Exception):
    """登录态过期（cookie 失效），需要重新登录。"""


class _Tee:
    """同时写多个流（屏幕 + 日志文件）。"""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


# ----------------------------- 配置 -----------------------------
def load_config():
    default = {
        "student_id": "",          # 学生关联 id；留空自动解析，不确定时手填
        "turn_id": "",             # 选课轮次 id，每学期变；从选课页 URL .../turn/{id}/select 取
        "target_lesson_id": "",    # 目标课 lessonId（最精确，推荐）
        "target_course_name": "",  # 或用课程名模糊匹配（兜底）
        "interval_seconds": 30,    # 尝试间隔（秒），建议 ≥ 30
        "mode": "spam",            # spam 盲抢(推荐) | monitor 仅提醒 | grab 监控到空位才抢
        "notify_webhook_url": "",  # 可选：事件时 GET 该 url（Server酱/Bark/QQbot）
        # headless 不在此处硬编码：默认按平台自动判断；需固定时在 config.json 加 "headless": true/false
    }
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            default.update(json.load(f))
    return default


# ----------------------------- HTTP -----------------------------
def _referer(sid, tid):
    return f"{BASE}/for-std/course-select/{sid}/turn/{tid}/select"


def post(context, path, pairs, referer):
    """发 form-urlencoded POST。pairs 为 list[(key,value)]，支持重复 key（数组）。"""
    body = urlencode(pairs)
    return context.request.post(
        BASE + path,
        data=body,
        headers={
            "x-requested-with": "XMLHttpRequest",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "referer": referer,
        },
    )


def list_lessons(context, sid, tid, page=None, size=None):
    pairs = [("turnId", tid), ("studentId", sid)]
    if page is not None:
        pairs.append(("page", page))
    if size is not None:
        pairs.append(("size", size))
    r = post(context, "/ws/for-std/course-select/addable-lessons", pairs, _referer(sid, tid))
    return r.json()


def list_all_lessons(context, sid, tid, max_pages=30, size=100):
    """自动翻页取全部可选课。按 lessonId 去重；分页参数无效时会自动停止。"""
    seen, order = {}, []
    for page in range(1, max_pages + 1):
        try:
            data = list_lessons(context, sid, tid, page=page, size=size)
        except Exception:
            break
        items = _iter_items(data)
        if not items:
            break
        new = 0
        for it in items:
            lid = _lesson_id(it)
            if lid and lid not in seen:
                seen[lid] = it
                order.append(it)
                new += 1
        if new == 0:            # 每页重复 → 分页参数无效，停止
            break
        if len(items) < size:   # 最后一页
            break
    return order


def add_request(context, sid, tid, lesson_id):
    r = post(context, "/ws/for-std/course-select/add-request",
             [("studentAssoc", sid), ("lessonAssoc", str(lesson_id)),
              ("courseSelectTurnAssoc", tid), ("scheduleGroupAssoc", ""),
              ("virtualCost", "0")], _referer(sid, tid))
    return r


def add_drop_response(context, sid, tid, request_id):
    r = post(context, "/ws/for-std/course-select/add-drop-response",
             [("studentId", sid), ("requestId", request_id)], _referer(sid, tid))
    return r


# ----------------------------- 解析 -----------------------------
def _iter_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "lessons", "rows", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _name(it):
    if it.get("nameZh"):
        return it["nameZh"]
    c = it.get("course")
    if isinstance(c, dict):
        return c.get("nameZh") or c.get("name") or ""
    return it.get("courseNameZh") or ""


def _teacher(it):
    ta = it.get("teachers") or it.get("teacherAssignmentList") or []
    if isinstance(ta, list) and ta:
        first = ta[0]
        if isinstance(first, dict):
            p = first.get("person") or first
            return p.get("nameZh") or p.get("name") or first.get("nameZh") or ""
        return str(first)
    return it.get("teacherNameZh") or ""


def _limit(it):
    return it.get("limitCount") or it.get("capacity") or 0


def _std(it):
    return it.get("stdCount") or it.get("selectedCount") or 0


def _lesson_id(it):
    return str(it.get("id") or it.get("lessonId") or "")


def find_target(data, lesson_id=None, course_name=""):
    items = _iter_items(data)
    for it in items:
        if lesson_id and _lesson_id(it) == str(lesson_id):
            return it, items
        if course_name and course_name in _name(it):
            return it, items
    return None, items


def parse_add_result(r2):
    """add-drop-response 结果解析。真实结构：{success, errorMessage:{textZh,...}, ...}"""
    txt = r2.text()
    try:
        data = r2.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        ok = bool(data.get("success", data.get("ok", False)))
        em = data.get("errorMessage")
        if isinstance(em, dict):
            msg = (em.get("textZh") or em.get("text") or em.get("textEn")
                   or json.dumps(em, ensure_ascii=False))
        else:
            msg = (data.get("msg") or data.get("message")
                   or json.dumps(data, ensure_ascii=False))
        if not ok and ("成功" in msg or "已选" in msg):
            ok = True
        return ok, msg
    ok = ("true" in txt.lower()) or ("成功" in txt) or ("已选" in txt)
    return ok, txt[:200]


def _looks_like_login_page(body, status):
    """判断响应是否其实是登录页/未授权（登录态过期的典型表现）。"""
    low = body.lower()
    return (status in (401, 403) or "<html" in low or "/login" in low
            or "passport.ustc" in low or "id.ustc" in low or "cas/login" in low)


# ----------------------------- 选课 -----------------------------
def grab(context, sid, tid, lesson_id):
    """执行一次选课：add-request 拿 requestId，再 add-drop-response 确认。

    若检测到登录态过期（响应不是 UUID 而是 HTML/401），抛 LoginExpired。
    """
    r1 = add_request(context, sid, tid, lesson_id)
    body1 = r1.text()
    status1 = r1.status
    rid = body1.strip().strip('"')

    if not _UUID_RE.match(rid):
        if _looks_like_login_page(body1, status1):
            raise LoginExpired(
                f"登录态已过期（add-request status={status1}, 片段={body1[:80]!r}）")
        if not rid:
            return False, f"add-request 未返回 requestId(status={status1}): {body1[:160]}"

    r2 = add_drop_response(context, sid, tid, rid)
    body2 = r2.text()
    if _looks_like_login_page(body2, r2.status):
        raise LoginExpired(f"登录态已过期（add-drop-response status={r2.status}）")
    return parse_add_result(r2)


# ----------------------------- 通知 -----------------------------
def notify(cfg, message):
    print("[通知]", message)
    url = cfg.get("notify_webhook_url", "")
    if url:
        try:
            import requests as _rq
            sep = "&" if "?" in url else "?"
            _rq.get(url + sep + urlencode({"m": message}), timeout=10)
        except Exception as e:
            print("  推送失败:", e)


# ----------------------------- 展示 -----------------------------
def print_lessons(items, limit=20):
    if not items:
        print("（无数据）")
        return
    print("字段示例(第一条keys):", list(items[0].keys()))
    print(f"{'lessonId':<10}{'容量':<6}{'课程名':<28}老师")
    for it in items[:limit]:
        print(f"{_lesson_id(it):<10}{str(_limit(it)):<6}{_name(it):<28}{_teacher(it)}")


# ----------------------------- 主流程 -----------------------------
def main():
    ap = argparse.ArgumentParser(description="USTC 教务系统 盲抢选课（Windows / Linux 通用）")
    ap.add_argument("--lesson", help="目标课 lessonId（如 123456），优先于配置文件")
    ap.add_argument("--name", help="目标课课程名（模糊匹配）")
    ap.add_argument("-m", "--mode", choices=["spam", "monitor", "grab"],
                    help="spam 盲抢(推荐) | monitor 仅提醒 | grab 监控到空位才抢")
    ap.add_argument("-t", "--interval", type=int, help="尝试间隔（秒）")
    ap.add_argument("--headless", action="store_true", help="强制无头模式")
    ap.add_argument("--login", action="store_true", help="仅登录建立登录态后退出（需图形界面）")
    ap.add_argument("--list", action="store_true", help="列出可选课再退出（用于查 lessonId）")
    ap.add_argument("--log", help="同时把输出写入该日志文件（相对路径基于本项目目录）")
    args = ap.parse_args()

    if args.log:
        log_path = args.log if os.path.isabs(args.log) else os.path.join(HERE, args.log)
        log_fp = open(log_path, "a", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, log_fp)

    cfg = load_config()
    if args.lesson: cfg["target_lesson_id"] = args.lesson
    if args.name: cfg["target_course_name"] = args.name
    if args.mode: cfg["mode"] = args.mode
    if args.interval: cfg["interval_seconds"] = args.interval

    headless = cfg.get("headless", _default_headless())
    if args.headless: headless = True
    if args.login: headless = False   # 登录必须有可见浏览器

    storage_state = jw_login.AUTH_JSON if os.path.exists(jw_login.AUTH_JSON) else None
    pw, context = jw_login.open_context(headless=headless, storage_state=storage_state)
    try:
        try:
            sid, tid = jw_login.ensure_login(
                context, headless=headless,
                forced_sid=cfg.get("student_id") or None,
                forced_tid=cfg.get("turn_id") or None,
                need_turn=not args.login)
        except Exception as e:
            print(f"登录失败：{type(e).__name__}: {e}")
            if headless:
                print("提示：当前为 headless 模式，无法人工登录。请在有图形界面的机器上\n"
                      "      运行 `python grabbing.py --login` 生成 auth.json，再把它拷到\n"
                      "      本项目下（Linux 服务器场景），然后重新运行。")
            return

        print(f"已登录。studentId={sid}  turnId={tid}")

        if args.login:
            jw_login.save_auth(context)
            print("登录态已保存到 ./auth.json（cookie，跨平台）。")
            print("下一步：在浏览器里点进选课页，地址栏 .../turn/<数字>/select 中的 <数字> 即")
            print("        turn_id，填入 config.json；再填 student_id、target_lesson_id 即可抢课。")
            print("部署到无图形界面的机器：把 auth.json 拷过去即可。")
            return

        need_list = args.list or cfg["mode"] in ("monitor", "grab")
        data = list_all_lessons(context, sid, tid) if need_list else []
        target, items = find_target(data, cfg.get("target_lesson_id"), cfg.get("target_course_name"))

        if args.list:
            print_lessons(items)
            return

        mode = cfg["mode"]
        lid = cfg.get("target_lesson_id") or (target and _lesson_id(target)) or ""
        if not lid:
            print("\n未指定目标课。请在 config.json 设置 target_lesson_id。")
            print("可选课片段（前 5 条）：")
            print_lessons(items, limit=5)
            return
        cname = _name(target) if target else f"lessonId={lid}"

        if mode != "spam" and not target:
            print(f"\n未在可选列表中找到 lessonId={lid}（monitor/grab 模式需要它在列表里）。")
            print("可用 `--list` 核对，或改用 spam 盲抢模式（config.json 设 mode=spam）。")
            return

        interval = max(5, int(cfg.get("interval_seconds", 30)))
        n = 0

        if mode == "spam":
            print(f"盲抢模式：每 {interval}s 直接尝试选课 {cname}（lessonId={lid}），成功即退出。")
            print("（不检查容量；一旦有人退课空出位置，下一轮即命中。）\n")
            while True:
                n += 1
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                try:
                    ok, msg = grab(context, sid, tid, lid)
                    print(f"[{ts}] #{n} 选课: ok={ok} | {msg}")
                    if ok:
                        notify(cfg, f"选课成功：{cname}（lessonId={lid}）")
                        print("选课成功，退出。")
                        return
                except LoginExpired as e:
                    print(f"[{ts}] #{n} ⚠️ {e}")
                    print("    请在有图形界面的机器上运行 `python grabbing.py --login` 重新生成\n"
                          "    auth.json，再把它拷到本项目下，然后重新运行。")
                    notify(cfg, f"登录态过期，需重新登录：{cname}")
                    return  # 退出，等待人工重登，避免空转
                except Exception as e:
                    print(f"[{ts}] #{n} 异常 {type(e).__name__}: {e}")
                time.sleep(interval)

        # ---------- monitor / grab ----------
        print(f"目标课：{cname}  lessonId={lid}  容量={_limit(target) if target else '?'}  模式={mode}")
        print("提示：addable-lessons 当前不返回已选人数，monitor/grab 判断可能不准，建议用 spam。\n")
        while True:
            n += 1
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                data = list_lessons(context, sid, tid)
                target, _ = find_target(data, lid, "")
                if target is None:
                    print(f"[{ts}] #{n} 该课不在可选列表（可能已选上/被移除）")
                else:
                    std, lim = _std(target), _limit(target)
                    if std < lim:
                        print(f"[{ts}] #{n} 有空位！{std}/{lim}")
                        notify(cfg, f"空位提醒：{cname} {std}/{lim}")
                        if mode == "grab":
                            ok, msg = grab(context, sid, tid, lid)
                            print("  选课结果:", ok, "|", msg)
                            if ok:
                                notify(cfg, f"选课成功：{cname}")
                                print("选课成功，退出。")
                                return
                    else:
                        print(f"[{ts}] #{n} 已满 {std}/{lim}")
            except LoginExpired as e:
                print(f"[{ts}] #{n} ⚠️ {e}")
                print("    登录态过期，请重新 `--login` 生成 auth.json。")
                notify(cfg, f"登录态过期，需重新登录：{cname}")
                return
            except Exception as e:
                print(f"[{ts}] #{n} 轮询异常 {type(e).__name__}: {e}")
            time.sleep(interval)
    finally:
        try:
            context.close()
        except Exception:
            pass
        pw.stop()


if __name__ == "__main__":
    main()
