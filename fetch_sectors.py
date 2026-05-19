import os
import json
import time
import re
import html
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

CF_ACCOUNT_ID   = os.environ['CF_ACCOUNT_ID']
CF_KV_NAMESPACE = os.environ['CF_KV_NAMESPACE_ID']
CF_KV_TOKEN     = os.environ['CF_KV_TOKEN']
KV_API          = f'https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE}/values'

TW_TZ = timezone(timedelta(hours=8))

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}

HOT_KEYWORD = '熱門族群'
KEEP_DAYS = 15
MAX_ARTICLES = 30

# 常用台股名稱對照表：新聞內文沒有「名稱(代號)」時，用標題/內文關鍵字補抓。
# 可持續增加你常看到的股票。
STOCK_MASTER = {
    '台積電':'2330','鴻海':'2317','聯發科':'2454','廣達':'2382','緯創':'3231','技嘉':'2376','華碩':'2357','英業達':'2356','仁寶':'2324','和碩':'4938',
    '大立光':'3008','玉晶光':'3406','亞光':'3019','佳凌':'4976','先進光':'3362','今國光':'6209','聯亞':'3081','采鈺':'6789',
    '事欣科':'4916','信錦':'1582','乙盛-KY':'5243','台揚':'2314','敬鵬':'2355','華通':'2313','燿華':'2367','台郡':'6269','欣興':'3037','景碩':'3189','南電':'8046','臻鼎-KY':'4958',
    '矽力-KY':'6415','強茂':'2481','台半':'5425','朋程':'8255','茂矽':'2342','漢磊':'3707','嘉晶':'3016','德微':'3675','尼克森':'3317','杰力':'5299',
    '群聯':'8299','南亞科':'2408','華邦電':'2344','旺宏':'2337','威剛':'3260','十銓':'4967','創見':'2451','宇瞻':'8271','晶豪科':'3006','鈺創':'5351','品安':'8088','宜鼎':'5289','力積電':'6770',
    '友達':'2409','群創':'3481','彩晶':'6116','達運':'6120','明基材':'8215','誠美材':'4960','力特':'3051',
    '台光電':'2383','金像電':'2368','聯茂':'6213','台燿':'6274','尖點':'8021','志聖':'2467','均豪':'5443','牧德':'3563',
    '昇達科':'3491','啟碁':'6285','耀登':'3138','華星光':'4979','聯鈞':'3450','光聖':'6442','波若威':'3163','上詮':'3363','智邦':'2345','台達電':'2308',
    '世芯-KY':'3661','創意':'3443','M31':'6643','力旺':'3529','智原':'3035','聯詠':'3034','瑞昱':'2379','矽統':'2363',
}

# 標題常出現「三雄、四雄、雙雄」但內文抓不到時，用族群關鍵字補常見指標股。
SECTOR_HINTS = [
    ('記憶體', ['南亞科','華邦電','旺宏','群聯','威剛','十銓','創見','宇瞻','晶豪科','鈺創']),
    ('低軌衛星', ['事欣科','信錦','乙盛-KY','台揚','敬鵬','昇達科','啟碁','耀登']),
    ('衛星', ['事欣科','信錦','乙盛-KY','台揚','敬鵬','昇達科','啟碁','耀登']),
    ('AI眼鏡', ['大立光','玉晶光','亞光','佳凌','先進光','今國光']),
    ('眼鏡', ['大立光','玉晶光','亞光','佳凌','先進光','今國光']),
    ('功率半導體', ['強茂','台半','朋程','漢磊','嘉晶','德微','尼克森','杰力']),
    ('載板', ['欣興','景碩','南電','臻鼎-KY','華通','燿華','台郡']),
    ('偏光片', ['明基材','誠美材','力特','友達','群創']),
    ('PCB', ['台光電','金像電','聯茂','台燿','華通','燿華','欣興','景碩']),
]


def make_stock(name):
    code = STOCK_MASTER.get(name)
    return {'name': name, 'code': code} if code else None


def merge_stock_lists(*lists, limit=8):
    seen, out = set(), []
    for lst in lists:
        for x in lst or []:
            if isinstance(x, dict):
                code = str(x.get('code','')).strip()
                name = str(x.get('name','')).strip()
            else:
                code = str(x).strip()
                name = ''
            if not re.match(r'^\d{4}$', code) or code in seen:
                continue
            seen.add(code)
            out.append({'name': name, 'code': code})
            if len(out) >= limit:
                return out
    return out


def extract_stocks_from_text(text):
    """從文字中抓 1) 名稱(代號) 2) 股票名稱關鍵字。"""
    text = html.unescape(text or '')
    found = []

    pairs = re.findall(r'([\u4e00-\u9fffA-Za-z0-9\-＊*]{1,12})\s*[（(](\d{4})[）)]', text)
    for raw_name, code in pairs:
        name = clean_stock_name(raw_name)
        if name and re.match(r'^\d{4}$', code):
            found.append({'name': name, 'code': code})

    # 直接掃股票名稱，補沒有代號格式的新聞。
    for name, code in STOCK_MASTER.items():
        if name in text:
            found.append({'name': name, 'code': code})

    return merge_stock_lists(found, limit=8)


def guess_stocks_by_title(title):
    """標題出現族群但內文/Google 轉址抓不到時，補常見指標股。"""
    title = title or ''
    found = extract_stocks_from_text(title)
    for kw, names in SECTOR_HINTS:
        if kw in title:
            found.extend([make_stock(name) for name in names if make_stock(name)])
    return merge_stock_lists(found, limit=8)


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ts_ms):
    d = datetime.fromtimestamp(ts_ms / 1000, tz=TW_TZ)
    hour = d.hour
    ampm = '上午' if hour < 12 else '下午'
    h12 = hour if hour <= 12 else hour - 12
    if h12 == 0:
        h12 = 12
    return f"{d.year}/{d.month}/{d.day} {ampm}{h12:02d}:{d.minute:02d}:{d.second:02d}"


def kv_get(key):
    try:
        r = requests.get(f'{KV_API}/{key}', headers={'Authorization': f'Bearer {CF_KV_TOKEN}'}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'KV GET error {key}: {e}')
    return None


def kv_put(key, value, ttl_seconds=None):
    params = {}
    if ttl_seconds:
        params['expiration_ttl'] = ttl_seconds
    try:
        r = requests.put(
            f'{KV_API}/{key}',
            headers={'Authorization': f'Bearer {CF_KV_TOKEN}', 'Content-Type': 'application/json'},
            params=params,
            data=json.dumps(value, ensure_ascii=False)
        )
        if r.status_code != 200:
            print(f'KV PUT error {key}: {r.status_code} {r.text[:300]}')
        return r.status_code == 200
    except Exception as e:
        print(f'KV PUT exception {key}: {e}')
        return False


def clean_title(title):
    title = html.unescape(title or '')
    title = re.sub(r'<[^>]+>', '', title)
    title = title.rsplit(' - ', 1)[0].strip()
    title = re.sub(r'\s+', ' ', title)
    return title


def normalize_url(url):
    url = html.unescape(url or '').strip()
    if url.startswith('//'):
        url = 'https:' + url
    if url.startswith('/'):
        url = 'https://money-link.com.tw' + url
    url = url.replace('http://money-link.com.tw', 'https://money-link.com.tw')
    url = url.replace('https://news.google.com/rss/articles/', 'https://news.google.com/articles/')
    return url


def parse_moneylink_time(text):
    text = html.unescape(text or '')
    patterns = [
        r'(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?',
        r'(\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?',
    ]
    for i, pat in enumerate(patterns):
        m = re.search(pat, text)
        if not m:
            continue
        try:
            if i == 0:
                y, mo, d, hh, mm, ss = m.groups()
            else:
                y = datetime.now(TW_TZ).year
                mo, d, hh, mm, ss = m.groups()
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0), tzinfo=TW_TZ)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return None


def clean_stock_name(name):
    """整理股票名稱，避免把太長的前文一起抓進來。"""
    name = html.unescape(name or '')
    name = re.sub(r'<[^>]+>', '', name)
    name = re.sub(r'\s+', '', name)
    name = name.strip('，。、；:：()（）「」『』【】[]')
    # 常見會黏在股票名稱前面的字，切掉。
    for sep in ['包括', '看好', '如', '有', '與', '及', '、', '，', '。', '；', '：', ':', ' '] :
        if sep in name:
            name = name.split(sep)[-1]
    # 股票名稱通常不會太長，保留最後 8 個字元避免抓到整句。
    if len(name) > 8:
        name = name[-8:]
    return name


def fetch_article_stocks(url):
    """從新聞內文抓出 股票名稱(代號)，回傳 [{name, code}]。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return []
        text = html.unescape(r.text)
        text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
        text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)

        return extract_stocks_from_text(text)[:8]
    except Exception as e:
        print(f'    → stocks error: {e}')
        return []


def fetch_moneylink_direct():
    """直接從富聯網頁面撈熱門族群連結，通常比 Google News RSS 早。"""
    candidates = []
    urls = [
        'https://money-link.com.tw/RealtimeNews/Index.aspx',
        'https://ww2.money-link.com.tw/RealtimeNews/Index.aspx',
        'https://money-link.com.tw/RealtimeNews/',
        'https://ww2.money-link.com.tw/RealtimeNews/',
    ]

    link_pat = re.compile(
        r'<a[^>]+href=["\']([^"\']*NewsContent\.aspx[^"\']*)["\'][^>]*>(.*?)</a>',
        re.I | re.S
    )

    for page_url in urls:
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=10)
            print(f'DIRECT {page_url} → HTTP {r.status_code}, {len(r.text)} bytes')
            if r.status_code != 200:
                continue
            page = r.text
            for href, title_html in link_pat.findall(page):
                title = clean_title(title_html)
                if HOT_KEYWORD not in title:
                    continue
                url = normalize_url(href)
                if 'money-link.com.tw' not in url:
                    continue
                # 取連結附近文字，盡量抓頁面上列出的時間；抓不到就用現在時間
                pos = page.find(href)
                near = page[max(0, pos - 500): pos + 500] if pos >= 0 else title_html
                ts = parse_moneylink_time(near) or now_ms()
                candidates.append({
                    'title': title,
                    'time': fmt_time(ts),
                    'url': url,
                    'src': '富聯網',
                    'ts': ts,
                    'stocks': [],
                })
                print(f'  ✅ DIRECT {title[:50]}')
        except Exception as e:
            print(f'DIRECT error {page_url}: {e}')

    return candidates


def fetch_google_rss_backup():
    """備援：Google RSS 會慢，但可以補漏。"""
    items = []
    rss_urls = [
        'https://news.google.com/rss/search?q=%22%E7%86%B1%E9%96%80%E6%97%8F%E7%BE%A4%22+site:money-link.com.tw+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant',
        'https://news.google.com/rss/search?q=%22%E7%86%B1%E9%96%80%E6%97%8F%E7%BE%A4%22+%E5%AF%8C%E8%81%AF%E7%B6%B2+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant',
    ]
    for rss_url in rss_urls:
        try:
            feed = feedparser.parse(rss_url)
            print(f'RSS → {len(feed.entries)} 筆')
            for entry in feed.entries:
                raw_title = entry.get('title', '')
                title = clean_title(raw_title)
                if HOT_KEYWORD not in title:
                    continue
                src = entry.source.get('title', '') if hasattr(entry, 'source') else ''
                link = normalize_url(entry.get('link', '#'))
                if 'money-link' not in link and '富聯' not in src and '旺得富' not in src:
                    continue
                try:
                    pub_dt = parsedate_to_datetime(entry.get('published', '')).astimezone(TW_TZ)
                    pub_ts = int(pub_dt.timestamp() * 1000)
                except Exception:
                    pub_ts = now_ms()
                items.append({
                    'title': title,
                    'time': fmt_time(pub_ts),
                    'url': link,
                    'src': '富聯網',
                    'ts': pub_ts,
                    'stocks': [],
                })
                print(f'  ✅ RSS {title[:50]}')
        except Exception as e:
            print(f'RSS error: {e}')
    return items




def normalize_title_key(title):
    """同一則熱門族群常會從 direct/RSS/ww2 進來，網址不同但標題相同；用標題去重。"""
    t = clean_title(title)
    t = t.replace('《熱門族群》', '')
    t = re.sub(r'\s+', '', t)
    # 去掉常見標點差異，避免同一標題因符號不同被當成兩篇。
    t = re.sub(r'[，,。．\.！!？?：:；;、\-—_\s]+', '', t)
    return t

def dedupe(items):
    """去重：同標題只留一筆；股票清單多的優先保留。"""
    item_map = {}
    for n in items:
        title = clean_title(n.get('title', ''))
        url = normalize_url(n.get('url', ''))
        if HOT_KEYWORD not in title:
            continue

        # 先用標題當主 key，因為 Google RSS 與 money-link/ww2 的網址常不同。
        title_key = normalize_title_key(title)
        sn = re.search(r'[?&]sn=([0-9]+)', url, re.I)
        url_key = sn.group(1) if sn else (url.split('#')[0].split('?')[0] or title_key)
        key = title_key or url_key

        n['title'] = title
        n['url'] = url
        n['stocks'] = merge_stock_lists(n.get('stocks', []), limit=8)

        old = item_map.get(key)
        if not old:
            item_map[key] = n
            continue

        old_stocks = old.get('stocks') or []
        new_stocks = n.get('stocks') or []
        merged_stocks = merge_stock_lists(old_stocks, new_stocks, limit=8)

        # 選主資料：優先選股票較多者；股票數相同時選較新者。
        if len(new_stocks) > len(old_stocks) or (
            len(new_stocks) == len(old_stocks) and n.get('ts', 0) > old.get('ts', 0)
        ):
            keep = {**old, **n}
        else:
            keep = {**n, **old}

        keep['stocks'] = merged_stocks
        # 時間保留較新的，避免舊資料覆蓋新時間。
        if n.get('ts', 0) > old.get('ts', 0):
            keep['ts'] = n.get('ts', 0)
            keep['time'] = n.get('time', old.get('time', ''))
        item_map[key] = keep

    return list(item_map.values())


def add_stocks_parallel(items):
    todo = [n for n in items[:MAX_ARTICLES] if n.get('url') and not n.get('stocks')]
    if not todo:
        return items
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(fetch_article_stocks, n['url']): n for n in todo}
        for fut in as_completed(future_map):
            n = future_map[fut]
            try:
                n['stocks'] = fut.result()
                if not n.get('stocks'):
                    n['stocks'] = guess_stocks_by_title(n.get('title', ''))
                print(f"    → {n['title'][:28]} 個股: {n['stocks']}")
            except Exception as e:
                print(f'parallel stock error: {e}')
    return items


def fetch_sectors():
    direct = fetch_moneylink_direct()
    rss = fetch_google_rss_backup()
    items = dedupe([*direct, *rss])
    items.sort(key=lambda x: x.get('ts', 0), reverse=True)
    items = add_stocks_parallel(items)
    print(f'新抓到 {len(items)} 則')
    return items


def main():
    print('=== 開始抓取《熱門族群》新聞：直接來源 + RSS備援 ===')
    new_items = fetch_sectors()

    old_items = kv_get('sectors') or []
    print(f'KV 現有 {len(old_items)} 則')

    merged = dedupe([*new_items, *old_items])
    cutoff = now_ms() - KEEP_DAYS * 24 * 60 * 60 * 1000
    merged = [n for n in merged if n.get('ts', 0) > cutoff]
    merged.sort(key=lambda x: x.get('ts', 0), reverse=True)

    # 舊資料若沒有個股，依標題補上常見指標股。
    for n in merged:
        if not n.get('stocks'):
            n['stocks'] = guess_stocks_by_title(n.get('title', ''))

    payload = merged[:120]
    ok1 = kv_put('sectors', payload, ttl_seconds=60 * 60 * 24 * (KEEP_DAYS + 1))
    ok2 = kv_put('sectors_meta', {'updated_at': now_ms(), 'updated_time': fmt_time(now_ms()), 'count': len(payload)}, ttl_seconds=60 * 60 * 24 * 2)
    print('✅ 寫入 KV 成功' if ok1 and ok2 else '❌ 寫入 KV 有失敗')

    print('\n前5則：')
    for n in payload[:5]:
        def stock_label(x):
            if isinstance(x, dict):
                return f"{x.get('name','')}({x.get('code','')})"
            return str(x)
        stocks_str = ', '.join(stock_label(x) for x in n.get('stocks', [])) or '無'
        print(f"  [{n['time']}] {n['title'][:45]} → {stocks_str}")


if __name__ == '__main__':
    main()
