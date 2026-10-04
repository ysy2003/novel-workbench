"""选题研究：番茄排行榜快照与书籍详情。

榜单页上的书名、作者、简介用了字体反爬（部分字是私有码位），这里不做字体解码，残缺处显示为 □。
书的详情页（/page/{id}）的书名、标签、字数、简介、目录是正常文字，按需逐本抓取。不抓正文。
"""
from __future__ import annotations

import html
import json
import re
import time

import httpx

from novel.store import ValidationError, clean_text

BASE = 'https://fanqienovel.com'
USER_AGENT = 'Mozilla/5.0 (compatible; novel-workbench/0.1; personal research)'
CHANNELS = {'0': '女频', '1': '男频'}
BOARDS = {'1': '新书榜', '2': '阅读榜'}
HIDDEN = re.compile('[-]')


class ResearchError(RuntimeError):
    pass


def _text(fragment):
    return html.unescape(re.sub(r'<[^>]+>', '', fragment)).strip()


def parse_reading(text):
    match = re.match(r'([\d.]+)\s*(万|亿)?', text.strip())
    if not match:
        return None
    return round(float(match.group(1)) * {'万': 10_000, '亿': 100_000_000}.get(match.group(2), 1))


def parse_categories(page):
    result, seen = [], set()
    for path, channel, board, category_id, name in re.findall(
            r'href="(/rank/(\d+)_(\d+)_(\d+))"[^>]*>(.*?)</a>', page):
        if path in seen or channel not in CHANNELS or board not in BOARDS:
            continue
        seen.add(path)
        result.append({'path': path, 'channel': CHANNELS[channel], 'board': BOARDS[board],
                       'category_id': int(category_id), 'category': _text(name)})
    return result


def parse_rank_page(page):
    stat = re.search(r'统计时间截止至(?:<!-- -->)?([^<]+)<', page)
    entries = []
    for block in page.split('class="rank-book-item"')[1:]:
        def field(pattern):
            match = re.search(pattern, block, flags=re.S)
            return _text(match.group(1)) if match else ''
        rank, book_id = field(r'<h1>(\d+)</h1>'), re.search(r'href="/page/(\d+)"', block)
        if not rank or not book_id:
            continue
        title = field(r'<div class="title"><a[^>]*>(.*?)</a>')
        entries.append({'rank': int(rank), 'book_id': book_id.group(1), 'title': HIDDEN.sub('□', title),
                        'title_complete': 0 if HIDDEN.search(title) else 1,
                        'reading': parse_reading(field(r'在读：(?:<!-- -->)?([^<]*)<')),
                        'status': field(r'book-item-footer-status">(.*?)</span>'),
                        'latest_chapter': field(r'最近更新：(?:<!-- -->)?(.*?)</a>'),
                        'updated_at': field(r'book-item-footer-time">(.*?)</span>')})
    return (_text(stat.group(1)) if stat else ''), entries


def save_snapshot(store, stat_date, pages, rows, failed):
    with store.connect(True) as c:
        snapshot = c.execute('INSERT INTO rank_snapshots(stat_date,pages,failed) VALUES(?,?,?)',
                             (stat_date, pages, json.dumps(failed, ensure_ascii=False))).lastrowid
        c.executemany('INSERT INTO rank_entries(snapshot_id,channel,board,category_id,category,rank,book_id,title,'
                      'title_complete,reading,status,latest_chapter,updated_at) '
                      'VALUES(:snapshot,:channel,:board,:category_id,:category,:rank,:book_id,:title,'
                      ':title_complete,:reading,:status,:latest_chapter,:updated_at)',
                      [{**row, 'snapshot': snapshot} for row in rows])
    return snapshot


def fetch_snapshot(store, transport=None, delay=2.0, progress=None):
    """抓取全部榜单分类页。每页之间间隔 delay 秒；单页失败记录下来，不影响其他页。"""
    progress = progress or (lambda done, total, category: None)
    with httpx.Client(base_url=BASE, transport=transport, timeout=20, follow_redirects=False,
                      headers={'User-Agent': USER_AGENT}) as client:
        try:
            root = client.get('/rank')
        except httpx.HTTPError as error:
            raise ResearchError(f'无法打开番茄排行榜：{error.__class__.__name__}') from None
        if root.status_code != 200:
            raise ResearchError(f'无法打开番茄排行榜（HTTP {root.status_code}）。')
        categories = parse_categories(root.text)
        if not categories:
            raise ResearchError('排行榜页面里没有找到分类，页面结构可能已经变化。')
        rows, failed, stat_date, pages = [], [], '', 0
        for index, category in enumerate(categories, 1):
            progress(index, len(categories), f'{category["channel"]}{category["board"]} · {category["category"]}')
            time.sleep(delay)
            try:
                response = client.get(category['path'])
            except httpx.HTTPError as error:
                failed.append({'path': category['path'], 'reason': error.__class__.__name__})
                continue
            if response.status_code != 200:
                failed.append({'path': category['path'], 'reason': f'HTTP {response.status_code}'})
                continue
            page_date, entries = parse_rank_page(response.text)
            if not entries:
                failed.append({'path': category['path'], 'reason': '页面里没有书目'})
                continue
            pages += 1
            stat_date = stat_date or page_date
            rows += [{**entry, **{k: category[k] for k in ('channel', 'board', 'category_id', 'category')}}
                     for entry in entries]
    snapshot = save_snapshot(store, stat_date, pages, rows, failed)
    return snapshot, {'pages': pages, 'entries': len(rows), 'failed': failed, 'stat_date': stat_date}


def snapshots(store):
    return store.all('SELECT s.*, COUNT(e.rank) AS entries FROM rank_snapshots s '
                     'LEFT JOIN rank_entries e ON e.snapshot_id=s.id GROUP BY s.id ORDER BY s.id DESC')


def entries(store, snapshot_id):
    return store.all('SELECT e.*, n.title AS note_title, n.tags AS note_tags, n.note AS note, d.title AS detail_title, '
                     'd.tags AS detail_tags, d.word_count AS word_count FROM rank_entries e '
                     'LEFT JOIN rank_notes n ON n.book_id=e.book_id LEFT JOIN book_details d ON d.book_id=e.book_id '
                     'WHERE e.snapshot_id=? '
                     'ORDER BY e.channel, e.board, e.category, e.rank', (snapshot_id,))


def category_heat(store, snapshot_id, previous_id=None):
    """各榜单分类头部在读合计，及与上一份快照相比的变化。"""
    def totals(snapshot):
        return {(r['channel'], r['board'], r['category_id']): r for r in store.all(
            'SELECT channel, board, category_id, category, SUM(reading) AS total, COUNT(*) AS books '
            'FROM rank_entries WHERE snapshot_id=? GROUP BY channel, board, category_id', (snapshot,))}
    current, previous = totals(snapshot_id), totals(previous_id) if previous_id else {}
    rows = []
    for key, row in current.items():
        before = previous.get(key)
        change = row['total'] - before['total'] if before else None
        rows.append({**row, 'change': change,
                     'change_pct': round(change / before['total'] * 100, 1) if before and before['total'] else None})
    return sorted(rows, key=lambda r: -(r['total'] or 0))


def chapter_number(text):
    match = re.search(r'第\s*(\d+)\s*章', text or '')
    return int(match.group(1)) if match else None


def rising_books(store, snapshot_id, previous_id):
    """同一榜单里在读增长最多的书；新上榜的排在后面并标记。"""
    previous = {(r['board'], r['book_id']): r for r in entries(store, previous_id)}
    rows = []
    for row in entries(store, snapshot_id):
        before = previous.get((row['board'], row['book_id']))
        now_chapter, old_chapter = chapter_number(row['latest_chapter']), before and chapter_number(before['latest_chapter'])
        rows.append({**row, 'is_new': before is None,
                     'previous_rank': before['rank'] if before else None,
                     'growth': row['reading'] - before['reading'] if before and row['reading'] is not None
                     and before['reading'] is not None else None,
                     'chapters_gained': now_chapter - old_chapter if now_chapter and old_chapter else None})
    return sorted(rows, key=lambda r: (r['is_new'], -(r['growth'] or 0), -(r['reading'] or 0)))


def save_note(store, book_id, title, tags, note):
    values = (clean_text(book_id, '书的 ID', 40, True), clean_text(title, '书名', 120),
              clean_text(tags, '标签', 200), clean_text(note, '备注', 2000))
    with store.connect(True) as c:
        c.execute('INSERT INTO rank_notes(book_id,title,tags,note) VALUES(?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET '
                  "title=excluded.title, tags=excluded.tags, note=excluded.note, "
                  "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", values)


def notes(store):
    return store.all('SELECT * FROM rank_notes ORDER BY updated_at DESC')


def parse_book_page(page):
    def field(pattern):
        match = re.search(pattern, page, flags=re.S)
        return HIDDEN.sub('□', _text(match.group(1))) if match else ''
    words = re.search(r'info-count-word"><span class="detail">([\d.]+)</span><span class="text">([^<]*)</span>', page)
    start = page.find('page-directory-content')
    directory = page[start:] if start >= 0 else ''
    return {'title': field(r'<div class="info-name"><h1>(.*?)</h1>'),
            'status': field(r'class="info-label-yellow">(.*?)</span>'),
            'tags': [HIDDEN.sub('□', _text(tag)) for tag in re.findall(r'class="info-label-grey">(.*?)</span>', page)],
            'word_count': round(float(words.group(1)) * (10_000 if '万' in words.group(2) else 1)) if words else None,
            'intro': field(r'class="page-abstract-content"><p>(.*?)</p>'),
            'chapters': [HIDDEN.sub('□', _text(c)) for c in re.findall(r'class="chapter-item-title"[^>]*>(.*?)</a>', directory)]}


def save_details(store, book_id, info):
    with store.connect(True) as c:
        c.execute('INSERT INTO book_details(book_id,title,status,tags,word_count,intro,chapters) VALUES(?,?,?,?,?,?,?) '
                  'ON CONFLICT(book_id) DO UPDATE SET title=excluded.title, status=excluded.status, tags=excluded.tags, '
                  "word_count=excluded.word_count, intro=excluded.intro, chapters=excluded.chapters, "
                  "fetched_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                  (book_id, info['title'], info['status'], json.dumps(info['tags'], ensure_ascii=False),
                   info['word_count'], info['intro'], json.dumps(info['chapters'], ensure_ascii=False)))


def fetch_details(store, book_ids, transport=None, delay=2.0, progress=None):
    """逐本抓取详情页（书名、标签、字数、简介、目录），每本之间间隔 delay 秒。"""
    progress = progress or (lambda done, total, book_id: None)
    fetched, failed = 0, []
    with httpx.Client(base_url=BASE, transport=transport, timeout=20, follow_redirects=False,
                      headers={'User-Agent': USER_AGENT}) as client:
        for index, book_id in enumerate(book_ids, 1):
            progress(index, len(book_ids), book_id)
            if index > 1:
                time.sleep(delay)
            try:
                response = client.get(f'/page/{book_id}')
            except httpx.HTTPError as error:
                failed.append({'book_id': book_id, 'reason': error.__class__.__name__})
                continue
            if response.status_code != 200:
                failed.append({'book_id': book_id, 'reason': f'HTTP {response.status_code}'})
                continue
            info = parse_book_page(response.text)
            if not info['title']:
                failed.append({'book_id': book_id, 'reason': '页面里没有书名'})
                continue
            save_details(store, book_id, info)
            fetched += 1
    return {'fetched': fetched, 'failed': failed}


def details(store, book_id):
    rows = store.all('SELECT * FROM book_details WHERE book_id=?', (book_id,))
    if not rows:
        return None
    row = rows[0]
    return {**row, 'tags': json.loads(row['tags']), 'chapters': json.loads(row['chapters'])}
