"""Task 5B 主审探针：热点上游失败时旧缓存被报告成当前成功数据。

仅内存假热点与受控 fetch 替身；不会访问公网或真实供应商。
"""
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    from web import app as worker
    cache = dict(worker._TREND_CACHE)
    fetcher = worker._fetch_platform
    cached_at = time.time() - 3600
    try:
        worker._TREND_CACHE.clear()
        worker._TREND_CACHE['weibo'] = (cached_at, [
            {'title': 'OLD-CACHED-TREND', 'hot': '', 'url': ''}])
        worker._fetch_platform = lambda platform: []
        data = asyncio.run(worker.api_trends('weibo', 1))
        group = data['trends'][0]
        print('UPSTREAM_FETCH_RESULT=EMPTY_FAILURE')
        print('CACHED_AT_UTC=' + datetime.fromtimestamp(
            cached_at, timezone.utc).isoformat())
        print('RESPONSE_UPDATED_AT_UTC=' + datetime.fromtimestamp(
            data['updated'], timezone.utc).isoformat())
        print('CACHE_AGE_SECONDS=' + str(int(time.time() - cached_at)))
        print('RESPONSE_UPDATED_AGE_SECONDS=' + str(int(time.time() - data['updated'])))
        print('RETURNED_OLD_TITLE=' + str(group['items'][0]['title'] == 'OLD-CACHED-TREND'))
        print('GROUP_OK=' + str(group['ok']))
        print('STALE_MARKER_PRESENT=' + str(any('stale' in key for key in group)))
        print('GROUP_FIELDS=' + ','.join(sorted(group)))
    finally:
        worker._TREND_CACHE.clear()
        worker._TREND_CACHE.update(cache)
        worker._fetch_platform = fetcher
    print('IN_MEMORY_FIXTURE_CLEANUP=COMPLETE')

if __name__ == '__main__':
    main()
