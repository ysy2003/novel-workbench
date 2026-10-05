import json

import httpx
import pytest

from novel import shelf
from novel.autowrite import AUTO_SOURCE
from novel.model import ModelClient, ModelConfig
from novel.store import Store, ValidationError
from novel.writer import TASKS, Writer


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'shelf.sqlite3')


def write_chapter(db, book, title, body, source='手工稿'):
    chapter = db.create_chapter(book, title)
    revision = db.save_revision(chapter, title, '', body, db.chapter(chapter)['current_revision_id'], source=source)
    db.finalize(chapter, revision, '摘要', [], db.book(book)['context_version'])
    return chapter


def test_overview_summarises_progress_of_every_book(db):
    first = db.create_book('夜班守则', genre='悬疑脑洞')
    db.save_volume(first, 1, '第一夜', 1, 30, '')
    db.save_beats(first, 1, 30, [{'number': n, 'title': f'细纲{n}', 'brief': '要点'} for n in range(1, 6)])
    chapter = write_chapter(db, first, '第一章', '正文' * 500)
    write_chapter(db, first, '第二章', '正文' * 300, source=AUTO_SOURCE)
    db.create_chapter(first, '第三章')
    db.record_publication(chapter, db.chapter(chapter)['finalized_revision_id'], '已发布')
    second = db.create_book('遗言收殓师')
    rows = {r['title']: r for r in shelf.overview(db)}
    assert rows['夜班守则'] == {**rows['夜班守则'], 'genre': '悬疑脑洞', 'chapters': 3, 'finalized': 2, 'chars': 1600,
                               'last_beat': 5, 'beats_ahead': 2, 'auto_unreviewed': 1, 'stale': 0, 'published': 1}
    assert rows['遗言收殓师']['chapters'] == 0 and rows['遗言收殓师']['latest_stats'] is None
    assert rows['夜班守则']['book_id'] == first and rows['遗言收殓师']['book_id'] == second


def test_platform_stats_are_saved_per_day_and_compared_with_the_previous_record(db):
    book = db.create_book('夜班守则')
    shelf.save_stats(db, book, '2026-10-05', 3, {'在读人数': 120, '加书架': 40})
    shelf.save_stats(db, book, '2026-10-06', 5, {'在读人数': 300, '加书架': 70, '完读率（%）': 32.5}, '上了推荐')
    shelf.save_stats(db, book, '2026-10-06', 5, {'在读人数': 320, '加书架': 70, '完读率（%）': 32.5}, '修正')
    history = shelf.stats(db, book)
    assert [h['recorded_on'] for h in history] == ['2026-10-05', '2026-10-06']
    assert history[-1]['metrics']['在读人数'] == 320 and history[-1]['note'] == '修正'
    row = shelf.comparison(db)[0]
    assert row['latest']['recorded_on'] == '2026-10-06'
    assert row['changes'] == {'在读人数': 200, '加书架': 30, '完读率（%）': None}


@pytest.mark.parametrize('date, metrics', [('10月6日', {'在读人数': 1}), ('2026-10-06', {}),
                                           ('2026-10-06', {'在读人数': -1}), ('2026-10-06', {'粉丝数': 3})])
def test_platform_stats_are_validated(db, date, metrics):
    book = db.create_book('夜班守则')
    with pytest.raises(ValidationError):
        shelf.save_stats(db, book, date, 0, metrics)


def fake_model():
    def serve(request):
        system = json.loads(request.content)['messages'][0]['content']
        if TASKS['plan'] in system:
            text = '场景计划'
        elif TASKS['draft'] in system:
            text = '林川推开门。' * 300
        else:
            text = json.dumps({'summary': '摘要', 'facts': []}, ensure_ascii=False)
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
    return ModelClient(ModelConfig('https://example.test/chat/completions', 'm', 'k'), httpx.MockTransport(serve))


def test_batch_writing_runs_books_in_turn_and_skips_books_that_are_not_ready(db):
    ready = db.create_book('可以连写')
    db.save_volume(ready, 1, '第一卷', 1, 30, '')
    db.save_beats(ready, 1, 30, [{'number': n, 'title': f'细纲{n}', 'brief': '要点'} for n in range(1, 10)])
    blocked = db.create_book('有未定稿章节')
    db.create_chapter(blocked, '没定稿')
    seen = []
    results = shelf.batch_autowrite(db, Writer(db, fake_model()), [blocked, ready], 2, auto_beats=False,
                                    progress=lambda title, number, step: seen.append(title))
    assert [(r['title'], r['done'], r['error'] is None) for r in results] == [('有未定稿章节', 0, False), ('可以连写', 2, True)]
    assert '还没定稿' in results[0]['error']
    assert [c['number'] for c in db.chapters(ready)] == [1, 2]
    assert set(seen) == {'可以连写'}
