/**
 * build-season.js
 * ---------------------------------------------------------------
 * Reads the raw dumps in .cache/ (produced by refresh-season.ps1),
 * aggregates them into a "current season live picture", and writes
 * season-data.js  ->  window.SEASON_DATA  for the guide page.
 *
 * Offline by design: this script never touches the network, so it
 * runs fine inside the sandbox. Fetching is PowerShell's job.
 * ---------------------------------------------------------------
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const CACHE = path.join(ROOT, '.cache');
const GUIDE = path.join(ROOT, 'anime-fansub-guide.html');
const OUT = path.join(ROOT, 'season-data.js');

/* ------------------------------------------------------------------
 * Traditional -> Simplified. Two roles:
 *   - keep the alias matcher from being fooled by 諸神/诸神, 銀色子彈/银色子弹
 *   - collapse 無職轉生 and 无职转生 into one show row
 * Written as whitespace-separated 2-char pairs and asserted below, so a
 * typo can never shift the whole table out of alignment.
 * ---------------------------------------------------------------- */
const T2S_PAIRS = `
組组 發发 佈布 櫻樱 諸诸 銀银 動动 園园 學学 華华 聯联 夢梦 語语 劇剧 場场 將将 獵猎 戶户 壓压 製制
們们 來来 這这 無无 與与 為为 會会 過过 點点 資资 質质 內内 務务 實实 愛爱 戀恋 風风 葉叶 藍蓝 綠绿
紅红 雲云 龍龙 貓猫 貝贝 車车 時时 間间 覺觉 譯译 漢汉 號号 寶宝 兒儿 國国 開开 關关 長长 門门 馬马
鳥鸟 魚鱼 寫写 讀读 聽听 話话 種种 樣样 術术 現现 鐵铁 戰战 記记 級级 節节 氣气 體体 聲声 萬万 簡简
總总 統统 歸归 納纳 純纯 絲丝 織织 線线 緣缘 終终 縣县 運运 頻频 視视 訊讯 電电 腦脑 網网 絡络 軟软
畫画 職职 轉转 說说 誰谁 請请 謝謝 對对 錯错 麼么 嗎吗 頭头 臉脸 見见 讓让 認认 識识 書书 圖图 館馆
醫医 師师 課课 題题 試试 驗验 產产 業业 員员 議议 團团 隊队 軍军 鬥斗 勝胜 敗败 負负 贏赢 輸输 賽赛
廣广 陽阳 陰阴 擊击 險险 麗丽 變变 護护 週周 樂乐 億亿 價价 儀仪 傳传 傷伤 偵侦 備备 傑杰 傾倾 鍵键
鍊链 鋒锋 鏡镜 鐘钟 閃闪 閉闭 陣阵 陸陆 階阶 隨随 隱隐 雙双 雜杂 難难 靈灵 靜静 領领 顏颜 願愿 飛飞
飾饰 驅驱 髮发 魯鲁 鮮鲜 鳴鸣 叢丛 於于 蓋盖 盡尽 監监 盤盘 當当 疊叠 療疗 鹽盐 憶忆 應应 懷怀 懸悬
戲戏 掃扫 揚扬 損损 搶抢 撲扑 擴扩 擺摆 攔拦 騰腾 蘿萝 蔔卜 蔥葱 薑姜 藥药 衛卫 複复 襪袜 褲裤 襯衬
襲袭 觸触 計计 討讨 論论 訓训 訪访 設设 訴诉 診诊 評评 詞词 詩诗 詳详 誌志 誠诚 誤误 調调 談谈 諾诺
謀谋 謙谦 講讲 證证 讚赞 違违 遠远 適适 選选 遺遗 邁迈 還还 邊边 邏逻 郵邮 鄉乡 釀酿 釋释 裏里 補补
裝装 觀观 規规 親亲 覽览 許许 詠咏 詐诈 詢询 詣诣 詰诘 檔档 襪袜 蘇苏 蘋苹 蘭兰 覺觉 觸触 擬拟 廳厅
異异 妳你 專专 奧奥 顧顾 類类 顯显 颯飒 飄飘 餵喂 鬧闹 鴉鸦 鴻鸿 鷹鹰 麥麦 黃黄 齊齐 齒齿 龜龟 罷罢
罰罚 羅罗 義义 習习 聰聪 肅肃 膚肤 臨临 舉举 舊旧 藝艺 處处 蟲虫 衝冲 連连 進进 達达 遙遥 遞递 遲迟
遼辽 較较 載载 輔辅 輕轻 輛辆 輝辉 輩辈 輪轮 輯辑 辭辞 農农 迴回 賤贱 賺赚 購购 贈赠 趕赶 趙赵 蹤踪
軀躯 軒轩 轄辖 轟轰 燦灿 額额 頰颊 麵面 誒诶 襖袄 誘诱 誦诵 虛虚 誕诞 輝辉 懸悬 戀恋 戲戏 點点 爛烂
`;

const T2S = {};
for (const pair of T2S_PAIRS.split(/\s+/).filter(Boolean)) {
  if (pair.length !== 2) throw new Error('T2S pair must be exactly 2 chars, got: "' + pair + '"');
  T2S[pair[0]] = pair[1];
}

function simp(s) {
  return String(s == null ? '' : s)
    .replace(/[\u3000]/g, '')
    .replace(/[【】\[\]「」『』<>《》()（）]/g, '')
    .split('').map(c => T2S[c] || c).join('');
}

/** Normalised lowercase form used for alias matching. */
function norm(s) {
  return simp(s).replace(/[\s·・\-—_&＆+＋]/g, '').toLowerCase();
}

const SUFFIX = /(字幕组|字幕社|字幕組|制作组|製作組|发布组|發布組|发布組|压制部|壓制部|压制组|壓制組|汉化组|漢化組|同好会|同好會|工作室|制作委员会|发布|發布|字幕)$/;
function stripSuffix(s) { return s.replace(SUFFIX, ''); }

/**
 * Extra noise stripped only when KEYING live-discovered groups, so that
 * "jibaketa合成&壓制" / "jibaketa合成&音頻壓制" / "jibaketa合成&amp;压制"
 * collapse into one entry instead of three.
 */
const NOISE = /(音频|音頻|视频|視頻|压制|壓制|合成|搬运|搬運|整理|发布|發布|字幕|汉化|漢化|制作|製作)+$/g;
function groupKey(raw) {
  let s = stripSuffix(norm(raw));
  let prev = null;
  while (prev !== s) { prev = s; s = s.replace(NOISE, ''); }
  return s;
}

/** Pure aggregator / re-upload accounts -- excluded from the group board. */
function isRelay(raw) {
  const k = stripSuffix(norm(raw));
  return /^(整理)?(搬运|搬運|合集|汇总|匯總)$/.test(k);
}

/* ------------------------------------------------------------------
 * 1. Registry: the guide page's GROUPS array is the single source of
 *    truth for canonical names, aliases and tiers. No duplication.
 * ---------------------------------------------------------------- */
function loadRegistry() {
  const html = fs.readFileSync(GUIDE, 'utf8');
  const m = html.match(/const GROUPS\s*=\s*(\[[\s\S]*?\n\];)/);
  if (!m) throw new Error('GROUPS array not found in ' + GUIDE);
  const arr = eval(m[1].replace(/;\s*$/, ''));
  return arr.map(g => {
    const tokens = [g.name, ...String(g.alias || '').split(/[\/、·]/)]
      .map(t => stripSuffix(norm(t)))
      .filter(Boolean);
    return { name: g.name, tier: g.tier, type: g.type, tokens: [...new Set(tokens)] };
  });
}

function matchGroup(raw, registry) {
  const n = stripSuffix(norm(raw));
  if (!n || n.length < 2) return null;
  let best = null;
  for (const g of registry) {
    for (const t of g.tokens) {
      if (!t) continue;
      if (n === t) return g;
      // substring matching: keep ASCII tokens long enough to be safe (ani/caso are risky)
      const ascii = /^[\x00-\x7F]+$/.test(t);
      if (ascii && t.length < 4) continue;
      if (t.length < 2) continue;
      if (n.includes(t) || t.includes(n)) {
        const score = t.length;
        if (!best || score > best.score) best = { g, score };
      }
    }
  }
  return best ? best.g : null;
}

/* ------------------------------------------------------------------
 * 2. Release-title parser. Both dmhy and mikan use the same shape:
 *      [组名] 中文名 / 日文名 [集数] [分辨率] [语言] [2026年7月番]
 * ---------------------------------------------------------------- */
const EP_IN_HEAD = /[-–—]\s*(\d{1,3})\s*(?:v\d)?\s*(?:END|Fin)?\s*$/i;
// e.g. "... / Daiseijo - 01 (CR 1920x1080 AVC AAC MKV)"
const EP_HEAD_PAREN = /[-–—]\s*(\d{1,3})\s*(?:v\d)?\s*[\(\（]/;
// e.g. "... / 転生したら剣でしたⅡ - 01 - [繁日內嵌][WebRip 1080P]"  (dash on BOTH sides)
// head is trimmed, so the closing dash may sit at end-of-string.
const EP_DASHED = /[-–—]\s*(\d{1,3})\s*(?:v\d)?\s*[-–—](?:\s|$)/;
const EP_CN = /第?\s*(\d{1,3})\s*[话話集]/;
// e.g. "... / Yasei no Last Boss ga Arawareta S02E02"
const EP_SXXEXX = /[Ss]\d{1,2}\s*[Ee](\d{1,3})(?!\d)/;
const EP_BRACKET = /^\s*(\d{1,3})(?!\d)(?:\s*[-~～]\s*(\d{1,3})(?!\d))?\s*(?:v\d|END|Fin|合集|合輯|完)?\s*$/i;
const SEASON = /(\d{4})\s*年\s*(\d{1,2})\s*月(?:新)?番/;

function parseRelease(title, hintGroup) {
  const raw = String(title || '').replace(/\s+/g, ' ').trim();
  let group = hintGroup || '';
  let rest = raw;

  // leading [group] / 【group】, allowing a 搬运 wrapper
  let lead = rest.match(/^(?:\[|【)\s*([^\]】]{1,44})\s*(?:\]|】)\s*/);
  while (lead) {
    const g = lead[1].trim();
    rest = rest.slice(lead[0].length);
    if (/^(搬运|轉載|转载|搬运组|合集)$/.test(g)) {
      lead = rest.match(/^(?:\[|【)\s*([^\]】]{1,44})\s*(?:\]|】)\s*/);
      continue;
    }
    if (!group) group = g;
    break;
  }

  const brackets = [...rest.matchAll(/[\[【]([^\]】]{0,80})[\]】]/g)].map(m => m[1].trim());
  let head = rest.split(/[\[【]/)[0].trim();

  // Some groups wrap the name in 【】 and then bracket EVERYTHING else, e.g.
  //   【悠哈璃羽字幕社】[元祖！邦多利酱/GANSO BanG Dream Chan][52][1080p HEVC][繁体内嵌]
  // so the text before the first bracket is empty and the real title is a bracket.
  if (!head) {
    for (const b of brackets) {
      if (!b) continue;
      if (EP_BRACKET.test(b)) continue;
      if (SEASON.test(b)) continue;
      if (/^\d{4}年\d{1,2}月(?:新)?番$/.test(b)) continue;
      if (/^(4K|2160p|1080p|720p|480p|360p|MKV|MP4|AVI|WEBM|HEVC|AVC|AAC|FLAC|CHS|CHT|JP|GB|BIG5)$/i.test(b)) continue;
      if (/(简日|繁日|简繁|双语|内嵌|内封|外挂|简体|繁體|中日|简中|繁中)/.test(b) && b.length <= 10) continue;
      head = b;
      break;
    }
  }

  let ep = '';
  const m1 = head.match(EP_IN_HEAD);
  if (m1) { ep = String(+m1[1]); head = head.slice(0, m1.index).trim(); }
  if (!ep) { const m1b = head.match(EP_HEAD_PAREN); if (m1b) ep = String(+m1b[1]); }
  if (!ep) { const mSE = head.match(EP_SXXEXX); if (mSE) ep = String(+mSE[1]); }
  if (!ep) { const m1c = head.match(EP_DASHED); if (m1c) ep = String(+m1c[1]); }
  if (!ep) { const m2 = head.match(EP_CN); if (m2) ep = String(+m2[1]); }
  if (!ep) {
    for (const b of brackets) {
      const m3 = b.match(EP_BRACKET);
      if (m3) { ep = m3[2] ? `${+m3[1]}-${+m3[2]}` : String(+m3[1]); break; }
    }
  }

  let season = '';
  const ms = raw.match(SEASON);
  if (ms) season = `${ms[1]}年${+ms[2]}月番`;

  const langTags = [];
  if (/简日|简繁日|简日双语/.test(raw) || (/简|CHS/i.test(raw) && /日|JP/i.test(raw))) langTags.push('简日');
  else if (/简|简体|CHS/i.test(raw)) langTags.push('简中');
  if (/繁|繁體|CHT/i.test(raw)) langTags.push('繁中');
  if (/双语|雙語|Bilingual/i.test(raw)) langTags.push('双语');
  if (/内嵌|內嵌/.test(raw)) langTags.push('内嵌');
  if (/内封|內封/.test(raw)) langTags.push('内封');
  if (/外挂|外掛/.test(raw)) langTags.push('外挂');
  if (/合集|合輯|全集/.test(raw)) langTags.push('合集');

  return {
    group,
    showRaw: head,
    season,
    ep,
    langTags: [...new Set(langTags)],
    isBatch: /合集|合輯|全集|Fin/i.test(raw) && !ep,
    title: raw
  };
}

/** Collapse a bilingual show string down to one stable key + display name. */
function pickTitle(showRaw) {
  const segs = String(showRaw || '')
    .split(/\s*[\/|｜]\s*/)
    .map(s => s.trim())
    .filter(Boolean);
  const cn = segs.find(s => /[\u4e00-\u9fa5]/.test(s));
  return (cn || segs[0] || showRaw || '').replace(/^[『「《]+|[』」》]+$/g, '').trim();
}

function showKey(showRaw) {
  const t = pickTitle(showRaw);
  return simp(t).toLowerCase()
    .replace(/第[一二三四五六七八九十\d]+季/g, '')
    .replace(/\b(s\d+|season\s*\d+|2nd|3rd|4th|second|third|final)\b/g, '')
    .replace(/[\s~～・:：!！?？,，.。'’"“”\-—_…]/g, '')
    .replace(/tv|动画|動畫/g, '')
    .trim();
}

/* ------------------------------------------------------------------
 * 3. Source adapters
 * ---------------------------------------------------------------- */
function parseDmhy(file) {
  const p = path.join(CACHE, file);
  if (!fs.existsSync(p)) return [];
  const html = fs.readFileSync(p, 'utf8');
  const rows = html.match(/<tr[^>]*>[\s\S]*?<\/tr>/g) || [];
  const out = [];
  for (const r of rows) {
    const td = r.match(/<td class="title">([\s\S]*?)<\/td>/);
    if (!td) continue;
    const seg = td[1];
    const tag = seg.match(/<span class="tag">[\s\S]*?<a[^>]*>([\s\S]*?)<\/a>/);
    const body = seg.match(/<a[^>]*href="(\/topics\/view\/[^"]+)"[^>]*>([\s\S]*?)<\/a>/);
    if (!body) continue;
    const dec = s => s.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/\s+/g, ' ').trim();
    const hidden = r.match(/<span style="display: none;">\s*(\d{4})\/(\d{1,2})\/(\d{1,2})\s+(\d{1,2}):(\d{2})/);
    let at = null;
    if (hidden) {
      // normalised to China-local ISO without offset, so string compare works
      at = `${hidden[1]}-${String(hidden[2]).padStart(2, '0')}-${String(hidden[3]).padStart(2, '0')}T` +
           `${String(hidden[4]).padStart(2, '0')}:${hidden[5]}:00`;
    }
    out.push({
      src: 'dmhy',
      rawGroup: tag ? dec(tag[1]) : '',
      title: dec(body[2]),
      url: 'https://share.dmhy.org' + body[1],
      at
    });
  }
  return out;
}

function parseMikan(file) {
  const p = path.join(CACHE, file);
  if (!fs.existsSync(p)) return [];
  const xml = fs.readFileSync(p, 'utf8');
  const items = xml.split('<item>').slice(1);
  const grab = (s, tag) => {
    const m = s.match(new RegExp('<' + tag + '>([\\s\\S]*?)</' + tag + '>'));
    return m ? m[1].replace(/<!\[CDATA\[|\]\]>/g, '').trim() : '';
  };
  const out = [];
  for (const it of items) {
    const title = grab(it, 'title');
    if (!title) continue;
    let at = grab(it, 'pubDate');
    const m = at.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
    at = m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}` : null;
    const link = grab(it, 'link');
    out.push({ src: 'mikan', rawGroup: '', title, url: link, at });
  }
  return out;
}

/** Format a Date as China-local ISO (no offset), matching the other sources. */
function toCN(dateObj) {
  const d = new Date(dateObj.getTime() + 8 * 3600 * 1000);
  const p = n => String(n).padStart(2, '0');
  return d.getUTCFullYear() + '-' + p(d.getUTCMonth() + 1) + '-' + p(d.getUTCDate()) + 'T' +
         p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
}

/* ------------------------------------------------------------------
 * AniBT adapters. This is the highest-quality source: the Open API
 * hands us group / anime / episode / bgmId as real fields instead of
 * forcing us to regex them out of a filename.
 *   /rss/magnets.xml        -> site-wide release stream (structured)
 *   /api/seasons/anime      -> authoritative current-season roster
 *   /api/subtitle-groups    -> registered fansub-group directory
 * ---------------------------------------------------------------- */
function xmlGrab(s, tag) {
  const m = s.match(new RegExp('<' + tag + '(?:\\s[^>]*)?>([\\s\\S]*?)</' + tag + '>'));
  return m ? m[1].replace(/<!\[CDATA\[|\]\]>/g, '').trim() : '';
}

function parseAniBTMagnets(file) {
  const p = path.join(CACHE, file);
  if (!fs.existsSync(p)) return [];
  const xml = fs.readFileSync(p, 'utf8');
  const out = [];
  for (const it of xml.split('<item>').slice(1)) {
    const relTitle = xmlGrab(it, 'anibt:releaseTitle') || xmlGrab(it, 'title');
    if (!relTitle) continue;
    const animeTitle = xmlGrab(it, 'anibt:animeTitle');
    let at = null;
    const raw = xmlGrab(it, 'pubDate');
    const d = raw ? new Date(raw) : null;
    if (d && !isNaN(d.getTime())) at = toCN(d);
    // the bracket in the release title is the ORIGINAL fansub group; groupName
    // is merely the AniBT account that uploaded it -- they can differ.
    const lead = relTitle.match(/^(?:\[|【)\s*([^\]】]{1,44})\s*(?:\]|】)/);
    out.push({
      src: 'anibt',
      rawGroup: lead ? lead[1].trim() : '',
      account: xmlGrab(it, 'anibt:groupName'),
      slug: xmlGrab(it, 'anibt:groupSlug'),
      title: relTitle,
      animeTitle,
      bgmId: xmlGrab(it, 'anibt:bgmId'),
      episode: xmlGrab(it, 'anibt:episodeKey') || xmlGrab(it, 'anibt:episode'),
      resolution: xmlGrab(it, 'anibt:resolution'),
      subtitle: xmlGrab(it, 'anibt:subtitle'),
      format: xmlGrab(it, 'anibt:format'),
      url: xmlGrab(it, 'link'),
      at
    });
  }
  return out;
}

function parseAniBTRoster(file) {
  const p = path.join(CACHE, file);
  if (!fs.existsSync(p)) return null;
  try {
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const d = j.data || j;
    const roster = [];
    for (const w of (d.byWeekday || [])) {
      for (const a of (w.animes || [])) {
        roster.push({
          bgmId: String(a.bgmId || ''),
          titles: a.title || {},
          title: (a.title && (a.title.chinese || a.title.chineseTraditional || a.title.primary)) || '',
          season: a.seasonLabel || d.currentSeason || '',
          premiereDate: a.premiereDate || '',
          weekday: a.weekdayLabel || w.weekdayLabel || '',
          rating: typeof a.rating === 'number' ? a.rating : null,
          episodes: a.episodes || null,
          hasRelease: !!a.hasRelease,
          releaseCount: a.rssReleaseCount || 0,
          cover: a.cover || ''
        });
      }
    }
    return {
      currentSeason: d.currentSeason || '',
      availableSeasons: d.availableSeasons || [],
      total: d.total || roster.length,
      roster
    };
  } catch (e) {
    return null;
  }
}

function parseAniBTDirectory(file) {
  const p = path.join(CACHE, file);
  if (!fs.existsSync(p)) return [];
  try {
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const d = j.data || j;
    const list = Array.isArray(d) ? d : (d.items || d.groups || []);
    return list.map(g => ({
      slug: g.slug || '',
      name: g.name || '',
      status: g.status || '',
      releases: g.totalReleases || 0,
      animes: g.totalAnimes || 0
    })).filter(g => g.name);
  } catch (e) {
    return [];
  }
}

/* ------------------------------------------------------------------
 * 4. Aggregate
 * ---------------------------------------------------------------- */
function round(n, d) { return Number(Number(n).toFixed(d == null ? 1 : d)); }

function main() {
  const registry = loadRegistry();
  const files = fs.existsSync(CACHE) ? fs.readdirSync(CACHE) : [];

  const find = re => files.find(f => re.test(f)) || '';
  const season = parseAniBTRoster(find(/^anibt[_-]?seasons?.*\.json$/i));
  const directory = parseAniBTDirectory(find(/^anibt[_-]?(groups|directory).*\.json$/i));

  const raw = [
    ...files.filter(f => /^dmhy(_p\d+)?\.html$/.test(f)).flatMap(parseDmhy),
    ...files.filter(f => /^mikan.*\.xml$/.test(f)).flatMap(parseMikan),
    ...files.filter(f => /^anibt[_-]?magnets.*\.xml$/i.test(f)).flatMap(parseAniBTMagnets)
  ];

  /* bgmId lookup, built from every title spelling AniBT knows about.
     This is what lets a dmhy filename line up with the official season roster. */
  const bgmIndex = new Map();
  if (season) {
    for (const a of season.roster) {
      const t = a.titles || {};
      [t.chinese, t.chineseTraditional, t.primary, t.japanese, t.romaji, t.english]
        .filter(Boolean)
        .forEach(v => {
          const k = showKey(v);
          if (k && k.length >= 2 && !bgmIndex.has(k)) bgmIndex.set(k, a.bgmId);
        });
    }
  }
  const rosterByBgm = new Map(season ? season.roster.map(a => [a.bgmId, a]) : []);

  /* dedupe across sources: the same release appears on dmhy, mikan and AniBT
     with the same filename, so key on the normalised filename + hour. */
  const flatKey = s => simp(s).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]/g, '');
  const seen = new Set();
  const releases = [];
  let skippedNoTitle = 0;
  for (const r of raw) {
    const dk = flatKey(r.title) + '|' + (r.at ? r.at.slice(0, 13) : '');
    if (seen.has(dk)) continue;
    seen.add(dk);

    const p = parseRelease(r.title, r.rawGroup);
    const display = r.animeTitle || pickTitle(p.showRaw);
    let sk = showKey(display);
    let bgmId = r.bgmId || '';
    if (!bgmId && sk) bgmId = bgmIndex.get(sk) || '';
    if (!bgmId && p.showRaw) {
      const k2 = showKey(p.showRaw);
      if (k2) bgmId = bgmIndex.get(k2) || '';
    }
    if (!sk) sk = showKey(p.showRaw);
    if (!display || simp(display).length < 2) { skippedNoTitle++; continue; }

    const g = matchGroup(p.group, registry);
    releases.push({
      ...p,
      at: r.at,
      url: r.url,
      src: r.src,
      rawGroup: p.group,
      account: r.account || '',
      slug: r.slug || '',
      canonical: g ? g.name : null,
      tier: g ? g.tier : null,
      key: bgmId ? 'bgm:' + bgmId : 't:' + sk,
      display,
      bgmId,
      apiEp: r.episode || '',
      apiSub: r.subtitle || ''
    });
  }

  // ---- per-group ----
  // Known groups key on their canonical name; live-discovered groups key on a
  // noise-stripped form so that 繁體/簡體 and suffix variants collapse together.
  const gmap = new Map();
  const forms = new Map();
  let relayReleases = 0;
  const relayNames = new Set();

  function keyForRelease(r) {
    if (r.canonical) return 'K:' + r.canonical;
    if (isRelay(r.rawGroup)) { relayReleases++; relayNames.add(r.rawGroup); return null; }
    const k = groupKey(r.rawGroup);
    if (!k || k.length < 2) return null;
    return 'U:' + k;
  }

  for (const r of releases) {
    const key = keyForRelease(r);
    r.gkey = key;
    if (!key) continue;
    if (!gmap.has(key)) {
      gmap.set(key, {
        key, name: r.canonical || r.rawGroup, known: !!r.canonical, tier: r.tier,
        releases: 0, shows: new Set(), langs: new Set(), lastAt: null,
        slug: '', srcs: new Set()
      });
      forms.set(key, new Map());
    }
    const e = gmap.get(key);
    // slug belongs to the AniBT *account*; only adopt it when the account is
    // also the original group, otherwise 黒ネズミたち would inherit the
    // slug of the Kirara Fantasia account that republishes it.
    if (r.slug && !e.slug && (!r.account || norm(r.account) === norm(r.rawGroup))) e.slug = r.slug;
    e.srcs.add(r.src);
    const fm = forms.get(key);
    const form = r.rawGroup || r.canonical;
    fm.set(form, (fm.get(form) || 0) + 1);

    e.releases++;
    e.shows.add(r.key || r.display);
    r.langTags.forEach(l => e.langs.add(l));
    if (r.at && (!e.lastAt || r.at > e.lastAt)) e.lastAt = r.at;
  }

  // live-discovered groups: display the most frequently used raw form
  for (const [key, e] of gmap) {
    if (e.known) continue;
    const fm = forms.get(key);
    const best = [...fm.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh'))[0];
    if (best && best[0]) e.name = best[0];
  }

  const nameByKey = new Map([...gmap].map(([k, e]) => [k, e.name]));

  const groups = [...gmap.values()].map(e => ({
    key: e.key,
    name: e.name,
    known: e.known,
    tier: e.tier,
    slug: e.slug || "",
    onAniBT: !!e.slug,
    srcs: [...e.srcs],
    releases: e.releases,
    showCount: e.shows.size,
    langs: [...e.langs].slice(0, 5),
    lastAt: e.lastAt
  })).sort((a, b) => b.releases - a.releases || b.showCount - a.showCount);

  /* ---- AniBT registered-group directory (33 orgs as of writing) ---- */
  const directoryOut = directory.map(d => {
    const m = matchGroup(d.name, registry);
    const live = gmap.get(m ? 'K:' + m.name : 'U:' + groupKey(d.name));
    return {
      slug: d.slug,
      name: d.name,
      status: d.status,
      totalReleases: d.releases,
      totalAnimes: d.animes,
      tier: m ? m.tier : null,
      known: !!m,
      windowReleases: live ? live.releases : 0,
      windowShows: live ? live.shows.size : 0
    };
  }).sort((a, b) => b.windowReleases - a.windowReleases || b.totalReleases - a.totalReleases);

  /* AniBT accounts can republish on behalf of another group (e.g. the
     "Kirara Fantasia" account carrying 黒ネズミたち releases), so count
     account-level output separately from the filename-derived group. */
  const accountCounts = new Map();
  for (const r of releases) {
    if (!r.account) continue;
    accountCounts.set(r.account, (accountCounts.get(r.account) || 0) + 1);
  }

  /* ---- unified group board: live activity joined with AniBT registration ---- */
  const boardMap = new Map();
  for (const g of groups) {
    boardMap.set(g.key, {
      name: g.name, known: g.known, tier: g.tier,
      slug: g.slug || '', registered: !!g.slug, accountName: '',
      releases: g.releases, showCount: g.showCount,
      accountReleases: 0, totalReleases: 0,
      langs: g.langs, lastAt: g.lastAt
    });
  }
  for (const d of directory) {
    const m = matchGroup(d.name, registry);
    const k = m ? 'K:' + m.name : 'U:' + groupKey(d.name);
    let e = boardMap.get(k);
    if (!e) {
      e = {
        name: d.name, known: !!m, tier: m ? m.tier : null,
        slug: '', registered: true, accountName: d.name,
        releases: 0, showCount: 0, accountReleases: 0, totalReleases: 0,
        langs: [], lastAt: null
      };
      boardMap.set(k, e);
    }
    e.registered = true;
    if (!e.slug) e.slug = d.slug;
    e.totalReleases = d.releases || 0;
    e.accountName = d.name;
    e.accountReleases = accountCounts.get(d.name) || 0;
    if (e.known && !e.tier) e.tier = m.tier;
  }
  const board = [...boardMap.values()]
    .filter(g => g.releases > 0 || g.registered)
    .sort((a, b) => b.releases - a.releases ||
                    b.accountReleases - a.accountReleases ||
                    b.totalReleases - a.totalReleases);

  // ---- per-show ----
  const smap = new Map();
  for (const r of releases) {
    if (!r.key || r.key.length < 2) continue;
    if (!r.gkey) continue;   // relay / unidentifiable publisher
    if (!smap.has(r.key)) smap.set(r.key, {
      key: r.key, title: r.display, season: r.season || '', bgmId: r.bgmId || '',
      groups: new Map(), forms: new Map(), epSet: new Set(), totalReleases: 0, lastAt: null, maxEp: 0
    });
    const s = smap.get(r.key);
    if (r.season && !s.season) s.season = r.season;
    if (r.bgmId && !s.bgmId) s.bgmId = r.bgmId;
    s.totalReleases++;
    if (r.at && (!s.lastAt || r.at > s.lastAt)) s.lastAt = r.at;
    // Groups number episodes differently (per-season vs cumulative across the
    // whole series), so "max episode seen" is unreliable. Counting DISTINCT
    // episode keys is unambiguous.
    const epRaw = String(r.apiEp || r.ep || '').trim();
    const epKey = epRaw.replace(/^0+(?=\d)/, '');
    if (epKey) s.epSet.add(epKey);
    const epNum = parseInt(epRaw.split('-').pop(), 10);
    if (!isNaN(epNum) && epNum > s.maxEp) s.maxEp = epNum;
    // the display title is decided after the loop: most-used form wins
    s.forms.set(r.display, (s.forms.get(r.display) || 0) + 1);

    const gname = (r.gkey && nameByKey.get(r.gkey)) || '（未识别）';
    if (!s.groups.has(gname)) s.groups.set(gname, { name: gname, known: !!r.canonical, tier: r.tier, count: 0, lastAt: null, maxEp: 0, epSet: new Set(), langs: new Set() });
    const ge = s.groups.get(gname);
    ge.count++;
    if (epKey) ge.epSet.add(epKey);
    if (!isNaN(epNum) && epNum > ge.maxEp) ge.maxEp = epNum;
    if (r.at && (!ge.lastAt || r.at > ge.lastAt)) ge.lastAt = r.at;
    r.langTags.forEach(l => ge.langs.add(l));
  }

  const shows = [...smap.values()]
    .filter(s => s.groups.size > 0)
    .map(s => {
      const ro = s.bgmId ? rosterByBgm.get(s.bgmId) : null;
      const formTitle = [...s.forms.entries()]
        .sort((a, b) => b[1] - a[1] || b[0].length - a[0].length)[0][0];
      const title = (ro && ro.title) ? ro.title : formTitle;
      return {
        key: s.key,
        title,
        cover: ro ? (ro.cover || '') : '',
        altTitle: (ro && ro.title && ro.title !== formTitle) ? formTitle : '',
        season: s.season || (ro ? ro.season : ""),
        bgmId: s.bgmId || "",
        rating: ro ? ro.rating : null,
        premiereDate: ro ? ro.premiereDate : "",
        weekday: ro ? ro.weekday : "",
        plannedEpisodes: ro ? ro.episodes : null,
        inSeasonRoster: !!ro,
        lastAt: s.lastAt,
        maxEp: s.maxEp,
        episodes: s.epSet.size,
        groupCount: s.groups.size,
        totalReleases: s.totalReleases,
        groups: [...s.groups.values()]
          .map(g => ({ name: g.name, known: g.known, tier: g.tier, count: g.count, episodes: g.epSet.size, maxEp: g.maxEp, lastAt: g.lastAt, langs: [...g.langs].slice(0, 3) }))
          .sort((a, b) => b.maxEp - a.maxEp || b.count - a.count || a.name.localeCompare(b.name, 'zh'))
      };
    })
    .sort((a, b) => b.groupCount - a.groupCount || b.totalReleases - a.totalReleases);

  /* ---- current-season roster, joined against what is actually released ---- */
  let seasonOut = null;
  if (season) {
    const order = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    const byWeekday = order.map(lbl => {
      const animes = season.roster
        .filter(a => a.weekday === lbl)
        .map(a => {
          const live = smap.get('bgm:' + a.bgmId);
          const gset = live ? [...live.groups.values()] : [];
          return {
            bgmId: a.bgmId,
            title: a.title,
            cover: a.cover || '',
            premiereDate: a.premiereDate,
            rating: a.rating,
            episodes: a.episodes,
            hasRelease: a.hasRelease || gset.length > 0,
            groupCount: gset.length,
            groups: gset
              .sort((x, y) => (y.maxEp || 0) - (x.maxEp || 0) || y.count - x.count)
              .slice(0, 8)
              .map(g => ({ name: g.name, tier: g.tier || null, known: !!g.known, episodes: g.epSet.size, maxEp: g.maxEp || 0, count: g.count }))
          };
        })
        .sort((x, y) => (y.groupCount - x.groupCount) || String(x.premiereDate).localeCompare(String(y.premiereDate)));
      return { weekdayLabel: lbl, animes };
    }).filter(w => w.animes.length);

    seasonOut = {
      currentSeason: season.currentSeason,
      total: season.total,
      availableSeasons: (season.availableSeasons || []).slice(0, 14),
      withRelease: season.roster.filter(a => a.hasRelease || smap.has('bgm:' + a.bgmId)).length,
      byWeekday
    };
  }

  // ---- window ----
  const dates = releases.map(r => r.at).filter(Boolean).sort();
  const from = dates[0] || null;
  const to = dates[dates.length - 1] || null;

  const unmatched = groups.filter(g => !g.known).map(g => ({ name: g.name, releases: g.releases, showCount: g.showCount, lastAt: g.lastAt }));

  const data = {
    generatedAt: new Date().toISOString(),
    window: { from, to },
    stats: {
      releases: releases.length,
      shows: shows.length,
      groups: groups.length,
      knownGroups: groups.filter(g => g.known).length,
      unknownGroups: unmatched.length,
      relayReleases,
      relayNames: [...relayNames],
      skippedNoTitle,
      bySource: {
        anibt: raw.filter(r => r.src === 'anibt').length,
        dmhy: raw.filter(r => r.src === 'dmhy').length,
        mikan: raw.filter(r => r.src === 'mikan').length
      },
      seasonRoster: season ? season.total : 0,
      seasonWithRelease: seasonOut ? seasonOut.withRelease : 0,
      directoryGroups: directoryOut.length
    },
    sources: [
      { name: 'AniBT · 全站磁力 RSS', url: 'https://anibt.net/rss/magnets.xml', note: '结构化字段：字幕组 / 番剧 / 集数 / bgmId' },
      { name: 'AniBT · 本季番剧接口', url: 'https://anibt.net/api/seasons/anime', note: '官方季度番剧表（权威口径）' },
      { name: '动漫花园 · 动画/新番', url: 'https://share.dmhy.org/topics/list/sort_id/2', note: '更长历史窗口' },
      { name: '蜜柑计划 · 列表模式 RSS', url: 'https://mikanime.tv/RSS/Classic', note: '交叉校验' }
    ],
    season: seasonOut,
    directory: directoryOut,
    board,
    groups,
    shows,
    unmatched
  };

  const body = '/* auto-generated by scripts/build-season.js -- do not edit by hand */\n' +
    'window.SEASON_DATA = ' + JSON.stringify(data, null, 1) + ';\n';
  fs.writeFileSync(OUT, body, 'utf8');

  // Plain ASCII logging only: this output is piped back through a
  // PowerShell console that may decode it as GBK, so keep it simple.
  const L = [];
  L.push('[build-season] releases=' + data.stats.releases +
    '  shows=' + data.stats.shows +
    '  groups=' + data.stats.groups +
    '  known=' + data.stats.knownGroups +
    '  unknown=' + data.stats.unknownGroups +
    '  relay=' + data.stats.relayReleases);
  L.push('[build-season] bySource anibt=' + data.stats.bySource.anibt +
    ' dmhy=' + data.stats.bySource.dmhy +
    ' mikan=' + data.stats.bySource.mikan);
  L.push('[build-season] season=' + (seasonOut ? seasonOut.currentSeason : 'n/a') +
    ' roster=' + data.stats.seasonRoster +
    ' withRelease=' + data.stats.seasonWithRelease +
    ' directory=' + data.stats.directoryGroups);
  L.push('[build-season] window ' + from + ' -> ' + to);
  L.push('[build-season] wrote season-data.js (' + Math.round(body.length / 1024) + ' KB)');
  L.push('');
  L.push('TOP GROUPS (known first)');
  groups.slice(0, 16).forEach(g => {
    L.push('  ' + (g.known ? '  ' : '? ') + pad(g.name, 22) + pad(g.releases, 6, true) +
      pad(g.showCount, 5, true) + '  ' + (g.tier || '-'));
  });
  L.push('');
  L.push('TOP SHOWS');
  shows.slice(0, 12).forEach(s => {
    L.push('  ' + pad(s.title, 30) + pad(s.groupCount, 4, true) + ' groups  已出 ' + (s.episodes || '-') + ' 集');
  });
  console.log(L.join('\n'));
}
function pad(v, n, right) {
  const s = String(v == null ? '' : v);
  const len = [...s].reduce((a, c) => a + (/[\u4e00-\u9fa5\u3040-\u30ff\uff00-\uffef]/.test(c) ? 2 : 1), 0);
  const space = ' '.repeat(Math.max(0, n - len));
  return right ? space + s : s + space;
}

main();
