#!/usr/bin/env python3
# encoding=utf8
"""USTC 教务系统：监控式抢课（Windows / Linux 通用）。

接口契约（全部为 application/x-www-form-urlencoded，来自真实抓包）：
  GET  /for-std/course-select                     -> 302 到 .../{sid}/turn/{tid}/select
  POST /ws/for-std/course-select/addable-lessons   body: turnId, studentId            （--list 列课用）
  POST /ws/for-std/course-select/std-count         body: lessonIds[]=...              （取已选人数）
  POST /ws/for-std/course-select/add-request       body: studentAssoc, lessonAssoc,
                                                      courseSelectTurnAssoc, scheduleGroupAssoc, virtualCost
                                                  -> 返回 requestId
  POST /ws/for-std/course-select/add-drop-response body: studentId, requestId         -> 确认选课

核心思路（监控式，模仿"刷新看人数"）：
  每轮先 POST std-count 查「已选人数」，只有 stdCount < limitCount（有空位）时，
  才 POST add-request / add-drop-response 真正选课。把高频"写操作"降成高频"读操作"，
  显著降低被风控判定为刷选课的概率。
  叠加：随机间隔（打掉固定节拍）、活跃时段（凌晨不跑）、失败熔断（连续异常自动停）。

模式：
  spam    : 监控到空位→自动选课，成功即退出（推荐）
  monitor : 监控到空位→仅提醒，不选课
  grab    : 同 spam（别名）

跨平台：Windows/macOS 默认非 headless；Linux 无 $DISPLAY 默认 headless。
登录态：本机 `--login` 生成 auth.json，拷到无界面机器即可。

用法：
  python grabbing.py --login            # 本机：登录并生成 auth.json
  python grabbing.py --list             # 列出可选课（用于查 lessonId / 容量）
  python grabbing.py                    # 按 config.json 监控抢课
  python grabbing.py --lesson 123456 -t 60 --log run.log
"""
import argparse
import json
import os
import random
import re
import sys
import time
from urllib.parse import urlencode

import jw_login

BASE = "https://jw.ustc.edu.cn"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

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
        "student_id": "",          # 学生关联 id；留空自动解析
        "turn_id": "",             # 选课轮次 id，每学期变
        "target_lesson_id": "",    # 目标课 lessonId
        "target_course_name": "",  # 课程名模糊匹配（兜底）
        "limit_count": 0,          # 必填：目标课「满课人数」(容量上限)
        "interval_seconds": 60,    # 查询基准间隔（秒），实际 = interval ± jitter
        "jitter_seconds": 15,      # 随机抖动（秒）；60±15 = 45~75
        "heart_beat": 300,         # 非活跃时段心跳间隔（秒），实际 = heart_beat ± jitter；用于保活
        "active_hours": "6:30-1:00",  # 活跃时段 HH:MM-HH:MM；非活跃时段仅发心跳保活；空=全天
        "max_errors": 20,          # 连续异常熔断阈值
        "mode": "spam",            # spam 监控到空位就抢(推荐) | monitor 仅提醒 | grab 同 spam
        "notify_webhook_url": "",  # 可选：事件时 GET 该 url
        # headless 不在此硬编码：默认按平台自动判断
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
        if new == 0:
            break
        if len(items) < size:
            break
    return order


def std_count_raw(context, lesson_id, sid, tid):
    """POST std-count 查已选人数，返回原始 response。body: lessonIds[]={lessonId}"""
    return post(context, "/ws/for-std/course-select/std-count",
                [("lessonIds[]", str(lesson_id))], _referer(sid, tid))


def add_request(context, sid, tid, lesson_id):
    return post(context, "/ws/for-std/course-select/add-request",
                [("studentAssoc", sid), ("lessonAssoc", str(lesson_id)),
                 ("courseSelectTurnAssoc", tid), ("scheduleGroupAssoc", ""),
                 ("virtualCost", "0")], _referer(sid, tid))


def add_drop_response(context, sid, tid, request_id):
    return post(context, "/ws/for-std/course-select/add-drop-response",
                [("studentId", sid), ("requestId", request_id)], _referer(sid, tid))


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


def _parse_std_count(data, lesson_id):
    """从 std-count 响应里解析已选人数（int），结构以实际为准，多兜底。返回 None 表示没解析出来。"""
    lid = str(lesson_id)

    def coerce(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        if isinstance(v, dict):
            for k in ("stdCount", "count", "selectedCount", "stdCnt"):
                if k in v:
                    return coerce(v[k])
        return None

    def find(obj):
        if isinstance(obj, dict):
            if lid in obj:
                r = coerce(obj[lid])
                if r is not None:
                    return r
            for key in ("data", "rows", "result", "lessons"):
                if key in obj:
                    r = find(obj[key])
                    if r is not None:
                        return r
            for v in obj.values():
                r = find(v)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = find(v)
                if r is not None:
                    return r
        return None

    return find(data)


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
    low = body.lower()
    return (status in (401, 403) or "<html" in low or "/login" in low
            or "passport.ustc" in low or "id.ustc" in low or "cas/login" in low)


def _safe_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


# ----------------------------- 选课 -----------------------------
def grab(context, sid, tid, lesson_id):
    """执行一次选课：add-request 拿 requestId，再 add-drop-response 确认。"""
    r1 = add_request(context, sid, tid, lesson_id)
    body1 = r1.text()
    rid = body1.strip().strip('"')
    if not _UUID_RE.match(rid):
        if _looks_like_login_page(body1, r1.status):
            raise LoginExpired(f"登录态已过期（add-request status={r1.status}）")
        return False, f"add-request 未返回 requestId(status={r1.status}): {body1[:160]}"
    r2 = add_drop_response(context, sid, tid, rid)
    body2 = r2.text()
    if _looks_like_login_page(body2, r2.status):
        raise LoginExpired(f"登录态已过期（add-drop-response status={r2.status}）")
    return parse_add_result(r2)


# ----------------------------- 时段 / 间隔 -----------------------------
def _parse_hhmm(s):
    """'6:30' -> 390 (当天 0:00 起的分钟数)。失败返回 None。"""
    try:
        h, m = s.strip().split(":", 1)
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _now_minutes():
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min


def _in_active_hours(spec):
    """spec 如 '6:30-1:00' → 当前时刻是否在活跃段内。

    支持跨天：start>=end 时视为 [start,24:00)∪[00:00,end)。
    例如 6:30-1:00 = 6:30~次日1:00 活跃，1:00~6:30 暂停。
    空则全天放行。
    """
    if not spec or not spec.strip():
        return True
    try:
        a, b = spec.split("-", 1)
        start = _parse_hhmm(a)
        end = _parse_hhmm(b)
        if start is None or end is None:
            return True
    except Exception:
        return True
    now = _now_minutes()
    if start < end:
        return start <= now < end
    else:  # 跨天
        return now >= start or now < end


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
    ap = argparse.ArgumentParser(description="USTC 教务系统 监控式抢课（Windows / Linux 通用）")
    ap.add_argument("--lesson", help="目标课 lessonId，优先于配置文件")
    ap.add_argument("--name", help="目标课课程名（模糊匹配）")
    ap.add_argument("-m", "--mode", choices=["spam", "monitor", "grab"],
                    help="spam 监控到空位就抢(推荐) | monitor 仅提醒 | grab 同 spam")
    ap.add_argument("-t", "--interval", type=int, help="查询基准间隔（秒）")
    ap.add_argument("--headless", action="store_true", help="强制无头模式")
    ap.add_argument("--login", action="store_true", help="仅登录建立登录态后退出（需图形界面）")
    ap.add_argument("--list", action="store_true", help="列出可选课再退出（查 lessonId / 容量）")
    ap.add_argument("--log", help="同时把输出写入该日志文件")
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
    if args.login: headless = False

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
            print("下一步：点进选课页，地址栏 .../turn/<数字>/select 中的 <数字> 即 turn_id；")
            print("        再把 student_id / turn_id / target_lesson_id 填入 config.json。")
            return

        data = list_all_lessons(context, sid, tid) if args.list else []
        target, items = find_target(data, cfg.get("target_lesson_id"), cfg.get("target_course_name"))

        if args.list:
            print_lessons(items)
            return

        mode = cfg["mode"]
        lid = cfg.get("target_lesson_id") or ""
        if not lid:
            print("\n未指定目标课。请在 config.json 设置 target_lesson_id。")
            return
        cname = cfg.get("target_course_name") or (target and _name(target)) or f"lessonId={lid}"

        # ---- 满课人数 / 容量上限（用户手动填）----
        limit = int(cfg.get("limit_count") or 0)
        if not limit:
            print(f"\n请在 config.json 填入 limit_count：目标课的「满课人数」(容量上限)。")
            print("脚本在「已选人数 < 满课人数」(有人退课空出位置) 时才发起选课。")
            print("可用 `grabbing.py --list` 查看课程的 limitCount 字段作参考。")
            return

        # ---- 探测 std-count 响应结构（首次，便于排查）----
        try:
            probe = std_count_raw(context, lid, sid, tid)
            probe_body = probe.text()
            if _looks_like_login_page(probe_body, probe.status):
                raise LoginExpired("登录态已过期（std-count 探测）")
            probe_count = _parse_std_count(_safe_json(probe_body), lid)
            print(f"std-count 探测：HTTP {probe.status}，解析人数={probe_count}，原始片段={probe_body[:120]!r}")
        except LoginExpired as e:
            print(f"⚠️ 探测发现登录态过期：{e}")
            print("请在有图形界面的机器 `python grabbing.py --login` 重新生成 auth.json，拷回后重跑。")
            notify(cfg, f"登录态过期，需重新登录：{cname}")
            return
        except Exception as e:
            print(f"std-count 探测异常：{type(e).__name__}: {e}")

        interval = max(10, int(cfg.get("interval_seconds", 60)))
        jitter = max(0, int(cfg.get("jitter_seconds", interval // 3)))
        heart_beat = max(30, int(cfg.get("heart_beat", 300)))
        active = (cfg.get("active_hours") or "").strip()
        max_errors = max(3, int(cfg.get("max_errors", 20)))

        print(f"\n监控抢课：目标 {cname}（lessonId={lid}）容量={limit}")
        print(f"间隔 {interval}±{jitter}s | 活跃时段={active or '全天'} | 模式={mode} | 熔断={max_errors}")
        print("逻辑：每轮先查人数，stdCount<容量 才提交选课。\n")

        n = 0
        consecutive_err = 0
        while True:
            n += 1
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            in_active = _in_active_hours(active)
            try:
                r = std_count_raw(context, lid, sid, tid)
                body = r.text()
                if _looks_like_login_page(body, r.status):
                    raise LoginExpired(f"登录态已过期（std-count status={r.status}）")
                count = _parse_std_count(_safe_json(body), lid)
                if count is None:
                    print(f"[{ts}] #{n} 人数解析失败，原始: {body[:160]!r}")
                    consecutive_err += 1
                elif count < limit:
                    consecutive_err = 0
                    print(f"[{ts}] #{n} 有空位！{count}/{limit}")
                    notify(cfg, f"空位提醒：{cname} {count}/{limit}")
                    if mode in ("spam", "grab"):
                        ok, msg = grab(context, sid, tid, lid)
                        print(f"  选课结果: ok={ok} | {msg}")
                        if ok:
                            notify(cfg, f"选课成功：{cname}（lessonId={lid}）")
                            print("选课成功，退出。")
                            return
                else:
                    consecutive_err = 0
                    tag = "" if in_active else "（心跳保活）"
                    print(f"[{ts}] #{n} 已满 {count}/{limit}{tag}")
            except LoginExpired as e:
                print(f"[{ts}] #{n} ⚠️ {e}")
                print("    请在有图形界面的机器 `python grabbing.py --login` 重新生成 auth.json，")
                print("    拷回本项目后重新运行。")
                notify(cfg, f"登录态过期，需重新登录：{cname}")
                return
            except Exception as e:
                consecutive_err += 1
                print(f"[{ts}] #{n} 异常 {type(e).__name__}: {e}")
                if consecutive_err >= max_errors:
                    print(f"连续 {consecutive_err} 次异常，触发熔断，停止。请检查网络/登录态。")
                    notify(cfg, f"{cname} 监控熔断停止（连续异常）")
                    return
            # 活跃→正常间隔；非活跃→心跳间隔（均为 ± jitter）
            base = interval if in_active else heart_beat
            sleep_s = base + random.randint(-jitter, jitter)
            time.sleep(max(5, sleep_s))
    finally:
        # 三处资源清理彼此独立，任一失败不影响其余
        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        try:
            if args.log:
                log_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
