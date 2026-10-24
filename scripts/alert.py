#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLaDOS 云端签到失败邮件告警脚本（仅 Python 标准库，零第三方依赖）.

由 GitHub Actions 工作流在失败时调用，也可本地测试:
  SMTP_HOST=smtp.qq.com SMTP_PORT=465 SMTP_USER=xxx@qq.com \
  SMTP_PASS=授权码 ALERT_TO=xxx@qq.com \
  python3 scripts/alert.py "测试告警"

环境变量:
  SMTP_HOST   SMTP 服务器（如 smtp.qq.com / smtp.163.com）
  SMTP_PORT   465(SSL) 或 587(STARTTLS)，缺省 465
  SMTP_USER   发件邮箱账号
  SMTP_PASS   邮箱授权码（非登录密码，QQ/163 均需单独开启）
  ALERT_TO    收件地址（可与发件相同）

退出码恒为 0：告警通道本身失败只记日志，不改变工作流结局（日志仍可查）。
"""

from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr


def main() -> int:
    ap = argparse.ArgumentParser(description="GLaDOS 云端签到失败邮件告警")
    ap.add_argument("--run-url", default="", help="本次工作流运行记录链接")
    ap.add_argument("message", nargs="*", default=[], help="告警内容")
    args = ap.parse_args()

    msg_text = " ".join(args.message) or "GLaDOS 云端签到工作流执行失败"

    host = (os.environ.get("SMTP_HOST") or "").strip()
    port = (os.environ.get("SMTP_PORT") or "465").strip()
    user = (os.environ.get("SMTP_USER") or "").strip()
    passwd = os.environ.get("SMTP_PASS") or ""
    to = (os.environ.get("ALERT_TO") or "").strip()

    if not (host and user and passwd and to):
        print("[alert] SMTP 环境变量不完整，跳过邮件告警（请检查 GitHub Secrets 配置）")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        "GLaDOS 云端签到告警\n"
        f"时间: {now}\n"
        f"原因: {msg_text}\n"
    )
    if args.run_url:
        body += f"运行记录: {args.run_url}\n"
    body += (
        "\n排查指引:\n"
        "1. 打开上方运行记录，查看失败步骤与日志（日志已作为 artifact 上传，保留 30 天）\n"
        "2. 若为「登录态失效」：cookie 过期或被踢，请用手机/家庭宽带浏览器登录 "
        "glados.space 重新获取，更新 secrets.GLADOS_CREDENTIALS_JSON 即恢复\n"
        "3. 若为「API 不可达」：runner 出口 IP 可能被拦截，可改期重跑或改用备用云平台\n"
        "4. 断签当天可用手机浏览器手动签到一次兜底\n"
    )

    mime = MIMEText(body, "plain", "utf-8")
    mime["Subject"] = Header(f"[GLaDOS签到告警] {msg_text[:40]}", "utf-8")
    mime["From"] = formataddr(("GLaDOS-Checkin-Bot", user))
    mime["To"] = to

    ctx = ssl.create_default_context()
    try:
        if port == "465":
            with smtplib.SMTP_SSL(host, int(port), timeout=30, context=ctx) as s:
                s.login(user, passwd)
                s.sendmail(user, [to], mime.as_string())
        else:
            with smtplib.SMTP(host, int(port), timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, passwd)
                s.sendmail(user, [to], mime.as_string())
        print(f"[alert] 告警邮件已发送至 {to}")
    except Exception as e:  # 告警通道失败不致命
        print(f"[alert] 邮件发送失败: {type(e).__name__}: {e}（工作流结局不变，请查 Actions 日志）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
