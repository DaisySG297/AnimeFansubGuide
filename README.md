# AnimeFansubGuide

本地运行的番剧订阅助手：本季实况 + 订阅审计 + 更新提醒 + 订阅推送（一键写进 ani-rss）。

前端是一个单文件页面（`anime-fansub-guide.html`），由一个本地 Python 服务
（`app/anime_fansub_server.py`）托管，服务同时作为 ani-rss 的代理桥：
浏览器不直连 ani-rss，**apiKey 只留在服务端**。

## 功能

| 视图 | 说明 |
|---|---|
| 订阅推送 | 搜索番剧（蜜柑）→ 选主/备字幕组 → 生成计划 → 写入 ani-rss |
| 本季实况 | 本季新番 × 字幕组发布动态（来自 `season-data.js`） |
| 订阅审计 | 有人在发片但没订阅的番 / 已订阅但缺备用组；支持「不看」忽略 |
| 更新提醒 | 今日播出 + Emby 在库对照，可推送到钉钉 |
| 订阅管理 | 老订阅批量退订（删除前自动镜像去重记录） |

## 快速开始

```bash
# 1) 准备配置：复制模板并填写 ani-rss 地址与 apiKey
cp app/config.example.json app/config.json

# 2) 启动（默认监听 127.0.0.1:7788）
python app/anime_fansub_server.py

# 3) 浏览器打开
#    http://127.0.0.1:7788/
```

ani-rss 的 apiKey 在其数据目录的 `config.v2.json` 里（字段 `apiKey`），
请求头用 `X-Api-Key: <apiKey>`。

## 目录

```
anime-fansub-guide.html      前端单页（所有视图 + 样式 + 脚本）
app/anime_fansub_server.py   本地服务：静态托管 + ani-rss 代理 + 审计/推送
app/reminder.py              更新提醒（Emby 在库对照 / 钉钉推送）
app/config.example.json      配置模板
scripts/build-season.js      聚合本季快照 → season-data.js
scripts/refresh-season.ps1   定时抓取（本季快照数据源）
scripts/build-exe.ps1        PyInstaller 打包成单文件 exe
season-data.js               本季快照数据（由脚本生成，未随仓库提供）
```

## 设计要点

- **凭据不外泄**：ani-rss apiKey / qBittorrent API Key 只存在于服务端 `config.json`
- **安全闸门**：本地服务只绑 `127.0.0.1`，校验 `Host` 头，POST 校验 `Origin`
- **删除保护**：ani-rss 的 `deleteAni` 会无条件清掉下载去重记录，
  退订前先跑 `ani_rss_tool.py mirror` 备份，避免重订时全量重下
- **蜜柑搜索阶梯**：长标题 / 带季数后缀 / 中英混排的标题在蜜柑常常 0 命中，
  服务端按「原名 → 去季数 → 剥尾部语气词 → 中文词段 / 英文词段 → 尾 4·首 4 字」
  逐级回退并合并去重

## 免责声明

本项目只做 RSS 订阅与元数据聚合，不存储、不分发任何影视资源。
字幕组评价为社区口碑的归纳，不代表官方评价。
