# GLaDOS 每日自动签到（GitHub Actions 云端版）

每天北京时间 12:00 自动签到 GLaDOS（glados.space），无需本地电脑常开，月成本 ¥0。
含**月度保活**（自动延长定时任务有效期）+ cookie 龄期提醒 + 多层重试 + 日志回写仓库。

## 运行机制

| 项 | 说明 |
|---|---|
| 定时签到 | `.github/workflows/glados-checkin.yml`，cron `0 4 * * *`（UTC）= 北京 12:00；push 到 main 也会触发（机器人回写日志的 push 不会再触发，无死循环） |
| 保活 | `.github/workflows/keepalive.yml` 每月 1 日调用 enable API 重置 60 天无活动计时器，防止定时任务被自动停用 |
| 凭证 | GitHub Secrets `GLADOS_CREDENTIALS_JSON`（公开仓库模式，凭证不入仓库；workflow 亦兼容私有仓库文件模式） |
| 日志 | 每次运行把 `logs/` 回写到仓库 + artifact 保留 30 天，打开仓库 logs 目录即可看结果 |
| 重试 | 脚本内 1s/2s/4s 退避 + 外层整跑 3 次（60s/120s 退避）；登录态失效参与重试（服务端有瞬时 -2 误报） |
| 告警 | 失败时可选邮件告警（需配置 SMTP_* Secrets；Gmail 需"应用专用密码"，普通密码不可用） |

## 更新 Cookie（约 30 天一次，社区实测会话寿命）

1. **固定用一个浏览器**登录 https://glados.space/（在其它设备登录会使旧会话失效）
2. F12 → 网络 → 刷新 → 任选请求 → 复制请求头 Cookie 完整值（`gld:sess` + `gld:sess.sig`）
3. 控制台执行 `navigator.userAgent` 复制 UA
4. 编辑本地 `config/credentials.json`：更新 cookie、user_agent，**把 `created` 改为当天日期** → 复制全文
5. 粘贴到 仓库 Settings → Secrets and variables → Actions → `GLADOS_CREDENTIALS_JSON` → Update secret
6. Actions 页手动 Run workflow 验证（不验证也行，次日 12:00 自动生效）

## 文件说明

| 文件 | 用途 |
|---|---|
| `scripts/glados_checkin.py` | 签到主脚本（与本地 skill 同源，零改动，纯标准库零依赖） |
| `scripts/cookie_health.py` | cookie 龄期检查（>25 天黄色提醒、>35 天红色催办，不阻断） |
| `scripts/alert.py` | 失败邮件告警（纯标准库 SMTP） |
| `config/credentials.json` | 签到凭证（仅存本地与 GitHub Secrets，**不入仓库**；含 created 龄期字段） |
| `config/credentials.example.json` | 凭证格式模板 |
