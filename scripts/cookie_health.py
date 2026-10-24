#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLaDOS Cookie 健康度检查（仅标准库，退出码恒为 0，不阻断签到）.

背景：社区多个 2026 年维护的 GLaDOS 自动签到项目实测，会话 cookie 约 30 天有效
（无官方数值）。本脚本按 credentials.json 中 accounts[].created 记录的获取日期
估算 cookie 龄期，在运行日志中给出分级提醒，帮助你在失效前从容续期：
  - >25 天：黄色 ::warning::（建议近期续期）
  - >35 天：红色 ::error::（可能随时失效，尽快续期）

用法（可选增强）：更新 cookie 时在账号对象里加一个字段 "created": "YYYY-MM-DD"。
不填也能正常签到，只是没有提前提醒，失效时仍有邮件告警兜底。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

WARN_DAYS = 25   # 黄色提醒阈值
ERR_DAYS = 35    # 红色催办阈值
EST_LIFE = 30    # 社区实测参考寿命（天）


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg_path = Path(__file__).resolve().parent.parent / "config" / "credentials.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("::warning::cookie_health: 无法读取凭证文件，跳过健康度检查")
        return 0

    today = date.today()
    checked = 0
    for acc in cfg.get("accounts", []):
        if not isinstance(acc, dict) or not (acc.get("cookie") or "").strip():
            continue
        name = str(acc.get("name") or "账号")
        created_raw = str(acc.get("created") or "").strip()
        if not created_raw:
            print(
                f"::warning::账号 [{name}] 未填写 created 字段，无法估算 cookie 龄期"
                f"（可选：更新 cookie 时在 accounts 项里加 \"created\": \"YYYY-MM-DD\"）"
            )
            continue
        try:
            created = date.fromisoformat(created_raw[:10])
        except ValueError:
            print(f"::warning::账号 [{name}] created 字段格式无效({created_raw[:10]})，跳过")
            continue
        age = (today - created).days
        checked += 1
        if age >= ERR_DAYS:
            print(
                f"::error::账号 [{name}] cookie 已使用 {age} 天"
                f"（超过社区实测约 {EST_LIFE} 天寿命），可能随时失效，请尽快续期"
            )
        elif age >= WARN_DAYS:
            print(
                f"::warning::账号 [{name}] cookie 已使用 {age} 天，接近社区实测"
                f"约 {EST_LIFE} 天寿命，建议近期续期（手机登录 glados.space 重新复制即可）"
            )
        else:
            print(f"账号 [{name}] cookie 已使用 {age} 天，状态健康（预计约 {EST_LIFE - age} 天后到期）")

    if checked == 0:
        print("cookie_health: 没有可检查的 created 字段（此检查为可选的提前续期提醒，不影响签到）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
