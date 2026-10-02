import pytest

from novel.store import Conflict, Store, ValidationError


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'outline.sqlite3')


def book_with_volumes(db):
    book = db.create_book('夜班守则')
    db.save_volume(book, 1, '第一夜', 1, 30, '林默入职，活过第一周。')
    db.save_volume(book, 2, '梧桐路', 31, 60, '进入夜里的梧桐路。')
    return book


def beat(number, title=None, brief=None):
    return {'number': number, 'title': title or f'第{number}章', 'brief': brief or f'第{number}章要点'}


def test_volumes_are_listed_in_order_and_cannot_overlap(db):
    book = db.create_book('夜班守则')
    db.save_volume(book, 2, '梧桐路', 31, 60, '')
    db.save_volume(book, 1, '第一夜', 1, 30, '')
    assert [(v['number'], v['title']) for v in db.volumes(book)] == [(1, '第一夜'), (2, '梧桐路')]
    with pytest.raises(ValidationError, match='重叠'):
        db.save_volume(book, 3, '重叠卷', 25, 40, '')
    db.save_volume(book, 1, '第一夜（改）', 1, 30, '新目标')
    assert db.volumes(book)[0]['outline'] == '新目标'


def test_volume_for_finds_the_volume_containing_a_chapter(db):
    book = book_with_volumes(db)
    assert db.volume_for(book, 30)['number'] == 1
    assert db.volume_for(book, 31)['number'] == 2
    assert db.volume_for(book, 61) is None


def test_saving_beats_replaces_only_the_given_range(db):
    book = book_with_volumes(db)
    db.save_beats(book, 1, 30, [beat(1), beat(2), beat(3)])
    db.save_beats(book, 31, 60, [beat(31)])
    db.save_beats(book, 1, 30, [beat(1, '新标题'), beat(3)])
    assert [(b['number'], b['title']) for b in db.beats(book)] == [(1, '新标题'), (3, '第3章'), (31, '第31章')]
    with pytest.raises(ValidationError, match='范围'):
        db.save_beats(book, 1, 30, [beat(31)])


def test_adopting_beats_never_overwrites_existing_beats(db):
    book = book_with_volumes(db)
    db.save_beats(book, 1, 30, [beat(3, '作者写的')])
    with pytest.raises(Conflict):
        db.adopt_beats(book, [beat(3, 'AI 写的'), beat(4)], db.book(book)['context_version'])
    assert [(b['number'], b['title']) for b in db.beats(book)] == [(3, '作者写的')]
    db.adopt_beats(book, [beat(4), beat(5)], db.book(book)['context_version'])
    assert [b['number'] for b in db.beats(book)] == [3, 4, 5]


def test_adopting_beats_rejects_candidates_generated_before_outline_changed(db):
    book = book_with_volumes(db)
    version = db.book(book)['context_version']
    db.save_volume(book, 1, '第一夜', 1, 30, '卷目标改了')
    with pytest.raises(Conflict):
        db.adopt_beats(book, [beat(1)], version)


def test_beat_window_continues_after_last_beat_or_chapter_within_volume(db):
    book = book_with_volumes(db)
    assert db.beat_window(book, 10)[:2] == (1, 10)
    db.save_beats(book, 1, 30, [beat(n) for n in range(1, 13)])
    start, end, volume = db.beat_window(book, 10)
    assert (start, end, volume['number']) == (13, 22, 1)
    assert db.beat_window(book, 30)[:2] == (13, 30)
    for number in range(1, 16):
        db.create_chapter(book, f'第{number}章')
    assert db.beat_window(book, 5)[:2] == (16, 20)


def test_beat_window_requires_a_volume(db):
    book = db.create_book('没有分卷')
    with pytest.raises(ValidationError, match='分卷'):
        db.beat_window(book, 10)


def test_chapter_context_includes_volume_beat_and_next_beats(db):
    book = book_with_volumes(db)
    db.save_beats(book, 1, 30, [beat(n) for n in range(1, 8)])
    chapter = db.create_chapter(book, '第1章')
    context = db.context(chapter)
    assert context['volume']['title'] == '第一夜'
    assert context['beat']['number'] == 1
    assert [b['number'] for b in context['next_beats']] == [2, 3, 4]
    assert [v['title'] for v in context['volumes']] == ['第一夜', '梧桐路']


def test_outline_edits_invalidate_pending_ai_candidates(db):
    book = book_with_volumes(db)
    chapter = db.create_chapter(book, '第1章')
    revision = db.chapter(chapter)['current_revision_id']
    candidate = db.add_candidate(chapter, '第1章', '', 'AI 稿', revision, db.book(book)['context_version'])
    db.save_beats(book, 1, 30, [beat(1, '改过的细纲')])
    with pytest.raises(Conflict):
        db.adopt_candidate(chapter, candidate)


import json

import httpx

from novel.model import ModelClient, ModelConfig, parse_beats
from novel.writer import TASKS, Writer


def config():
    return ModelConfig('https://example.test/chat/completions', 'writing-model', 'test-secret')


def reply(text, finish='stop'):
    return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': finish}],
                                     'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})


def test_chapter_prompt_includes_volume_outline_and_nearby_beats(db):
    book = book_with_volumes(db)
    db.save_beats(book, 1, 30, [beat(n) for n in range(1, 7)])
    chapter = db.create_chapter(book, '第1章', '计划')
    _, _, _, messages = Writer(db, ModelClient(ModelConfig())).prepare(chapter, 'draft')
    payload = json.loads(messages[1]['content'])
    assert payload['本卷大纲']['outline'] == '林默入职，活过第一周。'
    assert payload['本章细纲']['title'] == '第1章'
    assert [b['number'] for b in payload['后续细纲']] == [2, 3, 4]
    assert [v['title'] for v in payload['分卷概览']] == ['第一夜', '梧桐路']


def test_plan_and_draft_use_later_beats_only_for_setup():
    assert '后续细纲' in TASKS['plan'] and '后续细纲' in TASKS['draft']


def test_beat_generation_returns_candidates_without_touching_beats(db):
    book = book_with_volumes(db)
    sent = {}

    def serve(request):
        sent.update(json.loads(json.loads(request.content)['messages'][1]['content']))
        return reply(json.dumps({'beats': [beat(1), beat(2)]}, ensure_ascii=False))
    result = Writer(db, ModelClient(config(), httpx.MockTransport(serve))).run_beats(book, 2)
    assert sent['续写范围'] == {'start': 1, 'end': 2}
    assert sent['本卷大纲']['title'] == '第一夜'
    assert [b['number'] for b in result['candidates']] == [1, 2]
    assert db.beats(book) == []
    run = db.runs(book)[0]
    assert (run['task'], run['status'], run['chapter_id']) == ('beats', '成功', None)


def test_beat_generation_with_wrong_chapter_numbers_is_not_adoptable(db):
    book = book_with_volumes(db)
    client = ModelClient(config(), httpx.MockTransport(lambda r: reply(json.dumps({'beats': [beat(5), beat(6)]}))))
    result = Writer(db, client).run_beats(book, 2)
    assert result['candidates'] is None
    assert db.runs(book)[0]['status'] == '结果待修正'


def test_parse_beats_accepts_code_fences_and_extra_fields():
    text = '```json\n{"beats":[{"number":3,"title":"灯牌","brief":"林默不关灯牌"}],"note":"x"}\n```'
    assert parse_beats(text, 3, 3) == [{'number': 3, 'title': '灯牌', 'brief': '林默不关灯牌'}]
    with pytest.raises(ValidationError):
        parse_beats(text, 3, 4)
