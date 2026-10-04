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


def model(drafts=(LONG_DRAFT,), status=200, beats='ok', calls=None):
    drafts = list(drafts)

    def serve(request):
        if status != 200:
            return httpx.Response(status, text='error')
        messages = json.loads(request.content)['messages']
        system = messages[0]['content']
        if calls is not None:
            calls.append(system)
        if '章节细纲' in system:
            window = json.loads(messages[1]['content'])['续写范围']
            rows = [{'number': n, 'title': f'AI细纲{n}', 'brief': f'第{n}章AI要点'}
                    for n in range(window['start'], window['end'] + 1)]
            text = json.dumps({'beats': rows if beats == 'ok' else rows[1:]}, ensure_ascii=False)
        elif TASKS['plan'] in system:
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


def run(db, book, count, client=None, auto_beats=False):
    return autowrite(db, Writer(db, client or model()), book, count, target_words=2000, auto_beats=auto_beats)


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


def beat_numbers(db, book):
    return [b['number'] for b in db.beats(book)]


def test_generates_beats_when_none_are_left_and_keeps_writing(db):
    book = book_with_beats(db, [])
    result = run(db, book, 2, auto_beats=True)
    assert result['stopped'] is None
    assert result['beats'] == [{'start': 1, 'end': 10}]
    assert beat_numbers(db, book) == list(range(1, 11))
    assert [(c['number'], c['title']) for c in db.chapters(book)] == [(1, 'AI细纲1'), (2, 'AI细纲2')]


def test_tops_up_beats_to_keep_three_upcoming_beats_and_never_overwrites(db):
    book = book_with_beats(db, [1, 2, 3])
    result = run(db, book, 1, auto_beats=True)
    assert result['beats'] == [{'start': 4, 'end': 13}]
    assert [b['title'] for b in db.beats(book)][:3] == ['细纲1', '细纲2', '细纲3']
    assert db.chapters(book)[0]['title'] == '细纲1'


def test_failed_top_up_is_a_warning_when_the_chapter_already_has_a_beat(db):
    book = book_with_beats(db, [1, 2])
    calls = []
    result = run(db, book, 2, model(beats='bad', calls=calls), auto_beats=True)
    assert result['stopped'] is None
    assert [c['number'] for c in result['done']] == [1, 2]
    assert len(result['warnings']) == 1 and result['beats'] == []
    assert sum('章节细纲' in system for system in calls) == 1


def test_stops_when_beats_cannot_be_generated_for_a_chapter_without_one(db):
    book = book_with_beats(db, [])
    result = run(db, book, 1, model(beats='bad'), auto_beats=True)
    assert result['stopped']['step'] == '续写细纲'
    assert db.chapters(book) == []


def test_stops_when_no_volume_covers_the_next_chapter(db):
    book = db.create_book('没有下一卷')
    db.save_volume(book, 1, '第一夜', 1, 1, '')
    db.save_beats(book, 1, 1, [{'number': 1, 'title': '细纲1', 'brief': '要点'}])
    result = run(db, book, 2, auto_beats=True)
    assert [c['number'] for c in result['done']] == [1]
    assert result['stopped']['number'] == 2 and '分卷' in result['stopped']['reason']


def test_stops_at_a_gap_in_beats_that_top_up_cannot_fill(db):
    book = book_with_beats(db, [1, 2, 5, 6, 7, 8])
    result = run(db, book, 3, auto_beats=True)
    assert [c['number'] for c in result['done']] == [1, 2]
    assert result['stopped']['number'] == 3 and '第 3 章' in result['stopped']['reason']
