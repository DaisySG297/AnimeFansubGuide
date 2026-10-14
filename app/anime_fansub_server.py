# -*- coding: utf-8 -*-
"""AnimeFansubGuide -- 番剧字幕组推荐指南 本地服务 + ani-rss 订阅推送桥

Why this exists
---------------
ani-rss ships with `allowCors: false` in config.v2.json, so a browser page can
NOT call http://<host>:7789 directly. Every call must be proxied by a local
server. This process is that proxy: it serves the guide page on its own port and
holds the ani-rss apiKey server-side so the credential is never exposed to the
browser (note that ani-rss's own /api/custom.js is world-readable, so leaking a
key into page JS would be a real disclosure).

Safety model for writes
-----------------------
Push uses the documented chain only:
    /api/mikan  ->  /api/mikanGroup  ->  /api/rssToAni  ->  /api/addAni
/api/rssToAni builds the complete Ani object; we never hand-assemble fields.

Two hard rules from the ani-rss skills are enforced here:
  1. NEVER call a write endpoint with an empty/degenerate body. `POST /api/addAni`
     with {} persists a null-field subscription BEFORE validation throws, and the
     resulting releaseDate=null makes /api/listAni and /api/calendar.ics both
     return 500 (the whole WebUI breaks). _validate_ani() blocks this.
  2. Deletion is gated: `AniService.deleteAni` wipes the torrents/<dir> dedup
     records unconditionally (even with deleteFiles=false), which causes full
     re-downloads. The old-subscription module therefore runs the records
     mirror FIRST (blobs land in downloads_mirror.db, restorable) and snapshots
     the full Ani objects, refusing to delete when the mirror fails.

Stdlib only -- no third-party runtime dependencies.
"""

from __future__ import annotations

import base64
import datetime
import difflib
import email.utils
import hmac
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "AnimeFansubGuide"
VERSION = "1.0.0"
DEFAULT_PORT = 7788
DEFAULT_ANIRSS = "http://127.0.0.1:7789"
DEFAULT_REMINDER = "http://127.0.0.1:8903"

# 老订阅清理：删除订阅会无条件清掉 torrents 去重记录目录（deleteFiles=false 也挡不住），
# 所以删除前强制跑 ani_rss_tool.py mirror —— 记录以 blob 存进 downloads_mirror.db，可完整还原。
MIRROR_PY = sys.executable  # 默认用当前解释器；可在 config.json 里用 mirrorPy 指定
MIRROR_TOOL = os.path.join(os.path.expanduser("~"), "ani-rss", "ani_rss_tool.py")

GUIDE_FILE = "anime-fansub-guide.html"
DATA_FILE = "season-data.js"
CONFIG_FILE = "config.json"
LOG_FILE = "server.log"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


# --------------------------------------------------------------------------
# paths: bundled resources vs. mutable state
# --------------------------------------------------------------------------
def bundled_dir() -> str:
    """Directory of read-only resources shipped inside the exe."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def app_dir() -> str:
    """Directory of the running exe (or of this .py file in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """Writable directory for config/log.

    Preferred: next to the exe, so the tool stays portable (one folder = one
    install). Falls back to %LOCALAPPDATA% when that folder is read-only
    (e.g. installed under Program Files).
    """
    cand = app_dir()
    probe = os.path.join(cand, ".write-test")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.remove(probe)
        return cand
    except OSError:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        out = os.path.join(base, APP_NAME)
        os.makedirs(out, exist_ok=True)
        return out


DATA = data_dir()
CONFIG_PATH = os.path.join(DATA, CONFIG_FILE)
LOG_PATH = os.path.join(DATA, LOG_FILE)

# --------------------------------------------------------------------------
# season data 实时同步：页面发现快照过期时触发 refresh-season.ps1 后台重建
# --------------------------------------------------------------------------
SEASON_REFRESH_TIMEOUT = 900   # dmhy 6 页 + mikan + anibt，30s/源最坏也要几分钟
_season_lock = threading.Lock()
_season_state = {"running": False, "startedAt": None, "finishedAt": None,
                 "ok": None, "msg": ""}


def _resource_path(name: str):
    """Locate a servable file, preferring user-editable copies.

    The daily refresh rewrites season-data.js, so a copy sitting next to the
    exe must win over the one frozen inside it -- that way the exe never has
    to be rebuilt to pick up new data. The parent directory is included so
    the server also works when run straight from source (app/ -> project root).
    """
    seen = []
    for base in (DATA, app_dir(), os.path.dirname(app_dir()), bundled_dir()):
        if not base or base in seen:
            continue
        seen.append(base)
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return None


def _find_season_script():
    """refresh-season.ps1 位置：dist/exe 旁的 scripts/，或源码运行的上级目录。"""
    here = app_dir()
    for p in (os.path.join(here, "scripts", "refresh-season.ps1"),
              os.path.join(os.path.dirname(here), "scripts", "refresh-season.ps1"),
              os.path.join(bundled_dir(), "scripts", "refresh-season.ps1")):
        if os.path.isfile(p):
            return p
    return None


def _season_generated_at():
    """当前生效 season-data.js 的 generatedAt（前端加载的就是这一份）。"""
    p = _resource_path(DATA_FILE)
    if not p:
        return None
    try:
        with open(p, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
        m = re.search(r'"generatedAt"\s*:\s*"([^"]+)"', head)
        return m.group(1) if m else None
    except OSError:
        return None


def _no_window_kwargs() -> dict:
    """子进程防弹窗三重保险：CREATE_NO_WINDOW + SW_HIDE + 隐藏启动信息。

    本面板常以管理员令牌运行（Administrator），powershell 等控制台子进程若不加
    这些标志，每次都会在桌面弹出一个「管理员: C:\\WINDOWS\\System32\\...」黑框。
    """
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    kw["startupinfo"] = si
    return kw


def _season_build_worker(ps1: str):
    code, msg = 1, ""
    logf = None
    try:
        root = os.path.dirname(os.path.dirname(ps1))
        cache = os.path.join(root, ".cache")
        os.makedirs(cache, exist_ok=True)
        logf = open(os.path.join(cache, "refresh-server-trigger.log"), "ab")
        logf.write(("\n[server] triggered %s\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            stdout=logf, stderr=subprocess.STDOUT, timeout=SEASON_REFRESH_TIMEOUT,
            **_no_window_kwargs())
        code = r.returncode
        msg = "重建完成" if code == 0 else "重建失败（exit %s），保留旧数据" % code
    except Exception as exc:  # noqa: BLE001
        code, msg = -1, "重建异常：%r" % exc
    finally:
        if logf:
            try:
                logf.close()
            except OSError:
                pass
    with _season_lock:
        _season_state.update(running=False, ok=(code == 0), msg=msg,
                             finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"))
    log("[season] refresh exit=%s %s" % (code, msg))
    if code == 0:
        # 快照更新后自动追新扫描（enable=False 时内部直接返回，开销为 0）
        threading.Thread(target=_autosub_scan, args=("season",), daemon=True).start()

_log_lock = threading.Lock()


def log(msg: str) -> None:
    stamped = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        # 控制台只写一路：console 模式下 stdout/stderr 指向同一控制台，
        # 各写一遍会重复显示；无窗口(--noconsole)时两者皆 None，跳过。
        stream = sys.stdout if sys.stdout is not None else sys.stderr
        try:
            if stream is not None:
                stream.write(str(msg) + "\n")
                stream.flush()
        except Exception:
            pass
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(stamped + "\n")
        except OSError:
            pass


DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "bind": "127.0.0.1",          # "127.0.0.1" = 仅本机; "0.0.0.0" = 允许局域网
    "anirssBase": DEFAULT_ANIRSS,
    "apiKey": "",
    "openBrowser": True,
    "enable": True,               # 新增订阅时是否直接启用
    "defaultMain": "",            # 默认主订阅字幕组（mikan label）
    "defaultStandby": [],         # 默认备用订阅字幕组（mikan label 列表）
    "timeout": 25,
    "reminderBase": DEFAULT_REMINDER,  # 兜底代理目标（引擎未启用时才用）
    "reminder": True,                  # 内嵌「番剧更新提醒」引擎（原独立 exe 已搬入）
    "autoSub": {                       # 全自动追新（默认关，显式开启后才随快照重建自动订阅）
        "enable": False,
        "dryRun": True,                # 安全默认：先演练，确认后再切正式写入
        "minRating": 0,
        "maxPerRun": 5,
        "mainGroups": [],              # 主组限定：命中的第一个当主订阅，兼筛番
        "standbyGroups": [],           # 备组限定：依次命中的加入备用列表
        "keywords": [],
        "exclude": [],
    },
    "standbyCleanup": {                # 备源清理：主组已有同集文件 → 删备组文件/下载
        "enable": False,
        "intervalMin": 10,
        "qbHost": "http://127.0.0.1:33900",
        "qbApiKey": "",
        "deleteFiles": True,
        "scanDirs": ["E:/Emby Media Part1/Bangumi", "F:/Emby Media Part2/Bangumi"],
        "downloadDir": "F:/Bangumi暂存",
    },
}

_config_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh) or {})
    except FileNotFoundError:
        save_config(cfg)
    except Exception as exc:  # noqa: BLE001 - a corrupt config must not brick startup
        log("config unreadable (%s); falling back to defaults" % exc)
    return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def discover_local_key() -> str:
    """Best-effort: read the apiKey from a local ani-rss install.

    Only used to prefill first-run config on this machine; failure is harmless.
    """
    for cand in (
        os.path.join(os.path.expanduser("~"), "ani-rss", "config.v2.json"),
        os.path.join(os.environ.get("USERPROFILE", ""), "ani-rss", "config.v2.json"),
    ):
        try:
            with open(cand, encoding="utf-8") as fh:
                key = (json.load(fh) or {}).get("apiKey") or ""
            if key:
                return key
        except Exception:  # noqa: BLE001
            continue
    return ""


# --------------------------------------------------------------------------
# ani-rss client
# --------------------------------------------------------------------------
class AniRssError(Exception):
    pass


class AniRss:
    """Thin client. HTTP status is ALWAYS 200; the real result is body.code."""

    def __init__(self, base: str, key: str, timeout: int = 25):
        self.base = (base or DEFAULT_ANIRSS).rstrip("/")
        self.key = (key or "").strip()
        self.timeout = timeout

    def call(self, path: str, payload=None, query=None, method: str = "POST"):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body if method == "POST" else None,
            method=method,
            headers={
                "X-Api-Key": self.key,      # the ONLY correct auth scheme
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "%s/%s" % (APP_NAME, VERSION),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise AniRssError("HTTP %s: %s" % (exc.code, exc.read()[:200])) from exc
        except Exception as exc:  # noqa: BLE001
            raise AniRssError("无法连接 %s: %s" % (self.base, exc)) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise AniRssError("响应不是 JSON: %s" % raw[:200]) from exc

    # ---- endpoints ----
    def list_ani(self):
        res = self.call("/api/listAni", {})
        if res.get("code") != 200:
            raise AniRssError(res.get("message") or "listAni 失败")
        return res.get("data") or {}

    def subscriptions(self):
        data = self.list_ani()
        return [it for wk in (data.get("weekList") or []) for it in (wk.get("items") or [])]

    def rss_to_ani(self, rss: str):
        # {"url": ...} is REQUIRED: a bare JSON string makes gson throw
        # "Expected BEGIN_OBJECT but was STRING".
        res = self.call("/api/rssToAni", {"url": rss})
        if res.get("code") != 200 or not isinstance(res.get("data"), dict):
            raise AniRssError(res.get("message") or "rssToAni 失败")
        ani = res["data"]
        # 应用全局排除规则（如 {{三明治摆烂组}}:繁 —— 组内同时发简/繁两版时优先下简中）。
        # ani-rss 该开关默认关闭，不显式打开则全局排除列表形同虚设（2026-10-07 修复）。
        ani["globalExclude"] = True
        return ani

    def add_ani(self, ani: dict):
        res = self.call("/api/addAni", ani)
        if res.get("code") != 200:
            raise AniRssError(res.get("message") or "addAni 失败")
        return res

    def mikan_search(self, text: str):
        """蜜柑候选搜索（渐进降级 + 逐级合并去重）。
        蜜柑站内搜索（/Home/Search?searchstr=）对完整标题经常 0 命中——
        实测《侦探已经死了。 第二季》全名、去季名、日文原名全为 0 条，
        而尾 4 字「已经死了」反而命中。因此逐级缩短关键词。
        ⚠️ 不能「命中即返回」：蜜柑对长标题只匹配到部分条目——实测
        「魔法姐妹露露特莉莉」11 字全名只出第 1 部，「露特莉莉」才同时
        命中第 1 部 + 第 2 部分（2026-10-14 实测）。所以逐级搜索后按
        url 合并去重，越具体的查询排越前；凑够 8 条提前收工控制时延。
        全部 0 命中时返回空列表（不报错，前端正常展示"没有匹配条目"）。"""
        base = (text or "").strip()
        # ⚠️ "部分"是两个字，必须用分组 (?:部分|…) 而非单字类 [季期部]，
        #    否则「第2部分」剥不掉 → 阶梯回退 q0[-4:] 恰好取到「第2部分」，
        #    蜜柑会按这 4 个字返回一堆不相干的第二季番（2026-10-14 事故）。
        q0 = re.sub(r"[\s。．.·]*第\s*[0-9０-９一二三四五六七八九十]+\s*(?:部分|季|期|部|章|章节)\s*$", "", base)
        q0 = re.sub(r"[\s]*[Ss]eason\s*[0-9]+\s*$", "", q0).strip(" 。．.·\t")
        # ⚠️ 尾部语气词会拖垮整个回退：「…疯狂点击女孩子啦」按尾 4 字"女孩子啦"必落空，
        #    剥掉"啦"后尾 4 字窗口才落得进「点击女孩子」（蜜柑条目是日文名，靠别名匹配）（2026-10-14 实测）
        q0 = re.sub(r"[啦吗呢吧哦噢嘛呀哇咯喽哒]$", "", q0).strip(" 。．.·\t")
        ladder = [base, q0]
        # ⚠️ 回退键的选取规则（2026-10-14 三连事故的教训）：
        #    ① 禁止盲取尾 4 字——混排标题「暗机 DARK MACHINE」尾 4 字是 "HINE"，
        #       蜜柑按它返回 LoveLive! Sunshine!! 等 4 部不相干番；
        #    ② 禁止用单个泛词英文作查询——「决斗大师 LOST ～断罪的少年～」按
        #       "LOST" 搜出失落的歌谣/迷失的孩子/lost butter 等 4 部垃圾；
        #       英文词段只有「多词」或「≥6 字符」才够具体（如 DARK MACHINE）；
        #    ③ 中文词段是最可靠的回退键——「决斗大师」直接命中正确条目。
        has_latin = bool(re.search(r"[A-Za-z]", q0))
        cjk_parts = re.findall(r"[\u4e00-\u9fff]{2,}", q0)[:2]
        latin_parts = [p.strip(" .'&!?-") for p in
                       re.findall(r"[A-Za-z][A-Za-z0-9'&!.?]*(?:\s+[A-Za-z][A-Za-z0-9'&!.?]*)*", q0)]
        latin_parts = [p for p in latin_parts if len(p) >= 6 or " " in p][:2]
        ladder += cjk_parts + latin_parts
        if not has_latin and len(q0) > 4:
            # 纯中文标题才走尾 4 / 首 4 字（「露特莉莉」这类短词蜜柑才命中）
            ladder += [q0[-4:], q0[:4]]
        seen, queries = set(), []
        for q in ladder:
            q = (q or "").strip()
            if len(q) >= 2 and q not in seen:
                seen.add(q)
                queries.append(q)
        last_err = ""
        merged, seen_urls = [], set()
        for q in queries:
            res = self.call("/api/mikan", {}, query={"text": q})
            if res.get("code") != 200:
                last_err = res.get("message") or "蜜柑搜索失败"
                continue
            data = res.get("data") or {}
            items = [it for wk in (data.get("weeks") or []) for it in (wk.get("items") or [])]
            for it in items:
                key = it.get("url") or it.get("title")
                if key and key not in seen_urls:
                    seen_urls.add(key)
                    merged.append(it)
            if len(merged) >= 8:  # 候选足够多，后续宽泛查询只会加噪声
                break
        if merged:
            return merged
        if last_err:
            raise AniRssError(last_err)
        return []

    def mikan_groups(self, mikan_url: str):
        res = self.call("/api/mikanGroup", {}, query={"url": mikan_url})
        if res.get("code") != 200:
            raise AniRssError(res.get("message") or "字幕组列表获取失败")
        data = res.get("data")
        return data if isinstance(data, list) else []

    def refresh_all(self):
        res = self.call("/api/refreshAll", {})
        if res.get("code") != 200:
            raise AniRssError(res.get("message") or "refreshAll 失败")
        return res

    def delete_ani(self, ids):
        """deleteAni: body 是 id 字符串数组；deleteFiles 恒为 false（绝不删本地文件）。
        ⚠️ ani-rss 会无条件清掉该番的 torrents 去重记录目录——调用方必须先跑
        ani_rss_tool.py mirror 备份（见 _records_tool），否则日后重订会全量重下。"""
        res = self.call("/api/deleteAni", [str(i) for i in ids if i],
                        query={"deleteFiles": "false"})
        if res.get("code") != 200:
            raise AniRssError(res.get("message") or "deleteAni 失败")
        return res


def subject_id(bgm_url: str) -> str:
    u = bgm_url or ""
    return u.rsplit("/", 1)[-1].strip() if "/subject/" in u else ""


def mikan_ids(rss: str):
    """Pull (bangumiId, subgroupid) out of a mikan RSS url."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(rss or "").query)
        return (q.get("bangumiId", [""])[0], q.get("subgroupid", [""])[0])
    except Exception:  # noqa: BLE001
        return ("", "")


def _validate_ani(ani: dict) -> None:
    """Block the degenerate-payload footgun.

    An ani object missing title/releaseDate/url is exactly what gets persisted by
    the empty-body bug, and it poisons listAni + calendar.ics with a 500. Refuse
    to write anything that does not look complete and well-formed.
    """
    if not isinstance(ani, dict):
        raise AniRssError("构造出的订阅对象无效")
    for field in ("title", "url", "releaseDate"):
        if not (ani.get(field) or "").strip() if isinstance(ani.get(field), str) else not ani.get(field):
            raise AniRssError("订阅对象缺少必要字段 %s，已阻止写入" % field)
    if not ani.get("id"):
        raise AniRssError("订阅对象缺少 id，已阻止写入")
    sb = ani.get("standbyRssList")
    if sb is not None and not isinstance(sb, list):
        raise AniRssError("standbyRssList 必须是列表")


# --------------------------------------------------------------------------
# 番名归一化匹配（与前端 live 视图 isSubscribed 同一套规则）
# --------------------------------------------------------------------------
_KANJI_VARIANTS = {"転": "转", "覚": "觉", "険": "险", "薬": "药", "価": "价", "楽": "乐",
                   "圧": "压", "渕": "渊", "艶": "艳", "戻": "戾", "毎": "每", "録": "录"}
_NAME_STRIP_RE = re.compile(r"[\s\u3000（）()\[\]【】「」·・×x/*★☆:/\\\-—～~?!？!第季期、。．.'\"]+")
_KANJI_RE = re.compile(r"[転覚険薬価楽圧渕艶戻毎録]")


def _norm_name(s) -> str:
    s = str(s or "").lower().strip()
    s = _NAME_STRIP_RE.sub("", s)
    s = _KANJI_RE.sub(lambda m: _KANJI_VARIANTS[m.group(0)], s)
    return s


def _name_hit(a, b) -> bool:
    x, y = _norm_name(a), _norm_name(b)
    if not x or not y:
        return False
    if x == y:
        return True
    return len(x) >= 3 and len(y) >= 3 and (x in y or y in x)


def _hit_any(title, words) -> bool:
    return any(_norm_name(w) and _norm_name(w) in _norm_name(title) for w in (words or []))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _make_client(cfg: dict) -> AniRss:
    return AniRss((cfg.get("anirssBase") or DEFAULT_ANIRSS).strip(),
                  (cfg.get("apiKey") or "").strip(),
                  int(cfg.get("timeout") or 25))


# --------------------------------------------------------------------------
# 多源资源聚合搜索：番剧候选走蜜柑（ani-rss），资源级聚合 动漫花园 / Nyaa。
# 任一来源失败或超时都只降级跳过，其余来源照常返回。
# --------------------------------------------------------------------------
_DMHY_RSS = "https://share.dmhy.org/topics/rss/rss.xml"
_NYAA_RSS = "https://nyaa.si/"
_ANIBT_TORZNAB = "https://anibt.net/torznab/api"   # 官方 Torznab，匿名可查（wiki.anibt.net/docs/open-api）
_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
_RSS_TIMEOUT = 9          # 单源超时（秒）
_RSS_MAX_ITEMS = 60       # 每源最多解析条数
_RESOURCE_CAP = 120       # 聚合去重后的返回上限

_HEAD_GROUP_RE = re.compile(r"^[\[［【]([^\]］】]{1,50})[\]］】]\s*")
_BRACKET_RE = re.compile(r"[\[［【][^\]］】]{0,60}[\]］】]")
_PAREN_RE = re.compile(r"[（(]([^)）]{1,50})[)）]")
_GROUP_NOISE_RE = re.compile(
    r"1080|720|2160|\b4k\b|\b2k\b|x26[45]|hevc|avc|av1|vp9|aac|flac|opus|简|繁|chs|cht|big5|"
    r"multi|mp4|mkv|web|bd|dvd|hdtv|remux|rip|双语|无字|\bsub(?:s)?\b", re.I)
_GROUP_SUB_RE = re.compile(r"字幕(?!组)")   # 「字幕组」是组名，「简繁字幕」不是
# 集数：E01 / EP01 / EP01-12 / 第N话 / "- 15"（SubsPlease 风格）/ 01-28（区间）。
# 裸数字形式要求 ≤2 位或带前导零，拦住 1900/2026 这类年份与哈希串里的 E2。
_EP_RE = re.compile(
    r"(?<![A-Za-z])EP?\.?\s*(\d{1,4})(?:\s*-\s*(\d{1,4}))?(?!\d)"
    r"|第\s*(\d{1,4})\s*[话話集]"
    r"|(?<!\d)-\s*(\d{1,4})(?:\s*[-~]\s*(\d{1,4}))?"
    r"|(?<!\d)(\d{1,2})\s*[-~]\s*(\d{1,2})(?!\d)",
    re.I)
_SEASON_RE = re.compile(r"\bS(\d{1,2})\b|第\s*(\d{1,2})\s*季", re.I)
_RES_RE = re.compile(r"2160p?|1080[pi]|720p|480p|4k|2k", re.I)
_SRC_RE = re.compile(r"bd\s*remux|bdmv|baha|b-global|web-?dl|webr?ip|web\s?rip|bdrip|blu-?ray|hdtv|dvdrip|tvrip", re.I)
_SRC_ORDER = ("bdremux", "bdmv", "bdrip", "web-dl", "baha", "webrip", "hdtv", "dvdrip", "tvrip")
_NOISE_EN_RE = re.compile(
    r"(?i)\b(?:bd\s*remux|bdrip|blu-?ray|web-?dl|webr?ip|web\s?rip|hdtv|dvdrip|baha|b-global|at-x|"
    r"x26[45]|h\.?26[45]|hevc|avc|av1|vp9|aac|flac|opus|truehd|dts(?:-hd)?|ac3|"
    r"2160p?|4k|1080[pi]|720p|480p|2k|hi10p?|10bit|8bit|60fps|mp4|mkv|mka|"
    r"multi-?subs?|multi|chs|cht|big5)\b")
_NOISE_CN_RE = re.compile(
    r"简繁内封|简繁外挂|简繁字幕|简体字幕|简体中文|繁體中文|内嵌字幕|内封字幕|外挂字幕|简繁|简日双语|简英双语|"
    r"简日|简中|繁中|简体|繁体|双语|无字幕|无字|生肉|熟肉|字幕组|完结|合集")
_NAME_SPLIT_RE = re.compile(
    r"[/｜|]|(?<=[\u3040-\u30ff\u3400-\u9fff])\s+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9%])\s+(?=[\u3040-\u30ff\u3400-\u9fff])")
_TAIL_SEASON_RE = re.compile(r"(?i)\s*(?:\d{1,2}(?:st|nd|rd|th)\s+season|season\s*\d{1,2}|第\s*\d{1,2}\s*季)$")


def _src_token(f: str) -> str:
    """片源匹配片段归一化为标准 token。"""
    f2 = re.sub(r"[\s\-]+", "", f.lower())
    if "remux" in f2:
        return "bdremux"
    if "bdmv" in f2:
        return "bdmv"
    if f2 in ("baha", "bglobal"):
        return "baha"
    if "webdl" in f2:
        return "web-dl"
    if "web" in f2:
        return "webrip"
    if "bd" in f2 or "bluray" in f2 or "blu" in f2:
        return "bdrip"
    if "hdtv" in f2:
        return "hdtv"
    if "dvd" in f2:
        return "dvdrip"
    return f2


def _parse_release(title: str) -> dict:
    """把混乱的资源标题清洗成结构化字段：组名 / 番名（含多语言别名）/ 集数 / 季 / 分辨率 / 片源 / 字幕语言。"""
    raw = str(title or "").strip()
    t = raw
    group = ""
    m = _HEAD_GROUP_RE.match(t)
    if m:
        gc = m.group(1).strip()
        if not _GROUP_NOISE_RE.search(gc) and not _GROUP_SUB_RE.search(gc):
            group = gc
            t = t[m.end():]
    body = t

    # 字段提取：集数/季在去括号后的正文找（避免吞下哈希串里的 E2）；分辨率/片源/语言在原文找（常见于括号内）
    body_nb = _BRACKET_RE.sub(" ", body)
    ep = ""
    for me in _EP_RE.finditer(body_nb):
        gs = me.groups()
        if gs[0]:
            ep = gs[0] + "-" + gs[1] if gs[1] else gs[0]
        elif gs[2]:
            ep = gs[2]
        elif gs[3] or gs[5]:
            a, b = (gs[3], gs[4]) if gs[3] else (gs[5], gs[6])
            oka = len(a) <= 2 or a[0] == "0"
            okb = (not b) or len(b) <= 2 or b[0] == "0"
            ep = (a + "-" + b if (b and okb) else a) if (oka and okb) else ""
        if ep:
            break
    season = ""
    ms = _SEASON_RE.search(body_nb) or _SEASON_RE.search(body)
    if ms:
        season = next(x for x in ms.groups() if x)
    res = ""
    mr = _RES_RE.search(body)
    if mr:
        res = mr.group(0).lower()
    src = ""
    found = [_src_token(f.group(0)) for f in _SRC_RE.finditer(body)]
    for tok in _SRC_ORDER:                      # 多个命中时取质量最优而非最左
        if tok in found:
            src = tok
            break
    lang = ""
    if re.search(r"无字幕|无字|生肉|\braw\b", body, re.I):
        lang = "无字"
    else:
        has_s = bool(re.search(r"简|chs|gb2312|\bhans\b", body, re.I))
        has_t = bool(re.search(r"繁|cht|big5|\bhant\b|巴哈|baha", body, re.I))
        if has_s and has_t:
            lang = "简繁"
        elif re.search(r"multi", body, re.I):
            lang = "Multi"
        elif has_s:
            lang = "简"
        elif has_t:
            lang = "繁"

    # 圆括号段先摘出来当别名（如 "Beyond Journey's End (Sousou no Frieren)"），再从正文移除
    paren_names = []
    for pm in _PAREN_RE.finditer(body):
        pc = pm.group(1).strip()
        if len(pc) >= 2 and re.search(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]", pc) \
                and not re.search(r"[\d]{3,}|x26|hevc|aac|web|1080|720|dub", pc, re.I):
            paren_names.append(pc)
    body = _PAREN_RE.sub(" ", body)

    core = _BRACKET_RE.sub(" ", body)
    core = _NOISE_EN_RE.sub(" ", core)
    core = _NOISE_CN_RE.sub(" ", core)
    core = re.sub(r"(?i)\bS\d{1,2}\b", " ", core)
    core = re.sub(r"\d{1,4}\s*[-+~]\s*\d{1,4}(?:\s*[+x×]\s*(?:SP)?\d{0,4})?", " ", core, flags=re.I)
    parts = [re.sub(r"\s{2,}", " ", p).strip(" \t-—–_|｜~～·:：,，。()（）") for p in _NAME_SPLIT_RE.split(core)]
    seen, names = set(), []
    for p in parts:
        if len(p) < 2 or not re.search(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]", p):
            continue
        if re.fullmatch(r"[\d\s\-+xXvV.]+", p):
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        names.append(p)
        if len(names) >= 4:
            break
    for pc in paren_names:                      # 括号别名追加，参与匹配但不抢主名
        k = pc.lower()
        if k not in seen:
            seen.add(k)
            names.append(pc)
    name = ""
    if names:
        name = re.sub(r"(?i)[\s\-–—]*S\d{1,2}$", "", names[0])
        name = _TAIL_SEASON_RE.sub("", name).strip(" \t-—–_|｜~～·:：,，")
        names[0] = name
    names = [n for n in names if n]
    return {
        "group": group,
        "name": name or raw[:60],
        "names": names[:5],
        "ep": ep, "season": season, "res": res, "src": src, "lang": lang,
    }


def _btih_norm(v: str) -> str:
    """磁力哈希统一成 40 位小写 hex（Base32 自动还原），用于跨源去重。"""
    v = re.sub(r"(?i)^magnet:\?xt=urn:btih:", "", str(v or "").strip())
    if re.fullmatch(r"[0-9a-fA-F]{40}", v):
        return v.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", v):
        try:
            return base64.b32decode(v.upper() + "=" * ((8 - len(v) % 8) % 8)).hex()
        except Exception:  # noqa: BLE001
            return v.lower()
    return v.lower()


def _parse_pubdate(s: str) -> str:
    try:
        dt = email.utils.parsedate_to_datetime(str(s or "").strip())
        return dt.strftime("%Y-%m-%d %H:%M") if dt else ""
    except Exception:  # noqa: BLE001
        return (s or "")[:16]


def _to_int(v) -> int:
    try:
        return int(str(v or "0").strip())
    except ValueError:
        return 0


# ---- 本地代理探测（与 scripts/refresh-season.ps1 同一套约定）----
# 面板以 SYSTEM 身份运行，WinINET 用户级代理对它不可见；dmhy / Nyaa 直连被墙，
# 每次搜索都要干等 _RSS_TIMEOUT 超时。因此显式探测本地代理：存活则优先走代理，
# 单次请求失败自动回退直连（代理刚挂的场景），探测结果缓存避免每次连 TCP。
_LOCAL_PROXY = "http://127.0.0.1:10808"
_proxy_alive = None          # None=未探测；True/False=上次探测结果
_opener_proxy = None
_opener_direct = None


def _probe_proxy(timeout: float = 0.5) -> bool:
    global _proxy_alive
    if _proxy_alive is None:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 10808), timeout=timeout):
                _proxy_alive = True
        except OSError:
            _proxy_alive = False
    return _proxy_alive


def _open_with_fallback(req, timeout: float):
    """优先走本地代理发起请求，失败回退直连并重置探测缓存。"""
    global _proxy_alive, _opener_proxy, _opener_direct
    if _probe_proxy():
        if _opener_proxy is None:
            _opener_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({
                "http": _LOCAL_PROXY, "https": _LOCAL_PROXY,
            }))
        try:
            return _opener_proxy.open(req, timeout=timeout)
        except Exception:  # noqa: BLE001 - 代理刚挂/该站代理不通 → 直连兜底
            _proxy_alive = None
            _opener_proxy = None
    if _opener_direct is None:
        _opener_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener_direct.open(req, timeout=timeout)


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "%s/%s" % (APP_NAME, VERSION),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
    })
    with _open_with_fallback(req, timeout) as resp:
        return resp.read()


def _dmhy_search(text: str, timeout: float):
    """动漫花园：RSS 关键词搜索，磁力在 description 里，无大小/做种字段。"""
    url = _DMHY_RSS + "?keyword=" + urllib.parse.quote(text) + "&order=date-desc"
    root = ET.fromstring(_http_get(url, timeout))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        magnet = ""
        mh = re.search(r"magnet:\?xt=urn:btih:[0-9A-Za-z]{32,40}", it.findtext("description") or "")
        if mh:
            magnet = mh.group(0)
        out.append({
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "date": _parse_pubdate(it.findtext("pubDate") or ""),
            "magnet": magnet,
            "hash": _btih_norm(magnet),
            "size": "", "seeders": 0,
        })
        if len(out) >= _RSS_MAX_ITEMS:
            break
    return out


def _nyaa_search(text: str, timeout: float):
    """Nyaa：RSS 搜索，字段最全（大小/做种数/哈希），磁力由哈希构造。"""
    url = _NYAA_RSS + "?page=rss&f=0&c=0_0&q=" + urllib.parse.quote(text)
    root = ET.fromstring(_http_get(url, timeout))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        ih = (it.findtext("{*}infoHash") or "").strip()
        out.append({
            "title": title,
            "link": (it.findtext("guid") or "").strip(),
            "date": _parse_pubdate(it.findtext("pubDate") or ""),
            "magnet": ("magnet:?xt=urn:btih:" + ih + "&dn=" + urllib.parse.quote(title)) if ih else "",
            "hash": _btih_norm(ih),
            "size": (it.findtext("{*}size") or "").strip(),
            "seeders": _to_int(it.findtext("{*}seeders")),
        })
        if len(out) >= _RSS_MAX_ITEMS:
            break
    return out


def _fmt_bytes(v) -> str:
    """Torznab 字节数 → '6.6 GiB' 样式；解析失败返回空串。"""
    try:
        f = float(str(v).strip())
    except (ValueError, TypeError):
        return ""
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or u == "TiB":
            return "%d %s" % (f, u) if u == "B" else "%.1f %s" % (f, u)
        f /= 1024
    return ""


def _anibt_search(text: str, timeout: float):
    """AniBT 官方 Torznab 端点：/torznab/api?t=search&q=关键词（匿名）。
    attr 提供 infohash（40 位 hex）与字节数 size；link 是 .torrent 直链、comments 是资源页。"""
    url = _ANIBT_TORZNAB + "?t=search&q=" + urllib.parse.quote(text)
    root = ET.fromstring(_http_get(url, timeout))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        ih, size_b = "", ""
        for attr in it.findall(_TORZNAB_NS + "attr"):
            nm, val = attr.get("name") or "", attr.get("value") or ""
            if nm == "infohash":
                ih = val.strip()
            elif nm == "size":
                size_b = val.strip()
        out.append({
            "title": title,
            "link": (it.findtext("comments") or "").strip() or (it.findtext("link") or "").strip(),
            "date": _parse_pubdate(it.findtext("pubDate") or ""),
            "magnet": ("magnet:?xt=urn:btih:" + ih + "&dn=" + urllib.parse.quote(title)) if ih else "",
            "hash": _btih_norm(ih),
            "size": _fmt_bytes(size_b),
            "seeders": 0,
        })
        if len(out) >= _RSS_MAX_ITEMS:
            break
    return out


_RSS_SOURCES = (
    ("dmhy", "动漫花园", _dmhy_search),
    ("nyaa", "Nyaa", _nyaa_search),
    ("anibt", "AniBT", _anibt_search),
)


def _match_score(q_norm: str, names) -> int:
    """与查询词的匹配度 0-100：全等 100、双向包含 90、difflib 相似度 ≥0.6 按比例给分。"""
    best = 0
    for n in names or ():
        n = _norm_name(n)
        if not n or not q_norm:
            continue
        if n == q_norm:
            best = max(best, 100)
        elif (q_norm in n or n in q_norm) and min(len(n), len(q_norm)) >= 3:
            best = max(best, 90)
        else:
            r = difflib.SequenceMatcher(None, q_norm, n).ratio()
            if r >= 0.6:
                best = max(best, int(r * 80))
    return best


def _quality_score(pr: dict, seeders: int, has_magnet: bool, date: str) -> int:
    """资源质量分 0-100：分辨率/片源/字幕语言/做种数/磁力可得性/新鲜度。"""
    sc = 0
    res = pr.get("res") or ""
    if res in ("2160p", "4k"):
        sc += 28
    elif res == "1080p":
        sc += 24
    elif res in ("1080i", "720p"):
        sc += 14
    elif res:
        sc += 6
    else:
        sc += 2
    src = pr.get("src") or ""
    if src in ("bdremux", "bdmv"):
        sc += 22
    elif src == "bdrip":
        sc += 20
    elif src in ("web-dl", "baha"):
        sc += 18
    elif src == "webrip":
        sc += 14
    elif src:
        sc += 10
    else:
        sc += 4
    lang = pr.get("lang") or ""
    sc += 12 if lang == "简繁" else 9 if lang in ("简", "繁") else 6 if lang == "Multi" else 3 if lang == "无字" else 0
    if seeders >= 100:
        sc += 18
    elif seeders >= 30:
        sc += 14
    elif seeders >= 10:
        sc += 10
    elif seeders >= 3:
        sc += 6
    elif seeders > 0:
        sc += 3
    if has_magnet:
        sc += 8
    if date:
        try:
            age = (datetime.datetime.now() - datetime.datetime.strptime(date[:10], "%Y-%m-%d")).days
            sc += 10 if age <= 7 else 7 if age <= 30 else 4 if age <= 180 else 2
        except Exception:  # noqa: BLE001
            pass
    return max(0, min(100, sc))


def _shortened_queries(text: str):
    """多词查询在 RSS 源上是整串匹配，太长常 0 命中（如 "xxx second season" 资源标题写 2nd Season）：
    0 结果时逐词从尾部缩短重试，最多查到只剩一个词。"""
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    if not words:
        return
    yield " ".join(words)
    for n in range(len(words) - 1, 0, -1):
        yield " ".join(words[:n])


def _aggregate_resources(text: str, aliases=None, timeout: float = _RSS_TIMEOUT):
    """并发抓取全部外部资源站并聚合。返回 (resources, source_status, took_ms)。
    单源失败/超时 → 记入状态并跳过，不影响其余来源。"""
    t0 = time.time()
    q_norm = _norm_name(text)
    alias_norms = [q_norm]
    for a in (aliases or []):
        na = _norm_name(a)
        if na and na not in alias_norms:
            alias_norms.append(na)
    # 缩短重试用到的查询前缀也算匹配基准（资源按 "frieren" 命中时对原长词 "frieren second season" 打不上分）
    for q in _shortened_queries(text):
        na = _norm_name(q)
        if na and na not in alias_norms:
            alias_norms.append(na)

    def run(src):
        sid, label, fn = src
        ts = time.time()
        for q in _shortened_queries(text):
            try:
                items = fn(q, timeout) or []
            except Exception as exc:  # noqa: BLE001
                return (sid, label, [], (str(exc) or exc.__class__.__name__)[:200],
                        int((time.time() - ts) * 1000))
            if items:
                return (sid, label, items, "", int((time.time() - ts) * 1000))
        return (sid, label, [], "", int((time.time() - ts) * 1000))

    status, rows = [], []
    with ThreadPoolExecutor(max_workers=len(_RSS_SOURCES)) as pool:
        for sid, label, items, err, ms in pool.map(run, _RSS_SOURCES):
            status.append({"id": sid, "label": label, "ok": not err,
                           "count": len(items), "error": err, "ms": ms})
            for it in items:
                it["_src"] = label
                rows.append(it)

    merged = {}
    for it in rows:
        pr = _parse_release(it["title"])
        msc = max([_match_score(a, pr["names"]) for a in alias_norms] or [0])
        key = ("btih:" + it["hash"]) if it["hash"] else (
            "name:" + "|".join([_norm_name(pr["name"]), pr["ep"], pr["res"], pr["group"].lower()]))
        row = {
            "title": it["title"],
            "name": pr["name"],
            "sources": [it["_src"]],
            "group": pr["group"], "ep": pr["ep"], "season": pr["season"],
            "res": pr["res"], "src": pr["src"], "lang": pr["lang"],
            "size": it.get("size") or "", "seeders": it.get("seeders") or 0,
            "date": it.get("date") or "", "magnet": it.get("magnet") or "",
            "link": it.get("link") or "",
            "match": msc, "quality": _quality_score(pr, it.get("seeders") or 0,
                                                    bool(it.get("magnet")), it.get("date") or ""),
        }
        old = merged.get(key)
        if old is None:
            merged[key] = row
            continue
        for lb in row["sources"]:
            if lb not in old["sources"]:
                old["sources"].append(lb)
        for f in ("size", "seeders", "magnet", "link", "date", "group", "lang", "res", "src", "ep", "season"):
            if not old.get(f) and row.get(f):
                old[f] = row[f]
        old["match"] = max(old["match"], row["match"])
        old["quality"] = max(old["quality"], _quality_score(pr, old["seeders"], bool(old["magnet"]), old["date"]))
        if len(row["name"]) > len(old["name"]) and len(row["name"]) <= 40:
            old["name"] = row["name"]

    # 第二级去重：不同站同一发布常因 announce 等差异 btih 不同，按清洗后的标题再合并一次
    by_title, deduped = {}, []
    for r in merged.values():
        k = re.sub(r"\s+", " ", r["title"]).strip().lower()
        old = by_title.get(k)
        if old is None:
            by_title[k] = r
            deduped.append(r)
            continue
        for lb in r["sources"]:
            if lb not in old["sources"]:
                old["sources"].append(lb)
        for f in ("size", "seeders", "magnet", "link", "date", "group", "lang", "res", "src", "ep", "season"):
            if not old.get(f) and r.get(f):
                old[f] = r[f]
        old["match"] = max(old["match"], r["match"])
        old["quality"] = max(old["quality"], r["quality"])
        if len(r["name"]) > len(old["name"]) and len(r["name"]) <= 40:
            old["name"] = r["name"]

    def dnum(d):
        try:
            return int(datetime.datetime.strptime(d[:10], "%Y-%m-%d").strftime("%Y%m%d"))
        except Exception:  # noqa: BLE001
            return 0

    resources = sorted(deduped,
                       key=lambda r: (-r["match"], -r["quality"], -dnum(r["date"]), -len(r["sources"])))
    return resources[:_RESOURCE_CAP], status, int((time.time() - t0) * 1000)


# --------------------------------------------------------------------------
# 订阅计划引擎：番名 → 蜜柑搜索 → 匹配候选 → 默认主/备组
# --------------------------------------------------------------------------
_AUTOPLAN_SLEEP = 0.25   # 每番之间限速，避免连续请求触发蜜柑/ani-rss 反爬


def _pick_groups(usable, dm, dbs, prefer_main=None, prefer_standby=None):
    """从可用字幕组里选默认主/备：主=匹配 defaultMain，否则第一个；备=defaultStandby 依次匹配。

    自动追新的两个字段分开生效（都按书写顺序、双向包含匹配）：
    - prefer_main：命中的第一个组当主订阅（该字段同时是自动追新的筛番条件）；
    - prefer_standby：依次命中的组加入备用列表（≤6）。
    某一字段未配置或无命中时，该位置回落全局默认逻辑。"""
    main = None
    if prefer_main:
        for g in usable:
            if any(_name_hit(g["label"], p) or _name_hit(p, g["label"]) for p in prefer_main):
                main = g
                break
    if main is None and dm:
        for g in usable:
            if _name_hit(g["label"], dm):
                main = g
                break
    if main is None:
        main = usable[0]
    standby = []
    if prefer_standby:
        for p in prefer_standby:
            for g in usable:
                if g is main or any(s["label"] == g["label"] for s in standby):
                    continue
                if _name_hit(g["label"], p) or _name_hit(p, g["label"]):
                    standby.append({"label": g["label"], "rss": g["rss"]})
                    break
    else:
        for d in dbs:
            for g in usable:
                if g is main or any(s["label"] == g["label"] for s in standby):
                    continue
                if _name_hit(g["label"], d):
                    standby.append({"label": g["label"], "rss": g["rss"]})
                    break
    return main, standby[:6]


def _known_sub_names(client):
    """已订阅番名归一化集合（title + jpTitle）。失败抛 AniRssError。"""
    known = set()
    for s in client.subscriptions():
        for f in ("title", "jpTitle"):
            v = _norm_name(s.get(f))
            if len(v) >= 3:
                known.add(v)
    return known


def _autoplan_titles(client, titles, cfg, known=None, prefer_main=None, prefer_standby=None):
    """为每个番名生成订阅计划，返回 plans（status ∈ ok|subscribed|unmatched|error）。

    prefer_main / prefer_standby：自动追新的主/备限定组，分开生效（见 _pick_groups）。"""
    dm = (cfg.get("defaultMain") or "").strip()
    dbs = [str(x).strip() for x in (cfg.get("defaultStandby") or []) if str(x).strip()]
    if known is None:
        known = _known_sub_names(client)
    plans = []
    for t in titles:
        t = (t or "").strip()
        if not t:
            continue
        nt = _norm_name(t)
        if len(nt) >= 3 and any(nt in k or k in nt for k in known):
            plans.append({"title": t, "status": "subscribed", "message": "已在 ani-rss 订阅"})
            continue
        try:
            cands = client.mikan_search(t)
        except AniRssError as exc:
            plans.append({"title": t, "status": "error", "message": str(exc)})
            continue
        best = None
        for c in cands:
            if _name_hit(c.get("title") or "", t):
                best = c
                break
        if best is None:
            plans.append({"title": t, "status": "unmatched", "message": "蜜柑未匹配到同名条目"})
            continue
        if best.get("exists"):
            plans.append({"title": t, "status": "subscribed", "mikanTitle": best.get("title"),
                          "message": "该番已在订阅（蜜柑标记 exists）"})
            continue
        try:
            groups = client.mikan_groups(best.get("url") or "")
        except AniRssError as exc:
            plans.append({"title": t, "status": "error", "mikanTitle": best.get("title"),
                          "message": str(exc)})
            continue
        usable = [{"label": (g.get("label") or "").strip(),
                   "rss": (g.get("rss") or "").strip(),
                   "updateDay": g.get("updateDay") or "",
                   "count": len(g.get("items") or []),
                   # 与 /api/anirss/groups 同款瘦身：最近资源明细供前端行内展开预览
                   "items": [
                       {"title": (it.get("title") or "")[:150],
                        "size": it.get("formatSize") or "",
                        "date": (it.get("createdAt") or "")[:16],
                        "magnet": it.get("magnet") or ""}
                       for it in (g.get("items") or [])[:5]]
                   }
                  for g in groups]
        usable = [g for g in usable if g["rss"]]
        if not usable:
            plans.append({"title": t, "status": "unmatched", "mikanTitle": best.get("title"),
                          "message": "条目下暂无可用字幕组 RSS"})
            continue
        main, standby = _pick_groups(usable, dm, dbs,
                                     prefer_main=prefer_main, prefer_standby=prefer_standby)
        plans.append({"title": t, "status": "ok", "mikanTitle": best.get("title"),
                      "url": best.get("url"), "cover": best.get("cover") or "",
                      "main": main, "standby": standby, "groups": usable[:12], "message": ""})
        time.sleep(_AUTOPLAN_SLEEP)
    return plans


def _push_items(client, items, dry):
    """批量推送订阅（主 + 备），返回与旧 _api_push 相同结构的汇总。"""
    # Pre-flight: one listAni for the whole batch (avoids N round-trips and
    # lets us skip shows that are already subscribed under the same subgroup).
    existing = client.subscriptions()
    seen = set()
    for s in existing:
        seen.add(tuple(mikan_ids(s.get("url"))))

    results = []
    for raw in items:
        title = (raw.get("title") or "").strip()
        main = raw.get("main") or {}
        standby = raw.get("standby") or []
        rec = {"title": title, "ok": False, "stage": "", "message": "", "standby": len(standby)}
        try:
            rss_main = (main.get("rss") or "").strip()
            if not rss_main:
                raise AniRssError("未指定主订阅字幕组")
            key_ids = tuple(mikan_ids(rss_main))
            if key_ids in seen and all(key_ids):
                rec.update(stage="skip", message="该字幕组已订阅，已跳过")
                results.append(rec)
                continue

            rec["stage"] = "rssToAni"
            ani = client.rss_to_ani(rss_main)          # never hand-assemble

            clean = [
                {"label": (s.get("label") or "").strip(),
                 "url": (s.get("rss") or s.get("url") or "").strip(),
                 "offset": 0}
                for s in standby
            ]
            clean = [s for s in clean if s["url"] and tuple(mikan_ids(s["url"])) != key_ids]
            ani["standbyRssList"] = clean
            ani["enable"] = bool(raw.get("enable", load_config().get("enable", True)))
            # 用户偏好：推送的订阅默认关闭遗漏检测(omit)与完结迁移(completed)
            ani["omit"] = False
            ani["completed"] = False

            rec["stage"] = "validate"
            _validate_ani(ani)                          # blocks the degenerate payload
            rec["subgroupId"] = mikan_ids(rss_main)[1]
            rec["subgroup"] = ani.get("subgroup")

            if dry:
                rec.update(stage="dryRun", ok=True,
                           message="演练通过（未写入）: 主 %s / 备用 %d 个"
                                   % (ani.get("subgroup"), len(clean)))
                results.append(rec)
                continue

            rec["stage"] = "addAni"
            client.add_ani(ani)
            rec.update(ok=True, message="已添加：主订阅 %s%s"
                                       % (ani.get("subgroup"),
                                          ("，备用 %d 个" % len(clean)) if clean else ""))
            seen.add(key_ids)
        except AniRssError as exc:
            rec["message"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            rec["message"] = "未预期错误: %s" % exc
        results.append(rec)

    return {
        "ok": True,
        "pushed": sum(1 for r in results if r["ok"]),
        "skipped": sum(1 for r in results if r["stage"] == "skip"),
        "failed": sum(1 for r in results if not r["ok"] and r["stage"] != "skip"),
        "results": results,
    }


# --------------------------------------------------------------------------
# 切换字幕组（覆盖旧订阅）
# 顺序硬约束（见 ani-rss-openapi-integration 技能）：
#   ① 校验新载荷（新选择不合法就中止，绝不能把旧订阅先删了）
#   ② mirror：把 torrents 去重记录增量镜像到 SQLite（deleteAni 会无条件删记录目录）
#   ③ deleteAni(deleteFiles=false) 删旧订阅
#   ④ restore：把镜像里缺失的记录写回磁盘（含刚被 ③ 删掉的旧组记录，
#      日后换回旧组不会全量重下）
#   ⑤ addAni 新订阅
# --------------------------------------------------------------------------
_ANIRSS_TOOL_PY = sys.executable
_ANIRSS_TOOL = os.path.join(os.path.expanduser("~"), "ani-rss", "ani_rss_tool.py")


def _records_tool(action: str) -> str:
    """Run ani_rss_tool.py mirror/restore. Raise AniRssError on any failure —
    记录守护是删除操作的硬闸门，跑不成就不许删。"""
    if not os.path.exists(_ANIRSS_TOOL):
        raise AniRssError("未找到记录守护工具: %s" % _ANIRSS_TOOL)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        p = subprocess.run([_ANIRSS_TOOL_PY, _ANIRSS_TOOL, action],
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace", creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        raise AniRssError("记录 %s 执行失败: %s" % (action, exc)) from exc
    if p.returncode != 0:
        tail = ((p.stderr or "") + (p.stdout or ""))[-300:].strip()
        raise AniRssError("记录 %s 失败（退出码 %s）: %s" % (action, p.returncode, tail))
    return p.stdout or ""


def _bgm_of_sub(sub: dict) -> str:
    """订阅条目 → Bangumi subject id（bgmUrl 形如 https://bgm.tv/subject/12345）。"""
    m = re.search(r"/subject/(\d+)", sub.get("bgmUrl") or "")
    return m.group(1) if m else ""


def _switch_items(client, items, dry):
    """切换字幕组：同番删旧订阅 → 按新主/备重订。返回与 push 相同结构的汇总。"""
    results = []
    removed_n = 0
    for raw in items:
        title = (raw.get("title") or "").strip()
        main = raw.get("main") or {}
        standby = raw.get("standby") or []
        rec = {"title": title, "ok": False, "stage": "", "message": "",
               "standby": len(standby), "removed": []}
        try:
            rss_main = (main.get("rss") or "").strip()
            if not rss_main:
                raise AniRssError("未指定主订阅字幕组")
            bgm_id = (str(raw.get("bgmId") or "").strip()
                      or mikan_ids(rss_main)[0]
                      or mikan_ids(raw.get("mikanUrl") or "")[0])
            if not bgm_id:
                raise AniRssError("无法定位番剧（缺少 bgmId / mikanUrl）")

            subs = client.subscriptions()
            olds = [s for s in subs if _bgm_of_sub(s) == bgm_id]
            rec["removed"] = [(s.get("subgroup") or s.get("title") or "未知组") for s in olds]

            # ① 先构造并校验新载荷——新选择不合法就中止，旧订阅原地不动
            rec["stage"] = "rssToAni"
            ani = client.rss_to_ani(rss_main)
            clean = [
                {"label": (s.get("label") or "").strip(),
                 "url": (s.get("rss") or s.get("url") or "").strip(),
                 "offset": 0}
                for s in standby
            ]
            key_ids = mikan_ids(rss_main)
            clean = [s for s in clean if s["url"] and mikan_ids(s["url"]) != key_ids]
            ani["standbyRssList"] = clean
            ani["enable"] = bool(raw.get("enable", load_config().get("enable", True)))
            ani["omit"] = False
            ani["completed"] = False
            rec["stage"] = "validate"
            _validate_ani(ani)
            old_desc = "、".join(rec["removed"]) if rec["removed"] else ""

            if dry:
                rec.update(ok=True, stage="dryRun",
                           message="演练通过（未写入）：%s → 新主 %s / 备用 %d 个"
                                   % (("将删除旧订阅 %d 条（%s）" % (len(olds), old_desc))
                                      if olds else "无旧订阅（等同新增）",
                                      ani.get("subgroup"), len(clean)))
                results.append(rec)
                continue

            if olds:
                ids = [s.get("id") for s in olds if s.get("id")]
                if not ids:
                    raise AniRssError("旧订阅缺少 id，无法安全删除（请到 ani-rss 检查）")
                rec["stage"] = "mirror"
                _records_tool("mirror")
                rec["stage"] = "deleteAni"
                client.delete_ani(ids)
                rec["stage"] = "restore"
                _records_tool("restore")
                removed_n += len(olds)

            rec["stage"] = "addAni"
            client.add_ani(ani)
            rec.update(ok=True,
                       message=("已删除旧订阅 %d 条（%s）→ " % (len(olds), old_desc) if olds
                                else "无旧订阅，直接新增：")
                       + "主 %s%s" % (ani.get("subgroup"),
                                      ("，备用 %d 个" % len(clean)) if clean else ""))
        except AniRssError as exc:
            rec["message"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            rec["message"] = "未预期错误: %s" % exc
        results.append(rec)

    return {
        "ok": True,
        "pushed": sum(1 for r in results if r["ok"]),
        "removed": removed_n,
        "skipped": sum(1 for r in results if r["stage"] == "skip"),
        "failed": sum(1 for r in results if not r["ok"] and r["stage"] != "skip"),
        "results": results,
    }


# --------------------------------------------------------------------------
# 全自动追新：season 快照重建后按规则自动订阅新番
# --------------------------------------------------------------------------
AUTOSUB_STATE_FILE = "autosub-state.json"
_autosub_lock = threading.Lock()


def _autosub_state_path():
    return os.path.join(DATA, AUTOSUB_STATE_FILE)


def _load_autosub_state():
    try:
        with open(_autosub_state_path(), encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = {}
    st.setdefault("processed", {})
    st.setdefault("history", [])
    st.setdefault("lastRun", "")
    return st


def _save_autosub_state(st):
    st["history"] = (st.get("history") or [])[-100:]
    tmp = _autosub_state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _autosub_state_path())


# --------------------------------------------------------------------------
# 订阅审计「不看」忽略名单（2026-10-14）
# 用户明确不打算追的漏订番，忽略后不再出现在审计 gaps 里；可随时恢复。
# 双键定位：bgmId（快照侧精确）+ 归一化标题（两份数据源 id 体系不保证同源）。
# --------------------------------------------------------------------------
AUDIT_IGNORE_FILE = "audit-ignore-state.json"


def _audit_ignore_path():
    return os.path.join(DATA, AUDIT_IGNORE_FILE)


def _load_audit_ignore():
    try:
        with open(_audit_ignore_path(), encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        return []
    items = st.get("ignored")
    return items if isinstance(items, list) else []


def _save_audit_ignore(items):
    tmp = _audit_ignore_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ignored": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _audit_ignore_path())


def _load_season_shows():
    """从 season-data.js 提取 shows 列表（文件带注释头，取首尾大括号之间的 JSON）。"""
    p = _resource_path(DATA_FILE)
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        data = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        return data.get("shows") or []
    except Exception:  # noqa: BLE001
        return []


# 公共搬运 / 生肉来源：几乎出现在每一部番剧里，审计时只降级标注、不当作「在做这个番的组」提示
_COMMON_GROUP_RE = re.compile(r"(生肉|不明字幕|整理搬运|Kirara\s*Fantasia|NCOP|NCED)", re.I)


def _load_season_data():
    """读取整份 season-data.js（stats / season.byWeekday / shows 全量）。"""
    p = _resource_path(DATA_FILE)
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return {}


def _audit_payload(client, cfg):
    """订阅审计：本季番剧 vs ani-rss 订阅的覆盖差集（纯本地计算，不额外打外部接口）。

    回答两个问题：
      ① gaps    —— 本季已有字幕组在做、但 ani-rss 里没订阅的番
      ② partial —— 已订阅、但「在做这个番的组」里还有没纳入主/备的

    口径与面板其它页面保持一致：bgmId 优先精确对齐，番名归一化回退用 _norm_name/_name_hit；
    （比对时整份 season-data.js 与一次 listAni，均为本地/内网调用，秒级返回。）
    """
    data = _load_season_data()
    season = data.get("season") or {}
    stats = data.get("stats") or {}

    roster = []
    for wd in season.get("byWeekday") or []:
        for a in wd.get("animes") or []:
            item = dict(a)
            item["weekdayLabel"] = wd.get("weekdayLabel") or ""
            roster.append(item)

    subs = client.subscriptions()

    freq = {}       # 字幕组出现次数（主订阅 + 备用订阅），用于标「常用组」
    sub_index = []  # 订阅索引：bgmId / 归一化标题 / 主组 / 备用组
    for s in subs:
        main = (s.get("subgroup") or "").strip()
        standby = [str(x.get("label") or "").strip() for x in (s.get("standbyRssList") or [])]
        for lb in [main] + standby:
            if lb:
                freq[lb] = freq.get(lb, 0) + 1
        sub_index.append({
            "bgmId": subject_id(s.get("bgmUrl")) or "",
            "norm": _norm_name(s.get("title")) or "",
            "title": s.get("title") or "",
            "main": main,
            "standby": [x for x in standby if x],
        })

    top_groups = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    mine = {k for k, _ in top_groups}

    # 「不看」忽略名单（bgmId / 归一化标题双键）
    _ign = _load_audit_ignore()
    ign_bids = {str(x.get("bgmId") or "").strip() for x in _ign}
    ign_norms = {str(x.get("norm") or "").strip() for x in _ign}

    def find_sub(a):
        bid = str(a.get("bgmId") or "").strip()
        if bid:
            for r in sub_index:
                if r["bgmId"] and r["bgmId"] == bid:
                    return r
        nt = _norm_name(a.get("title"))
        if len(nt) >= 3:
            for r in sub_index:
                if len(r["norm"]) >= 3 and (nt in r["norm"] or r["norm"] in nt):
                    return r
        return None

    def label_hit(x, y):
        return bool(x) and bool(y) and (_name_hit(x, y) or _name_hit(y, x))

    gaps, partial, matched = [], [], 0
    for a in roster:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        groups = []
        for g in (a.get("groups") or []):
            name = (g.get("name") or "").strip()
            if not name:
                continue
            groups.append({
                "name": name,
                "count": int(g.get("count") or 0),
                "known": bool(g.get("known")),
                "tier": g.get("tier") or "",
                "common": bool(_COMMON_GROUP_RE.search(name)),
                "mine": name in mine,
            })
        # 常用组 / 发布量排序，前端直接按序渲染即可
        groups.sort(key=lambda g: (0 if g["mine"] else 1, -g["count"], g["name"]))
        doing = [g for g in groups if g["count"] > 0]
        base = {
            "bgmId": str(a.get("bgmId") or ""),
            "title": title,
            "weekdayLabel": a.get("weekdayLabel") or "",
            "premiereDate": a.get("premiereDate") or "",
            "rating": a.get("rating"),
            "episodes": a.get("episodes"),
            "hasRelease": bool(a.get("hasRelease")),
            "groups": groups,
            "doingCount": len(doing),
        }

        rec = find_sub(a)
        if rec:
            matched += 1
            have = [x for x in [rec["main"]] + rec["standby"] if x]
            missing = [g for g in doing
                       if not g["common"] and not any(label_hit(g["name"], h) for h in have)]
            if missing:
                # subBgmId = 订阅自身的 bgmId：前端「补齐备用」走覆盖重订时用它定位旧订阅，
                # 不能用快照里的 bgmId（两份数据源的 id 体系不一定同源，误判会变成「新增」重复订阅）
                partial.append(dict(base, main=rec["main"], standby=rec["standby"],
                                    subBgmId=rec["bgmId"], missing=missing))
            continue

        # 未订阅：只报「真的有人在发片、且不是纯公共组」的番，避免列出一堆空壳
        if not a.get("hasRelease"):
            continue
        if not any((not g["common"]) for g in doing):
            continue
        # 「不看」忽略名单：bgmId 或归一化标题命中即跳过
        g_bid = str(a.get("bgmId") or "").strip()
        g_nt = _norm_name(title)
        if (g_bid and g_bid in ign_bids) or (g_nt and g_nt in ign_norms):
            continue
        gaps.append(base)

    gaps.sort(key=lambda x: (-sum(1 for g in x["groups"] if g["mine"] and g["count"] > 0),
                             -(x.get("rating") or 0), x["title"]))
    partial.sort(key=lambda x: (-len([g for g in x["missing"] if g["mine"]]),
                                -(x.get("rating") or 0), x["title"]))
    ignored = _load_audit_ignore()
    return {
        "ok": True,
        "generatedAt": data.get("generatedAt") or "",
        "seasonLabel": season.get("currentSeason") or "",
        "seasonTotal": season.get("total") or len(roster),
        "seasonWithRelease": season.get("withRelease") or stats.get("seasonWithRelease") or 0,
        "subTotal": len(subs),
        "subMatched": matched,
        "topGroups": [{"label": k, "count": v} for k, v in top_groups],
        "gaps": gaps,
        "partial": partial,
        "ignored": ignored,
    }


def _autosub_scan(trigger="manual"):
    """按 autoSub 规则扫描本季新番并自动订阅。enable=False 或并发时直接返回。"""
    cfg = load_config()
    a = cfg.get("autoSub") or {}
    if not a.get("enable"):
        return {"ok": False, "msg": "自动追新未开启"}
    if not _autosub_lock.acquire(blocking=False):
        return {"ok": False, "msg": "上一轮扫描仍在进行"}
    try:
        st = _load_autosub_state()
        processed = st["processed"]
        history = st["history"]
        shows = _load_season_shows()
        if not shows:
            return {"ok": False, "msg": "season-data.js 不可用"}

        cand = []
        for s in shows:
            title = (s.get("title") or "").strip()
            if not title or title in processed:
                continue
            if _hit_any(title, a.get("exclude")):
                processed[title] = {"ts": _now(), "action": "excluded"}
                continue
            kw = a.get("keywords") or []
            if kw and not _hit_any(title, kw):
                continue
            mr = float(a.get("minRating") or 0)
            if mr and (s.get("rating") is None or float(s["rating"]) < mr):
                continue
            og = a.get("mainGroups") or []   # 主组限定：兼筛番（只订阅该组本季有更新的番）
            if og:
                names = [(g.get("name") or "") for g in (s.get("groups") or [])]
                if not any(_name_hit(n, o) or _name_hit(o, n) for o in og for n in names):
                    continue
            cand.append(s)

        cand.sort(key=lambda s: -(s.get("rating") or 0))
        limit = max(1, min(30, int(a.get("maxPerRun") or 5)))
        cand = cand[:limit]
        dry = bool(a.get("dryRun"))
        client = _make_client(cfg)
        try:
            known = _known_sub_names(client)
        except AniRssError as exc:
            return {"ok": False, "msg": "读取 ani-rss 订阅失败：%s" % exc}

        results = []
        for s in cand:
            title = s["title"]
            # 主/备限定组分开生效：主组=mainGroups 第一个命中，备用=standbyGroups 依次命中
            plans = _autoplan_titles(client, [title], cfg, known=known,
                                     prefer_main=(og or None),
                                     prefer_standby=(a.get("standbyGroups") or None))
            p = plans[0] if plans else {"status": "error", "message": "内部错误"}
            action, detail = p["status"], (p.get("message") or "")
            if p["status"] == "ok":
                pr = _push_items(client, [{
                    "title": p.get("mikanTitle") or title,
                    "mikanUrl": p.get("url"),
                    "main": {"label": p["main"]["label"], "rss": p["main"]["rss"]},
                    "standby": p["standby"],
                    "enable": cfg.get("enable", True),
                }], dry)
                r0 = (pr.get("results") or [{}])[0]
                if dry:
                    action, detail = "dryRun", r0.get("message") or "演练通过"
                elif r0.get("ok"):
                    action, detail = "subscribed", r0.get("message") or ""
                elif r0.get("stage") == "skip":
                    action, detail = "already", r0.get("message") or ""
                else:
                    action, detail = "failed", r0.get("message") or "推送失败"
            processed[title] = {"ts": _now(), "action": action}
            history.append({"ts": _now(), "title": title, "action": action,
                            "detail": detail, "trigger": trigger})
            results.append({"title": title, "action": action, "detail": detail})
            time.sleep(_AUTOPLAN_SLEEP)

        st["lastRun"] = _now()
        _save_autosub_state(st)
        log("[autosub] %s 触发：%s" % (trigger,
            "、".join("%s=%s" % (r["title"], r["action"]) for r in results) or "无候选"))
        return {"ok": True, "dryRun": dry, "results": results}
    finally:
        _autosub_lock.release()


# --------------------------------------------------------------------------
# 备源清理：主 RSS 下载完成的集，自动删除备用 RSS 的同集下载
#
# 背景：ani-rss 的主订阅 + 备用订阅（standbyRssList）各自独立轮询下载，同一集
# 主组/备组可能先后都下载，造成重复。入库流水线（qB autorun）下载完成即归档并
# 删除种子任务，扫 qB 种子为时已晚 —— 所以这里扫【文件】：
#   1) 媒体库 + 下载暂存目录里的文件（文件名保留字幕组名与集号）：
#      同番同集已存在主组文件 => 删除备组文件（连同同名 nfo/字幕/缩略图）
#   2) qB 里还在下载的备组种子：同番同集主组已完成或主组文件已存在 => 删任务
# 安全约束：解析不出集号跳过；匹配不到订阅跳过；主组文件/种子永不删；全量记日志。
# --------------------------------------------------------------------------

SC_STATE_FILE = "standby-cleanup-state.json"
_SC_LOCK = threading.Lock()
_DEFAULT_SC = {
    "enable": False,
    "intervalMin": 10,                                # 3-720
    "qbHost": "http://127.0.0.1:33900",           # qBittorrent（ani-rss 同款）
    "qbApiKey": "",                                   # qB WebUI API Key（Bearer）
    "deleteFiles": True,                              # true=文件一并删除
    "scanDirs": ["E:/Emby Media Part1/Bangumi",       # 媒体库根目录（可多个）
                 "F:/Emby Media Part2/Bangumi"],
    "downloadDir": "F:/Bangumi暂存",                  # 下载暂存目录（ani-rss downloadPathTemplate 根）
}

_SC_EP_RES = [
    re.compile(r"[Ss]\d{1,2}\s*[Ee]\s*(\d{1,4})"),
    re.compile(r"第\s*(\d{1,4})\s*[话話集]"),
    re.compile(r"[-\s]\s*(\d{1,4})\s*[-\s\[]"),
    re.compile(r"[\[【]\s*(\d{1,4})\s*[\]】]"),
]
_SC_EP_BLACK = {"480", "720", "1080", "2160", "4320", "60", "30", "24", "25"}
_SC_MEDIA_EXT = {".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".wmv", ".mov", ".flv", ".webm", ".rmvb"}
_SC_SIDE_EXT = {".nfo", ".ass", ".srt", ".ssa", ".sup", ".jpg", ".png", ".xml", ".lrc"}


def _sc_cfg(cfg=None):
    c = dict(_DEFAULT_SC)
    if cfg is None:
        cfg = load_config()
    c.update((cfg.get("standbyCleanup") or {}) if isinstance(cfg, dict) else {})
    try:
        c["intervalMin"] = max(3, min(720, int(c.get("intervalMin") or 10)))
    except (TypeError, ValueError):
        c["intervalMin"] = 10
    c["enable"] = bool(c.get("enable"))
    c["deleteFiles"] = bool(c.get("deleteFiles"))
    c["qbHost"] = str(c.get("qbHost") or "").strip().rstrip("/")
    c["qbApiKey"] = str(c.get("qbApiKey") or "").strip()
    if isinstance(c.get("scanDirs"), list):
        c["scanDirs"] = [str(x).strip() for x in c["scanDirs"] if str(x).strip()]
    elif isinstance(c.get("scanDirs"), str):
        c["scanDirs"] = [s.strip() for s in c["scanDirs"].replace("，", ",").split(",") if s.strip()]
    else:
        c["scanDirs"] = list(_DEFAULT_SC["scanDirs"])
    c["downloadDir"] = str(c.get("downloadDir") or "").strip().rstrip("/\\")
    return c


def _sc_state_path():
    return os.path.join(DATA, SC_STATE_FILE)


def _sc_load_state():
    try:
        with open(_sc_state_path(), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def _sc_save_state(st):
    try:
        tmp = _sc_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _sc_state_path())
    except Exception as exc:  # noqa: BLE001
        log("[备源清理] 状态写入失败: %r" % exc)


def _sc_parse_ep(name):
    """从种子/文件名解析集号（纯数字 int），失败返回 None。"""
    s = str(name or "")
    for rx in _SC_EP_RES:
        m = rx.search(s)
        if m and m.group(1) not in _SC_EP_BLACK:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _qb_call(sc, path, method="GET", data=None):
    url = sc["qbHost"] + path
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + sc["qbApiKey"],
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "%s/%s" % (APP_NAME, VERSION),
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else None


def _sc_subs_index(client):
    """有备用订阅的番 → 匹配索引。"""
    out = []
    try:
        subs = client.subscriptions()
    except AniRssError as exc:
        raise AniRssError("listAni 失败: %s" % exc)
    for it in subs:
        sb = it.get("standbyRssList") or []
        if not sb:
            continue
        labels = [str(s.get("label") or "").strip() for s in sb
                  if str(s.get("label") or "").strip()]
        if not labels:
            continue
        out.append({
            "title": it.get("title") or "",
            "tmdb": _norm_name(it.get("themoviedbName") or it.get("title") or ""),
            "main": str(it.get("subgroup") or "").strip(),
            "standby": labels,
        })
    return out


def _sc_match_sub(save_path, subs):
    n = _norm_name(str(save_path or ""))
    if not n:
        return None
    best, best_len = None, 0
    for s in subs:
        if s["tmdb"] and s["tmdb"] in n and len(s["tmdb"]) > best_len:
            best, best_len = s, len(s["tmdb"])   # 取最长命中，避免短名误配
    return best


def _sc_hit_label(name, labels):
    low = str(name or "").lower()
    for lab in labels:
        if lab and lab.lower() in low:
            return lab
    return None


def _sc_iter_files(roots):
    """遍历目录（跳过 _ 开头目录），产出 (dirpath, filename)。"""
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith("_")]
            for fn in filenames:
                yield dirpath, fn


def _sc_file_ep_group(dirpath, fn, subs):
    """(目录, 文件名) -> (sub, ep, role) ；role: "main"/"standby"/None。"""
    full = os.path.join(dirpath, fn)
    sub = _sc_match_sub(full, subs)
    if not sub:
        return None
    low = fn.lower()
    main_lab = _sc_hit_label(fn, [sub["main"]]) if sub["main"] else None
    if main_lab:
        role = "main"
    elif _sc_hit_label(fn, sub["standby"]):
        role = "standby"
    else:
        return None
    ep = _sc_parse_ep(fn)
    if ep is None:
        return None
    return (sub, ep, role)


def _sc_delete_media(dirpath, fn):
    """删除媒体文件及其同名配套（.nfo/.ass/-thumb.jpg 等），返回删除数。"""
    base = os.path.splitext(fn)[0]
    n = 0
    for name in os.listdir(dirpath):
        candidate = os.path.splitext(name)
        same_base = (name == fn or candidate[0] == base)
        if not same_base:
            continue
        ext = candidate[1].lower()
        if name != fn and ext not in _SC_SIDE_EXT:
            continue
        try:
            os.remove(os.path.join(dirpath, name))
            n += 1
        except OSError as exc:  # noqa: BLE001
            log("[备源清理] 删除失败 %s: %r" % (os.path.join(dirpath, name), exc))
    return n


def _standby_cleanup_run(trigger="manual"):
    """执行一轮备源清理，返回结果 dict（ok / deleted / results）。

    主扫描对象是【文件】（媒体库 + 下载暂存目录）：同番同集已有主组文件时删除
    备组文件。qB 里仍在下载的备组种子若同番同集主组已完成/文件已存在，也一并删
    任务（种子删完即被流水线归档删除，能扫到是锦上添花）。"""
    with _SC_LOCK:
        sc = _sc_cfg()
        client = _make_client(load_config())
        try:
            subs = _sc_subs_index(client)
        except AniRssError as exc:
            return {"ok": False, "error": str(exc)}

        results, deleted = [], 0

        # ---- ① 文件扫描：媒体库 + 下载暂存目录 ----
        roots = list(sc["scanDirs"])
        if sc["downloadDir"]:
            roots.append(sc["downloadDir"])
        seen = {}          # (sub_id, ep) -> {"main": bool, "standby": [(dir, fn)]}
        for dirpath, fn in _sc_iter_files(roots):
            hit = _sc_file_ep_group(dirpath, fn, subs)
            if not hit:
                continue
            sub, ep, role = hit
            key = (id(sub), ep)
            rec = seen.setdefault(key, {"sub": sub, "ep": ep, "dir": dirpath,
                                        "main": [], "standby": []})
            rec[role].append((dirpath, fn))
        for rec in seen.values():
            if not rec["standby"] or not rec["main"]:
                continue
            if not sc["deleteFiles"]:
                results.append({"torrent": "%s E%03d（备组文件保留，deleteFiles=false）"
                                % (rec["sub"]["title"], rec["ep"]),
                                "action": "skip", "detail": "未开启文件删除"})
                continue
            n = 0
            for dirpath, fn in rec["standby"]:
                n += _sc_delete_media(dirpath, fn)
            if n:
                deleted += n
                results.append({"torrent": "%s E%03d" % (rec["sub"]["title"], rec["ep"]),
                                "action": "delete",
                                "detail": "主组已有同集文件, 已删 %d 个备组文件(%s)"
                                          % (n, os.path.basename(rec["standby"][0][1])[:40])})

        # ---- ② qB 在途种子：备组仍在下载、主组已完成或文件已存在 => 删任务 ----
        torrents, qb_ok = [], True
        if sc["qbHost"] and sc["qbApiKey"]:
            try:
                torrents = _qb_call(sc, "/api/v2/torrents/info") or []
            except Exception as exc:  # noqa: BLE001
                qb_ok = False
                results.append({"torrent": "", "action": "error",
                                "detail": "qB 连接失败: %s" % exc})
        if qb_ok and torrents:
            main_eps = set()
            for rec in seen.values():
                if rec["main"]:
                    main_eps.add((id(rec["sub"]), rec["ep"]))
            to_del = {}
            for t in torrents:
                if str(t.get("category") or "") not in ("ani-rss", ""):
                    continue
                name = t.get("name") or ""
                hit = _sc_match_sub(t.get("savePath") or "", subs)
                if not hit:
                    continue
                if hit["main"] and hit["main"].lower() in name.lower():
                    continue                      # 主组种子永不删
                if not _sc_hit_label(name, hit["standby"]):
                    continue
                ep = _sc_parse_ep(name)
                if ep is None:
                    continue
                main_ready = ((id(hit), ep) in main_eps) or any(
                    (o.get("hash") or "").lower() != (t.get("hash") or "").lower()
                    and hit["main"] and hit["main"].lower() in (o.get("name") or "").lower()
                    and _sc_parse_ep(o.get("name")) == ep
                    and float(o.get("progress") or 0) >= 1.0
                    for o in torrents)
                if not main_ready:
                    continue
                to_del[(t.get("hash") or "").lower()] = name
            if to_del:
                try:
                    qs = urllib.parse.urlencode(
                        {"hashes": "|".join(to_del), "deleteFiles": "true"})
                    _qb_call(sc, "/api/v2/torrents/delete?" + qs)
                    deleted += len(to_del)
                    for name in to_del.values():
                        results.append({"torrent": name[:70], "action": "delete",
                                        "detail": "主组已有同集, 已删除在途备组种子"})
                except Exception as exc:  # noqa: BLE001
                    results.append({"torrent": "", "action": "error",
                                    "detail": "qB 删除失败: %s" % exc})

        st = _sc_load_state()
        hist = st.get("history") or []
        if results or trigger != "auto":
            hist.append({"ts": _now(), "trigger": trigger,
                         "deleted": deleted, "results": results})
        st.update({"lastRun": _now(), "lastTrigger": trigger, "history": hist[-30:]})
        _sc_save_state(st)
        if deleted:
            log("[备源清理] %s 触发：已删除 %d 个备组重复文件/种子" % (trigger, deleted))
        return {"ok": True, "deleted": deleted, "results": results}


def _standby_cleanup_loop():
    while True:
        sc = _sc_cfg()
        if sc["enable"]:
            try:
                _standby_cleanup_run("auto")
            except Exception as exc:  # noqa: BLE001
                log("[备源清理] 自动清理异常: %r" % exc)
        time.sleep(sc["intervalMin"] * 60)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = APP_NAME + "/" + VERSION
    sys_version = ""          # do not advertise the Python version

    # ---- helpers ----
    def log_message(self, fmt, *args):  # silence per-request noise
        pass

    def _allowed_hosts(self):
        hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
        try:
            hosts.add(socket.gethostbyname(socket.gethostname()))
        except Exception:  # noqa: BLE001
            pass
        # 额外允许的主机名：config.json -> "allowedHosts": ["example.com", ...]
        # 以及本机全部网卡 IP（bind=0.0.0.0 时按 IP 直连也应放行）
        try:
            extra = (load_config().get("allowedHosts") or [])
            for h in extra:
                h = (h or "").strip().strip("[]")
                if h:
                    hosts.add(h)
        except Exception:  # noqa: BLE001
            pass
        if (load_config().get("bind") or "") == "0.0.0.0":
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None):
                    hosts.add(info[4][0])
            except Exception:  # noqa: BLE001
                pass
        return hosts

    def _check_auth(self) -> bool:
        """HTTP Basic 认证闸门。

        config.json -> "auth": {"enable": true, "username": "x", "password": "y"}
        未启用或未配密码时直接放行（保持本机开发体验）。
        本机回环（127.0.0.1 / ::1）免认证，避免本地脚本/联调被打断。
        """
        try:
            auth = load_config().get("auth") or {}
        except Exception:  # noqa: BLE001
            return True
        if not auth.get("enable"):
            return True
        user = (auth.get("username") or "").strip()
        pwd = (auth.get("password") or "")
        if not pwd:
            return True
        # 本机回环免密，避免打断本地开发脚本
        peer = (self.client_address[0] or "")
        if peer in ("127.0.0.1", "::1", "localhost"):
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
                got_user, _, got_pwd = raw.partition(":")
            except Exception:  # noqa: BLE001
                got_user, got_pwd = "", ""
            ok_user = hmac.compare_digest(
                got_user.encode("utf-8"), user.encode("utf-8"))
            ok_pwd = hmac.compare_digest(
                got_pwd.encode("utf-8"), pwd.encode("utf-8"))
            if ok_user and ok_pwd:
                return True
        body = ("认证失败：需要有效的用户名与密码。\n"
                "Authentication required.\n").encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="DaisySG", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return False

    def _guard(self) -> bool:
        """Defence against DNS-rebinding and cross-site POSTs.

        A page on evil.com can reach 127.0.0.1, but it cannot forge the Host or
        Origin header. Local scripts/curl send no Origin at all -> allowed.
        """
        if not self._check_auth():
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host and host not in self._allowed_hosts():
            self._json({"ok": False, "error": "Host 不被允许: %s" % host}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            ohost = urllib.parse.urlparse(origin).hostname or ""
            if ohost not in self._allowed_hosts():
                self._json({"ok": False, "error": "Origin 不被允许: %s" % ohost}, 403)
                return False
        return True

    def _send(self, status: int, body: bytes, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), _MIME[".json"])

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(min(n, 4 * 1024 * 1024))
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _resource(self, name: str):
        """Locate a servable file, preferring user-editable copies (see module helper)."""
        return _resource_path(name)

    def _client(self) -> AniRss:
        cfg = load_config()
        base = (self.headers.get("X-AniRss-Base") or cfg.get("anirssBase") or DEFAULT_ANIRSS).strip()
        key = (self.headers.get("X-AniRss-Key") or cfg.get("apiKey") or "").strip()
        return AniRss(base, key, int(cfg.get("timeout") or 25))

    # ---- reminder proxy (番剧更新提醒, port 8903) ----
    _REMINDER_MAP = {
        "/data": ("/api/data", "GET"),
        "/episodes": ("/api/episodes", "GET"),
        "/toggle": ("/api/toggle", "POST"),
        "/hide": ("/api/hide", "POST"),
        "/test": ("/api/test", "POST"),
    }

    def _reminder_proxy(self, sub: str, body=None):
        """Reminder endpoints. Preferred: the embedded engine (same process).
        Fallback: proxy to the standalone service on 8903."""
        eng = REMINDER_ENGINE
        if eng is not None:
            try:
                if sub == "/data":
                    self._json(eng.api_data(), 200)
                elif sub == "/episodes":
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    self._json(eng.api_episodes(int(q.get("id", ["0"])[0])), 200)
                elif sub == "/toggle":
                    self._json(eng.api_toggle((body or {}).get("id", 0)), 200)
                elif sub == "/hide":
                    self._json(eng.api_hide((body or {}).get("id", 0)), 200)
                elif sub == "/test":
                    self._json(eng.api_test(), 200)
                elif sub == "/rematch":
                    self._json(eng.api_rematch(), 200)
                elif sub == "/config":
                    self._json(eng.api_config(body), 200)
                else:
                    self._json({"ok": False, "error": "unknown reminder path: %s" % sub}, 404)
            except Exception as exc:  # noqa: BLE001 - engine errors degrade, never 500
                log("reminder engine %s failed: %r" % (sub, exc))
                self._json({"ok": False, "error": "提醒引擎异常：%s" % exc}, 200)
            return

        # ---- fallback: standalone service ----
        cfg = load_config()
        base = (cfg.get("reminderBase") or DEFAULT_REMINDER).rstrip("/")
        mapped = self._REMINDER_MAP.get(sub)
        if not mapped:
            self._json({"ok": False, "error": "unknown reminder path: %s" % sub}, 404)
            return
        upstream_path, method = mapped
        qs = ""
        if method == "GET":
            q = urllib.parse.urlparse(self.path).query
            if q:
                qs = "?" + q
        url = base + upstream_path + qs
        # /api/episodes may kick a TMDB+Emby match in the reminder's background
        # thread; give it materially more time than the fast cached reads.
        timeout = 40 if upstream_path == "/api/episodes" else 15
        data = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if method == "POST":
            data = json.dumps(body or {}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            self._json(json.loads(payload.decode("utf-8")), 200)
        except Exception as exc:  # noqa: BLE001 - proxy must degrade, never 500
            log("reminder proxy %s failed: %r" % (sub, exc))
            self._json({"ok": False, "error": "提醒服务不可达（%s）：%s" % (base, exc),
                        "reachable": False}, 200)

    # ---- routing ----
    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/guide"):
            p = self._resource(GUIDE_FILE)
            if not p:
                self._send(500, b"guide html missing", "text/plain; charset=utf-8")
                return
            with open(p, "rb") as fh:
                self._send(200, fh.read(), _MIME[".html"])
            return
        if path == "/season-data.js":
            p = self._resource(DATA_FILE)
            if not p:
                self._send(200, b"window.SEASON_DATA=null;", _MIME[".js"])
                return
            with open(p, "rb") as fh:
                self._send(200, fh.read(), _MIME[".js"])
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/state":
            self._api_state()
            return
        if path == "/api/oldrss/list":
            self._api_old_list()
            return
        if path == "/api/anirss/names":
            self._api_sub_names()
            return
        if path == "/api/anirss/audit":
            self._api_audit()
            return
        if path == "/api/autosub/status":
            self._api_autosub_status()
            return
        if path == "/api/standbyCleanup/status":
            self._api_sc_status()
            return
        if path == "/api/season/status":
            self._api_season_status()
            return
        if path.startswith("/api/reminder/"):
            self._reminder_proxy(path[len("/api/reminder"):])
            return
        self._json({"ok": False, "error": "not found: %s" % path}, 404)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()
        route = {
            "/api/anirss/probe": self._api_probe,
            "/api/anirss/search": self._api_search,
            "/api/anirss/groups": self._api_groups,
            "/api/anirss/push": self._api_push,
            "/api/anirss/switch": self._api_switch,
            "/api/anirss/refresh": self._api_refresh,
            "/api/anirss/autoplan": self._api_autoplan,
            "/api/season/refresh": self._api_season_refresh,
            "/api/oldrss/delete": self._api_old_delete,
            "/api/auditIgnore/set": self._api_audit_ignore,
            "/api/autosub/config": self._api_autosub_config,
            "/api/autosub/run": self._api_autosub_run,
            "/api/standbyCleanup/status": self._api_sc_status,
            "/api/standbyCleanup/config": self._api_sc_config,
            "/api/config": self._api_config,
            "/api/quit": self._api_quit,
        }.get(path)
        if not route and path.startswith("/api/reminder/"):
            self._reminder_proxy(path[len("/api/reminder"):], body)
            return
        if not route:
            self._json({"ok": False, "error": "not found: %s" % path}, 404)
            return
        try:
            route(body)
        except AniRssError as exc:
            self._json({"ok": False, "error": str(exc)}, 200)
        except Exception as exc:  # noqa: BLE001
            log("ERROR %s: %r" % (path, exc))
            self._json({"ok": False, "error": "服务器内部错误: %s" % exc}, 200)

    # ---- endpoints ----
    def _api_season_status(self, _body=None):
        with _season_lock:
            st = dict(_season_state)
        age = None
        p = _resource_path(DATA_FILE)
        if p:
            try:
                age = max(0, int((time.time() - os.path.getmtime(p)) / 60))
            except OSError:
                pass
        self._json({
            "ok": True,
            "running": st["running"],
            "generatedAt": _season_generated_at(),
            "ageMinutes": age,
            "startedAt": st["startedAt"],
            "finishedAt": st["finishedAt"],
            "lastOk": st["ok"],
            "lastMsg": st["msg"],
            "script": _find_season_script() is not None,
        })

    def _api_season_refresh(self, _body=None):
        """后台跑 refresh-season.ps1 重建 season-data.js；并发触发直接合并。"""
        ps1 = _find_season_script()
        if not ps1:
            self._json({"ok": False, "error": "找不到 scripts/refresh-season.ps1"})
            return
        with _season_lock:
            if _season_state["running"]:
                self._json({"ok": True, "running": True, "already": True})
                return
            _season_state.update(running=True, startedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                                 ok=None, msg="running")
        threading.Thread(target=_season_build_worker, args=(ps1,), daemon=True).start()
        log("[season] refresh triggered via /api/season/refresh")
        self._json({"ok": True, "running": True, "startedAt": _season_state["startedAt"]})

    def _api_state(self, _body=None):
        cfg = load_config()
        out = {
            "ok": True,
            "app": {"name": APP_NAME, "version": VERSION, "dataDir": DATA},
            "config": {
                "port": cfg.get("port"),
                "bind": cfg.get("bind"),
                "anirssBase": cfg.get("anirssBase"),
                "apiKeySet": bool((cfg.get("apiKey") or "").strip()),
                "apiKeyTail": ("…" + cfg["apiKey"][-4:]) if (cfg.get("apiKey") or "").strip() else "",
                "openBrowser": cfg.get("openBrowser"),
                "enable": cfg.get("enable"),
                "defaultMain": cfg.get("defaultMain") or "",
                "defaultStandby": cfg.get("defaultStandby") or [],
            },
            "anirss": {"reachable": False, "authOk": False, "total": 0, "subscribed": []},
        }
        try:
            subs = self._client().subscriptions()
            out["anirss"] = {
                "reachable": True,
                "authOk": True,
                "total": len(subs),
                "subscribed": [
                    {
                        "bgmId": subject_id(s.get("bgmUrl")),
                        "title": s.get("title"),
                        "subgroup": s.get("subgroup"),
                        "ids": list(mikan_ids(s.get("url"))),
                        "enable": s.get("enable"),
                        "standby": [
                            {"label": x.get("label"), "url": x.get("url")}
                            for x in (s.get("standbyRssList") or [])
                        ],
                    }
                    for s in subs
                ],
            }
        except AniRssError as exc:
            out["anirss"]["error"] = str(exc)
        self._json(out)

    def _api_probe(self, body):
        base = (body.get("anirssBase") or "").strip()
        key = (body.get("apiKey") or "").strip()
        cfg = load_config()
        key = key or (cfg.get("apiKey") or "")
        try:
            subs = AniRss(base or cfg.get("anirssBase"), key, int(cfg.get("timeout") or 25)).subscriptions()
        except AniRssError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True, "total": len(subs), "authOk": True})

    def _api_search(self, body):
        """多源聚合搜索：① 蜜柑番剧候选（ani-rss）② 动漫花园 / Nyaa 资源级聚合。
        蜜柑失败只降级（candidates 空 + mikanError），资源聚合照常。"""
        text = (body.get("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "缺少搜索关键词"})
            return
        cands, mikan_err = [], ""
        try:
            cands = self._client().mikan_search(text)
        except AniRssError as exc:
            mikan_err = str(exc)
        candidates = [
            {
                "title": c.get("title"),
                "url": c.get("url"),
                "exists": bool(c.get("exists")),
                "score": c.get("score"),
                "cover": c.get("cover"),
            }
            for c in cands
        ]
        # 资源聚合；别名表带上蜜柑候选名，让资源标题能跨命名方式匹配
        aliases = [text]
        for c in cands:
            for k in ("title", "title_cn"):
                v = (c.get(k) or "").strip()
                if v:
                    aliases.append(v)
        resources, sources, took = _aggregate_resources(text, aliases)
        self._json({
            "ok": True,
            "q": text,
            "candidates": candidates,
            "resources": resources,
            "sources": sources,
            "mikanError": mikan_err,
            "tookMs": took,
        })

    def _api_groups(self, body):
        url = (body.get("url") or "").strip()
        if not url:
            self._json({"ok": False, "error": "缺少蜜柑条目地址"})
            return
        groups = self._client().mikan_groups(url)
        self._json({
            "ok": True,
            "bgmId": subject_id(groups[0].get("bgmUrl")) if groups else "",
            "bgmUrl": groups[0].get("bgmUrl") if groups else "",
            "groups": [
                {
                    "label": g.get("label"),
                    "rss": g.get("rss"),
                    "updateDay": g.get("updateDay"),
                    "count": len(g.get("items") or []),
                    # 最近 15 条资源明细，供前端点开行内展开（太老的对选订阅没参考价值）
                    "items": [
                        {
                            "title": (it.get("title") or "")[:150],
                            "size": it.get("formatSize") or "",
                            "date": (it.get("createdAt") or "")[:16],
                            "magnet": it.get("magnet") or "",
                        }
                        for it in (g.get("items") or [])[:15]
                    ],
                }
                for g in groups
            ],
        })

    def _api_push(self, body):
        """Push one or more subscriptions, each with a 主订阅 + 备用订阅 set."""
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            self._json({"ok": False, "error": "没有要推送的条目"})
            return
        self._json(_push_items(self._client(), items, bool(body.get("dryRun"))))

    def _api_switch(self, body):
        """切换字幕组（覆盖旧订阅）：删同番旧订阅 → 按新主/备重订。
        记录守护：删前 mirror、删后 restore，去重记录零丢失。"""
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            self._json({"ok": False, "error": "没有要切换的条目"})
            return
        self._json(_switch_items(self._client(), items, bool(body.get("dryRun"))))

    def _api_autoplan(self, body):
        """批量订阅计划：番名列表 → 每番的蜜柑匹配与默认主/备组（不写入）。"""
        titles = body.get("titles") or []
        if not isinstance(titles, list) or not titles:
            self._json({"ok": False, "error": "没有要规划的番剧"})
            return
        plans = _autoplan_titles(self._client(), [str(t) for t in titles], load_config())
        self._json({"ok": True, "plans": plans})

    def _api_autosub_status(self, _body=None):
        a = (load_config().get("autoSub") or {})
        st = _load_autosub_state()
        self._json({"ok": True, "config": a, "lastRun": st.get("lastRun") or "",
                    "processedN": len(st.get("processed") or {}),
                    "history": (st.get("history") or [])[-30:]})

    def _api_autosub_config(self, body):
        cfg = load_config()
        a = body.get("autoSub")
        if not isinstance(a, dict):
            self._json({"ok": False, "error": "缺少 autoSub 配置"})
            return
        keep = {"enable": bool(a.get("enable")), "dryRun": bool(a.get("dryRun"))}
        try:
            keep["minRating"] = round(max(0.0, float(a.get("minRating") or 0)), 1)
        except (TypeError, ValueError):
            keep["minRating"] = 0
        try:
            keep["maxPerRun"] = max(1, min(30, int(a.get("maxPerRun") or 5)))
        except (TypeError, ValueError):
            keep["maxPerRun"] = 5
        for k in ("mainGroups", "standbyGroups", "keywords", "exclude"):
            v = a.get(k)
            if isinstance(v, list):
                keep[k] = [str(x).strip() for x in v if str(x).strip()]
            else:
                keep[k] = [s.strip() for s in str(v or "").replace("，", ",").split(",") if s.strip()]
        cfg["autoSub"] = keep
        save_config(cfg)
        self._json({"ok": True, "message": "自动追新规则已保存", "config": keep})

    def _api_autosub_run(self, _body=None):
        self._json(_autosub_scan("manual"))

    def _api_sc_status(self, _body=None):
        cfg = load_config()
        st = _sc_load_state()
        c = _sc_cfg(cfg)
        c["qbApiKey"] = "…" + c["qbApiKey"][-4:] if len(c["qbApiKey"]) > 6 else ("…" if c["qbApiKey"] else "")
        self._json({"ok": True, "config": c, "lastRun": st.get("lastRun") or "",
                    "lastTrigger": st.get("lastTrigger") or "",
                    "history": (st.get("history") or [])[-30:]})

    def _api_sc_config(self, body):
        cfg = load_config()
        a = body.get("standbyCleanup")
        if isinstance(a, dict):
            c = _sc_cfg(cfg)
            if "enable" in a:
                c["enable"] = bool(a.get("enable"))
            if "intervalMin" in a:
                try:
                    c["intervalMin"] = max(3, min(720, int(a.get("intervalMin") or 10)))
                except (TypeError, ValueError):
                    pass
            if "qbHost" in a:
                c["qbHost"] = str(a.get("qbHost") or "").strip().rstrip("/")
            if "deleteFiles" in a:
                c["deleteFiles"] = bool(a.get("deleteFiles"))
            if "scanDirs" in a:
                c["scanDirs"] = a.get("scanDirs")
            if "downloadDir" in a:
                c["downloadDir"] = str(a.get("downloadDir") or "").strip()
            key = body.get("qbApiKey") or a.get("qbApiKey")
            if isinstance(key, str):
                key = key.strip()
                if key and not key.startswith("…"):
                    c["qbApiKey"] = key
            cfg["standbyCleanup"] = c
            save_config(cfg)
        run_now = bool(body.get("runNow"))
        out = {"ok": True, "message": "备源清理设置已保存", "config": _sc_cfg(load_config())}
        if run_now:
            res = _standby_cleanup_run("manual")
            out["run"] = res
            out["message"] = ("检查完成：已删除 %d 个备组重复文件"
                              % res.get("deleted", 0)
                              if res.get("ok") else "检查失败：" + str(res.get("error")))
        self._json(out)

    def _api_refresh(self, _body):
        client = self._client()
        try:
            client.refresh_all()
        except AniRssError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True, "message": "已触发 ani-rss 全量刷新"})

    # ---- 老订阅管理（列表 / 带闸门的批量删除） ----
    _SUB_NAMES_CACHE = {"ts": 0.0, "names": [], "subs": []}   # 60s 缓存：番名 + 主/备字幕组

    def _api_sub_names(self, _body=None):
        """轻量端点：已订阅番名列表 + 各自的主/备字幕组。

        names 供「本季实况」标注是否已订阅（旧字段，保持兼容）；
        subs 为 [{name, main, standby:[...]}]，前端用它显示"已订阅了哪个字幕组"。"""
        import time as _t
        now = _t.time()
        c = self._SUB_NAMES_CACHE
        if now - c["ts"] > 60:
            try:
                subs = self._client().subscriptions()
            except AniRssError as exc:
                self._json({"ok": False, "error": str(exc)})
                return
            names = []
            subs_out = []
            for s in subs:
                main = (s.get("subgroup") or "").strip()
                standby = []
                for r in (s.get("standbyRssList") or []):
                    if isinstance(r, dict):
                        g = str(r.get("subgroup") or r.get("label") or "").strip()
                    elif isinstance(r, str):
                        g = r.strip()
                    else:
                        g = ""
                    if g and g not in standby:
                        standby.append(g)
                for f in ("title", "jpTitle"):
                    v = (s.get(f) or "").strip()
                    if not v:
                        continue
                    if v not in names:
                        names.append(v)
                    subs_out.append({"name": v, "main": main, "standby": standby})
            c["ts"], c["names"], c["subs"] = now, names, subs_out
        self._json({"ok": True, "total": len(c["names"]), "names": c["names"],
                    "subs": c.get("subs") or []})

    def _api_audit(self, _body=None):
        """订阅审计（只读）：有字幕组在做但未订阅的番 / 已订阅但缺的备用组。

        数据源全部在本地或内网：season-data.js（本季快照）+ 一次 ani-rss listAni，
        不额外请求蜜柑，因此可以随页面随时刷新。写操作仍走既有链路
        （一键订阅 = /api/anirss/autoplan + /api/anirss/push；补齐备用 = /api/anirss/switch）。
        """
        try:
            payload = _audit_payload(self._client(), load_config())
        except AniRssError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            log("ERROR /api/anirss/audit: %r" % exc)
            self._json({"ok": False, "error": "审计失败: %s" % exc})
            return
        self._json(payload)

    def _api_audit_ignore(self, body):
        """审计「不看」忽略名单的增删（持久化到 audit-ignore-state.json）。
        body: {bgmId?, title?, ignore?=true} 或 {clear: true} 恢复全部。"""
        items = _load_audit_ignore()
        if body.get("clear"):
            _save_audit_ignore([])
            self._json({"ok": True, "ignored": []})
            return
        title = (body.get("title") or "").strip()
        bid = str(body.get("bgmId") or "").strip()
        nt = _norm_name(title)
        if not bid and len(nt or "") < 3:
            self._json({"ok": False, "error": "缺少 bgmId 或有效标题"})
            return
        ignore = body.get("ignore") is not False  # 缺省 = 加入忽略

        def same(x):
            xb = str(x.get("bgmId") or "").strip()
            xn = str(x.get("norm") or "").strip()
            return (bid and xb and xb == bid) or (nt and len(nt) >= 3 and xn and xn == nt)

        if ignore:
            if not any(same(x) for x in items):
                items.append({"bgmId": bid, "title": title, "norm": nt,
                              "at": time.strftime("%Y-%m-%d %H:%M")})
        else:
            items = [x for x in items if not same(x)]
        _save_audit_ignore(items)
        self._json({"ok": True, "ignored": items})

    def _api_old_list(self, _body=None):
        client = self._client()
        try:
            subs = client.subscriptions()
        except AniRssError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        items = []
        for s in subs:
            ldt = s.get("lastDownloadTime")
            if isinstance(ldt, (int, float)) or (isinstance(ldt, str) and ldt.isdigit()):
                try:
                    ldt = time.strftime("%Y-%m-%d", time.localtime(float(ldt) / 1000.0))
                except Exception:  # noqa: BLE001
                    pass
            items.append({
                "id": s.get("id"), "title": s.get("title"), "jpTitle": s.get("jpTitle"),
                "season": s.get("season"), "subgroup": s.get("subgroup"),
                "enable": bool(s.get("enable")), "completed": bool(s.get("completed")),
                "cur": s.get("currentEpisodeNumber"), "total": s.get("totalEpisodeNumber"),
                "lastDownload": str(ldt or "")[:10],
                "standbyN": len(s.get("standbyRssList") or []),
                "bgmUrl": s.get("bgmUrl"), "image": s.get("image") or "",
            })
        self._json({"ok": True, "items": items, "total": len(items)})

    def _run_mirror(self):
        cfg = load_config()
        py = (cfg.get("mirrorPy") or MIRROR_PY).strip()
        tool = (cfg.get("mirrorTool") or MIRROR_TOOL).strip()
        if not (os.path.isfile(py) and os.path.isfile(tool)):
            return {"ok": False, "msg": "镜像工具缺失：%s / %s" % (py, tool)}
        try:
            p = subprocess.run([py, tool, "mirror"], capture_output=True, text=True,
                               timeout=600, encoding="utf-8", errors="replace",
                               **_no_window_kwargs())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": str(exc)}
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip()[-300:]
            return {"ok": False, "msg": "mirror 退出码 %s %s" % (p.returncode, tail)}
        return {"ok": True, "msg": (p.stdout or "").strip()[-200:] or "mirror 完成"}

    def _api_old_delete(self, body):
        if body.get("confirm") is not True:
            self._json({"ok": False, "error": "缺少确认标记（confirm）"})
            return
        ids = body.get("ids")
        if (not isinstance(ids, list) or not ids
                or not all(isinstance(x, str) and x.strip() for x in ids)):
            self._json({"ok": False, "error": "ids 需为非空字符串数组"})
            return
        ids = [x.strip() for x in ids]
        client = self._client()
        try:
            subs = client.subscriptions()
        except AniRssError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        by_id = {s.get("id"): s for s in subs if s.get("id")}
        targets = [by_id[i] for i in ids if i in by_id]
        missing = [i for i in ids if i not in by_id]
        if not targets:
            self._json({"ok": False, "error": "所选订阅均不存在（可能已被删除）"})
            return
        # 闸门 1：先把去重记录镜像入库（blob 可还原），失败即拒绝删除
        mir = self._run_mirror()
        if not mir["ok"]:
            self._json({"ok": False,
                        "error": "去重记录镜像失败，已拒绝删除（防止日后重订阅全量重下）：" + mir["msg"]})
            return
        # 闸门 2：快照完整 Ani 对象（重订阅时的 addAni 载荷）
        ts = time.strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(app_dir(), "oldrss_backup")
        try:
            os.makedirs(bdir, exist_ok=True)
            snap = os.path.join(bdir, "%s-subscriptions.json" % ts)
            with open(snap, "w", encoding="utf-8") as fh:
                json.dump(targets, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            self._json({"ok": False, "error": "订阅快照写入失败，已拒绝删除：%s" % exc})
            return
        try:
            res = client.call("/api/deleteAni", ids, query={"deleteFiles": "false"})
        except AniRssError as exc:
            self._json({"ok": False, "error": "删除请求失败：" + str(exc), "snapshot": snap})
            return
        if res.get("code") != 200:
            self._json({"ok": False, "error": "ani-rss 返回失败：" + str(res.get("message")),
                        "snapshot": snap})
            return
        self._json({"ok": True, "deleted": len(targets), "missing": missing,
                    "snapshot": snap, "mirror": mir["msg"],
                    "message": "已删除 %d 条订阅；去重记录已镜像入库。重订阅须先 restore 记录再 addAni" % len(targets)})

    def _api_config(self, body):
        cfg = load_config()
        restart = False
        if "port" in body:
            try:
                p = int(body["port"])
                if not (1024 <= p <= 65535):
                    raise ValueError
                if p != cfg.get("port"):
                    cfg["port"] = p
                    restart = True
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "端口需为 1024-65535 的整数"})
                return
        if body.get("bind") in ("127.0.0.1", "0.0.0.0") and body["bind"] != cfg.get("bind"):
            cfg["bind"] = body["bind"]
            restart = True
        for k in ("anirssBase", "defaultMain"):
            if k in body:
                cfg[k] = str(body.get(k) or "").strip()
        if "defaultStandby" in body and isinstance(body["defaultStandby"], list):
            cfg["defaultStandby"] = [str(x).strip() for x in body["defaultStandby"] if str(x).strip()]
        if "openBrowser" in body:
            cfg["openBrowser"] = bool(body["openBrowser"])
        if "enable" in body:
            cfg["enable"] = bool(body["enable"])
        # A masked placeholder means "unchanged"; never overwrite the real key.
        newkey = body.get("apiKey")
        if isinstance(newkey, str):
            newkey = newkey.strip()
            if newkey and not newkey.startswith("…"):
                cfg["apiKey"] = newkey
        save_config(cfg)
        self._json({"ok": True, "restartRequired": restart,
                    "message": "配置已保存" + ("（端口/绑定已变更，需重启生效）" if restart else "")})

    def _api_quit(self, _body):
        self._json({"ok": True, "message": "服务正在退出"})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------
def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


REMINDER_ENGINE = None   # set in main(): the embedded 番剧更新提醒 module


def _migrate_reminder_data(data_dir: str) -> None:
    """First run after merging the standalone reminder exe: carry over its
    live config (DingTalk / Emby / TMDB / ani-rss credentials), dedup state,
    Emby alignment cache and the AniBT groups cache. The live instance kept
    its files in its dist/ folder next to the exe."""
    import shutil
    old_bases = ()  # 一次性迁移源（原作者环境），公开版留空；需要迁移时在此填旧目录
    pairs = [
        ("config.json", "reminder-config.json"),
        ("state.json", "reminder-state.json"),
        ("emby_map.json", "reminder-emby-map.json"),
        ("anibt_groups_cache.json", "reminder-groups-cache.json"),
    ]
    for src_name, dst_name in pairs:
        dst = os.path.join(data_dir, dst_name)
        if os.path.exists(dst):
            continue
        for base in old_bases:
            p = os.path.join(base, src_name)
            if os.path.isfile(p):
                try:
                    shutil.copy2(p, dst)
                    log("提醒数据迁移：%s -> %s" % (p, dst))
                except Exception as exc:  # noqa: BLE001
                    log("提醒数据迁移失败 %s: %r" % (p, exc))
                break


def start_reminder_engine(data_dir: str):
    """Import + launch the embedded reminder engine (daemon threads)."""
    global REMINDER_ENGINE
    if not load_config().get("reminder", True):
        log("提醒引擎已在配置中停用（reminder=false），跳过")
        return
    _migrate_reminder_data(data_dir)
    os.environ["REMINDER_DATA_DIR"] = data_dir
    try:
        import reminder  # noqa: PLC0415 - delayed so the env var is set first
    except Exception as exc:  # noqa: BLE001
        log("提醒引擎加载失败（/api/reminder/* 将退回代理 8903）：%r" % exc)
        return
    try:
        reminder.start_background()
        REMINDER_ENGINE = reminder
    except Exception as exc:  # noqa: BLE001
        log("提醒引擎启动失败：%r" % exc)


def _build_https_context(cfg: dict):
    """按配置构建 TLS 上下文；未启用或证书缺失时返回 None。

    启用条件（config.json）：
      "https": {
        "enable": true,
        "port": 7789,
        "cert": "C:/path/fullchain.pem",
        "key":  "C:/path/privkey.pem"
      }
    """
    https_cfg = cfg.get("https") or {}
    if not https_cfg.get("enable"):
        return None
    cert = (https_cfg.get("cert") or "").strip()
    key = (https_cfg.get("key") or "").strip()
    if not cert or not key:
        log("HTTPS 已启用但 cert/key 未配置，跳过")
        return None
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        log("HTTPS 证书文件不存在：cert=%s key=%s" % (cert, key))
        return None
    try:
        import ssl as _ssl
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        return ctx
    except Exception as exc:  # noqa: BLE001
        log("HTTPS 上下文构建失败：%r" % exc)
        return None


def _serve_https(httpd_plain, cfg: dict):
    """在同一进程内并行启动 HTTPS 监听（不使用反向代理）。"""
    ctx = _build_https_context(cfg)
    if ctx is None:
        return None
    https_cfg = cfg.get("https") or {}
    hport = int(https_cfg.get("port") or 7789)
    bind = cfg.get("bind") or "127.0.0.1"
    try:
        httpsd = ThreadingHTTPServer((bind, hport), Handler)
    except OSError as exc:
        log("HTTPS 绑定 %s:%d 失败 -> %s" % (bind, hport, exc))
        return None
    httpsd.daemon_threads = True
    httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
    threading.Thread(target=httpsd.serve_forever, daemon=True).start()
    return httpsd


def _alert_already_running() -> None:
    """第二个实例启动时弹窗提示：本程序同一时间只允许运行一份。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "程序已在运行中。\n\n同一时间只能启动一份：\n请先关闭现有的控制台窗口，再启动新的实例。",
            APP_NAME, 0x40)
    except Exception:  # noqa: BLE001
        pass


# 命名互斥体句柄：必须持有整个进程生命周期，被 GC 回收等于自动释放
_instance_mutex = None


def _acquire_single_instance() -> bool:
    """Windows 命名互斥体：同一桌面会话只允许一个实例。

    之前仅靠 HTTP 端口探测（already_running）判定，但存在时序漏洞：
    第二个实例会借 SO_REUSEADDR 静默双绑 7788/7790 成功并打出"启动成功"
    横幅，第一个实例沦为僵尸窗口，控制台表现混乱。互斥体是权威判定，
    端口探测降级为兜底提示。
    """
    global _instance_mutex
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        _instance_mutex = k32.CreateMutexW(None, False, "Local\\AnimeFansubGuide_SingleInstance")
        return k32.GetLastError() != 183   # 183 = ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return True   # 非 Windows 或判定失败时不拦截，保持原有端口探测兜底


def main() -> int:
    # 原生层崩溃（无 Python traceback）时把现场转储到 fault.log，便于定位
    try:
        import faulthandler
        fault_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fault.log")
        faulthandler.enable(open(os.path.abspath(fault_path), "ab"))
    except Exception:
        pass
    # 单实例权威判定：命名互斥体（见 _acquire_single_instance 文档）
    if not _acquire_single_instance():
        log("[ERROR] 已有一个追番面板实例正在运行，本次启动取消。")
        log("        请使用现有控制台窗口；如需重启，先关闭旧窗口再双击启动。")
        _alert_already_running()
        return 1
    cfg = load_config()
    if not (cfg.get("apiKey") or "").strip():
        key = discover_local_key()
        if key:
            cfg["apiKey"] = key
            save_config(cfg)
            log("已自动读取本机 ani-rss apiKey（可在外观设置中修改）")

    port = int(cfg.get("port") or DEFAULT_PORT)
    bind = cfg.get("bind") or "127.0.0.1"
    url = "http://127.0.0.1:%d/" % port

    if already_running(port):
        log("检测到实例已在运行：同一时间只能运行一份，请先关闭现有窗口再启动。")
        _alert_already_running()
        return 1

    httpd = None
    for attempt in range(2):
        try:
            httpd = ThreadingHTTPServer((bind, port), Handler)
            break
        except OSError as exc:
            # Windows SO_REUSEADDR allows a second bind to silently succeed and
            # become a zombie listener; the already_running() probe above is the
            # guard for that, this handles a genuinely occupied port.
            if attempt == 0:
                time.sleep(3)
                if already_running(port):
                    log("检测到实例已在运行（二次探测）：请先关闭现有窗口再启动。")
                    _alert_already_running()
                    return 1
            log("无法绑定 %s:%d -> %s" % (bind, port, exc))
    if httpd is None:
        log("启动失败：端口 %d 不可用" % port)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "端口 %d 已被占用，无法启动。\n请修改 config.json 中的 port。" % port,
                APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass
        return 1

    httpd.daemon_threads = True
    # 内嵌「番剧更新提醒」引擎：钉钉提醒 / AniBT 轮询 / Emby 对齐 / ani-rss 联动
    start_reminder_engine(DATA)
    # 备源清理后台线程：主 RSS 下载完成的集自动删除备用 RSS 同集下载
    threading.Thread(target=_standby_cleanup_loop, daemon=True).start()

    # 控制台窗口标题
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("DaisySG 追番面板 - AnimeFansubGuide")
    except Exception:  # noqa: BLE001
        pass

    # 可选 HTTPS 双栈监听（TLS 由本进程自持，不经反向代理）；先启动，面板反映真实状态
    httpsd = _serve_https(httpd, cfg)

    https_cfg = cfg.get("https") or {}
    https_on = httpsd is not None
    hport = int(https_cfg.get("port") or 7790)
    auth_cfg = cfg.get("auth") or {}
    lan = lan_ip()

    rule = "-" * 60
    log("")
    log("  +" + rule + "+")
    log("        DaisySG 自动化追番面板   %s v%s" % (APP_NAME, VERSION))
    log("  +" + rule + "+")
    log("   用户门户 : http://localhost:%d/" % port)
    if https_on:
        cert = (https_cfg.get("cert") or "").replace("/", "\\")
        log("   HTTPS    : https://<域名/主机>:%d/" % hport)
        log("              (证书: %s)" % cert)
    elif https_cfg.get("enable"):
        log("   HTTPS    : 启用失败（端口占用或证书缺失，见日志）")
    if bind == "0.0.0.0":
        if https_on:
            log("   局域网   : http://%s:%d/   |   https://%s:%d/" % (lan, port, lan, hport))
        else:
            log("   局域网   : http://%s:%d/" % (lan, port))
    log("   数据来源 : ani-rss @ %s" % cfg.get("anirssBase"))
    if auth_cfg.get("enable") and (auth_cfg.get("password") or ""):
        log("   访问认证 : 已启用（用户 %s；本机回环免认证）" % (auth_cfg.get("username") or "-"))
    else:
        log("   访问认证 : 未启用")
    log("   配置目录 : %s" % DATA)
    log("  +" + rule + "+")
    log("")
    log("   HTTP 服务已启动 (:%d)。保持本窗口打开即可；" % port)
    log("   关闭本窗口 = 停止服务。运行日志同步写入 server.log。")
    log("  +" + rule + "+")
    log("")

    if cfg.get("openBrowser", True):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
