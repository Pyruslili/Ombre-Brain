# Rhythm — 外场节律

嘉嘉的 phone / iWatch 活动 + 主动 Bark 推送，统一在 Nocturne（Ombre-Brain）这一层。

## 组件

| 入口 | 作用 |
|------|------|
| `POST /api/rhythm/event` | 快捷指令 / iWatch 上报 |
| `GET /api/rhythm/read` | 读快照 |
| `POST /api/rhythm/push` | 主动 Bark |
| MCP `rhythm` | `action=read\|push` |
| undercurrent | 有数据时附带 `Rhythm` 摘要 |
| heartbeat_bridge | 投递 Free Roam 念头时注入 `Rhythm：…`；有 Inner Signal 时 Bark |

## 环境变量

- `OMBRE_RHYTHM_TOKEN`（或 `RHYTHM_TOKEN`）：写入/推送鉴权；未设则开放（仅本机调试）
- `BARK_KEY` 或 `~/.bark_device_key`：Bark device key；也接受完整的 `https://api.day.app/<key>/...` URL（只提取 Bark 域名下的首段 key）
- `BARK_ICON_URL`：可选图标；默认使用 `docs/assets/nox-bark-avatar.png`
- 本地心跳：`NOX_RHYTHM_BARK=0` 可关掉手机弹窗（仍注入 Rhythm 行）

## iOS 快捷指令（一 app 一自动化，推荐）

1. 自动化 → App 已打开 → 勾选「小红书」
2. 获取 URL 内容：
   - URL：`https://ombre-brain-jyfz.onrender.com/api/rhythm/event`
   - 方法：POST
   - 请求头：`Content-Type: application/json`；若配置了 token 再加 `X-Rhythm-Token: <token>`
   - 请求体：

```json
{"app":"小红书","event":"open","source":"phone"}
```

3. 微信 / 抖音等同理，只改 `app` 字段。

局域网可打本地代理（需 Nocturne auth token）：

```
POST http://<mac-lan-ip>:8765/rhythm/event
Header: X-Auth-Token: <nocturne token>
Body: {"app":"小红书"}
```

## iWatch（以后）

```json
{"source":"watch","kind":"hr","value":72,"event":"sample"}
```

## MCP

```
rhythm(action="read")
rhythm(action="push", title="Nox", body="一点四十七。小红书。回来。")
```

**不要**用 push 绑聊天回复；只在主动想她时调用。
