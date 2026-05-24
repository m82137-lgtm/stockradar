import os
import json
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

CF_ACCOUNT_ID   = os.environ['CF_ACCOUNT_ID']
CF_KV_NAMESPACE = os.environ['CF_KV_NAMESPACE_ID']
CF_KV_TOKEN     = os.environ['CF_KV_TOKEN']
WORKER_URL      = os.environ.get('WORKER_URL', '')
KV_API          = f'https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE}/values'

TW_TZ = timezone(timedelta(hours=8))
COOLDOWN_SECONDS = 60 * 60          # 同一檔股票 1 小時內不重抓
TTL_SECONDS      = 60 * 60 * 24     # KV 保留 24 小時
MAX_NEWS         = 10

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ts_ms):
    d = datetime.fromtimestamp(ts_ms / 1000, tz=TW_TZ)
    return d.strftime('%Y/%m/%d %H:%M:%S')


def kv_get(key):
    try:
        r = requests.get(
            f'{KV_API}/{key}',
            headers={'Authorization': f'Bearer {CF_KV_TOKEN}'},
            timeout=15,
        )
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
    except Exception as e:
        print(f'KV GET error {key}: {e}')
    return None


def kv_put(key, value, ttl_seconds=None):
    params = {'expiration_ttl': ttl_seconds} if ttl_seconds else {}
    try:
        r = requests.put(
            f'{KV_API}/{key}',
            headers={
                'Authorization': f'Bearer {CF_KV_TOKEN}',
                'Content-Type': 'application/json',
            },
            params=params,
            data=json.dumps(value, ensure_ascii=False),
            timeout=20,
        )
        if r.status_code != 200:
            print(f'KV PUT error {key}: {r.status_code} {r.text[:250]}')
        return r.status_code == 200
    except Exception as e:
        print(f'KV PUT exception {key}: {e}')
        return False


def unwrap_stock_news(raw):
    """相容舊版 array 與新版 object。"""
    if not raw:
        return None, []
    if isinstance(raw, list):
        return None, raw
    if isinstance(raw, dict):
        return int(raw.get('updated_at') or 0), raw.get('items') or []
    return None, []


def should_skip_by_cooldown(code):
    raw = kv_get(f'stock_news_{code}')
    updated_at, items = unwrap_stock_news(raw)

    # 舊版是純 array，沒有 updated_at。為了建立新版快取格式，視為需要更新。
    if not updated_at:
        return False

    age_sec = (now_ms() - updated_at) / 1000
    if items and age_sec < COOLDOWN_SECONDS:
        print(f'  跳過 {code}：{int(age_sec // 60)} 分鐘前已更新，1小時內不重抓')
        return True
    return False


def clean_title(raw):
    raw = (raw or '').strip()
    title = raw.rsplit(' - ', 1)[0].strip() if ' - ' in raw else raw
    return title


def fetch_stock_news(code, name):
    """用 GitHub Actions 端查 Google News RSS，抓個股新聞。"""
    items = []
    seen = set()

    queries = []
    if code and name:
        queries.append(f'{code} {name} 台股')
    if name:
        queries.append(f'{name} 台股')
    if code:
        queries.append(f'{code} 股票')

    for query in queries:
        if len(items) >= MAX_NEWS:
            break
        try:
            rss_url = 'https://news.google.com/rss/search?q={}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'.format(
                requests.utils.quote(query)
            )
            feed = feedparser.parse(rss_url, request_headers=HEADERS)

            for entry in feed.entries[:8]:
                raw_title = entry.get('title', '')
                title = clean_title(raw_title)
                if not title:
                    continue

                # 基本過濾：至少要命中股票名稱或股票代號之一，避免抓到太泛的新聞。
                haystack = f'{title} {raw_title}'
                if name and name not in haystack and code not in haystack:
                    continue

                link = entry.get('link', '#')
                key = link or title
                if key in seen or title in seen:
                    continue
                seen.add(key)
                seen.add(title)

                src = ''
                if hasattr(entry, 'source'):
                    src = entry.source.get('title', '')
                if not src and ' - ' in raw_title:
                    src = raw_title.rsplit(' - ', 1)[-1].strip()

                try:
                    pub_dt = parsedate_to_datetime(entry.get('published', ''))
                    pub_ts = int(pub_dt.timestamp() * 1000)
                except Exception:
                    pub_ts = now_ms()

                items.append({
                    'title': title,
                    'link': link,
                    'src': src or '新聞',
                    'pub': pub_ts,
                })
                if len(items) >= MAX_NEWS:
                    break

        except Exception as e:
            print(f'  RSS error ({query}): {e}')

    items.sort(key=lambda x: x.get('pub', 0), reverse=True)
    return items[:MAX_NEWS]


def get_alert_stocks():
    alerts = []
    try:
        r = requests.get(f'{WORKER_URL}/api/alerts', timeout=10)
        alerts = r.json() if r.status_code == 200 else []
        print(f'alerts: {len(alerts)} 筆')
    except Exception as e:
        print(f'讀取 alerts 失敗: {e}')

    seen_codes = set()
    stocks = []
    for a in alerts or []:
        code = str(a.get('code') or '').strip()
        name = str(a.get('name') or '').strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        stocks.append({'code': code, 'name': name})
    return stocks


def main():
    print('=== 開始抓取個股新聞：全部入榜 + 1小時冷卻 ===')

    stocks = get_alert_stocks()
    if not stocks:
        print('沒有入榜股票，結束')
        return

    print(f'共 {len(stocks)} 檔入榜股票需要檢查新聞')

    fetched = 0
    skipped = 0
    written = 0
    no_news = 0

    for s in stocks:
        code = s['code']
        name = s['name']

        if should_skip_by_cooldown(code):
            skipped += 1
            continue

        print(f'  抓 {code} {name}...')
        fetched += 1
        try:
            items = fetch_stock_news(code, name)
            if items:
                payload = {
                    'code': code,
                    'name': name,
                    'updated_at': now_ms(),
                    'updated_time': fmt_time(now_ms()),
                    'items': items,
                }
                ok = kv_put(f'stock_news_{code}', payload, ttl_seconds=TTL_SECONDS)
                print(f'    → {len(items)} 則，寫入KV {"✅" if ok else "❌"}')
                if ok:
                    written += 1
            else:
                # 抓不到時不覆蓋舊 KV，避免前端新聞忽然消失。
                print('    → 無新聞，不覆蓋舊資料')
                no_news += 1
            time.sleep(0.5)
        except Exception as e:
            print(f'    → 錯誤: {e}')

    print('\n完成')
    print(f'  入榜檔數：{len(stocks)}')
    print(f'  實際抓取：{fetched}')
    print(f'  冷卻跳過：{skipped}')
    print(f'  寫入成功：{written}')
    print(f'  無新聞：{no_news}')


if __name__ == '__main__':
    main()
