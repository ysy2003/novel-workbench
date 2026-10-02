import json

import httpx
import pytest

from novel.model import ModelClient, ModelConfig, ModelError, parse_memory
from novel.store import Store, ValidationError
from novel.writer import TASKS, Writer, select_facts, tail_text


def config():
    return ModelConfig('https://example.test/chat/completions', 'writing-model', 'test-secret')


def response(text, finish='stop'):
    return {'choices': [{'message': {'content': text}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 80, 'total_tokens': 180}}


def test_adapter_serializes_request_and_parses_usage():
    def serve(request):
        assert request.headers['Authorization'] == 'Bearer test-secret'
        body = json.loads(request.content)
        assert body['model'] == 'writing-model'
        assert body['messages'][0]['content'] == '写一段'
        return httpx.Response(200, json=response('雨落下来了。'))
    client = ModelClient(config(), httpx.MockTransport(serve))
    answer = client.generate([{'role': 'user', 'content': '写一段'}])
    assert answer.text == '雨落下来了。'
    assert answer.usage['total_tokens'] == 180


@pytest.mark.parametrize('status', [401, 429, 500, 302])
def test_errors_do_not_expose_credentials_or_response_body(status):
    client = ModelClient(config(), httpx.MockTransport(lambda request:
                         httpx.Response(status, text='test-secret echoed by remote')))
    with pytest.raises(ModelError) as error:
        client.generate([{'role': 'user', 'content': 'x'}])
    assert 'test-secret' not in str(error.value)


def test_memory_parser_requires_evidence_fields_and_rejects_non_json():
    with pytest.raises(ValidationError):
        parse_memory('我认为人物很好。')
    with pytest.raises(ValidationError):
        parse_memory('{"summary":"摘要","facts":[{"entity":"林川"}]}')
    data = parse_memory('```json\n{"summary":"收到信件","facts":[]}\n```')
    assert data['summary'] == '收到信件'


def setup_story(tmp_path, text='旧段落。保留的结尾。'):
    db = Store(tmp_path / 'book.sqlite3')
    book = db.create_book('夜灯')
    chapter = db.create_chapter(book, '灯火', '收到来信')
    rev = db.save_revision(chapter, '灯火', '收到来信', text, db.chapter(chapter)['current_revision_id'])
    return db, book, chapter, rev


def test_generation_preserves_manual_text_and_records_provenance(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    client = ModelClient(config(), httpx.MockTransport(lambda r: httpx.Response(200, json=response('新初稿。'))))
    result = Writer(db, client).run(chapter, 'draft', '让冲突更明显', 2000)
    assert db.chapter(chapter)['current_revision_id'] == rev
    assert db.revision(result['candidate_id'])['body'] == '新初稿。'
    run = db.runs(book)[0]
    assert run['status'] == '成功' and run['revision_id'] == rev
    assert 'test-secret' not in json.dumps(run)
    assert db.memory(book) == []


def test_local_rewrite_replaces_exactly_one_passage(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    client = ModelClient(config(), httpx.MockTransport(lambda r: httpx.Response(200, json=response('新段落。'))))
    result = Writer(db, client).run(chapter, 'rewrite', '增加动作', 2000, passage='旧段落。')
    assert db.revision(result['candidate_id'])['body'] == '新段落。保留的结尾。'
    with pytest.raises(ValidationError):
        Writer(db, client).run(chapter, 'rewrite', '', 2000, passage='不存在的段落')


def test_truncated_response_is_saved_but_not_adoptable(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    client = ModelClient(config(), httpx.MockTransport(lambda r: httpx.Response(200, json=response('写了一半', 'length'))))
    result = Writer(db, client).run(chapter, 'draft', '', 2000)
    assert result.get('candidate_id') is None
    assert db.runs(book)[0]['status'] == '内容截断'
    assert db.runs(book)[0]['output'] == '写了一半'
    assert db.chapter(chapter)['current_revision_id'] == rev


def test_failed_request_keeps_draft_and_marks_run(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    client = ModelClient(config(), httpx.MockTransport(lambda r: httpx.Response(401, text='private-token')))
    with pytest.raises(ModelError):
        Writer(db, client).run(chapter, 'draft', '', 2000)
    assert db.runs(book)[0]['status'] == '失败'
    assert db.chapter(chapter)['current_revision_id'] == rev


def serial_story(tmp_path, chapters, facts_for, plan):
    db = Store(tmp_path / 'long.sqlite3')
    book = db.create_book('长夜')
    for number in range(1, chapters + 1):
        chapter = db.create_chapter(book, f'第{number}夜')
        revision = db.save_revision(chapter, f'第{number}夜', '', f'第{number}夜的正文。',
                                    db.chapter(chapter)['current_revision_id'])
        db.finalize(chapter, revision, f'第{number}夜的过场。', facts_for(number), db.book(book)['context_version'])
    chapter = db.create_chapter(book, '新的一夜', plan)
    _, _, _, messages = Writer(db, ModelClient(ModelConfig())).prepare(chapter, 'draft')
    return json.loads(messages[1]['content']), messages


def test_prompt_sends_mentioned_entities_and_recent_updates_only(tmp_path):
    def facts_for(number):
        if number == 1:
            return [{'entity': '林川', 'attribute': '左臂', 'value': '受伤'},
                    {'entity': '苏晴', 'attribute': '位置', 'value': '北城'}]
        return [{'entity': f'路人{number}', 'attribute': '状态', 'value': '路过'}]
    payload, _ = serial_story(tmp_path, 5, facts_for, '林川出门找人。')
    assert {fact['entity'] for fact in payload['历史事实']} == {'林川', '路人3', '路人4', '路人5'}


def test_long_book_prompt_stays_within_limit(tmp_path):
    def facts_for(number):
        return [{'entity': f'人物{number}_{i}', 'attribute': '状态',
                 'value': '这是一条大约四十个字的事实描述，用来模拟真实记忆条目的长度。'} for i in range(10)]
    payload, messages = serial_story(tmp_path, 100, facts_for, '主角独自赶路。')
    assert len(payload['历史事实']) == 30
    assert len(messages[1]['content']) < 60000


def fact(entity, number):
    return {'entity': entity, 'attribute': f'属性{number}', 'value': '值' * 50,
            'chapter_number': number, 'revision_id': number}


def size(facts):
    return sum(len(json.dumps(f, ensure_ascii=False)) for f in facts)


def test_fact_budget_keeps_newest_mentioned_facts():
    facts = [fact('林川', n) for n in range(1, 11)]
    selected, omitted = select_facts(facts, '林川', [], budget=size(facts[-3:]))
    assert [f['chapter_number'] for f in selected] == [8, 9, 10]
    assert omitted == 7


def test_mentioned_facts_rank_above_recent_ones():
    facts = [fact('林川', 1), fact('路人', 10)]
    selected, omitted = select_facts(facts, '林川', [10], budget=size(facts[:1]))
    assert [f['entity'] for f in selected] == ['林川']
    assert omitted == 1


def test_prompt_includes_finalized_ending_of_previous_chapter(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path, '第一段。\n林川推开门，雨停了。')
    db.finalize(chapter, rev, '林川出门。', [], db.book(book)['context_version'])
    db.save_revision(chapter, '灯火', '收到来信', '定稿后又改的草稿结尾。', rev)
    second = db.create_chapter(book, '来客', '陌生人敲门')
    _, _, _, messages = Writer(db, ModelClient(ModelConfig())).prepare(second, 'draft')
    payload = json.loads(messages[1]['content'])
    assert payload['上一章结尾'] == {'chapter_number': 1, 'revision_id': rev, 'text': '第一段。\n林川推开门，雨停了。'}
    assert '上一章结尾' in messages[0]['content']


def test_first_chapter_has_no_previous_ending(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    _, _, _, messages = Writer(db, ModelClient(ModelConfig())).prepare(chapter, 'draft')
    assert json.loads(messages[1]['content'])['上一章结尾'] is None


def test_long_ending_starts_at_a_paragraph_boundary():
    text = '甲' * 600 + '\n' + '乙' * 300 + '\n' + '丙' * 100
    assert tail_text(text, 500) == '乙' * 300 + '\n' + '丙' * 100
    assert tail_text('甲' * 1000, 500) == '……' + '甲' * 500


def test_ai_draft_keeps_first_paragraph_indent(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    text = '　　新初稿。\n　　第二段。'
    client = ModelClient(config(), httpx.MockTransport(lambda r: httpx.Response(200, json=response(text))))
    result = Writer(db, client).run(chapter, 'draft', '', 2000)
    assert db.revision(result['candidate_id'])['body'] == text


def test_memory_extraction_does_not_see_chapter_plan(tmp_path):
    db, book, chapter, rev = setup_story(tmp_path)
    _, _, _, messages = Writer(db, ModelClient(ModelConfig())).prepare(chapter, 'memory')
    payload = json.loads(messages[1]['content'])
    assert payload['本章计划'] == ''
    assert payload['当前正文'] == '旧段落。保留的结尾。'


def test_plan_marks_author_notes_and_draft_keeps_them_out_of_prose():
    assert '作者备注' in TASKS['plan']
    assert '作者备注' in TASKS['draft']


def test_memory_task_asks_for_short_stable_entity_names_and_few_facts():
    assert '简短' in TASKS['memory'] and '15' in TASKS['memory']


def test_memory_parser_ignores_extra_top_level_fields():
    data = parse_memory('{"summary":"收到信件","facts":[],"summary_length":4}')
    assert data == {'summary': '收到信件', 'facts': []}


def capture_bodies():
    bodies = []

    def serve(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response('计划或正文。'))
    return bodies, serve


@pytest.mark.parametrize('setting, fields', [('low', {'reasoning_effort': 'low'}),
                                             ('off', {'thinking': {'type': 'disabled'}})])
def test_plan_requests_carry_the_configured_reasoning_setting_only(tmp_path, setting, fields):
    db, book, chapter, rev = setup_story(tmp_path)
    bodies, serve = capture_bodies()
    cfg = ModelConfig('https://example.test/chat/completions', 'writing-model', 'test-secret', plan_reasoning=setting)
    writer = Writer(db, ModelClient(cfg, httpx.MockTransport(serve)))
    writer.run(chapter, 'plan')
    writer.run(chapter, 'draft')
    assert {k: bodies[0][k] for k in fields} == fields
    assert not {'reasoning_effort', 'thinking'} & set(bodies[1])


def test_plan_reasoning_setting_is_read_from_env_and_validated(monkeypatch):
    monkeypatch.setenv('NOVEL_PLAN_REASONING', 'low')
    assert ModelConfig.from_env().plan_reasoning == 'low'
    with pytest.raises(ValidationError, match='NOVEL_PLAN_REASONING'):
        ModelConfig('https://example.test/chat/completions', 'm', 'k', plan_reasoning='medium').validate()
