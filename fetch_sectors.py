import os
import json
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta

# Cloudflare KV 設定（從環境變數讀取）
CF_ACCOUNT_ID    = os.environ['CF_ACCOUNT_ID']
CF_KV_NAMESPACE  = os.environ['CF_KV_NAMESPACE_ID']
CF_KV_TOKEN      = os.environ['CF_KV_TOKEN']
KV_API           = f'https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE}/values'

TW_TZ = timezone(timedelta(hours=8))

def fmt_time(ts_ms):
    d = datetime.fromtimestamp(ts_ms / 1000, tz=TW_TZ)
    hour = d.hour
    ampm = '上午' if hour < 12 else '下午'
    h12  = hour if hour <= 12 else hour - 12
    if h12 == 0: h12 = 12
    return f"{d.year}/{d.month}/{d.day} {ampm}{h12:02d}:{d.minute:02d}:{d.second:02d}"

def kv_get(key):
    r = requests.get(
        f'{KV_API}/{key}',
        headers={'Authorization': f'Bearer {CF_KV_TOKEN}'}
    )
    if r.status_code == 200:
        try: return r.json()
        except: return None
    return None

def kv_put(key, value, ttl_seconds=None):
    params = {}
    if ttl_seconds:
        params['expiration_ttl'] = ttl_seconds
    r = requests.put(
        f'{KV_API}/{key}',
        headers={
            'Authorization': f'Bearer {CF_KV_TOKEN}',
            'Content-Type': 'application/json',
        },
        params=params,
        data=json.dumps(value)
    )
    return r.status_code == 200

def fetch_sectors():
    new_items = []

    # 只抓 Google News RSS，來源限定富聯網（ww2.money-link.com.tw）
    rss_urls = [
        'https://news.google.com/rss/search?q=%22%E7%86%B1%E9%96%80%E6%97%8F%E7%BE%A4%22+site:money-link.com.tw&hl=zh-TW&gl=TW&ceid=TW:zh-Hant',
        'https://news.google.com/rss/search?q=%22%E7%86%B1%E9%96%80%E6%97%8F%E7%BE%A4%22&hl=zh-TW&gl=TW&ceid=TW:zh-Hant',
    ]

    for rss_url in rss_urls:
        try:
            feed = feedparser.parse(rss_url)
            print(f'RSS → {len(feed.entries)} 筆')

            for entry in feed.entries:
                raw_title = entry.get('title', '')

                # 嚴格：標題必須含《熱門族群》（有書名號）
                if '《熱門族群》' not in raw_title:
                    continue

                # 來源必須是富聯網
                src = ''
                if hasattr(entry, 'source'):
                    src = entry.source.get('title', '')
                if not src and ' - ' in raw_title:
                    src = raw_title.rsplit(' - ', 1)[-1].strip()

                # 嚴格：只接受富聯網（money-link）來源
                if 'money-link' not in entry.get('link', '') and \
                   'money-link' not in src.lower() and \
                   '富聯' not in src:
                    continue

                # 清理標題
                title = raw_title.rsplit(' - ', 1)[0].strip()
                link  = entry.get('link', '#')
                link  = link.replace('https://news.google.com/rss/articles/', 'https://news.google.com/articles/')

                pub = entry.get('published', '')
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub)
                    pub_ts = int(pub_dt.timestamp() * 1000)
                except:
                    pub_ts = int(time.time() * 1000)

                time_str = fmt_time(pub_ts)

                if title:
                    new_items.append({
                        'title': title,
                        'time':  time_str,
                        'url':   link,
                        'src':   '富聯網',
                        'ts':    pub_ts,
                    })
                    print(f'  ✅ {title[:50]}')

        except Exception as e:
            print(f'RSS error: {e}')

    print(f'\n新抓到 {len(new_items)} 則熱門族群新聞')
    return new_items

def main():
    print('=== 開始抓取熱門族群新聞（只要富聯網《熱門族群》）===')

    new_items = fetch_sectors()

    # 讀取 KV 舊資料
    old_items = kv_get('sectors') or []
    print(f'KV 現有 {len(old_items)} 則舊資料')

    # 合併去重，嚴格只保留《熱門族群》且來源是富聯網
    item_map = {}
    for n in [*old_items, *new_items]:
        title = n.get('title', '')
        if '《熱門族群》' not in title:
            continue
        src = n.get('src', '')
        url = n.get('url', '')
        if '富聯' not in src and 'money-link' not in url:
            continue
        key = title.strip()
        if key and key not in item_map:
            item_map[key] = n

    cutoff = int(time.time() * 1000) - 15 * 24 * 60 * 60 * 1000
    merged = [n for n in item_map.values() if n.get('ts', 0) > cutoff]
    merged.sort(key=lambda x: x.get('ts', 0), reverse=True)

    print(f'合併後共 {len(merged)} 則')

    ok = kv_put('sectors', merged, ttl_seconds=60 * 60 * 24 * 16)
    print('✅ 寫入 KV 成功' if ok else '❌ 寫入 KV 失敗')

    print('\n前5則：')
    for n in merged[:5]:
        print(f"  [{n['time']}] {n['title'][:50]}")

if __name__ == '__main__':
    main()
