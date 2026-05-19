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

        # 抓「台積電(2330)」、「台積電（2330）」這種格式。
        pairs = re.findall(r'([\u4e00-\u9fffA-Za-z0-9\-＊*]{1,12})\s*[（(](\d{4})[）)]', text)

        seen, result = set(), []
        for raw_name, code in pairs:
            if not re.match(r'^\d{4}$', code):
                continue
            if code in seen:
                continue
            name = clean_stock_name(raw_name)
            if not name:
                continue
            seen.add(code)
            result.append({'name': name, 'code': code})

        return result[:8]
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


def dedupe(items):
    item_map = {}
    for n in items:
        title = n.get('title', '')
        url = normalize_url(n.get('url', ''))
        if HOT_KEYWORD not in title:
            continue
        key = re.search(r'[?&]sn=([0-9]+)', url, re.I)
        key = key.group(1) if key else (url.split('#')[0].split('?')[0] or title)
        old = item_map.get(key)
        if not old:
            n['url'] = url
            item_map[key] = n
        else:
            # 保留較新的時間，並補股票
            if n.get('ts', 0) > old.get('ts', 0):
                item_map[key] = {**old, **n}
            if not item_map[key].get('stocks') and n.get('stocks'):
                item_map[key]['stocks'] = n['stocks']
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
