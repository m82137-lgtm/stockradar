import os
import json
import time
import re
import requests
import feedparser
from datetime import datetime, timezone, timedelta

CF_ACCOUNT_ID   = os.environ['CF_ACCOUNT_ID']
CF_KV_NAMESPACE = os.environ['CF_KV_NAMESPACE_ID']
CF_KV_TOKEN     = os.environ['CF_KV_TOKEN']
WORKER_URL      = os.environ.get('WORKER_URL', '')
KV_API          = f'https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE}/values'

TW_TZ = timezone(timedelta(hours=8))

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

def kv_get(key):
    r = requests.get(f'{KV_API}/{key}', headers={'Authorization': f'Bearer {CF_KV_TOKEN}'})
    if r.status_code == 200:
        try: return r.json()
        except: return None
    return None

def kv_put(key, value, ttl_seconds=None):
    params = {'expiration_ttl': ttl_seconds} if ttl_seconds else {}
    r = requests.put(
        f'{KV_API}/{key}',
        headers={'Authorization': f'Bearer {CF_KV_TOKEN}', 'Content-Type': 'application/json'},
        params=params,
        data=json.dumps(value)
    )
    return r.status_code == 200

def fetch_stock_news(code, name):
    """用 Google News RSS 抓個股新聞"""
    items = []
    queries = [
        f'{code} {name}',
        f'{name}',
    ]
    seen = set()

    for query in queries:
        if len(items) >= 10:
            break
        try:
            rss_url = f'https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:8]:
                raw = entry.get('title', '')
                src = ''
                if hasattr(entry, 'source'):
                    src = entry.source.get('title', '')
                if not src and ' - ' in raw:
                    src = raw.rsplit(' - ', 1)[-1].strip()
                title = raw.rsplit(' - ', 1)[0].strip() if ' - ' in raw else raw

                pub = entry.get('published', '')
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub)
                    pub_ts = int(pub_dt.timestamp() * 1000)
                except:
                    pub_ts = int(time.time() * 1000)

                link = entry.get('link', '#')

                if title and title not in seen:
                    seen.add(title)
                    items.append({
                        'title': title,
                        'link':  link,
                        'src':   src or '新聞',
                        'pub':   pub_ts,
                    })
        except Exception as e:
            print(f'  RSS error ({query}): {e}')

    items.sort(key=lambda x: x.get('pub', 0), reverse=True)
    return items[:10]

def main():
    print('=== 開始抓取個股新聞 ===')

    # 從 Worker 讀取目前入榜的 alerts
    alerts = []
    try:
        r = requests.get(f'{WORKER_URL}/api/alerts', timeout=10)
        alerts = r.json() if r.status_code == 200 else []
        print(f'alerts: {len(alerts)} 檔')
    except Exception as e:
        print(f'讀取 alerts 失敗: {e}')

    if not alerts:
        print('沒有入榜股票，結束')
        return

    # 去重（同一檔可能出現在起漲K和量增K）
    seen_codes = set()
    stocks = []
    for a in alerts:
        code = a.get('code')
        if code and code not in seen_codes:
            seen_codes.add(code)
            stocks.append({'code': code, 'name': a.get('name', '')})

    print(f'共 {len(stocks)} 檔需要抓新聞')

    success = 0
    for s in stocks:
        code = s['code']
        name = s['name']
        print(f'  抓 {code} {name}...')
        try:
            items = fetch_stock_news(code, name)
            if items:
                ok = kv_put(f'stock_news_{code}', items, ttl_seconds=60 * 60 * 20)  # 保留20小時
                print(f'    → {len(items)} 則，寫入KV {"✅" if ok else "❌"}')
                success += 1
            else:
                print(f'    → 無新聞')
            time.sleep(0.5)  # 避免太快
        except Exception as e:
            print(f'    → 錯誤: {e}')

    print(f'\n完成！成功 {success}/{len(stocks)} 檔')

if __name__ == '__main__':
    main()
