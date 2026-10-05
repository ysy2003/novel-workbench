"""多开管理：书架总览、番茄后台数据记录与对比、多本书排队连写。

平台数据由作者从番茄作家后台抄录，这里不登录、不抓取作者后台。
"""
from __future__ import annotations

import datetime
import json

from novel.autowrite import AUTO_SOURCE, autowrite, count_chars
from novel.store import ValidationError, clean_text

METRICS = ('在读人数', '加书架', '追读人数', '完读率（%）', '收入（元）')


def _latest_stats(store, book_id, limit=1):
    rows = store.all('SELECT * FROM platform_stats WHERE book_id=? ORDER BY recorded_on DESC LIMIT ?', (book_id, limit))
    for row in rows:
        row['metrics'] = json.loads(row['metrics'])
    return rows


def overview(store):
    """每本书一行：章节、定稿、定稿字数、细纲进度、待审阅的自动章节、待复核、已发布、最近更新、最新平台数据。"""
    result = []
    for book in store.books():
        chapters = store.all('SELECT ch.number, ch.finalized_revision_id, r.body, r.source, m.stale FROM chapters ch '
                             'LEFT JOIN revisions r ON r.id=ch.finalized_revision_id '
                             'LEFT JOIN memories m ON m.chapter_id=ch.id WHERE ch.book_id=?', (book['id'],))
        finalized = [ch for ch in chapters if ch['finalized_revision_id']]
        last_chapter = max((ch['number'] for ch in chapters), default=0)
        last_beat = store.all('SELECT MAX(number) AS n FROM beats WHERE book_id=?', (book['id'],))[0]['n'] or 0
        published = store.all(
            "SELECT COUNT(*) AS n FROM publications p JOIN chapters ch ON ch.id=p.chapter_id WHERE ch.book_id=? "
            "AND p.status='已发布' AND p.id=(SELECT MAX(id) FROM publications WHERE chapter_id=p.chapter_id)",
            (book['id'],))[0]['n']
        updated = store.all('SELECT MAX(r.created_at) AS t FROM revisions r JOIN chapters ch ON ch.id=r.chapter_id '
                            'WHERE ch.book_id=?', (book['id'],))[0]['t']
        latest = _latest_stats(store, book['id'])
        result.append({'book_id': book['id'], 'title': book['title'], 'genre': book['genre'],
                       'chapters': len(chapters), 'finalized': len(finalized),
                       'chars': sum(count_chars(ch['body']) for ch in finalized),
                       'last_beat': last_beat, 'beats_ahead': last_beat - last_chapter,
                       'auto_unreviewed': sum(ch['source'] == AUTO_SOURCE for ch in finalized),
                       'stale': sum(bool(ch['stale']) for ch in finalized), 'published': published,
                       'updated_at': updated or book['created_at'], 'latest_stats': latest[0] if latest else None})
    return result


def save_stats(store, book_id, recorded_on, published_chapters, metrics, note=''):
    """同一本书同一天只保留一条，再次保存会覆盖。"""
    try:
        recorded_on = datetime.date.fromisoformat(recorded_on).isoformat()
    except (TypeError, ValueError):
        raise ValidationError('日期格式应为 YYYY-MM-DD。') from None
    if published_chapters is not None and (isinstance(published_chapters, bool) or not isinstance(published_chapters, int)
                                           or published_chapters < 0):
        raise ValidationError('已发布章数必须是不小于 0 的整数。')
    values = {}
    for name, value in metrics.items():
        if name not in METRICS:
            raise ValidationError(f'不认识的数据项：{name}。')
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValidationError(f'{name}必须是不小于 0 的数字。')
        values[name] = value
    if not values:
        raise ValidationError('至少填一项数据。')
    note = clean_text(note, '备注', 1000)
    with store.connect(True) as c:
        if not c.execute('SELECT 1 FROM books WHERE id=?', (book_id,)).fetchone():
            raise ValidationError('作品不存在。')
        c.execute('INSERT INTO platform_stats(book_id,recorded_on,published_chapters,metrics,note) VALUES(?,?,?,?,?) '
                  'ON CONFLICT(book_id,recorded_on) DO UPDATE SET published_chapters=excluded.published_chapters, '
                  'metrics=excluded.metrics, note=excluded.note',
                  (book_id, recorded_on, published_chapters, json.dumps(values, ensure_ascii=False), note))


def stats(store, book_id):
    return list(reversed(_latest_stats(store, book_id, limit=10_000)))


def comparison(store):
    """有平台数据的书：最新一条，以及每项数据与上一条相比的变化（上一条没有这项时为 None）。"""
    rows = []
    for book in store.books():
        latest = _latest_stats(store, book['id'], limit=2)
        if not latest:
            continue
        previous = latest[1]['metrics'] if len(latest) > 1 else {}
        changes = {name: (round(value - previous[name], 2) if name in previous else None)
                   for name, value in latest[0]['metrics'].items()}
        rows.append({'book_id': book['id'], 'title': book['title'], 'latest': latest[0], 'changes': changes})
    return rows


def batch_autowrite(store, writer, book_ids, count, target_words=2000, auto_beats=True, progress=None):
    """依次给每本书连写 count 章；某本书不满足前提时记下原因并继续下一本。"""
    results = []
    for book_id in book_ids:
        title = store.book(book_id)['title']

        def report(number, step, title=title):
            if progress:
                progress(title, number, step)
        try:
            result = autowrite(store, writer, book_id, count, target_words, report, auto_beats=auto_beats)
            results.append({'book_id': book_id, 'title': title, 'done': len(result['done']), 'stopped': result['stopped'],
                            'beats': result['beats'], 'warnings': result['warnings'], 'error': None})
        except ValidationError as error:
            results.append({'book_id': book_id, 'title': title, 'done': 0, 'stopped': None, 'beats': [],
                            'warnings': [], 'error': str(error)})
    return results
