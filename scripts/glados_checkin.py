#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLaDOS (glados.space) 每日自动签到脚本.

执行流程(每个账号):
  0. 页面结构探针(best-effort): 确认积分页 /console/checkin 可达
  1. 登录态校验: GET /api/user/status, 失效(code -2)时判定"登录态失效"并提示重新配置凭证
  2. 今日已签到预检: GET /api/user/points 的 history 中是否有当天的 checkin 记录,
     命中则跳过签到, 避免重复操作
  3. Daily Punch 签到: POST /api/user/checkin {"token": <域名>}
     - code 0        -> 签到成功
     - code 1        -> 服务端确认今天已签到(重复)
     - code 4 + reason=device-mismatch -> 设备平台校验不一致, 按服务端 loginDevice 切换 UA 重试一次
     - code -2       -> 登录态失效
  4. 成功后刷新积分(GET /api/user/points)记录总积分与连续签到天数

异常处理:
  - 网络层失败(连接/超时/DNS): 指数退避重试(1s/2s/4s), 默认 3 次
  - HTTP 429/5xx: 同样纳入重试
  - 非 200 且不可重试(403/404 等): 判定接口异常
  - 响应非 JSON 或缺少 code 字段: 判定"页面结构变化/接口变更"
  - 任何未预期异常: 兜底记录为 failed, 不影响其它账号

结果记录:
  - 文本日志: logs/checkin-YYYY-MM-DD.log (按天追加)
  - 结构化历史: logs/checkin-history.jsonl (每账号每行一条 JSON)

退出码:
  0 - 全部账号成功或已签到(含 dry-run 跳过)
  1 - 存在失败账号(登录失效/设备校验失败/网络失败/接口异常)
  2 - 凭证配置错误(文件缺失/为空/无有效账号)
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = SKILL_DIR / "config"
LOG_DIR = SKILL_DIR / "logs"
DEFAULT_CONFIG = CONFIG_DIR / "credentials.json"
HISTORY_FILE = LOG_DIR / "checkin-history.jsonl"
# 持久化告警文件: 登录态失效/设备校验失败/凭证缺失时写入, 供定时任务与用户发现;
# 签到成功或已签到时自动清除
ALERT_FILE = LOG_DIR / "ALERT-需要更新Cookie.txt"

DEFAULT_DOMAIN = "glados.space"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# GLaDOS 服务端设备平台 -> 对应 User-Agent(device-mismatch 时切换重试)
PLATFORM_UA = {
    "Windows": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "macOS": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Linux": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "iPhone": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Android": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
}

# 结果状态 -> 中文说明
STATUS_TEXT = {
    "success": "签到成功",
    "already_checked": "今日已签到",
    "login_expired": "登录态失效",
    "device_mismatch": "设备校验失败",
    "network_error": "网络请求失败",
    "api_error": "接口异常/页面结构变化",
    "config_error": "凭证配置错误",
    "dry_run_skipped": "dry-run 跳过",
    "failed": "签到失败",
}

# 视为"正常结局"(不计入失败)的状态
OK_STATUS = {"success", "already_checked", "dry_run_skipped"}

CONFIG_HINT = """【如何配置/更新 GLaDOS 签到凭证】
1. 在可访问 GLaDOS 的网络环境打开浏览器登录 https://glados.space/
   (政务网电脑通常无法打开该网页, 请改用手机流量或家庭宽带)
2. 按 F12 打开开发者工具 -> 网络(Network) -> 刷新页面
3. 任选一个请求, 在请求头(Request Headers)中找到 Cookie 字段, 复制完整值
   (需包含 gld:sess=...; gld:sess.sig=... 实测这两个字段即可登录)
4. 浏览器控制台执行 navigator.userAgent, 复制 User-Agent
5. 编辑凭证文件(见报错中的路径), 填入:
   {
     "domain": "glados.space",
     "accounts": [
       {"name": "主账号", "cookie": "粘贴完整的 Cookie", "user_agent": "粘贴 UA"}
     ]
   }
6. 保存后重新运行本脚本(或等待下一个中午 12:00 的定时任务)

注意: cookie 会过期; 在其它设备/浏览器登录同一账号通常会使旧 cookie 失效。
      失效时脚本会写入告警文件 logs/ALERT-需要更新Cookie.txt 并通过定时任务通知。"""


# ---------------------------------------------------------------- 日志器


class RunLogger:
    """同时输出到 stdout 与日志文件的简易日志器."""

    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / f"checkin-{date.today().isoformat()}.log"

    def log(self, level: str, msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # 极端控制台编码兜底
            print(line.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def info(self, msg: str) -> None:
        self.log("INFO", msg)

    def warn(self, msg: str) -> None:
        self.log("WARN", msg)

    def error(self, msg: str) -> None:
        self.log("ERROR", msg)


class NetworkError(Exception):
    """网络层异常(连接失败/超时/DNS 解析失败等)."""


# ---------------------------------------------------------------- HTTP 客户端


class GladosClient:
    """GLaDOS API 客户端(仅标准库, 带重试与响应结构校验)."""

    def __init__(self, domain: str, cookie: str, user_agent: str,
                 logger: RunLogger, max_retries: int = 3, timeout: int = 30):
        self.domain = domain
        self.base_url = f"https://{domain}"
        self.cookie = cookie
        self.ua = user_agent
        self.logger = logger
        self.max_retries = max(1, max_retries)
        self.timeout = timeout

    # ---------- 底层请求 ----------

    def _headers(self, ua: str) -> dict:
        return {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/console/checkin",
            "Cookie": self.cookie,
        }

    def _raw_request(self, method: str, path: str, body=None, ua: str = None, timeout: int = None):
        """发起单次请求. 返回 (http_status, body_text); 网络层异常抛 NetworkError."""
        url = self.base_url + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(ua or self.ua), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # HTTP 错误响应也尝试读取 body(如 Cloudflare 拦截页), 便于诊断
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            return e.code, raw
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as e:
            raise NetworkError(f"{type(e).__name__}: {e}")

    def request_json(self, method: str, path: str, body=None, ua: str = None, retries: int = None):
        """带重试的 JSON 请求.

        返回 (ok, data, err_kind, err_msg):
          ok=True  -> data 为解析后的 dict(含 code 字段)
          ok=False -> err_kind 为 network_error / api_error
        """
        retries = self.max_retries if retries is None else retries
        last_err = "未知错误"
        for attempt in range(1, retries + 1):
            try:
                status, raw = self._raw_request(method, path, body, ua)
            except NetworkError as e:
                last_err = str(e)
            else:
                if status in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {status}"
                elif status != 200:
                    snippet = raw[:150].replace("\n", " ")
                    return False, None, "api_error", (
                        f"{method} {path} 返回 HTTP {status}"
                        f"(接口可能变更或被拦截): {snippet}"
                    )
                else:
                    return self._parse_json(path, raw)
            if attempt < retries:
                wait = 2 ** (attempt - 1)  # 1s, 2s, 4s 指数退避
                self.logger.warn(
                    f"{method} {path} 第 {attempt}/{retries} 次失败({last_err}), {wait}s 后重试"
                )
                time.sleep(wait)
        return False, None, "network_error", (
            f"{method} {path} 重试 {retries} 次后仍失败: {last_err}"
        )

    @staticmethod
    def _parse_json(path: str, raw: str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            snippet = raw[:150].replace("\n", " ")
            return False, None, "api_error", (
                f"{path} 响应不是合法 JSON(页面结构可能变化/被防火墙拦截): {snippet}"
            )
        if not isinstance(data, dict) or "code" not in data:
            snippet = raw[:150].replace("\n", " ")
            return False, None, "api_error", (
                f"{path} 响应结构异常(缺少 code 字段, 接口可能已变更): {snippet}"
            )
        return True, data, None, None

    # ---------- 业务接口 ----------

    def get_user_status(self):
        """GET /api/user/status. 返回 (status_dict|None, err_kind, err_msg)."""
        ok, data, kind, msg = self.request_json("GET", "/api/user/status")
        if not ok:
            return None, kind, msg
        code = data.get("code")
        if code == 0:
            d = data.get("data") or {}
            return {"left_days": d.get("leftDays")}, None, None
        if code == -2:
            return None, "login_expired", str(data.get("message") or "没有权限")
        return None, "api_error", (
            f"/api/user/status 返回 code={code}, message={data.get('message')}"
        )

    def get_points(self):
        """GET /api/user/points. 返回 (points_dict|None, err_kind, err_msg)."""
        ok, data, kind, msg = self.request_json("GET", "/api/user/points")
        if not ok:
            return None, kind, msg
        code = data.get("code")
        if code == 0:
            history = data.get("history")
            if not isinstance(history, list):
                return None, "api_error", (
                    "/api/user/points 响应缺少 history 列表(接口结构可能变化)"
                )
            return {
                "points": data.get("points"),
                "history": history,
                "streak": data.get("streak"),
            }, None, None
        if code == -2:
            return None, "login_expired", str(data.get("message") or "没有权限")
        return None, "api_error", (
            f"/api/user/points 返回 code={code}, message={data.get('message')}"
        )

    def checkin(self, ua: str = None):
        """POST /api/user/checkin. 返回结果 dict(含 status 字段)."""
        ok, data, kind, msg = self.request_json(
            "POST", "/api/user/checkin", {"token": self.domain}, ua=ua
        )
        if not ok:
            return {"status": kind, "message": msg}
        code = data.get("code")
        message = str(data.get("message") or "")
        if code == 0:
            return {
                "status": "success",
                "points": data.get("points"),
                "streak": data.get("streak"),
                "message": message,
            }
        if code == 1:
            return {"status": "already_checked", "message": message}
        if code == 4 and data.get("reason") == "device-mismatch":
            return {
                "status": "device_mismatch",
                "message": message,
                "loginDevice": data.get("loginDevice"),
                "currentDevice": data.get("currentDevice"),
            }
        if code == -2:
            return {"status": "login_expired", "message": message}
        return {"status": "failed", "code": code, "message": message}

    def probe_console_page(self):
        """页面结构探针(best-effort): 确认积分页 /console/checkin 可达且结构未变.

        只告警不中断: 探针失败不影响 API 签到. 探针用较短超时, 避免拖慢整体流程.
        """
        try:
            status, raw = self._raw_request("GET", "/console/checkin", timeout=15)
        except NetworkError as e:
            self.logger.warn(f"页面探针失败(不影响签到): {e}")
            return
        if status != 200 or ("static/js" not in raw and "app.bundle" not in raw):
            self.logger.warn(
                f"页面探针异常: /console/checkin 返回 HTTP {status} 且结构不符预期, "
                "积分页结构可能已变化(不影响本次 API 签到, 但建议关注)"
            )


# ---------------------------------------------------------------- 工具函数


def normalize_cookie(raw) -> str:
    """规范化 cookie 字符串: 去掉换行/多余空白."""
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())


def fmt_num(value):
    """数值美化: 7.0/"7.0000000000000000" -> 7, 12.5 -> 12.5.

    接口部分数值字段以字符串形式返回(如 "7.0000000000000000"), 统一转为整数展示.
    """
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def parse_record_date(value):
    """解析积分历史记录的 time 字段为本地日期.

    支持 unix 秒/毫秒时间戳与 ISO 8601 字符串; 无法解析返回 None.
    """
    if value is None or isinstance(value, bool):
        return None
    ts = None
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            ts = float(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone()  # 转为本地时区再取日期
                return dt.date()
            except ValueError:
                return None
    if ts is None:
        return None
    if ts > 1e12:  # 毫秒时间戳
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts).date()
    except (OverflowError, OSError, ValueError):
        return None


def today_checked_in(history: list):
    """检查 history 中是否有当天的签到记录(business 含 checkin).

    返回 (bool, record|None). 说明: 与网页端判定逻辑一致(按本地日期比较),
    服务端 code=1 的重复判定作为最终兜底.
    """
    today = date.today()
    for rec in history:
        if not isinstance(rec, dict):
            continue
        business = str(rec.get("business") or "")
        if "checkin" not in business.lower():
            continue
        if parse_record_date(rec.get("time")) == today:
            return True, rec
    return False, None


def load_config(path: Path, domain_override: str = None):
    """加载凭证配置. 返回 (config|None, error_message|None)."""
    if not path.exists():
        return None, f"凭证文件不存在: {path}\n{CONFIG_HINT}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return None, f"凭证文件解析失败({path}): {e}\n{CONFIG_HINT}"
    if not isinstance(data, dict):
        return None, f"凭证文件格式错误: 顶层应为 JSON 对象({path})\n{CONFIG_HINT}"

    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return None, f"凭证文件中没有 accounts 账号列表({path})\n{CONFIG_HINT}"

    valid = []
    for i, acc in enumerate(accounts, 1):
        if not isinstance(acc, dict):
            continue
        cookie = normalize_cookie(acc.get("cookie"))
        if not cookie:
            continue
        valid.append({
            "name": str(acc.get("name") or f"账号{i}"),
            "cookie": cookie,
            "user_agent": str(acc.get("user_agent") or DEFAULT_UA),
        })
    if not valid:
        return None, (
            f"所有账号的 cookie 均为空, 请编辑 {path} 填入凭证\n{CONFIG_HINT}"
        )

    domain = domain_override or str(data.get("domain") or DEFAULT_DOMAIN)
    return {"domain": domain, "accounts": valid}, None


def write_alert(logger: RunLogger, account: str, reason: str) -> None:
    """写入持久化告警文件(登录态失效/设备校验失败时).

    告警文件是定时任务向用户发出通知的实体依据: 即使聊天输出被折叠,
    自动化执行时也能通过该文件发现待处理问题. 内容含更新 cookie 的完整步骤.
    """
    lines = [
        "=" * 56,
        "⚠ GLaDOS 每日签到 - 需要手动更新 Cookie",
        "=" * 56,
        f"告警时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"账号:     {account}",
        f"原因:     {reason}",
        "",
        "定时任务(每天 12:00)检测到登录态失效, 本次未完成签到。",
        "请在可访问 GLaDOS 的网络环境(政务网电脑通常无法打开该网页,",
        "请改用手机流量或家庭宽带)按以下步骤更新凭证:",
        "",
        "1. 浏览器登录 https://glados.space/",
        "2. F12 -> 网络(Network) -> 刷新页面",
        "3. 任选一个请求, 复制请求头(Request Headers)中 Cookie 完整值",
        "   (含 gld:sess=...; gld:sess.sig=... 两个字段即可)",
        "4. 控制台执行 navigator.userAgent 复制 User-Agent",
        f"5. 编辑凭证文件并保存: {DEFAULT_CONFIG}",
        "",
        "保存后无需重启, 下一个中午 12:00 的定时任务会自动恢复签到。",
        "=" * 56,
    ]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ALERT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.error(f"已写入告警文件: {ALERT_FILE}")
    except OSError as e:
        logger.warn(f"告警文件写入失败({e}), 请留意上方日志中的失效提示")


def clear_alert() -> None:
    """签到成功或已签到时清除告警文件(失效已恢复)."""
    try:
        ALERT_FILE.unlink()
    except OSError:
        pass


def make_result(account: str, status: str, message: str, logger: RunLogger, extra: dict = None):
    """构造结果记录并输出日志."""
    text = STATUS_TEXT.get(status, status)
    if status in OK_STATUS:
        logger.info(f"账号 [{account}] 结果: {text} - {message}")
    else:
        logger.error(f"账号 [{account}] 结果: {text} - {message}")
    return {
        "time": datetime.now().isoformat(timespec="seconds"),
        "account": account,
        "status": status,
        "status_text": text,
        "message": message,
        "extra": extra or {},
    }


# ---------------------------------------------------------------- 单账号签到流程


def process_account(account: dict, domain: str, logger: RunLogger, args) -> dict:
    """处理单个账号的完整签到流程."""
    name = account["name"]
    logger.info(f"----- 账号 [{name}] 开始 -----")
    client = GladosClient(
        domain, account["cookie"], account["user_agent"], logger,
        max_retries=args.retries, timeout=args.timeout,
    )

    # 0) 页面结构探针(默认关闭: 纯接口打卡, 不依赖打开网页, 适配政务网等受限网络;
    #    需要额外校验积分页结构时可传 --probe 开启)
    if args.probe:
        client.probe_console_page()

    # 1) 登录态校验
    status, kind, msg = client.get_user_status()
    if kind == "login_expired":
        write_alert(logger, name, f"登录态失效({msg})")
        return make_result(name, "login_expired",
                           f"登录态失效({msg}), 需要重新配置凭证", logger)
    if kind:
        return make_result(name, kind, msg, logger)
    logger.info(f"登录态有效(剩余 {fmt_num(status['left_days'])} 天)")

    # 2) 今日已签到预检(避免重复操作)
    points, kind, msg = client.get_points()
    if kind == "login_expired":
        write_alert(logger, name, f"登录态失效({msg})")
        return make_result(name, "login_expired",
                           f"登录态失效({msg}), 需要重新配置凭证", logger)
    if kind:
        return make_result(name, kind, msg, logger)
    logger.info(f"当前积分 {fmt_num(points['points'])}, 连续签到 {fmt_num(points['streak'])} 天")

    checked, rec = today_checked_in(points["history"])
    if checked:
        logger.info(f"预检: 今天已签到({rec.get('business')}), 跳过签到避免重复操作")
        clear_alert()
        return make_result(
            name, "already_checked",
            f"今天已签到, 无需重复操作(当前积分 {fmt_num(points['points'])}, "
            f"连续签到 {fmt_num(points['streak'])} 天)",
            logger,
            extra={"points_total": fmt_num(points["points"]), "streak": fmt_num(points["streak"])},
        )

    if args.dry_run:
        logger.info("dry-run 模式: 今天尚未签到, 跳过实际签到请求")
        return make_result(name, "dry_run_skipped",
                           "dry-run: 今天尚未签到(未执行签到请求)", logger)

    # 3) Daily Punch 签到
    result = client.checkin()

    # 3.1 设备平台校验不一致 -> 按服务端 loginDevice 切换 UA 重试一次
    if result["status"] == "device_mismatch":
        login_device = result.get("loginDevice")
        retry_ua = PLATFORM_UA.get(str(login_device))
        logger.warn(
            f"设备校验不一致(服务端记录登录设备: {login_device}, "
            f"当前请求设备: {result.get('currentDevice')})"
        )
        if retry_ua and retry_ua != account["user_agent"]:
            logger.info(f"按服务端登录设备({login_device})切换 User-Agent 后重试签到")
            result = client.checkin(ua=retry_ua)
        if result["status"] == "device_mismatch":
            write_alert(logger, name,
                        f"设备校验失败(UA 平台与登录设备不一致, loginDevice={login_device})")
            return make_result(
                name, "device_mismatch",
                "设备校验失败: 签到请求的 UA 平台与登录设备不一致"
                f"({result.get('message')}). 请在常用浏览器登录 glados.space "
                "手动签到一次, 并更新凭证中的 cookie 与 user_agent",
                logger, extra={"loginDevice": login_device},
            )

    if result["status"] == "login_expired":
        write_alert(logger, name, f"登录态失效({result.get('message')})")
        return make_result(name, "login_expired",
                           f"登录态失效({result.get('message')}), 需要重新配置凭证", logger)

    if result["status"] == "already_checked":
        logger.info(f"服务端确认今日已签到: {result.get('message')}")
        clear_alert()
        return make_result(name, "already_checked",
                           f"服务端确认今天已签到({result.get('message')})", logger)

    if result["status"] != "success":
        return make_result(name, result["status"], result.get("message") or "签到失败",
                           logger, extra={"code": result.get("code")})

    # 4) 签到成功, 刷新积分信息
    clear_alert()
    logger.info(f"签到成功! 本次积分 +{fmt_num(result.get('points'))}, {result.get('message')}")
    extra = {"points_gained": fmt_num(result.get("points")), "streak": fmt_num(result.get("streak"))}
    points_after, kind2, msg2 = client.get_points()
    if not kind2:
        extra["points_total"] = fmt_num(points_after["points"])
        extra["streak"] = fmt_num(points_after.get("streak") or result.get("streak"))
        logger.info(f"签到后总积分 {fmt_num(points_after['points'])}, "
                    f"连续签到 {fmt_num(points_after.get('streak'))} 天")
    else:
        logger.warn(f"签到后积分刷新失败({msg2}), 不影响本次签到结果")
    return make_result(name, "success",
                       f"签到成功, 本次 +{fmt_num(result.get('points'))} 积分", logger, extra=extra)


# ---------------------------------------------------------------- 主流程


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="GLaDOS (glados.space) 每日自动签到")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="凭证配置文件路径")
    p.add_argument("--domain", default=None,
                   help=f"覆盖配置中的域名(默认 {DEFAULT_DOMAIN})")
    p.add_argument("--retries", type=int, default=3, help="网络请求重试次数(默认 3)")
    p.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数(默认 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="只检查登录态与今日签到情况, 不实际签到")
    p.add_argument("--probe", action="store_true",
                   help="启用页面结构探针(GET /console/checkin); 默认关闭, "
                        "保持纯接口打卡以适配政务网等无法打开网页的环境")
    p.add_argument("--account", default=None, help="只处理指定名称的账号")
    return p.parse_args(argv)


def main(argv=None) -> int:
    # Windows 控制台 UTF-8 输出
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = parse_args(argv)
    logger = RunLogger(LOG_DIR)
    logger.info("=" * 64)
    logger.info("GLaDOS 每日自动签到开始")

    # 加载凭证
    config, err = load_config(Path(args.config), args.domain)
    if err:
        logger.error(f"凭证配置错误: {err}")
        write_alert(logger, "全部账号", f"凭证配置错误({err.splitlines()[0]})")
        return 2

    accounts = config["accounts"]
    if args.account:
        accounts = [a for a in accounts if a["name"] == args.account]
        if not accounts:
            logger.error(f"未找到名称为 [{args.account}] 的账号")
            return 2

    logger.info(f"域名 {config['domain']}, 待处理账号 {len(accounts)} 个, "
                f"重试 {args.retries} 次, 超时 {args.timeout}s"
                + ("(dry-run)" if args.dry_run else ""))

    # 逐账号执行(单账号异常不影响其它账号)
    results = []
    for acc in accounts:
        try:
            results.append(process_account(acc, config["domain"], logger, args))
        except Exception as e:  # 未预期异常兜底
            logger.error(f"账号 [{acc['name']}] 发生未预期异常: "
                         f"{type(e).__name__}: {e}")
            results.append(make_result(
                acc["name"], "failed",
                f"未预期异常: {type(e).__name__}: {e}", logger))

    # 汇总输出
    n_ok = sum(1 for r in results if r["status"] in OK_STATUS)
    logger.info("-" * 64)
    logger.info("本次签到汇总:")
    for r in results:
        logger.info(f"  [{r['account']}] {r['status_text']}: {r['message']}")
    logger.info(f"合计 {len(results)} 个账号: 成功/已签到 {n_ok}, 失败 {len(results) - n_ok}")

    # 结构化历史记录(追加)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warn(f"写入历史记录失败: {e}")

    # 告警状态提示(供定时任务与用户快速判断是否需要更新 cookie)
    if ALERT_FILE.exists():
        logger.error(f"⚠ 存在待处理告警, 需要手动更新 Cookie: {ALERT_FILE}")
    else:
        logger.info("无待处理告警(登录态正常)")

    logger.info(f"日志文件: {logger.log_file}")
    logger.info("GLaDOS 每日自动签到结束")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
