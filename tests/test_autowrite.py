import json

import httpx
import pytest

from novel.autowrite import AUTO_SOURCE, auto_chapters, autowrite, capacity, check_draft
from novel.model import ModelClient, ModelConfig
from novel.store import Store, ValidationError
from novel.writer import TASKS, Writer

LONG_DRAFT = '林川推开门。' * 300


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'auto.sqlite3')


def book_with_beats(db, numbers):
    book = db.create_book('夜班守则')
    db.save_volume(book, 1, '第一夜', 1, 30, '活过第一周。')
    db.save_beats(book, 1, 30, [{'number': n, 'title': f'细纲{n}', 'brief': f'第{n}章要点'} for n in numbers])
    return book


def finalize_manually(db, book, title):
    chapter = db.create_chapter(book, title, '手写计划')
    revision = db.save_revision(chapter, title, '手写计划', '作者自己写的正文。', db.chapter(chapter)['current_revision_id'])
    db.finalize(chapter, revision, '作者写的摘要', [], db.book(book)['context_version'])
    return chapter


def model(drafts=(LONG_DRAFT,), status=200):
    drafts = list(drafts)

    def serve(request):
        if status != 200:
            return httpx.Response(status, text='error')
        system = json.loads(request.content)['messages'][0]['content']
        if TASKS['plan'] in system:
            text = '场景一：林川值夜班。'
        elif TASKS['draft'] in system:
            text = drafts.pop(0) if len(drafts) > 1 else drafts[0]
        else:
            text = json.dumps({'summary': '林川值了一夜班。', 'facts': [{'entity': '林川', 'attribute': '位置', 'value': '店里'}]},
                              ensure_ascii=False)
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
    config = ModelConfig('https://example.test/chat/completions', 'writing-model', 'test-secret')
    return ModelClient(config, httpx.MockTransport(serve))


def run(db, book, count, client=None):
    return autowrite(db, Writer(db, client or model()), book, count, target_words=2000)


def test_writes_and_finalizes_new_chapters_without_touching_existing_ones(db):
    book = book_with_beats(db, range(2, 6))
    first = finalize_manually(db, book, '作者写的第一章')
    before = len(db.revisions(first))
    result = run(db, book, 2)
    assert result['stopped'] is None
    assert [c['number'] for c in result['done']] == [2, 3]
    chapters = db.chapters(book)
    assert [(c['number'], c['title']) for c in chapters] == [(1, '作者写的第一章'), (2, '细纲2'), (3, '细纲3')]
    assert all(c['finalized_revision_id'] for c in chapters)
    assert db.revision(db.chapter(chapters[2]['id'])['finalized_revision_id'])['source'] == AUTO_SOURCE
    assert len(db.revisions(first)) == before
    assert [m['number'] for m in db.memory(book)] == [1, 2, 3]
    assert [c['number'] for c in auto_chapters(db, book)] == [2, 3]


def test_requires_earlier_chapters_to_be_finalized(db):
    book = book_with_beats(db, range(1, 6))
    db.create_chapter(book, '没定稿的第一章')
    with pytest.raises(ValidationError, match='还没定稿'):
        run(db, book, 1)


def test_requires_consecutive_beats_for_every_chapter(db):
    book = book_with_beats(db, [1, 2, 4])
    assert capacity(db, book)['available'] == 2
    with pytest.raises(ValidationError, match='细纲'):
        run(db, book, 3)


def test_stops_at_a_draft_that_fails_checks_and_keeps_finished_chapters(db):
    book = book_with_beats(db, range(1, 6))
    result = run(db, book, 3, model(drafts=[LONG_DRAFT, '太短。']))
    assert [c['number'] for c in result['done']] == [1]
    assert result['stopped']['number'] == 2 and result['stopped']['step'] == '检查初稿'
    chapters = db.chapters(book)
    assert [c['number'] for c in chapters] == [1, 2]
    assert chapters[0]['finalized_revision_id'] and not chapters[1]['finalized_revision_id']
    assert chapters[1]['body'] == ''


def test_stops_when_draft_leaks_author_notes(db):
    book = book_with_beats(db, range(1, 3))
    result = run(db, book, 1, model(drafts=[LONG_DRAFT + '作者备注：她还活着。']))
    assert result['done'] == []
    assert '作者备注' in result['stopped']['reason']


def test_stops_on_model_error_without_finalizing(db):
    book = book_with_beats(db, range(1, 3))
    result = run(db, book, 2, model(status=500))
    assert result['stopped']['step'] == '规划本章'
    assert db.memory(book) == []


def test_draft_check_flags_length_and_author_notes():
    assert check_draft('字' * 2000, 2000) == []
    assert check_draft('字' * 900, 2000) and check_draft('字' * 4100, 2000)
    assert any('作者备注' in p for p in check_draft('字' * 2000 + '作者备注：', 2000))
