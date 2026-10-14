# -*- coding: utf-8 -*-
"""
番剧更新提醒助手 —— 基于 AniBT 季度新番表（唯一数据源）
=========================================================
功能：
  1. 抓取 AniBT 季度番表（默认自动跟随当前季），解析每周更新时间
  2. 依据首播日期 + 日本深夜档时间记法，计算每话的北京时间更新时刻与集数
  3. 到点前 N 分钟推送钉钉机器人提醒；每天固定时刻发送"今日更新汇总"
  4. 内置本地可视化面板（浏览器打开），含统计图表 / 倒计时 / 订阅管理 / 配置界面
  5. 单文件 exe，双击即用，数据与配置保存在 exe 同目录

用法：
  python main.py                 # 启动面板 + 后台提醒
  python main.py --port 8899     # 指定面板端口
  python main.py --once          # 只在控制台打印"今天更新什么"，不启动服务
  python main.py --test-dingtalk # 发送一条钉钉测试消息并退出

依赖：仅 Python 标准库（3.8+），无需 pip 安装任何包。
"""

import base64
import difflib
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

# ---------------------------------------------------------------------------
# 路径与日志
# ---------------------------------------------------------------------------
# 作为 AnimeFansubGuide 的内嵌引擎运行：数据目录由服务端通过环境变量指定，
# 文件名加 reminder- 前缀，避免与服务端自己的 config.json 等文件冲突。
APP_DIR = os.environ.get("REMINDER_DATA_DIR") or (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(APP_DIR, "reminder-config.json")
STATE_PATH = os.path.join(APP_DIR, "reminder-state.json")
EMBY_MAP_PATH = os.path.join(APP_DIR, "reminder-emby-map.json")
GROUPS_CACHE_PATH = os.path.join(APP_DIR, "reminder-groups-cache.json")
LOG_PATH = os.path.join(APP_DIR, "reminder.log")

try:  # Windows 控制台 UTF-8 兜底
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("anime-reminder")

JST = timezone(timedelta(hours=9))   # 日本时间
BJT = timezone(timedelta(hours=8))   # 北京时间
WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
# 到点提醒的补发窗口（秒）：开播后这个时间内仍会补推一次，超时不再补，
# 避免进程重启后把早已开播的集数当成新提醒刷出来。
AT_TIME_GRACE = 10 * 60

DEFAULT_CONFIG = {
    "dingtalk_webhook": "",
    "dingtalk_secret": "",
    "notify_times": ["09:00"],   # 每日汇总的推送时刻（北京时间），可写多个 ["09:00","18:30"]
    "notify_digest": True,       # 每日汇总开关：False = 完全关闭汇总推送
    "advance_minutes": 0,        # 提醒时机：0 = 到点提醒一次（默认，不提前）；正数 = 提前 N 分钟
    "subscribe_mode": "all",     # all=提醒全部; selected=仅提醒订阅的; anirss=跟随 ani-rss 订阅
    "subscribe_ids": [],
    "emby_url": "",              # Emby 服务器地址，如 http://192.168.1.10:8096
    "emby_api_key": "",          # Emby API 密钥（控制台 -> 高级 -> API 密钥）
    "tmdb_api_key": "",          # TMDB API Key (v3)，用于番剧名称对齐
    "tmdb_proxy": "",            # TMDB 代理，如 http://127.0.0.1:10808（TMDB 被墙时必填）
    "anibt_season": "",          # AniBT 季度（如 2026秋），留空自动跟随当前季
    "notify_release": False,     # 字幕组新资源发布提醒（依据 AniBT 资源进度）
    "release_check": True,       # 是否后台检测字幕组资源进度
    # ---- ani-rss 联动 ----
    "anirss_url": "",            # ani-rss 地址，如 http://127.0.0.1:7789
    "anirss_api_key": "",        # ani-rss 设置页里的 apiKey（走 X-Api-Key 鉴权）
    "anirss_sync": False,        # 只读对齐：拉取 ani-rss 订阅并在卡片打标（不写 ani-rss）
    "anirss_allow_subscribe": False,  # 允许从面板「订阅到 ani-rss」（写操作，默认关闭）
    "anirss_sync_minutes": 10,   # ani-rss 订阅缓存刷新间隔（分钟）
    "port": 8899,
}


# ---------------------------------------------------------------------------
# 配置 / 状态读写
# ---------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict):
            merged = dict(default)
            merged.update({k: v for k, v in data.items() if v is not None})
            return merged
        return data
    except Exception:
        return json.loads(json.dumps(default))


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


CONFIG = load_json(CONFIG_PATH, DEFAULT_CONFIG)
# 2026-10-03：数据源统一为 AniBT，清掉旧配置里残留的長門番堂（yuc.wiki）键，
# 防止旧 config.json 把已删除的数据源又带回来。
for _legacy_key in ("season_url", "data_source"):
    CONFIG.pop(_legacy_key, None)
STATE = load_json(STATE_PATH, {"sent_advance": [], "digest_done": {}, "seen_release": {}})
STATE.setdefault("seen_release", {})
EMBY_MAP = load_json(EMBY_MAP_PATH, {})   # 番剧标题 -> Emby/TMDB 对齐结果缓存
LOCK = threading.Lock()


def save_config():
    with LOCK:
        save_json(CONFIG_PATH, CONFIG)


def save_state():
    with LOCK:
        # 只保留最近 30 天的去重记录，防止无限膨胀
        cutoff = datetime.now(BJT) - timedelta(days=30)
        STATE["sent_advance"] = [k for k in STATE.get("sent_advance", [])
                                 if k.rsplit("|", 1)[-1] >= cutoff.strftime("%Y%m%d")]
        save_json(STATE_PATH, STATE)


# ---------------------------------------------------------------------------
# HTTP 通用
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------------------
# ani-rss 联动（只读对齐 + 一键订阅）
#
# 鉴权：ani-rss 用请求头 X-Api-Key: <config.v2.json 里的 apiKey>。
#       它的 Authorization 头属于自身 JWT 会话体系，本工具不使用。
# 响应：ani-rss 的 HTTP 状态码恒为 200，成败必须读 body 里的 code 字段。
# 对齐：ani-rss 每条订阅带 bgmUrl=https://bgm.tv/subject/<id>，
#       与本面板的 bgm_id 精确对应，不需要名称模糊匹配。
# 写入：绝不手工拼 Ani 字段——一律经 /rssToAni 生成完整对象后再交给 /addAni。
#       残缺对象会被 ani-rss 落盘（曾污染 ani.v2.json 并导致 listAni/日历 双 500）。
# ---------------------------------------------------------------------------
ANIRSS_LOCK = threading.Lock()
_ANIRSS = {"ts": 0.0, "by_bgm": {}, "by_title": {}, "err": "", "count": 0}
_ANIRSS_BGM_RE = re.compile(r"bgm\.tv/subject/(\d+)")


def anirss_base():
    return (CONFIG.get("anirss_url") or "").strip().rstrip("/")


def anirss_key():
    return (CONFIG.get("anirss_api_key") or "").strip()


def anirss_configured():
    return bool(anirss_base() and anirss_key())


def anirss_api(path, payload=None, timeout=20):
    """调用 ani-rss 开放接口。返回 (ok, data 或 错误说明)。"""
    base = anirss_base()
    if not base:
        return False, "未配置 ani-rss 地址"
    body = None
    headers = {"X-Api-Key": anirss_key(), "User-Agent": UA}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(base + path, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return False, "请求 ani-rss 失败：%s" % e
    if not isinstance(res, dict):
        return False, "ani-rss 返回格式异常"
    if res.get("code") != 200:
        return False, str(res.get("message") or ("code=%s" % res.get("code")))
    return True, res.get("data")


def anirss_bgm_key(anime):
    """取番剧的 Bangumi 条目 ID（与 ani-rss 的对齐主键）。"""
    for f in ("bgm_id", "bgmId"):
        v = anime.get(f)
        if v:
            digits = re.sub(r"\D", "", str(v))
            if digits:
                return digits
    m = _ANIRSS_BGM_RE.search(str(anime.get("bgm_url") or ""))
    return m.group(1) if m else None


def anirss_fetch_index():
    """拉取 ani-rss 订阅并建立索引。返回 (ok, 数量或错误说明)。"""
    ok, data = anirss_api("/api/listAni", {})
    if not ok:
        return False, data
    items = []
    if isinstance(data, dict):
        for grp in (data.get("weekList") or []):
            items.extend(grp.get("items") or [])
    by_bgm, by_title = {}, {}
    for it in items:
        if not isinstance(it, dict):
            continue
        rec = {
            "id": it.get("id"),
            "title": it.get("title") or "",
            "enable": bool(it.get("enable")),
            "ep": it.get("currentEpisodeNumber"),
            "total": it.get("totalEpisodeNumber"),
            "subgroup": it.get("subgroup") or "",
            "release_date": it.get("releaseDate"),
            "score": it.get("score"),
            "image": it.get("image") or "",
        }
        m = _ANIRSS_BGM_RE.search(str(it.get("bgmUrl") or ""))
        if m:
            by_bgm[m.group(1)] = rec
        t = norm_title(str(it.get("title") or ""))
        if t:
            by_title.setdefault(t, rec)
    with ANIRSS_LOCK:
        _ANIRSS.update(ts=time.time(), by_bgm=by_bgm, by_title=by_title,
                       err="", count=len(items))
    return True, len(items)


def anirss_ensure_cache(force=False):
    """按 anirss_sync_minutes 保持订阅缓存新鲜。未配置时直接返回 False。"""
    if not anirss_configured():
        return False
    try:
        minutes = max(1, int(CONFIG.get("anirss_sync_minutes") or 10))
    except Exception:
        minutes = 10
    with ANIRSS_LOCK:
        fresh = _ANIRSS["ts"] and (time.time() - _ANIRSS["ts"]) < minutes * 60
    if not force and fresh:
        return True
    ok, info = anirss_fetch_index()
    if not ok:
        with ANIRSS_LOCK:
            _ANIRSS["err"] = str(info)
        log.warning("ani-rss 订阅拉取失败：%s", info)
    return ok


def anirss_lookup(anime):
    """在订阅缓存里查找该番的 ani-rss 记录，未命中返回 None。"""
    with ANIRSS_LOCK:
        by_bgm = dict(_ANIRSS["by_bgm"])
        by_title = dict(_ANIRSS["by_title"])
    k = anirss_bgm_key(anime)
    rec = by_bgm.get(k) if k else None
    if rec is None:
        rec = by_title.get(norm_title(anime.get("title") or ""))
    return rec


def anirss_state_for(anime):
    """该番剧在 ani-rss 的订阅状态；未开启同步返回 None。"""
    if not CONFIG.get("anirss_sync"):
        return None
    rec = anirss_lookup(anime)
    if rec is None:
        return {"subscribed": False}
    out = dict(rec)
    out["subscribed"] = True
    return out


def anirss_mikan_search(text):
    """按关键词搜索蜜柑番剧。返回 (ok, 候选列表 [{url,title,cover}])。

    实测返回结构：data.weeks[].items[]，条目字段 {cover,url,exists,score,title}。
    """
    ok, data = anirss_api("/api/mikan?text=%s" % urllib.parse.quote(str(text)), {})
    if not ok:
        return False, data
    if not isinstance(data, dict):
        return False, "蜜柑搜索返回格式异常"
    out = []
    for w in (data.get("weeks") or []):
        for it in (w.get("items") or []):
            if isinstance(it, dict) and it.get("url"):
                out.append({
                    "url": it.get("url"),
                    "title": it.get("title") or "",
                    "cover": it.get("cover") or "",
                })
    return True, out


def anirss_mikan_groups(bangumi_url):
    """取蜜柑某番剧的字幕组列表。返回 (ok, [{label,rss,bgm_id}])。

    实测字段为 label（字幕组名）/ rss（订阅地址）/ bgmUrl（Bangumi 条目）。
    """
    ok, data = anirss_api("/api/mikanGroup?url=%s" % urllib.parse.quote(str(bangumi_url)), {})
    if not ok:
        return False, data
    if not isinstance(data, list):
        return False, "字幕组列表返回格式异常"
    out = []
    for g in data:
        if not isinstance(g, dict):
            continue
        m = _ANIRSS_BGM_RE.search(str(g.get("bgmUrl") or ""))
        out.append({
            "label": g.get("label") or g.get("subgroup") or "",
            "rss": g.get("rss") or g.get("url") or "",
            "bgm_id": m.group(1) if m else None,
        })
    return True, out


# 副标题分隔符（日式波浪号 / 中文冒号 / 破折号 / 间隔号 / 左括号）——
# 缺了中文全角冒号 `：` 会导致「小书痴的下克上：为了成为…」整条链路 0 候选。
_ANIRSS_SUB_SEPS = (r"[〜~～]", r"[：:]", r"[—–]", r"[·・]", r"[（(\[【]")
_ANIRSS_TAIL_RE = re.compile(
    r"\s*(第\s*[0-9一二三四五六七八九十]+\s*(?:部分|季|期|部|章)|最终章|剧场版|特别篇|OAD|OVA|ONA|"
    r"Season\s*\d+)\s*$", re.IGNORECASE)


def anirss_search_keywords(title, limit=6):
    """为蜜柑搜索生成候选关键词（从精确到宽泛）。

    实测：蜜柑对过长标题搜不到 —— 带「〜副标题〜」或「：副标题」的完整标题
    均返回 0 候选，必须逐级回退到主标题，否则整条订阅链路卡在第一步。
    """
    t = str(title or "").strip()
    out = []
    if not t:
        return out
    out.append(t)
    for pat in _ANIRSS_SUB_SEPS:
        head = re.split(pat, t, maxsplit=1)[0].strip()
        if head:
            out.append(head)
    for base in list(out):
        b = _ANIRSS_TAIL_RE.sub("", base).strip()
        if b:
            out.append(b)
    seen, res = set(), []
    for k in out:
        if k and k not in seen:
            seen.add(k)
            res.append(k)
        if len(res) >= limit:
            break
    return res


def anirss_resolve(title, bgm_id=None, prefer_subgroups=(), group_budget=12):
    """把番剧解析成蜜柑 RSS 地址。

    对齐优先级：
      ① 候选字幕组的 bgmUrl 与番剧 bgm_id 完全一致 —— 精确，实测必需
         （纯标题相似度会把《小书痴 第4季》错配到第1季，仅 0.500）
      ② 退回标题相似度最高的候选，并在其字幕组中优先选面板已知的字幕组

    搜索关键词走 anirss_search_keywords 阶梯并**逐个早退**：任一关键词的候选
    中出现 bgm_id 精确命中即刻返回，避免把每个关键词的候选都拉一遍字幕组。
    group_budget 限制字幕组接口的总调用次数，兜住最坏延迟。
    返回 (ok, {rss,label,bgm_id,matched_by,bangumi_title} 或 错误说明)。
    """
    want = norm_title(title)

    def pick(group_list):
        def junk(g):
            lbl = str(g.get("label") or "")
            return any(k in lbl for k in ("生肉", "不明", "合集", "无字幕"))

        for gg in group_list:
            if gg["rss"] and gg["label"] and not junk(gg) \
                    and any(gg["label"] == str(p) for p in prefer_subgroups):
                return gg
        for gg in group_list:
            if gg["rss"] and not junk(gg):
                return gg
        return next((gg for gg in group_list if gg["rss"]), None)

    keywords = anirss_search_keywords(title)
    if not keywords:
        return False, "该番剧没有可用标题"

    fallback = None
    tried = set()
    budget = group_budget
    total_cands = 0
    for kw in keywords:
        ok, cs = anirss_mikan_search(kw)
        if not ok:
            continue
        fresh = [c for c in cs if c.get("url") and c["url"] not in tried]
        if not fresh:
            continue
        total_cands += len(fresh)
        fresh.sort(
            key=lambda c: difflib.SequenceMatcher(None, want, norm_title(c["title"])).ratio(),
            reverse=True,
        )
        for c in fresh[:6]:
            if budget <= 0:
                break
            tried.add(c["url"])
            budget -= 1
            ok, groups = anirss_mikan_groups(c["url"])
            if not ok or not groups:
                continue
            if bgm_id:
                hits = [g for g in groups if g["bgm_id"] == str(bgm_id) and g["rss"]]
                if hits:
                    g = pick(hits) or hits[0]
                    return True, {"rss": g["rss"], "label": g["label"], "bgm_id": g["bgm_id"],
                                  "matched_by": "bgm_id", "bangumi_title": c["title"]}
            if fallback is None:
                g = pick(groups)
                if g:
                    fallback = {"rss": g["rss"], "label": g["label"], "bgm_id": g["bgm_id"],
                                "matched_by": "title", "bangumi_title": c["title"]}
        if budget <= 0:
            break
    if fallback:
        return True, fallback
    if not total_cands:
        return False, "蜜柑未找到《%s》" % title
    return False, "蜜柑候选均无可用字幕组"


def anirss_subscribe(anime):
    """把一部番订阅到 ani-rss。

    链路：蜜柑搜索 -> 字幕组（按 Bangumi ID 精确对齐）-> rssToAni -> addAni。
    关键：Ani 对象一律由 ani-rss 的 /rssToAni 生成，绝不手工拼字段 ——
          残缺对象会被 ani-rss 落盘并污染 ani.v2.json（曾导致 listAni/日历 双 500）。
    返回 (ok, 说明文字)。
    """
    if not CONFIG.get("anirss_allow_subscribe"):
        return False, "未开启「允许订阅到 ani-rss」"
    if not anirss_configured():
        return False, "未配置 ani-rss 地址或 apiKey"

    title = anime.get("title") or anime.get("title_jp") or ""
    if not title:
        return False, "该番剧没有可用标题"

    anirss_ensure_cache()
    if anirss_lookup(anime):
        return True, "该番已在 ani-rss 中，无需重复订阅"

    pref = anime.get("rel_names") or []
    if isinstance(pref, str):
        pref = [pref]

    ok, r = anirss_resolve(title, anirss_bgm_key(anime), pref)
    if not ok:
        return False, r

    ok, ani = anirss_api("/api/rssToAni", {"url": r["rss"]})
    if not ok:
        return False, "RSS 解析失败：%s" % ani
    if not isinstance(ani, dict) or not ani.get("title"):
        return False, "RSS 解析结果异常"

    ani["enable"] = True          # 仅显式打开启用位；其余字段由 ani-rss 自行生成与校验
    ani["globalExclude"] = True   # 应用全局排除（{{组}}:繁 等）——组内同发简/繁时优先下简中
    ok, msg = anirss_api("/api/addAni", ani)
    if not ok:
        return False, "添加订阅失败：%s" % msg
    anirss_ensure_cache(force=True)
    return True, "已订阅《%s》· 字幕组 %s · 对齐方式 %s" % (
        ani.get("title") or title, r.get("label") or "默认", r.get("matched_by"))


def anirss_sync_loop():
    """后台定期刷新 ani-rss 订阅缓存。"""
    time.sleep(20)
    while True:
        try:
            if anirss_configured() and (CONFIG.get("anirss_sync") or CONFIG.get("anirss_allow_subscribe")):
                anirss_ensure_cache()
        except Exception as e:
            log.warning("ani-rss 同步线程异常：%s", e)
        time.sleep(300)



# ---------------------------------------------------------------------------
# 播出时间推算（日本记法 -> 北京时间）
# ---------------------------------------------------------------------------
def air_datetime_jst(anime, date_str=None):
    """把番剧某一天的首播日 + 日本记法时间，换算成真实的 JST 播出时刻。"""
    if not anime["premiere"]:
        return None
    if anime["time_jst"] is None:
        h, mi = 0, 0
    else:
        h, mi = anime["time_jst"]
    d = datetime.strptime(date_str or anime["premiere"], "%Y-%m-%d")
    extra_days, hh = (1, h - 24) if h >= 24 else (0, h)
    return d.replace(hour=hh, minute=mi, tzinfo=JST) + timedelta(days=extra_days)


def next_airing(anime, now_jst):
    """下一话的 (JST时刻, 集数)。网络放送类返回 (单次时刻, None)。无法推算返回 (None, None)。"""
    if anime["is_net"]:
        m = re.search(r"(\d{1,2})/(\d{1,2})", anime.get("note", ""))
        if not m:
            return None, None
        mo_, dy = int(m.group(1)), int(m.group(2))
        cur = datetime.now(JST)
        year = cur.year
        if mo_ < cur.month - 6:
            year += 1   # 例：12月底看 1 月的日期 -> 明年
        elif mo_ > cur.month + 6:
            year -= 1   # 例：1月初看 12 月的日期 -> 去年
        dt = datetime(year, mo_, dy, 10, 0, tzinfo=JST)  # 无具体时刻，默认上午10点仅用于汇总
        return (dt, None) if dt >= now_jst else (None, None)

    first = air_datetime_jst(anime)
    if first is None:
        return None, None
    week = 0
    while first + timedelta(days=7 * week) < now_jst:
        week += 1
    if week and first + timedelta(days=7 * week) - first > timedelta(days=7 * 200):
        return None, None  # 防御：长篇无总集数时最多推算 200 周
    nxt = first + timedelta(days=7 * week)
    ep = week + 1
    if anime["total"] and ep > anime["total"]:
        return None, None  # 已完结
    return nxt, ep


def decorate(anime_list):
    """补全推算字段，供面板 / 提醒使用。AniBT 存在时优先采用其权威播出时刻。"""
    now_jst = datetime.now(JST)
    now_bjt = datetime.now(BJT)
    for a in anime_list:
        if a.get("anibt_next_ts"):
            nxt, ep, anchor, finished, prev, prev_ep = anibt_resolve(a, now_bjt)
            a["anibt_anchor_ts"] = anchor.timestamp() if anchor else None
            if nxt is not None:
                a["next_air_bjt"] = nxt.strftime("%Y-%m-%d %H:%M")
                a["next_ts"] = nxt.timestamp()
                a["next_ep"] = ep
                a["status"] = "放送中"
                a["time_bjt"] = nxt.strftime("%H:%M")
            else:
                a["next_air_bjt"] = a["next_ts"] = a["next_ep"] = None
                a["status"] = "已完结" if (finished or a.get("is_net")) else "待定"
                if anchor:
                    a["time_bjt"] = anchor.strftime("%H:%M")
        else:
            nxt, ep = next_airing(a, now_jst)
            a["next_air_bjt"] = nxt.astimezone(BJT).strftime("%Y-%m-%d %H:%M") if nxt else None
            a["next_ts"] = nxt.timestamp() if nxt else None
            a["next_ep"] = ep
            if a["total"] and ep is None and not a["is_net"] and a["premiere"]:
                a["status"] = "已完结"
            elif nxt is None:
                a["status"] = "待定"
            else:
                a["status"] = "放送中"
            # 無 AniBT 数据时，用下一话回推上一话，保证「今日更新」含已播出的场次
            if nxt is not None and ep and ep > 1:
                prev = nxt - timedelta(days=7)
                prev_ep = ep - 1
            else:
                prev, prev_ep = None, None
        a["prev_air_bjt"] = prev.strftime("%Y-%m-%d %H:%M") if prev else None
        a["prev_ep"] = prev_ep
        # 用于展示的北京时间开播时间（每周固定）
        if not a.get("time_bjt"):
            if a["time_jst"]:
                h, mi = a["time_jst"]
                extra, hh = (1, h - 24) if h >= 24 else (0, h)
                base = datetime(2026, 1, 1, hh, mi, tzinfo=JST) + timedelta(days=extra)
                a["time_bjt"] = base.astimezone(BJT).strftime("%H:%M")
            else:
                a["time_bjt"] = None
        a["sort_min"] = int(a["time_bjt"][:2]) * 60 + int(a["time_bjt"][3:5]) if a["time_bjt"] else 9999
        attach_release(a)
    return anime_list


# ---------------------------------------------------------------------------
# 钉钉机器人
# ---------------------------------------------------------------------------
def dingtalk_sign_url(webhook, secret):
    if not secret:
        return webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = "%s\n%s" % (ts, secret)
    sign = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    sep = "&" if "?" in webhook else "?"
    return "%s%stimestamp=%s&sign=%s" % (webhook, sep, ts, urllib.parse.quote_plus(sign))


def send_dingtalk_markdown(title, text, config=None):
    cfg = config or CONFIG
    webhook = cfg.get("dingtalk_webhook", "").strip()
    if not webhook:
        log.warning("未配置钉钉 webhook，跳过推送: %s", title)
        return False, "未配置钉钉 webhook"
    payload = json.dumps({"msgtype": "markdown", "markdown": {"title": title, "text": text}}).encode("utf-8")
    url = dingtalk_sign_url(webhook, cfg.get("dingtalk_secret", "").strip())
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        ok = body.get("errcode") == 0
        log.info("钉钉推送[%s]: %s", title, body)
        return ok, str(body)
    except Exception as e:
        log.error("钉钉推送失败: %s", e)
        return False, str(e)


# ---------------------------------------------------------------------------
# 提醒逻辑
# ---------------------------------------------------------------------------
def subscribed(a):
    # 面板上点 × 软删除（hidden_ids）的番：钉钉推送一律静默——
    # 单集提醒 / 字幕组资源提醒 / 每日汇总（digest_items 同样过滤）都不再发。
    # 界面「已隐藏 N 部」入口可恢复（api_hide toggle）。
    if a.get("id") in CONFIG.get("hidden_ids", []):
        return False
    mode = CONFIG.get("subscribe_mode", "all")
    if mode == "all" or not mode:
        # 非法/空值一律视为 all（历史 bug 曾写入 false 导致提醒全静默）
        return True
    if mode == "anirss":
        # 跟随 ani-rss：以 ani-rss 中「已启用」的订阅为准
        rec = anirss_lookup(a)
        return bool(rec and rec.get("enable"))
    return a["id"] in CONFIG.get("subscribe_ids", [])


def cover_md(a):
    """钉钉 markdown 的封面图片段（无图返回空串）。"""
    return "\n\n![封面](%s)" % a["img"] if a.get("img") else ""


def digest_items(anime_list, date_bjt, skip=None):
    """某天（北京时间）的汇总条目 [(hhmm, anime, ep, aired)]。
    skip 里的番名会被跳过——用于「同一天多次汇总只播报一次」。"""
    key = date_bjt.strftime("%Y-%m-%d")
    skip = skip or set()
    hidden = set(CONFIG.get("hidden_ids", []))   # 点 × 软删除的番不进汇总
    items = []
    for a in anime_list:
        if a["title"] in skip or a.get("id") in hidden:
            continue
        if (a.get("next_air_bjt") or "").startswith(key):
            items.append((a["next_air_bjt"][11:16], a, a.get("next_ep"), False))
        elif (a.get("prev_air_bjt") or "").startswith(key):
            items.append((a["prev_air_bjt"][11:16], a, a.get("prev_ep"), True))
    items.sort(key=lambda x: x[0])
    return items


def fmt_digest(anime_list, date_bjt, skip=None):
    """生成某天（北京时间）的更新汇总 markdown（含当天已播出的场次）。"""
    items = digest_items(anime_list, date_bjt, skip)
    if not items:
        return None
    lines = ["## 📺 今日番剧更新（%s %s）\n" % (date_bjt.strftime("%m月%d日"), WEEKDAY_CN.get(date_bjt.isoweekday(), ""))]
    for hhmm, a, ep, aired in items:
        tag = "（%s）" % "、".join(a["areas"]) if a.get("areas") else ""
        rel = "　🈶资源第%s集" % a["rel_ep"] if a.get("rel_ep") else ""
        lines.append("- **%s** 《%s》%s%s%s%s\n" % (
            hhmm, a["title"], ("第%s话" % ep) if ep else (a.get("note") or "更新"),
            "（已播）" if aired else "", tag, rel))
        if a.get("img"):
            lines.append("![封面](%s)\n" % a["img"])
    lines.append("\n> 数据来源：%s　时间为北京时间" % (DATA_HOLDER.get("label") or "番表"))
    return "".join(lines)


def check_release_notify(anime_list):
    """字幕组资源发布提醒：某番最新资源集数比上次记录多时推送。返回推送条数。"""
    if not CONFIG.get("notify_release"):
        return 0
    seen = STATE.setdefault("seen_release", {})
    sent, changed = 0, False
    for a in anime_list:
        ep = a.get("rel_ep")
        if not ep or not subscribed(a):
            continue
        key = a["title"]
        prev = seen.get(key)
        if prev is None:          # 首次见到只记录基线，不补发历史资源
            seen[key] = ep
            changed = True
            continue
        if ep > prev:
            emby = ""
            if a.get("emby_state") == "matched" and not a.get("movie"):
                emby = "\n\n> Emby：%s" % ("有缺集，注意下载" if a.get("miss") else "已跟更")
            elif a.get("emby_state") == "not_found":
                emby = "\n\n> Emby：尚未入库"
            names = "、".join(a.get("rel_names") or [])
            send_dingtalk_markdown(
                "字幕组更新提醒",
                "## 🈶 字幕组已发布新资源\n\n**《%s》第%s集** 资源已出%s%s%s\n\n> 数据来源：AniBT 资源进度" % (
                    a["title"], ep, ("（%s）" % names) if names else "", emby, cover_md(a)))
            seen[key] = ep
            changed = True
            sent += 1
    if changed:
        save_state()
    return sent


def check_episode_notify(anime_list, now):
    """单集提醒。默认 advance_minutes=0：不提前，到点（开播时刻）才推，
    且同一部番同一天只推一次；到点后留 AT_TIME_GRACE 补发窗口防漏。
    返回推送条数。"""
    adv = int(CONFIG.get("advance_minutes", 0) or 0)
    sent = 0
    for a in anime_list:
        if a["next_ts"] is None or not subscribed(a):
            continue
        delta = a["next_ts"] - now.timestamp()
        if adv > 0:
            hit = 0 <= delta <= adv * 60              # 提前 N 分钟窗口
        else:
            hit = 0 >= delta >= -AT_TIME_GRACE        # 到点后的补发窗口
        if not hit:
            continue
        # 去重键去掉集数：同一番同一天只提醒一次（集数偶发缺失/修正也不会重复推）
        key = "%s|%s" % (a["title"], a["next_air_bjt"][:10].replace("-", ""))
        if key in STATE["sent_advance"]:
            continue
        hhmm = a["next_air_bjt"][11:16]
        ep = "第%s话" % a["next_ep"] if a["next_ep"] else (a["note"] or "更新")
        areas = "（%s）" % "、".join(a["areas"]) if a["areas"] else ""
        if adv > 0:
            md = "## ⏰ 番剧更新提醒\n\n**《%s》%s** 将于 **%s**（北京时间）更新%s%s" % (
                a["title"], ep, hhmm, areas, cover_md(a))
        else:
            md = "## ⏰ 番剧已开播\n\n**《%s》%s** 已于 **%s**（北京时间）开播%s%s" % (
                a["title"], ep, hhmm, areas, cover_md(a))
        send_dingtalk_markdown("番剧更新提醒", md + "\n\n> 来自 %s" % source_label())
        STATE["sent_advance"].append(key)
        save_state()
        sent += 1
    return sent


def notify_loop(anime_list_holder):
    tick = 30  # 秒
    log.info("提醒线程已启动（每 %d 秒检查一次）", tick)
    while True:
        try:
            anime_list = decorate(anime_list_holder["list"]) if anime_list_holder.get("list") else []
            now = datetime.now(BJT)

            # 1) 单集提醒（默认不提前、到点推一次，见 check_episode_notify）
            check_episode_notify(anime_list, now)

            # 2) 每日汇总
            #    同一天可能配多个时刻（如 09:00 / 18:30）：同一部番当天只播报一次，
            #    否则同一条番会在一天里被推 2 遍，叠加开播提醒就是用户看到的"提醒 3 次"。
            day_key = now.strftime("%Y-%m-%d")
            digest_on = CONFIG.get("notify_digest", True)
            sent_today = STATE.setdefault("digest_sent_day", {}).setdefault(day_key, [])
            for t in (CONFIG.get("notify_times", ["09:00"]) if digest_on else []):
                tm = re.match(r"^(\d{1,2}):(\d{2})$", str(t).strip())
                if not tm:
                    continue
                target = now.replace(hour=int(tm.group(1)), minute=int(tm.group(2)), second=0, microsecond=0)
                done_key = "%s|%s" % (t, day_key)
                if target <= now < target + timedelta(seconds=tick + 5) and done_key not in STATE["digest_done"]:
                    items = digest_items(anime_list, now, skip=set(sent_today))
                    md = fmt_digest(anime_list, now, skip=set(sent_today))
                    if md:
                        send_dingtalk_markdown("今日番剧更新", md)
                        sent_today.extend(a["title"] for _, a, _, _ in items)
                    else:
                        log.info("%s 今日无新增更新，跳过汇总", t)
                    STATE["digest_done"][done_key] = True
                    # 清理 30 天前的记录
                    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
                    STATE["digest_done"] = {k: v for k, v in STATE["digest_done"].items()
                                            if k.split("|")[-1] >= cutoff}
                    STATE["digest_sent_day"] = {k: v for k, v in STATE["digest_sent_day"].items()
                                                if k >= cutoff}
                    save_state()
            # 3) 字幕组新资源提醒（依据 AniBT 资源进度）
            check_release_notify(anime_list)
        except Exception as e:
            log.exception("提醒循环异常: %s", e)
        time.sleep(tick)


# ---------------------------------------------------------------------------
# AniBT 数据源（官方开放 API，文档：https://wiki.anibt.net/docs/open-api）
#   提供：中文/日文原名、Bangumi ID、精确首播日期、下一话播出时刻、字幕组资源进度
# ---------------------------------------------------------------------------
ANIBT_BASE = "https://anibt.net"
ANIBT_TTL = 1800             # 季度番表缓存（秒）
ANIBT_GROUPS_TTL = 1800      # 字幕组资源缓存（秒）
_ANIBT_CACHE = {"ts": 0.0, "season": None, "list": []}
_ANIBT_GROUPS = load_json(GROUPS_CACHE_PATH, {})   # bgmId(str) -> {"ts","data"}，磁盘持久化：重启不再丢资源角标
_ANIBT_BGM = {}              # bgmId(str) -> {"ts": float, "info": {...}}
ANIBT_LOCK = threading.Lock()
_GROUPS_LAST_SAVE = [0.0]


def save_groups_cache(force=False):
    """字幕组资源缓存落盘（节流：默认 60 秒最多一次；release_loop 每批强制一次）。"""
    now = time.time()
    if not force and now - _GROUPS_LAST_SAVE[0] < 60:
        return
    _GROUPS_LAST_SAVE[0] = now
    try:
        with ANIBT_LOCK:
            snap = json.dumps(_ANIBT_GROUPS, ensure_ascii=False)
        with open(GROUPS_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(snap)
    except Exception as e:
        log.warning("字幕组资源缓存写入失败: %s", e)


def anibt_auto_season(now=None):
    """按播出季推算季度：1-3 冬、4-6 春、7-9 夏、10-12 秋。"""
    now = now or datetime.now(BJT)
    return "%d%s" % (now.year, _season_of_month(now.month))


def _season_of_month(month):
    return {1: "冬", 2: "冬", 3: "冬", 4: "春", 5: "春", 6: "春",
            7: "夏", 8: "夏", 9: "夏", 10: "秋", 11: "秋", 12: "秋"}[int(month)]


def _anibt_season_arg():
    """AniBT 季度：优先用设置值，留空则自动跟随当前季。"""
    conf = (CONFIG.get("anibt_season") or "").strip()
    if conf:
        return conf
    return anibt_auto_season()


def _anibt_normalize(e):
    """AniBT 条目 -> 内部番剧结构。airingAt 为「最近/下一次播出」的绝对时刻（秒）。"""
    t = e.get("title") or {}
    cn = (t.get("chinese") or t.get("primary") or "").strip()
    jp = (t.get("japanese") or "").strip()
    title = cn or jp or (t.get("english") or "").strip()
    if not title:
        return None
    wd = e.get("weekday")                      # 0=周日 … 6=周六（北京时间）
    try:
        wd = None if wd is None else (7 if int(wd) == 0 else int(wd))
    except Exception:
        wd = None
    fmt = (e.get("format") or e.get("kind") or "").upper()
    is_net = (wd is None) or fmt in ("ONA", "OVA", "OAD", "WEB", "SPECIAL")
    ts = e.get("airingAt")
    try:
        ts = int(ts) if ts else None
    except Exception:
        ts = None
    total = e.get("episodes")
    try:
        total = int(total) if total else None
    except Exception:
        total = None
    return {
        "title": title, "title_jp": jp,
        "weekday": wd, "weekday_name": WEEKDAY_CN.get(wd, "网络放送"),
        "time_jst": None,
        "premiere": (e.get("premiereDate") or None),
        "premiere_precision": e.get("premierePrecision"),
        "total": total, "areas": [], "img": e.get("cover") or "", "note": "",
        "is_net": is_net, "source": "anibt",
        "bgm_id": e.get("bgmId"), "anibt_id": e.get("animeId"),
        "rating": e.get("rating"), "format": e.get("format") or e.get("kind") or "",
        "has_release": bool(e.get("hasRelease")),
        "release_count": e.get("rssReleaseCount") or 0,
        "anibt_next_ts": ts, "season_label": e.get("seasonLabel") or "",
    }


def anibt_fetch_season(season=None, force=False):
    """拉取 AniBT 季度番表（带 30 分钟缓存）。返回 (list, season)。"""
    season = (season or "").strip() or _anibt_season_arg()
    with ANIBT_LOCK:
        c = dict(_ANIBT_CACHE)
    if (not force and c.get("list") and c.get("season") == season
            and time.time() - (c.get("ts") or 0) < ANIBT_TTL):
        return c["list"], season
    url = ANIBT_BASE + "/api/seasons/anime?" + urllib.parse.urlencode({"season": season})
    data = _get_json(url, timeout=25)
    d = (data or {}).get("data") or {}
    out = []
    for grp in d.get("byWeekday") or []:
        for e in grp.get("animes") or []:
            a = _anibt_normalize(e)
            if a:
                out.append(a)
    if not out:
        raise RuntimeError("AniBT 季度 %s 返回空数据" % season)
    with ANIBT_LOCK:
        _ANIBT_CACHE.update(ts=time.time(), season=season, list=out)
    log.info("AniBT 番表已获取：%s 共 %d 部", season, len(out))
    return out, season


def anibt_resolve(a, now_bjt):
    """解析 AniBT 的播出时刻 -> (下一次, 下一次集数, 锚点, 是否已完结, 最近一次, 最近集数)。
    airingAt 语义：番剧在播时为「下一话」，已完结时为「最后一话」。"""
    ts = a.get("anibt_next_ts")
    if not ts:
        return None, None, None, False, None, None
    try:
        dt0 = datetime.fromtimestamp(int(ts), BJT)
    except Exception:
        return None, None, None, False, None, None
    prem = None
    if a.get("premiere"):
        try:
            prem = datetime.strptime(str(a["premiere"])[:10], "%Y-%m-%d").replace(tzinfo=BJT)
        except Exception:
            prem = None

    def ep_of(d):
        return ((d.date() - prem.date()).days // 7 + 1) if prem else None

    total = a.get("total")
    if a.get("is_net"):          # 网络放送/特别篇：单次播出，不按周推进
        ep0 = ep_of(dt0)
        if dt0 > now_bjt:
            return dt0, ep0, dt0, False, None, None
        return None, None, dt0, True, dt0, ep0
    if dt0 > now_bjt:            # 常规在播番
        ep0 = ep_of(dt0)
        prev = dt0 - timedelta(days=7)
        prev_ep = (ep0 - 1) if (ep0 and ep0 > 1) else None
        if prem and prev < prem:
            prev, prev_ep = None, None
        return dt0, ep0, dt0, False, prev, prev_ep
    dt, ep, weeks = dt0, ep_of(dt0), 0
    while dt <= now_bjt:
        dt += timedelta(days=7)
        weeks += 1
        if ep:
            ep += 1
        if total and ep and ep > int(total):
            last = dt - timedelta(days=7)
            return None, None, last, True, last, ep - 1
        if weeks > 60:           # 站点数据长期滞后：视为停更
            return None, None, dt0, False, dt0, ep
    prev, prev_ep = dt - timedelta(days=7), (ep - 1) if ep else None
    if prem and prev < prem:
        prev, prev_ep = None, None
    return dt, ep, dt, False, prev, prev_ep


def anibt_groups(bgm_id, force=False, since_ms=None):
    """字幕组资源进度（含每组已发布集数）。带 30 分钟缓存。
    since_ms：只统计发布时间不早于该值的资源（过滤前季补档），缓存本体不过滤。"""
    if not bgm_id:
        return None
    key = str(bgm_id)
    with ANIBT_LOCK:
        c = _ANIBT_GROUPS.get(key)
    if not force and c and time.time() - (c.get("ts") or 0) < ANIBT_GROUPS_TTL:
        return _filter_groups_since(c.get("data"), since_ms)
    url = ANIBT_BASE + "/api/anime/groups?" + urllib.parse.urlencode({"bgmId": bgm_id})
    try:
        d = (_get_json(url, timeout=20) or {}).get("data") or {}
    except Exception as e:
        log.warning("AniBT 字幕组查询失败 [%s]: %s", bgm_id, e)
        return c.get("data") if c else None
    data = _anibt_groups_norm(d)
    with ANIBT_LOCK:
        _ANIBT_GROUPS[key] = {"ts": time.time(), "data": data}
    save_groups_cache()
    return data


def _anibt_groups_norm(d):
    groups = []
    for g in d.get("groups") or []:
        eps = []
        for x in g.get("publishedEpisodes") or []:
            try:
                eps.append(int(x))
            except Exception:
                pass
        items = []
        for it in (g.get("items") or [])[:5]:
            items.append({
                "title": it.get("title") or "",
                "ep": it.get("episodeKey"), "release_id": it.get("releaseId"),
                "resolution": it.get("resolution"), "format": it.get("format"),
                "subtitle": it.get("subtitle"), "magnet": it.get("magnet") or "",
                "size": it.get("size"), "ts": it.get("publishedAt"),
            })
        groups.append({"name": g.get("name") or "", "slug": g.get("slug") or "",
                       "status": g.get("status") or "", "episodes": sorted(set(eps)),
                       "total": g.get("totalReleases") or 0,
                       "updated": g.get("lastUpdatedAt"), "items": items})
    max_ep = 0
    for g in groups:
        if g["episodes"]:
            max_ep = max(max_ep, max(g["episodes"]))
    t = d.get("title") or {}
    return {"bgm_id": d.get("bgmId"), "title_cn": t.get("chinese") or t.get("primary") or "",
            "title_jp": t.get("japanese") or "", "groups": groups,
            "max_ep": max_ep, "group_count": len(groups),
            "updated": max([g["updated"] or 0 for g in groups] or [0])}


def anibt_bgm_subject(bgm_id):
    """按 Bangumi ID 取标准名称（比模糊搜索可靠）。返回 {name_cn, name, total}。"""
    if not bgm_id:
        return None
    key = str(bgm_id)
    with ANIBT_LOCK:
        c = _ANIBT_BGM.get(key)
    if c and time.time() - (c.get("ts") or 0) < 86400:
        return c.get("info")
    info = None
    try:
        url = ANIBT_BASE + "/api/bgm/search?" + urllib.parse.urlencode(
            {"q": str(bgm_id), "limit": 5})
        for r in (_get_json(url, timeout=15) or {}).get("data") or []:
            if str(r.get("bgmId")) == str(bgm_id):
                info = {"name_cn": (r.get("nameCn") or "").strip(),
                        "name": (r.get("name") or "").strip(),
                        "total": r.get("totalEpisodes"), "rating": r.get("rating")}
                break
    except Exception as e:
        log.warning("AniBT Bangumi 查询失败 [%s]: %s", bgm_id, e)
    with ANIBT_LOCK:
        _ANIBT_BGM[key] = {"ts": time.time(), "info": info}
    return info


def _filter_groups_since(d, since_ms):
    """按发布时间过滤字幕组资源。

    AniBT 按 bgmId 聚合的是**整个系列**：S2 开播前会混入 S1 的 WebRip/补档
    （实测《野生的大魔王出现了！第二季》首播当天，接口返回 S1 的 E01/E13
    补档，导致"未开播先显示资源到第13集"）。规则：某集的**最新**资源发布
    时间早于 since_ms 的视为前季资源，剔除；无资源样本可查的集保留
    （items 列表只截取最新 5 条，老集查不到样本属正常在播情形）。"""
    if not d or not since_ms:
        return d
    out = []
    for g in d.get("groups") or []:
        ep_max_ts = {}
        for it in g.get("items") or []:
            try:
                ep = int(str(it.get("ep") or "").strip())
            except (TypeError, ValueError):
                continue
            if ep <= 0:
                continue
            ts = it.get("ts") or 0
            if ts > ep_max_ts.get(ep, 0):
                ep_max_ts[ep] = ts
        kept = []
        for ep in g.get("episodes") or []:
            try:
                ep = int(ep)
            except (TypeError, ValueError):
                continue
            ts = ep_max_ts.get(ep)
            if ts is None or ts >= since_ms:
                kept.append(ep)
        if not kept:
            continue
        out.append(dict(g, episodes=sorted(kept)))
    if not out:
        return dict(d, groups=[], max_ep=0, group_count=0, updated=0)
    return dict(d, groups=out,
                max_ep=max(max(g["episodes"]) for g in out),
                group_count=len(out),
                updated=max([g.get("updated") or 0 for g in out] or [0]))


def _premiere_cutoff_ms(premiere):
    """首播日（北京时间）前 1 天的时间戳（毫秒），作资源过滤下限。"""
    if not premiere:
        return None
    try:
        dt = datetime.strptime(str(premiere)[:10], "%Y-%m-%d").replace(tzinfo=BJT)
    except ValueError:
        return None
    return (dt - timedelta(days=1)).timestamp() * 1000


def attach_release(a):
    """把（缓存的）字幕组资源进度摘要挂到条目上；仅读缓存，不发请求。"""
    gid = a.get("bgm_id")
    if not gid:
        return a
    with ANIBT_LOCK:
        c = _ANIBT_GROUPS.get(str(gid))
    d = (c or {}).get("data")
    if not d:
        return a
    d = _filter_groups_since(d, _premiere_cutoff_ms(a.get("premiere")))
    a["rel_ep"] = d.get("max_ep") or 0
    a["rel_count"] = d.get("group_count") or 0
    a["rel_names"] = [g["name"] for g in (d.get("groups") or [])[:3] if g.get("name")]
    a["rel_updated"] = d.get("updated")
    return a


def season_label_text(nanibt):
    """番表标签：数据源固定为 AniBT。"""
    return "AniBT %s（%d 部）" % (_anibt_season_arg(), nanibt)


def source_label():
    """推送 / 日志里使用的数据源标识。"""
    return "AniBT %s" % _anibt_season_arg()


# ---------------------------------------------------------------------------
# Emby / TMDB 对齐与剧集检测
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[\s·・，,。.\-—_!！?？:：;；'\"“”‘’（）()【】\[\]<>《》~～*]+")
_SEASON_TAIL_RE = re.compile(
    r"(\s*(第[0-9一二三四五六七八九十]+[期季部]\s*|Part\s*\.?\s*\d+\s*|Ⅱ|Ⅲ|IV|V)+)$", re.IGNORECASE)


# 库名里的花体分数要与番表的 "1/2" 写法对齐（如《乱马½》↔《乱马1/2》）
_FRACTION_MAP = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3"}


def norm_title(t):
    t = t or ""
    for k, v in _FRACTION_MAP.items():
        if k in t:
            t = t.replace(k, v)
    return _PUNCT_RE.sub("", t.lower())


def search_term(t):
    """去掉季度后缀的搜索词（《xx 第2期》通常按基础名搜更容易命中）。"""
    t = re.sub(r"\s*（[^）]*）\s*$", "", t or "").strip()
    return _SEASON_TAIL_RE.sub("", t).strip() or (t or "").strip()


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def emby_base():
    u = (CONFIG.get("emby_url") or "").strip().rstrip("/")
    return u[:-5] if u.lower().endswith("/emby") else u


_TMDB_DOWN_UNTIL = [0.0]   # 熔断：TMDB 连续不可达时，10 分钟内跳过，避免拖慢对齐
_PROXY_OPENER = {"p": None, "op": None}


def _tmdb_opener():
    """按配置返回走代理的 opener（tmdb_proxy 为空则直连）。"""
    p = (CONFIG.get("tmdb_proxy") or "").strip()
    if not p:
        return None
    if _PROXY_OPENER["p"] != p:
        _PROXY_OPENER["p"] = p
        _PROXY_OPENER["op"] = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p}))
    return _PROXY_OPENER["op"]


def _match_variants(raw):
    """参与比对的形态：全名 + 有效段。
    纯 ASCII 短段（如 "part2"、"the movie"）没有区分度，会产生误配，过滤掉；
    有效段 = 含中文且 ≥2 字，或 ASCII/混合且 ≥8 字。全名始终保留。"""
    vs = _name_variants(raw)
    if not vs:
        return []
    out = [vs[0]]
    for v in vs[1:]:
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in v)
        if (has_cjk and len(v) >= 2) or len(v) >= 8:
            out.append(v)
    return out


def _title_related(q_title, tmdb_name, tmdb_original):
    """番表标题与 TMDB 结果名称是否有词面关联（防 TMDB 文本搜索返回无关条目）。"""
    qvs = _match_variants(q_title) + [norm_title(search_term(q_title))]
    rvs = _match_variants((tmdb_name or "") + " " + (tmdb_original or ""))
    for qv in qvs:
        if not qv:
            continue
        for rv in rvs:
            if not rv:
                continue
            if _lcs_len(qv, rv) >= 4:
                return True
            if (len(rv) >= 0.6 * max(len(qv), 1)
                    and difflib.SequenceMatcher(None, qv, rv).ratio() >= 0.6):
                return True
    return False


# 作品名里的"通用后缀/修饰"（去掉后剩下的才是作品本体，用于判断两个名字是否同源）
_GENERIC_TITLE_RE = re.compile(
    r"theanimation|animation|television|series|"
    r"剧场版|劇場版|特别篇|特別篇|总集篇|総集編|合集|"
    r"最终章|最終章|终章|終章|完结篇|完結編|完結篇|最终回|最終回|序章|新章|"
    r"part[0-9]+|season[0-9]+|ova|oad", re.IGNORECASE)
# 段落式季度标记，可在名字任意位置出现（第2季 / 第5话 / 第2部分…）
_SEASON_INLINE_RE = re.compile(
    r"(?:第)?[0-9０-９一二三四五六七八九十]+(?:季|期|話|话|部分|部|弾|弹|篇|章|幕|回|集)")


def _title_core(n):
    """归一化名剥掉季度标记与通用后缀后的"作品本体"串。"""
    s = _GENERIC_TITLE_RE.sub("", norm_title(n))
    prev = None
    while s and s != prev:
        prev = s
        s = _SEASON_INLINE_RE.sub("", s)
    return s


def _candidate_related(title, cand, extra=()):
    """Emby RemoteSearch / Bangumi 返回的候选名是否真的与番剧同源。
    这两条通道都是**模糊文本搜索**，会把完全无关的作品当候选返回
    （实测："しろたん"→《浪客剑心》、《Dark Machine The Animation》→《蔚蓝档案 The Animation》），
    若只按"能在库里精确命中"就采纳，会得到"已入库"的错误结论。

    放行条件（任一）：本体相同 / 互为子序列 / 一方包含另一方 / 共享 ≥4 字。
    子序列这条必不可少：《侦探已死》是《侦探已经死了》的子序列（同一部作品，
    番表与 Bangumi 给了缩写译名），只按"共享 4 字"会把《侦探已经死了。 第二季》误判为未入库。
    """
    c = _title_core(cand)
    if not c:
        return False
    bases = [_title_core(title)] + [_title_core(e) for e in extra if e]
    for b in bases:
        if not b:
            continue
        if c == b:                                   # 《青之箱 第二季》↔《青之箱》
            return True
        if min(len(c), len(b)) >= 3 and (_is_subseq(c, b) or _is_subseq(b, c)):
            return True                              # 《侦探已死》⊂《侦探已经死了》
        if min(len(c), len(b)) >= 3 and (c in b or b in c):
            return True
        if _lcs_len(b, c) >= 4:                      # 《来自深渊 觉醒的神秘》↔《来自深渊》
            return True
    return False


def tmdb_search_tv(title, jp_titles=()):
    key = (CONFIG.get("tmdb_api_key") or "").strip()
    if not key:
        return None
    if time.time() < _TMDB_DOWN_UNTIL[0]:
        return None
    # 查询词：番表中文名在前。中文译名常搜不到（实测《东京复仇者 三天战争篇》
    # 中文在 TMDB/RemoteSearch 均零结果，日文原名"東京リベンジャーズ"一击命中），
    # 因此追加日文原名作为兜底查询词。
    queries = [search_term(title)]
    for j in (jp_titles or ()):
        sq = search_term((j or "").strip())
        if sq and sq not in queries:
            queries.append(sq)
    op = _tmdb_opener()
    net_err = None
    for q in queries:
        url = "https://api.themoviedb.org/3/search/tv?" + urllib.parse.urlencode(
            {"api_key": key, "language": "zh-CN", "query": q})
        for attempt in range(3):          # 本地代理偶发握手超时，多次尝试
            try:
                if op:
                    resp = op.open(url, timeout=8)
                else:
                    resp = urllib.request.urlopen(url, timeout=8)
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                for r in data.get("results", []):
                    # TMDB 文本搜索偶发返回完全无关的条目（曾把"魔神坛斗士"搜成
                    # R18 片），名称无词面关联的一律弃用。
                    # 另有"自由译名"情形（《转生就是剑》↔《转生成为魔剑》），
                    # 词面比不出，用 **日文原名** 兜底（跨源最稳定）。
                    if not r.get("id"):
                        continue
                    jp_ok = any(_jp_same(r.get("original_name") or "", j)
                                for j in jp_titles if j)
                    if jp_ok or _title_related(title, r.get("name") or "", r.get("original_name") or ""):
                        return {"id": r["id"], "name": r.get("name") or "",
                                "original": r.get("original_name") or ""}
                net_err = None
                break                     # 有响应但无相关结果：换下一个查询词（不是网络问题）
            except Exception as e:
                net_err = e
    log.warning("TMDB 搜索失败 [%s]: %s", title, net_err)
    msg = str(net_err)
    if "timed out" in msg or "SSL" in msg or "10060" in msg or "unreachable" in msg or "10061" in msg:
        _TMDB_DOWN_UNTIL[0] = time.time() + 600
    return None


_LIB_CACHE = {"ts": 0.0, "items": []}

# 疑似"视频文件/合集"的条目名：未刮削的家庭视频、B 站搬运的 AMV 合集等。
# 这类名字会把番剧名内嵌在装饰性括号里（如
# "【AMV_MAD】 钢琴家 【愚者之夜】 - 1.【AMV_MAD】 钢琴家 【愚者之夜】(Av1903191850,P1)"），
# 归一化后与番剧名一模一样，被相似度匹配误判成"已入库"。
# 关键在于只排除**文件名/合集**特征，绝不排除分辨率/压制标记——
# 像"葬送的芙莉莲 [1080p]"这种带分辨率前缀的才是真正的剧集目录，必须保留。
_JUNK_LIB_RE = re.compile(
    r"\(\s*[Aa]v\d{5,}"                                          # B 站 av 号
    r"|[,，]\s*[Pp]\d{1,3}\s*\)"                                 # B 站分 P 后缀
    r"|\bBV[0-9A-Za-z]{8,}\b"                                    # B 站 BV 号
    r"|\.(?:mkv|mp4|avi|rmvb|flv|m2ts|wmv|mov|iso|srt|ass)\b"    # 视频/字幕文件
    r"|【(?:AMV|MAD|MMD|MMV|MV|MEP|手书|混剪|剪辑)[^】]{0,12}】",   # 二创/合集标记
    re.IGNORECASE)


def _is_junk_lib_name(name):
    return bool(_JUNK_LIB_RE.search(name or ""))


def emby_library(force=False):
    """一次性拉取 Emby 全部剧集+电影条目（5 分钟缓存）。
    Emby 的 SearchTerm 对长中文标题分词很差，全量拉回来本地模糊匹配更可靠。"""
    base, key = emby_base(), (CONFIG.get("emby_api_key") or "").strip()
    if not base or not key:
        return []
    if not force and _LIB_CACHE["items"] and time.time() - _LIB_CACHE["ts"] < 300:
        return _LIB_CACHE["items"]
    url = base + "/Items?" + urllib.parse.urlencode(
        {"IncludeItemTypes": "Series,Movie", "Recursive": "true",
         "Fields": "ProviderIds,OriginalTitle,ProductionYear", "Limit": "100000",
         "api_key": key})
    try:
        items = _get_json(url, timeout=20).get("Items", [])
        kept = [c for c in items if not _is_junk_lib_name(c.get("Name"))]
        if len(kept) != len(items):
            log.info("Emby 媒体库过滤掉 %d 个非剧集条目（视频文件/二创合集）: %s",
                     len(items) - len(kept),
                     "; ".join((c.get("Name") or "")[:40] for c in items
                               if _is_junk_lib_name(c.get("Name")))[:300])
        _LIB_CACHE["ts"] = time.time()
        _LIB_CACHE["items"] = kept
        return kept
    except Exception as e:
        log.warning("Emby 媒体库拉取失败: %s", e)
        return _LIB_CACHE["items"]


def _name_variants(raw):
    """库名的候选形态：全名 + 按副标题分隔符（空格/。～，等）拆出的各段。
    库名常带副标题（如《暴怒千金发誓复仇。～凭借魔导书之力碾碎祖国～》），
    而番表用缩写译名，只与主标题段相似，整串比对会失配。"""
    out = []
    for p in re.split(r"[\s。～〜？！?!，,\-–—]+", raw or ""):
        n = norm_title(p)
        if n and n not in out:
            out.append(n)
    full = norm_title(raw)
    if full and full not in out:
        out.insert(0, full)
    return out


_RS_DOWN_UNTIL = [0.0]     # RemoteSearch 熔断：失败后 10 分钟内跳过

_LIB_SIG = {"v": "", "ts": 0.0}
_REMATCH_NOW = threading.Event()   # 手动「立即对齐」：唤醒 map_loop
_REMATCH_UNTIL = [0.0]             # 手动触发后的免门槛重查窗口（秒级时间戳）
# 「立即对齐」进度（前端通知条轮询展示）：titles 是剩余待查集合，left 随查完递减
_REMATCH_PROG = {"total": 0, "left": 0, "new": 0, "names": [], "titles": set()}
_REMATCH_INFLIGHT = set()   # 并发对齐在途条目：批次选择读 EMBY_MAP 时结果尚未写回，防重复提交
_REMATCH_DONE = set()       # 本次对齐会话已查完的条目：并发下 10s 防抖形同虚设，必须按会话去重


def _THREAD_POOL(n):
    """线程池工厂（延迟 import，避免模块加载期依赖 concurrent.futures）。"""
    from concurrent.futures import ThreadPoolExecutor
    return ThreadPoolExecutor(max_workers=n)


from concurrent.futures import as_completed


def emby_lib_sig():
    """库变化指纹（条目总数 + 最新入库时间），带 60 秒缓存。
    用于让 not_found 条目在「新内容入库」后尽快重查，而不是死等 6 小时周期。"""
    base, key = emby_base(), (CONFIG.get("emby_api_key") or "").strip()
    if not base or not key:
        return ""
    if _LIB_SIG["v"] and time.time() - _LIB_SIG["ts"] < 60:
        return _LIB_SIG["v"]
    url = base + "/Items?" + urllib.parse.urlencode(
        {"IncludeItemTypes": "Series,Movie", "Recursive": "true", "Limit": "1",
         "SortBy": "DateCreated", "SortOrder": "Descending",
         "Fields": "DateCreated", "api_key": key})
    try:
        d = _get_json(url, timeout=10)
        items = d.get("Items", [])
        newest = (items[0].get("DateCreated") or "") if items else ""
        sig = "%s|%s" % (d.get("TotalRecordCount", 0), newest)
        _LIB_SIG.update(v=sig, ts=time.time())
        return sig
    except Exception:
        return _LIB_SIG["v"]

# 纯季度/序号标记（第3季、第二期、第3話、第三章、Part 2…）：本身不含作品区分度。
# 用户的库里混有大量非番剧条目时，"第三季 ≈ 第三章" 会产生 0.667 的假匹配。
_SEASON_TOKEN_RE = re.compile(
    r"^(?:第)?[0-9０-９一二三四五六七八九十百千]+(?:季|期|話|话|部分|部|弾|弹|篇|章|幕|回|集)"
    r"$|^part\s*[0-9]+$|^[0-9]+(?:st|nd|rd|th)$|^[0-9]+$", re.IGNORECASE)
# 中文数字与"第"在判断作品区分度时视为无信息量
_NUM_NOISE_RE = re.compile(r"[0-9０-９一二三四五六七八九十百千两第\s/]+")


def _is_subseq(short, long):
    """short 是否是 long 的子序列（《青之箱》是《青春之箱》的子序列 → 同一部作品）。"""
    it = iter(long)
    return all(ch in it for ch in short)


# 日文原名里的季度尾巴：転生したら剣でしたII / 第2期 / Season 2 / Part 2 …
_JP_SEASON_TAIL_RE = re.compile(
    r"(?:xii|xi|ix|viii|vii|vi|iv|iii|ii|x|v|"
    r"第[0-9０-９一二三四五六七八九十]+[季期部話话]|"
    r"season[0-9]+|part[0-9]+)\s*$", re.IGNORECASE)


def _jp_core(n):
    s = _title_core(n)
    prev = None
    while s and s != prev:
        prev = s
        s = _JP_SEASON_TAIL_RE.sub("", s).rstrip()
    return s


def _jp_same(a, b):
    """两个"原题名"是否指同一部作品。

    日文原名跨数据源最稳定（番表/AniBT 给 title_jp，Emby 刮削后存 OriginalTitle），
    是校验"RemoteSearch 给的 TMDB ID 是否真对应本部作品"的可靠依据：
    实测《转生就是剑 第二季》(jp=転生したら剣でしたII) 与库内 TMDB:134667 的
    《转生成为魔剑》(OriginalTitle=転生したら剣でした) 日语名一致 → 确实是同一部，
    而《しろたん》与库内 TMDB:210879 的《浪客剑心》(るろうに剣心…) 无关 → 必须拒绝。
    """
    ca, cb = _jp_core(a), _jp_core(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if min(len(ca), len(cb)) >= 3 and (ca in cb or cb in ca):
        return True
    if _lcs_len(ca, cb) >= 5:
        return True
    return difflib.SequenceMatcher(None, ca, cb).ratio() >= 0.85


def _emby_index_by_tmdb():
    """库内 ProviderIds.Tmdb -> 条目（含 OriginalTitle）。"""
    idx = {}
    for c in emby_library():
        pid = str((c.get("ProviderIds") or {}).get("Tmdb", "") or "")
        if pid and pid not in idx:
            idx[pid] = c
    return idx


def _weak_pair(q, v):
    """判断一对字符串的相似是否"毫无区分度"（不足以认定是同一部作品）。

    三类必须拒绝的"假相似"（均为实测踩到过的）：
    1. 两侧都只是季度标记，**即使完全相同**——《少女与战车 lovelove大作战！ 第1幕》
       与库里无关条目《猟奇の檻 ～第2章～ 第1幕》会因"第1幕"一致而拿到 1.0 分。
    2. 去掉通用尾巴后为空——《Dark Machine The Animation》与《蔚蓝档案 The Animation》
       会因"the animation"一致而匹配。
    3. 公共内容太少——《女友的朋友》(彼女の友達) 与《妻ネトリ 零 …彼女の選択》只共享
       "彼女の"、《至高之力》与《至高之牌》只共享"至高之"，都属巧合；
       但《青之箱》是《青春之箱》的子序列（同一作品的两种译名），必须放行。
    """
    if not q or not v:
        return True
    if _SEASON_TOKEN_RE.match(q) and _SEASON_TOKEN_RE.match(v):
        return True
    cq, cv = _title_core(q), _title_core(v)
    if not cq or not cv:                 # 只剩通用尾巴（"the animation"、"第2季"）
        return True
    if cq == cv:                         # 本体相同（《青之箱 第二季》↔《青之箱》）
        return False
    if min(len(cq), len(cv)) >= 3 and (_is_subseq(cv, cq) or _is_subseq(cq, cv)):
        return False
    sm = difflib.SequenceMatcher(None, cq, cv)
    matched = "".join(cq[b.a:b.a + b.size] for b in sm.get_matching_blocks())
    core = _NUM_NOISE_RE.sub("", matched)
    if not core:
        return True
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in core)
    # 汉字：至少 4 字共同内容；纯拉丁：至少 8 字符（"lovel"+"e" 这种词头巧合不算）
    return len(core) < (4 if has_cjk else 8)


def emby_remote_search(name):
    """借道 Emby 的 RemoteSearch（识别对话框用的接口）做 TMDB 搜索。
    用户的库按 TMDB 对齐、Emby 自己能访问 TMDB，而本机直连 TMDB 常被墙，
    这样能拿到与库元数据同源的 TMDB ID + 标准译名。"""
    base, key = emby_base(), (CONFIG.get("emby_api_key") or "").strip()
    if not base or not key or not (name or "").strip() or time.time() < _RS_DOWN_UNTIL[0]:
        return []
    url = base + "/Items/RemoteSearch/Series?api_key=" + key
    body = json.dumps({"SearchInfo": {"Name": name}, "ItemId": None}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace"))
        return out if isinstance(out, list) else out.get("Items", [])
    except Exception as e:
        log.warning("Emby RemoteSearch 失败 [%s]: %s", name, e)
        _RS_DOWN_UNTIL[0] = time.time() + 600
        return []


def _lcs_len(a, b):
    """最长公共子串长度（用于判断两个译名是否指同一部作品的共同词）。
    纯 ASCII 短串（如 "part2"）没有区分度，不计入。"""
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    s = a[m.a:m.a + m.size]
    if not s:
        return 0
    if not any("\u4e00" <= ch <= "\u9fff" for ch in s) and len(s) < 6:
        return 0
    return m.size


def bangumi_search_cn(title):
    """Bangumi 搜索：AniBT 番表标题 → 标准中文译名候选列表。
    Emby 库元数据多来自 Bangumi，译名一致；番表的译名常常完全不同
    （如《底牌很多的维多利亚》vs《奇招百出的维多利亚》），字符串相似度无法桥接。"""
    if not title:
        return []
    url = "https://api.bgm.tv/search/subject/" + urllib.parse.quote(search_term(title)) + \
          "?" + urllib.parse.urlencode({"type": "2", "max_results": "5"})
    req = urllib.request.Request(url, headers={
        "User-Agent": "workbuddy/anime-reminder (https://anibt.net helper)",
        "Accept": "application/json"})
    out = []
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="replace"))
        for r in data.get("list", []):
            for n in (r.get("name_cn"), r.get("name")):
                n = (n or "").strip()
                if n and n not in out:
                    out.append(n)
    except Exception as e:
        log.warning("Bangumi 搜索失败 [%s]: %s", title, e)
    return out


def match_emby(anime, tmdb_info, extra_titles=()):
    """在 Emby 全库中挑最匹配的：优先 TMDB ID 对齐，其次标题相似度（双侧分段 + 去季度后缀比对）。"""
    queries = []
    for t in [anime["title"]] + list(extra_titles):
        for base in (t, search_term(t)):
            for qv in _match_variants(base):
                if qv not in queries:
                    queries.append(qv)
    # 候选排序键（从强到弱）：
    #   1) TMDB ID 精确命中
    #   2) 名称相似度
    #   3) 剧集优先（避免同分的电影/剧场版抢走）
    #   4) 首播年贴近番表放送年（-gap，gap 越小键越大）
    # 为什么需要 1) 和 4)：同名新旧两版会拿到相同的名称分。实测《乱马1/2 第三季》
    # 对《乱马½》1989 版与 2024 版名称都算 1.0 分，原实现"先到先得"选中 1989 版
    # （Id 更小），而 1989 版最新一集停留在 1992 年，触发 build_episode_detail 的
    # "旧季"守卫把在播集记成缺集 → 卡片出现假的"Emby 缺N集"。
    # TMDB ID 与首播年都能可靠区分新旧两版；库元数据按 TMDB 对齐，故 1) 最可信。
    try:
        want_year = int(str(anime.get("premiere") or "")[:4])
    except (TypeError, ValueError):
        want_year = None
    want_tmdb = str(tmdb_info["id"]) if (tmdb_info and tmdb_info.get("id")) else None
    best, best_key, best_movie, best_score = None, None, False, 0.0
    best_pair = ["", ""]
    for c in emby_library():
        variants = _match_variants(c.get("Name", ""))
        if not variants:
            continue
        score, pair = 0.0, ["", ""]
        for q in queries:
            for v in variants:
                s = difflib.SequenceMatcher(None, q, v).ratio()
                # 长度守卫：候选段不能太短，防止《地狱模式 第2期》匹配到《地狱》
                if s <= score or len(v) < 0.6 * max(len(q), 1):
                    continue
                # 区分度守卫：排除"第3季 ≈ 第3話"这类只有季度标记吻合的误配
                if _weak_pair(q, v):
                    continue
                score, pair = s, [q, v]
        tmdb_exact = bool(want_tmdb and
                          str((c.get("ProviderIds") or {}).get("Tmdb", "")) == want_tmdb)
        if tmdb_exact:
            score = 1.0
        py = c.get("ProductionYear")
        gap = abs(py - want_year) if (want_year and isinstance(py, int) and py > 0) else 9999
        is_series = (c.get("Type") != "Movie")
        key = (1 if tmdb_exact else 0, round(score, 4), 1 if is_series else 0, -gap)
        if best_key is None or key > best_key:
            best, best_key, best_movie, best_pair = c, key, (not is_series), pair
            best_score = score
    if best is not None and best_score >= 0.6:
        log.info("名称命中 %.2f [%s] ≈ [%s] -> 库[%s](年%s)",
                 best_score, best_pair[0][:26], best_pair[1][:26],
                 (best.get("Name") or "")[:30], best.get("ProductionYear"))
        return str(best["Id"]), best.get("Name", ""), round(best_score, 3), best_movie
    return None, None, round(best_score, 3), False


def emby_fetch_episodes(emby_id):
    base, key = emby_base(), (CONFIG.get("emby_api_key") or "").strip()
    url = base + "/Shows/%s/Episodes?%s" % (emby_id, urllib.parse.urlencode(
        {"Fields": "PremiereDate,Name", "api_key": key}))
    try:
        return _get_json(url, timeout=20).get("Items", [])
    except Exception as e:
        log.warning("Emby 剧集获取失败: %s", e)
        return []


def expected_eps(anime, now_jst=None):
    """按首播日 + 每周推算的应播集数 [(集数, 北京时间 "YYYY-MM-DD HH:MM")]，只含已到时间的。
    有 AniBT 锚点（真实播出时刻）时优先用它，可精确处理深夜档与时区跨界。"""
    now_bjt = datetime.now(BJT)
    anchor_ts = anime.get("anibt_anchor_ts")
    if anchor_ts and anime.get("premiere"):
        try:
            anchor = datetime.fromtimestamp(int(anchor_ts), BJT)
            d0 = datetime.strptime(str(anime["premiere"])[:10], "%Y-%m-%d")
            anchor_ep = max(1, (anchor.date() - d0.date()).days // 7 + 1)
            total = int(anime.get("total") or 0)
            span = max(total, anchor_ep, 1)
            out = []
            for ep in range(1, span + 1):
                t = anchor + timedelta(days=7 * (ep - anchor_ep))
                if t > now_bjt:
                    break
                out.append((ep, t.strftime("%Y-%m-%d %H:%M")))
            if out:
                return out
        except Exception:
            pass
    now_jst = now_jst or datetime.now(JST)
    if anime["is_net"]:
        m = re.search(r"(\d{1,2})/(\d{1,2})", anime.get("note", ""))
        if not m:
            return []
        mo_, dy = int(m.group(1)), int(m.group(2))
        year = now_jst.year
        if mo_ < now_jst.month - 6:
            year += 1
        elif mo_ > now_jst.month + 6:
            year -= 1
        dt = datetime(year, mo_, dy, 10, 0, tzinfo=JST)
        if dt <= now_jst:
            return [(1, dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M"))]
        return []
    first = air_datetime_jst(anime)
    if first is None:
        return []
    out, ep = [], 1
    while True:
        air = first + timedelta(days=7 * (ep - 1))
        if air > now_jst or (anime["total"] and ep > anime["total"]) or ep > 200:
            break
        out.append((ep, air.astimezone(BJT).strftime("%Y-%m-%d %H:%M")))
        ep += 1
    return out


_ONDEMAND_LOCK = threading.Lock()
_ONDEMAND_RUNNING = set()    # 正在后台对齐的番名（防重复派发）


def _kick_match(anime):
    """把单番对齐派发到后台线程，立即返回 True（前端据此轮询）。"""
    t = anime["title"]
    with _ONDEMAND_LOCK:
        if t in _ONDEMAND_RUNNING:
            return True
        _ONDEMAND_RUNNING.add(t)
    with LOCK:
        if not EMBY_MAP.get(t):
            EMBY_MAP[t] = {"state": "pending"}   # 占位，防止重复派发/误报未入库
            save_json(EMBY_MAP_PATH, EMBY_MAP)

    def _run():
        try:
            map_one(anime)
        except Exception:
            log.exception("后台对齐失败: %s", t)
        finally:
            with _ONDEMAND_LOCK:
                _ONDEMAND_RUNNING.discard(t)

    threading.Thread(target=_run, daemon=True).start()
    return True


def build_episode_detail(anime, map_entry):
    """点击卡片时的剧集明细：Emby 每季每集状态（有/缺/未播）。"""
    exp = expected_eps(anime)                       # [(ep, "YYYY-MM-DD HH:MM")]
    exp_map = dict(exp)
    seasons = {}

    if map_entry.get("emby_id") and not map_entry.get("movie"):
        cur_season = None
        for it in emby_fetch_episodes(map_entry["emby_id"]):
            s = it.get("ParentIndexNumber")
            e = it.get("IndexNumber")
            if s is None or e is None:
                continue
            s, e = int(s), int(e)
            if s <= 0:
                s = 0  # 特别篇
            seasons.setdefault(s, {})[e] = {
                "ep": e, "date": (it.get("PremiereDate") or "")[:10],
                "name": it.get("Name") or "", "status": "have",
            }
        # 当前季 = 有正片集数的最大季号（无则 1）
        pos = [s for s in seasons if s > 0]
        cur_season = max(pos) if pos else (1 if seasons else None)
        if cur_season is not None and exp_map:
            # 匹配到的可能只是同一作品的"旧季"（新放送季尚未入库，如《重生之道》
            # vs 新放送的三天战争篇）：若该季最新一集的首播日比番表应播日早 45 天
            # 以上，说明正在放送的季不在库里——期望集放进"当前季+1"（虚拟季）
            # 一律记缺，防止被旧季里同集数的集数冒名顶替成"已跟更"。
            stale = False
            cur = seasons.setdefault(cur_season, {})
            lib_dates = [v.get("date", "")[:10] for v in cur.values() if v.get("date")]
            if lib_dates:
                try:
                    newest = max(datetime.strptime(d, "%Y-%m-%d") for d in lib_dates)
                    if min((datetime.strptime(ds[:10], "%Y-%m-%d") - newest).days
                           for ds in exp_map.values()) > 45:
                        stale = True
                except ValueError:
                    pass
            if stale:
                cur_season += 1
                cur = seasons.setdefault(cur_season, {})
            for e, date_str in sorted(exp_map.items()):
                if stale or e not in cur:
                    cur[e] = {"ep": e, "date": date_str[:10], "name": "第%d话" % e,
                              "status": "miss"}
                elif not cur[e].get("date"):
                    cur[e]["date"] = date_str[:10]
        season_list = [{"season": s, "episodes": [seasons[s][e] for e in sorted(seasons[s])]}
                       for s in sorted(seasons)]
        return {"state": "matched", "seasons": season_list, "exp_count": len(exp_map),
                "cur_season": cur_season}

    # Emby 未匹配 / 未配置 / 电影形式收录：只给推算的播出表
    eps = [{"ep": e, "date": d[:10], "name": "第%d话" % e, "status": "no_emby"} for e, d in exp]
    return {"state": "movie" if map_entry.get("movie") else map_entry.get("state", "no_server"),
            "seasons": [{"season": 1, "episodes": eps}] if eps else [],
            "exp_count": len(exp), "cur_season": 1}


def map_one(anime):
    """对单部番做 TMDB 对齐 + Emby 匹配 + 缺集统计，写入 EMBY_MAP 缓存。"""
    title = anime["title"]
    with LOCK:
        cached = EMBY_MAP.get(title, {})
    has_server = bool(emby_base() and (CONFIG.get("emby_api_key") or "").strip())
    if not has_server and not (CONFIG.get("tmdb_api_key") or "").strip():
        entry = dict(cached, state="no_server", checked_at=datetime.now(BJT).isoformat())
        with LOCK:
            EMBY_MAP[title] = entry
            save_json(EMBY_MAP_PATH, EMBY_MAP)
        return

    if cached.get("tmdb_id"):
        # 已有 TMDB 对齐结果，直接复用，避免重复请求（代理不稳定时尤其重要）
        tmdb_info = {"id": cached["tmdb_id"], "name": cached.get("tmdb_name") or "", "original": ""}
    else:
        # 番表/AniBT 给的日文原名：TMDB 自由译名对不上时的决定性校验依据
        tmdb_info = tmdb_search_tv(title, [anime.get("title_jp")]) or \
            ({"id": None, "name": "", "original": ""} if not has_server else None)
    emby_id, emby_name, score, is_movie = (None, None, 0.0, False)
    miss = None
    bangumi_cn = None
    tmdb_hit = None          # RemoteSearch 命中的 TMDB 信息（库按 TMDB 对齐，最可信）
    chan = "名称"            # 记录最终由哪条通道命中，便于审计误配
    # AniBT 提供的日文原名与 Bangumi 标准名，作为标题比对的额外候选（对齐率提升明显）
    extra = []
    if anime.get("title_jp"):
        extra.append(anime["title_jp"])
    if anime.get("bgm_id"):
        subj = anibt_bgm_subject(anime["bgm_id"])
        if subj:
            for nm in (subj.get("name_cn"), subj.get("name")):
                if nm and nm not in extra:
                    extra.append(nm)
    if has_server:
        emby_id, emby_name, score, is_movie = match_emby(anime, tmdb_info, extra_titles=extra)
        if score < 0.9:
            # 通道 1：借道 Emby RemoteSearch 拿 TMDB ID / 标准译名
            # 库元数据按 TMDB 对齐 → 命中 TMDB ID 即精确对齐。
            # 注意：RemoteSearch 本身也是模糊文本搜索，可能返回完全无关的作品
            # （实测"しろたん"→《浪客剑心》、"Dark Machine"→《蔚蓝档案》），
            # 因此候选要么名称与番名同源，要么其 TMDB ID 在库内条目的**原题名**与番表日文原名一致。
            tmdb_idx = None
            jp_bases = [e for e in extra if e] + [title]
            # 中文查询词搜不到时用日文原名兜底（RemoteSearch 对中文新季译名常零结果，
            # 实测《东京复仇者 三天战争篇》日文原名可精确命中 TMDB 105009）
            rs_queries = [search_term(title), title]
            tj = (anime.get("title_jp") or "").strip()
            if tj:
                rs_queries += [search_term(tj), tj]
            for q in dict.fromkeys(q for q in rs_queries if q):
                for rs in emby_remote_search(q):
                    rid = str((rs.get("ProviderIds") or {}).get("Tmdb", "") or "")
                    rname = (rs.get("Name") or "").strip()
                    if not rname and not rid:
                        continue
                    ok = _candidate_related(title, rname, extra)
                    if not ok and rid:
                        # 名称可能只是"自由译名"（《转生就是剑》↔《转生成为魔剑》），
                        # 改用原题名做决定性校验
                        if tmdb_idx is None:
                            tmdb_idx = _emby_index_by_tmdb()
                        ot = ((tmdb_idx.get(rid) or {}).get("OriginalTitle") or "").strip()
                        if ot and any(_jp_same(ot, j) for j in jp_bases):
                            ok = True
                            log.info("RemoteSearch 候选经原题名校验通过：[%s] ≙ [%s]",
                                     ot[:30], rname[:24])
                    if not ok:
                        log.info("RemoteSearch 候选与番名无关联，弃用：[%s] ≠ [%s]",
                                 rname[:34], title[:24])
                        continue
                    info = {"id": rid, "name": rname, "original": ""} if rid else None
                    e2, n2, s2, m2 = match_emby(
                        anime, info, extra_titles=extra + ([rname] if rname else []))
                    if e2 and s2 > score and (s2 >= 0.85 or (rid and s2 >= 1.0)):
                        emby_id, emby_name, score, is_movie = e2, n2, s2, m2
                        tmdb_hit = {"id": rid, "name": rname} if rid else None
                        chan = "RemoteSearch" + ("(TMDB %s)" % rid if rid else "(仅名称)")
        if score < 0.9:
            # 通道 2：Bangumi 标准中译名（含日文名）。
            # Bangumi 是模糊搜索，会混入无关候选（可能恰好与库里别的条目同名），
            # 因此候选必须与番表原名共享 ≥5 字子串（如《底牌很多的维多利亚》↔
            # 《奇招百出的维多利亚》共享"的维多利亚"=5 字；而《世界最强的魔女，就此开始》↔
            # 《平凡职业造就世界最强》只共享"世界最强"=4 字，必须拒绝），
            # 且能在库里获得近乎精确的段匹配（≥0.85）时才采纳。
            base_norms = [norm_title(title), norm_title(search_term(title))]
            for cand in bangumi_search_cn(title):
                cn_norm = norm_title(cand)
                if max(_lcs_len(b, cn_norm) for b in base_norms) < 5:
                    continue
                e2, n2, s2, m2 = match_emby(anime, tmdb_info, extra_titles=extra + [cand])
                if e2 and s2 > score and s2 >= 0.85:
                    emby_id, emby_name, score, is_movie = e2, n2, s2, m2
                    bangumi_cn = cand
                    chan = "Bangumi"
                    break
        if emby_id and not is_movie:
            detail = build_episode_detail(anime, {"emby_id": emby_id})
            cur = [s for s in detail["seasons"] if s["season"] == detail.get("cur_season")]
            if cur and detail.get("exp_count"):
                miss = sum(1 for e in cur[0]["episodes"] if e["status"] == "miss")
            else:
                miss = 0
    if emby_id:
        state = "matched"
    elif has_server:
        # 未入库：应播过的每一集都算缺失
        state = "not_found"
        miss = len(expected_eps(anime))
    else:
        state = "tmdb_only" if (tmdb_info and tmdb_info.get("id")) else "no_server"
    tmdb_e = tmdb_hit or tmdb_info
    entry = {"state": state, "tmdb_id": tmdb_e["id"] if tmdb_e else None,
             "tmdb_name": tmdb_e.get("name") if tmdb_e else None,
             "bangumi_cn": bangumi_cn,
             "emby_id": emby_id, "emby_name": emby_name, "score": score,
             "movie": is_movie, "miss": miss,
             "lib_sig": emby_lib_sig(),
             "checked_at": datetime.now(BJT).isoformat()}
    with LOCK:
        EMBY_MAP[title] = entry
        save_json(EMBY_MAP_PATH, EMBY_MAP)
    log.info("对齐[%s]: %s%s%s", title[:20], state,
             " TMDB:%s" % entry["tmdb_id"] if entry["tmdb_id"] else "",
             (" [%s] Emby:%s 缺%s集" % (chan, emby_name, miss)) if emby_id else "")


def _rematch_track(a):
    """「立即对齐」进度记账：目标条目每查完一个就更新，供前端通知条轮询展示。"""
    p = _REMATCH_PROG
    if not p["total"] or p["left"] <= 0:
        return
    _REMATCH_DONE.add(a.get("title"))
    if a.get("title") in p["titles"]:
        p["titles"].discard(a["title"])
        p["left"] = len(p["titles"])
        c = EMBY_MAP.get(a["title"]) or {}
        if c.get("emby_id"):
            p["new"] += 1
            if len(p["names"]) < 12:
                p["names"].append(c.get("emby_name") or a["title"])


def map_loop():
    """后台逐个对齐（限速），已匹配的每 6 小时刷新缺集数。
    not_found 条目：检测到 Emby 库有新入库内容（库指纹变化）后 5 分钟即重查，
    解决「先对齐后入库」要死等 6 小时的问题（如 10-07 倪亚入库仍显示未入库）。
    手动「立即对齐」（_REMATCH_NOW）会跳过全部时间门槛立即重查未匹配条目。"""
    stale_ok = timedelta(hours=6)
    stale_fail = timedelta(hours=6)
    stale_nosig = timedelta(minutes=30)   # 旧条目无 lib_sig：降级为 30 分钟重查一次
    fresh_gate = timedelta(minutes=5)     # 库刚变化后的最短等待，避免高频重查
    while True:
        try:
            lst = DATA_HOLDER.get("list") or []
            now = datetime.now(BJT)
            has_server = bool(emby_base() and (CONFIG.get("emby_api_key") or "").strip())
            sig = emby_lib_sig() if has_server else ""
            rematch = time.time() < _REMATCH_UNTIL[0]
            todo = []
            for a in lst:
                if a["title"] in _REMATCH_INFLIGHT:
                    continue   # 并发在途：EMBY_MAP 结果还没写回，重复选入会浪费并发槽
                c = EMBY_MAP.get(a["title"])
                if c is None:
                    todo.append(a)
                    continue
                # 之前没配置 Emby 时的旧结果，配置后立即重新检测
                if c.get("state") in ("no_server", "no_tmdb_no_server", "tmdb_only") and has_server:
                    todo.append(a)
                    continue
                age = now - datetime.fromisoformat(c["checked_at"])
                limit = stale_ok if c.get("emby_id") else stale_fail
                if age > limit:
                    todo.append(a)
                    continue
                if not c.get("emby_id") and has_server:
                    # 未入库条目：手动立即对齐 → 跳过时间门槛（会话内每条只查一次）
                    if rematch and age > timedelta(seconds=10):
                        if a["title"] not in _REMATCH_DONE:
                            todo.append(a)
                        continue
                    # 库指纹与上次检查时不同（有新入库）→ 5 分钟后即重查
                    esig = c.get("lib_sig")
                    if not esig:
                        if age > stale_nosig:
                            todo.append(a)
                    elif sig and sig != esig and age > fresh_gate:
                        todo.append(a)
            if not todo:
                _REMATCH_NOW.wait(600)
                _REMATCH_NOW.clear()
                continue
            if rematch and len(todo) > 1:
                # 手动「立即对齐」：I/O 密集（TMDB/Emby/Bangumi 外部请求），并发跑
                # 6 路把 40s/部 的串行节奏压到 ~1/6。EMBY_MAP 更新有锁、库缓存竞争
                # 最坏只是重复拉一次，安全；在途集合防止同一条目被下一批重复提交。
                batch = todo[:12]
                for a in batch:
                    _REMATCH_INFLIGHT.add(a["title"])
                try:
                    with _THREAD_POOL(3) as ex:
                        futs = {ex.submit(map_one, a): a for a in batch}
                        for f in as_completed(futs):
                            a2 = futs[f]
                            try:
                                f.result()
                            except Exception as e:
                                log.warning("并行对齐[%s]失败: %s", a2["title"][:20], e)
                            _rematch_track(a2)
                finally:
                    for a in batch:
                        _REMATCH_INFLIGHT.discard(a["title"])
                time.sleep(0.5)
            else:
                for a in todo[:5]:
                    map_one(a)
                    _rematch_track(a)
                    time.sleep(1.5)
        except Exception as e:
            log.exception("对齐循环异常: %s", e)
            time.sleep(60)


# ---------------------------------------------------------------------------
# 可视化面板（本地 HTTP 服务）
# ---------------------------------------------------------------------------
# (可视化面板 HTML 已移除 —— 统一由 AnimeFansubGuide 页面呈现)



def build_api_payload(anime_list):
    now = datetime.now(BJT)
    items = []
    for a in anime_list:
        a = dict(a)
        attach_release(a)   # 每次请求实时挂当前缓存的资源进度，卡片角标随缓存预热即时更新
        m = EMBY_MAP.get(a["title"])
        if m:
            a["emby_state"] = m.get("state", "pending")
            a["miss"] = m.get("miss")
            a["emby_name"] = m.get("emby_name")
            a["tmdb_name"] = m.get("tmdb_name")
            a["movie"] = m.get("movie", False)
        else:
            a["emby_state"] = "pending"
        a["anirss"] = anirss_state_for(a)   # ani-rss 订阅状态；未开启同步时为 None
        items.append(a)
    return {
        "season": DATA_HOLDER.get("label") or source_label(),
        "anibt_season": _anibt_season_arg(),
        "rematch": (lambda p: {"done": p["total"] - p["left"], "total": p["total"],
                               "new": p["new"], "names": list(p["names"])}
                    if p["total"] else None)(_REMATCH_PROG),
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "today": now.strftime("%Y-%m-%d"),
        "today_wd": now.isoweekday(),
        "now_ts": now.timestamp(),
        "sub_ids": CONFIG.get("subscribe_ids", []),
        "has_emby": bool(emby_base() and (CONFIG.get("emby_api_key") or "").strip()),
        "has_anibt": any(a.get("bgm_id") for a in anime_list),
        "anirss": {
            "configured": anirss_configured(),
            "synced": bool(_ANIRSS["ts"]),
            "count": _ANIRSS["count"],
            "err": _ANIRSS["err"],
            "allow_subscribe": bool(CONFIG.get("anirss_allow_subscribe")),
        },
        "config": {k: CONFIG.get(k) for k in
                   ("dingtalk_webhook", "dingtalk_secret", "notify_times", "notify_digest",
                    "advance_minutes", "subscribe_mode",
                    "emby_url", "emby_api_key", "tmdb_api_key", "tmdb_proxy",
                    "anibt_season", "notify_release",
                    "anirss_url", "anirss_api_key", "anirss_sync",
                    "anirss_allow_subscribe", "anirss_sync_minutes")},
        "anime": items,
    }



# ---------------------------------------------------------------------------
# 后台刷新线程：定时重新抓取番表（每 6 小时 + 启动时，配置变更时立即刷新）
# ---------------------------------------------------------------------------
DATA_HOLDER = {"list": [], "label": ""}
REFRESH_NOW = threading.Event()


def build_anime_list():
    """构建番表。数据源固定为 AniBT（長門番堂已于 2026-10-03 移除）。"""
    errs = []
    try:
        anibt_list, _ = anibt_fetch_season(force=True)
    except Exception as e:
        errs.append("AniBT: %s" % e)
        anibt_list = []
    lst = anibt_list
    if not lst:
        raise RuntimeError("数据源不可用：%s" % ("；".join(errs) or "未知错误"))
    for i, a in enumerate(lst, 1):
        a["id"] = i
    decorate(lst)
    DATA_HOLDER["label"] = season_label_text(len(lst))
    return lst


def refresh_loop():
    while True:
        try:
            lst = build_anime_list()
            DATA_HOLDER["list"] = lst
            log.info("番表已刷新：%s 共 %d 部", DATA_HOLDER.get("label"), len(lst))
        except Exception as e:
            log.error("番表刷新失败（5 分钟后重试）: %s", e)
            time.sleep(300)
            continue
        REFRESH_NOW.wait(timeout=6 * 3600)
        REFRESH_NOW.clear()


def release_loop():
    """后台渐进式刷新字幕组资源进度（限速 2.2 秒/条，避免触发站点频控）。"""
    time.sleep(25)
    while True:
        try:
            if not CONFIG.get("release_check", True):
                time.sleep(600)
                continue
            lst = DATA_HOLDER.get("list") or []
            now = time.time()
            todo = []
            for a in lst:
                gid = a.get("bgm_id")
                if not gid:
                    continue
                with ANIBT_LOCK:
                    c = _ANIBT_GROUPS.get(str(gid))
                if not c or now - (c.get("ts") or 0) > ANIBT_GROUPS_TTL:
                    todo.append(a)
            if not todo:
                time.sleep(300)
                continue
            done = 0
            for a in todo[:60]:
                if anibt_groups(a.get("bgm_id")) is not None:
                    done += 1
                time.sleep(2.2)
            if done:
                log.info("字幕组资源进度已刷新 %d 部（剩余 %d 部下轮继续）", done, max(0, len(todo) - done))
                save_groups_cache(force=True)
        except Exception as e:
            log.exception("资源进度刷新异常: %s", e)
            time.sleep(60)


# ---------------------------------------------------------------------------
# 内嵌引擎 API（由 anime_fansub_server 调用；逻辑与原 HTTP 处理器逐行对应）
# ---------------------------------------------------------------------------
def api_data():
    hidden = set(CONFIG.get("hidden_ids", []))
    if not hidden:
        return build_api_payload(DATA_HOLDER["list"])
    visible = [a for a in DATA_HOLDER["list"] if a["id"] not in hidden]
    payload = build_api_payload(visible)
    # 隐藏项单独带回（前端「已隐藏 N 部」入口可查看/恢复）
    payload["hidden"] = [dict(a, hidden=True) for a in DATA_HOLDER["list"] if a["id"] in hidden]
    return payload


def api_hide(aid):
    """软删除：把番剧移出提醒列表（可恢复）。数据源是番表，无法真删。"""
    aid = int(aid)
    with LOCK:
        ids = CONFIG.setdefault("hidden_ids", [])
        if aid in ids:
            ids.remove(aid)
        else:
            ids.append(aid)
        save_json(CONFIG_PATH, CONFIG)
    return api_data()


def api_episodes(aid):
    anime = next((a for a in DATA_HOLDER["list"] if a["id"] == aid), None)
    if not anime:
        return {"error": "anime not found"}
    with LOCK:
        entry = dict(EMBY_MAP.get(anime["title"], {}))
    matching = False
    has_srv = bool(emby_base() and (CONFIG.get("emby_api_key") or "").strip())
    if entry.get("state") in ("pending",) or (not entry and has_srv):
        # 首次点击且后台还没轮到：把对齐丢进后台线程，接口立即返回。
        matching = _kick_match(anime)
    elif entry.get("state") == "not_found" and has_srv:
        # 库条目常晚于首播才入库/对齐元数据（如新番首播当晚才进库）：
        # 用户点开明细就是明确的重查意图，超过 10 分钟的失败结果就地重查，
        # 不必苦等 map_loop 的 6 小时周期（not_found 不重查会一直显示"未找到"）。
        try:
            age = datetime.now(BJT) - datetime.fromisoformat(entry["checked_at"])
        except (KeyError, ValueError, TypeError):
            age = timedelta(hours=9)
        if age > timedelta(minutes=10):
            matching = _kick_match(anime)
    detail = build_episode_detail(anime, entry)
    detail["matching"] = matching
    detail["anime"] = {"id": anime["id"], "title": anime["title"],
                       "title_jp": anime.get("title_jp") or "",
                       "weekday_name": anime["weekday_name"], "premiere": anime["premiere"],
                       "total": anime["total"], "img": anime["img"],
                       "bgm_id": anime.get("bgm_id"), "anibt_id": anime.get("anibt_id"),
                       "rating": anime.get("rating"), "source": anime.get("source")}
    detail["tmdb_id"] = entry.get("tmdb_id")
    detail["tmdb_name"] = entry.get("tmdb_name")
    detail["emby_name"] = entry.get("emby_name")
    detail["score"] = entry.get("score")
    if anime.get("bgm_id"):
        g = anibt_groups(anime["bgm_id"],
                         since_ms=_premiere_cutoff_ms(anime.get("premiere")))
        if g:
            detail["groups"] = g["groups"]
            detail["rel_max_ep"] = g["max_ep"]
            detail["rel_count"] = g["group_count"]
            if g.get("title_jp") and not detail["anime"]["title_jp"]:
                detail["anime"]["title_jp"] = g["title_jp"]
    return detail


def api_toggle(aid):
    aid = int(aid)
    with LOCK:
        ids = CONFIG.setdefault("subscribe_ids", [])
        if aid in ids:
            ids.remove(aid)
        else:
            ids.append(aid)
        if not ids:
            CONFIG["subscribe_mode"] = "all"
        elif CONFIG.get("subscribe_mode") == "all":
            CONFIG["subscribe_mode"] = "selected"
        save_json(CONFIG_PATH, CONFIG)
    return api_data()


def api_test():
    sample = next((a for a in DATA_HOLDER["list"] if a.get("img")), None)
    img = "\n\n![封面](%s)" % sample["img"] if sample else ""
    name = "\n\n示例推送：《%s》" % sample["title"] if sample else ""
    ok, msg = send_dingtalk_markdown(
        "测试消息",
        "## ✅ 番剧提醒助手测试成功%s%s\n\n> 来自 %s 面板 · %s" % (
            name, img, source_label(), datetime.now(BJT).strftime("%H:%M:%S")))
    return {"ok": ok, "msg": msg}


def api_subscribe(aid):
    a = next((x for x in DATA_HOLDER["list"] if int(x.get("id", 0)) == int(aid)), None)
    if a is None:
        return {"ok": False, "msg": "未找到该番剧"}
    ok2, msg2 = anirss_subscribe(a)
    return {"ok": ok2, "msg": msg2}


def api_rematch():
    """手动「立即对齐」：全部未匹配（未入库）条目跳过时间门槛立即重查。"""
    if not (emby_base() and (CONFIG.get("emby_api_key") or "").strip()):
        return {"ok": False, "msg": "未配置 Emby，无法对齐"}
    n = sum(1 for a in (DATA_HOLDER.get("list") or [])
            if not (EMBY_MAP.get(a["title"]) or {}).get("emby_id"))
    _REMATCH_PROG.update({"total": n, "left": n, "new": 0, "names": [],
                          "titles": {a["title"] for a in (DATA_HOLDER.get("list") or [])
                                     if not (EMBY_MAP.get(a["title"]) or {}).get("emby_id")}})
    _REMATCH_DONE.clear()
    _REMATCH_UNTIL[0] = time.time() + 900   # 足够 6 并发跑完全部队列（180s 曾导致后半程退回串行门槛停摆）
    _REMATCH_NOW.set()
    log.info("手动触发立即对齐：%d 个未匹配条目进入重查队列", n)
    return {"ok": True, "msg": "已开始重新对齐 %d 部，结果将在 1~3 分钟内陆续更新" % n, "kick": n}


def api_sync():
    ok2 = anirss_ensure_cache(force=True)
    with ANIRSS_LOCK:
        cnt, err = _ANIRSS["count"], _ANIRSS["err"]
    return {"ok": ok2,
            "msg": ("已同步 %d 条 ani-rss 订阅" % cnt) if ok2 else (err or "同步失败")}


def api_config(body):
    allowed = ("dingtalk_webhook", "dingtalk_secret", "notify_times", "notify_digest",
               "advance_minutes", "subscribe_mode",
               "emby_url", "emby_api_key", "tmdb_api_key", "tmdb_proxy",
               "anibt_season", "notify_release", "release_check",
               "anirss_url", "anirss_api_key", "anirss_sync",
               "anirss_allow_subscribe", "anirss_sync_minutes")
    body = body or {}
    # subscribe_mode 只接受合法枚举；历史 bug（前端把 SELECT 统一按布尔保存）
    # 曾写入 false，导致 subscribed() 全部落空、单集/资源提醒静默停发。
    if "subscribe_mode" in body and body["subscribe_mode"] not in ("all", "selected", "anirss"):
        body["subscribe_mode"] = "all"
    if "notify_digest" in body:
        body["notify_digest"] = bool(body["notify_digest"])
    emby_changed = (body.get("emby_url", CONFIG.get("emby_url")) != CONFIG.get("emby_url") or
                    body.get("emby_api_key", CONFIG.get("emby_api_key")) != CONFIG.get("emby_api_key"))
    src_changed = (str(body.get("anibt_season", CONFIG.get("anibt_season")) or "").strip()
                   != str(CONFIG.get("anibt_season") or "").strip())
    anirss_changed = (
        str(body.get("anirss_url", CONFIG.get("anirss_url")) or "").strip()
        != str(CONFIG.get("anirss_url") or "").strip() or
        str(body.get("anirss_api_key", CONFIG.get("anirss_api_key")) or "").strip()
        != str(CONFIG.get("anirss_api_key") or "").strip())
    with LOCK:
        for k in allowed:
            if k in body:
                CONFIG[k] = body[k]
        save_json(CONFIG_PATH, CONFIG)
        if emby_changed:
            EMBY_MAP.clear()
            save_json(EMBY_MAP_PATH, EMBY_MAP)
            log.info("Emby 配置已变更，对齐缓存已清空，将重新检测")
    if src_changed:
        with ANIBT_LOCK:
            _ANIBT_CACHE.update(ts=0.0, season=None, list=[])
        REFRESH_NOW.set()
        log.info("数据源配置已变更，立即刷新番表")
    if anirss_changed:
        threading.Thread(target=anirss_ensure_cache, kwargs={"force": True},
                         daemon=True).start()
        log.info("ani-rss 配置已变更，正在刷新订阅缓存")
    log.info("配置已更新")
    return {"ok": True, "reload": True}


def start_background():
    """Initial fetch + the five daemon loops (same set as the old main())."""
    try:
        lst = build_anime_list()
        DATA_HOLDER["list"] = lst
        log.info("番表加载完成：%s 共 %d 部", DATA_HOLDER.get("label"), len(lst))
    except Exception as e:
        log.error("启动抓取失败（将稍后重试）: %s", e)
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=notify_loop, args=(DATA_HOLDER,), daemon=True).start()
    threading.Thread(target=map_loop, daemon=True).start()
    threading.Thread(target=release_loop, daemon=True).start()
    threading.Thread(target=anirss_sync_loop, daemon=True).start()
    log.info("提醒引擎已内嵌启动（原独立服务可关闭）")
