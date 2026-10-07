import json

import httpx
import pytest

from novel import polish
from novel.model import ModelClient, ModelConfig
from novel.store import Conflict, Store, ValidationError

BODY = ('　　林默嘴角勾起一抹弧度。他深吸一口气，眼中闪过一丝寒意。\n'
        '　　冷柜门开着——里面空无一物……\n'
        '　　这一刻，他终于明白了守则的意思。')


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'polish.sqlite3')


def chapter_with(db, body=BODY):
    book = db.create_book('夜班守则')
    chapter = db.create_chapter(book, '第一夜')
    db.save_revision(chapter, '第一夜', '计划', body, db.chapter(chapter)['current_revision_id'])
    return chapter


def client(text, seen=None, finish='stop'):
    def serve(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': finish}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
    return ModelClient(ModelConfig('https://example.test/chat/completions', 'm', 'k'), httpx.MockTransport(serve))


def suggestions(*items):
    return json.dumps({'建议': [{'原句': a, '改为': b, '理由': '套话'} for a, b in items]}, ensure_ascii=False)


def test_check_finds_cliches_once_with_their_sentence_and_density():
    report = polish.check(BODY)
    assert [h['label'] for h in report['hits']] == ['嘴角勾起/扬起', '一抹笑意/弧度', '深吸一口气', '眼中闪过一丝',
                                                    '这一刻', '终于明白']
    assert report['hits'][3]['sentence'] == '他深吸一口气，眼中闪过一丝寒意。'
    assert report['hits'][3]['text'] == '眼中闪过一丝'
    assert report['categories'] == {'套路表情': 4, '总结升华': 2}
    assert report['chars'] == polish.count_chars(BODY)
    assert report['density'] == round(6 * 1000 / report['chars'], 1)


def test_check_counts_punctuation_sentences_and_paragraphs():
    long = '他' + '慢' * 70 + '走。'
    report = polish.check('——……\n短句。\n' + long)
    assert report['dashes'] == round(1000 / report['chars'], 1)
    assert report['ellipses'] == round(1000 / report['chars'], 1)
    assert report['sentences']['count'] == 3
    assert report['sentences']['long'] == [long]
    assert report['paragraphs']['count'] == 3
    assert polish.check('')['density'] == 0


def test_check_flags_negated_actions_pauses_and_fragments_but_not_dialogue():
    text = '他没回头。她没有说话，顿了顿。门没有动静。只一秒。\n“没有。”'
    report = polish.check(text)
    assert [h['text'] for h in report['hits']] == ['没回头', '没有说话', '顿了顿']
    assert report['categories'] == {'否定描写': 2, '套路表情': 1}
    assert report['sentences']['fragments'] == ['他没回头。', '只一秒。']


def test_repeated_phrases_keep_the_longest_form():
    text = '他推开冷柜门。' * 3 + '她看了看表。'
    assert polish.repeated_phrases(text) == [{'phrase': '他推开冷柜门', 'count': 3}]


def test_findings_group_hits_by_sentence():
    rows = polish.findings(polish.check(BODY))
    assert rows[0] == {'原句': '林默嘴角勾起一抹弧度。', '问题': '嘴角勾起/扬起、一抹笑意/弧度'}
    assert len(rows) == 3


def test_suggestions_are_kept_in_the_run_and_marked_by_how_they_match(db):
    chapter = chapter_with(db)
    seen = []
    text = suggestions(('林默嘴角勾起一抹弧度。', '林默笑了一下。'), ('不存在的句子', '改'), ('他', '林默'))
    result = polish.suggest(db, client(text, seen), chapter, '少用比喻')
    sent = json.loads(seen[0]['messages'][1]['content'])
    assert sent['本章正文'] == BODY and sent['作者本次要求'] == '少用比喻'
    assert sent['体检发现'][0]['原句'] == '林默嘴角勾起一抹弧度。'
    assert '逐字一致' in seen[0]['messages'][0]['content']
    run = db.run(result['run_id'])
    assert run['task'] == polish.SUGGEST_TASK and run['status'] == '成功'
    rows = polish.annotate(BODY, polish.parse_suggestions(run['output']))
    assert [r['status'] for r in rows] == ['可用', '正文中找不到', '正文中出现多次']


def test_picked_suggestions_become_one_new_revision(db):
    chapter = chapter_with(db)
    text = suggestions(('林默嘴角勾起一抹弧度。', '林默笑了一下。'), ('他深吸一口气，', '他攥紧手电，'),
                       ('这一刻，他终于明白了守则的意思。', '他把守则又读了一遍。'))
    run_id = polish.suggest(db, client(text), chapter)['run_id']
    before = db.chapter(chapter)['current_revision_id']
    revision = polish.apply_suggestions(db, run_id, [2, 0])
    saved = db.revision(revision)
    assert saved['body'] == BODY.replace('林默嘴角勾起一抹弧度。', '林默笑了一下。').replace(
        '这一刻，他终于明白了守则的意思。', '他把守则又读了一遍。')
    assert saved['source'] == '采纳润色建议 2 处' and saved['base_revision_id'] == before
    assert saved['plan'] == '计划' and db.revision(before)['body'] == BODY


def test_suggestions_cannot_be_applied_after_the_body_changes(db):
    chapter = chapter_with(db)
    run_id = polish.suggest(db, client(suggestions(('林默嘴角勾起一抹弧度。', '林默笑了一下。'))), chapter)['run_id']
    current = db.chapter(chapter)['current_revision_id']
    db.save_revision(chapter, '第一夜', '计划', BODY + '\n　　门关上了。', current)
    with pytest.raises(Conflict):
        polish.apply_suggestions(db, run_id, [0])


def test_missing_repeated_or_overlapping_picks_are_rejected(db):
    chapter = chapter_with(db, BODY + '\n　　他深吸一口气，又吐出来。')
    text = suggestions(('不存在的句子', '改'), ('他深吸一口气，', '他停住，'),
                       ('眼中闪过一丝寒意。', '没说话。'), ('一丝寒意', '冷意'))
    run_id = polish.suggest(db, client(text), chapter)['run_id']
    for picks, message in (([0], '找不到'), ([1], '多次'), ([2, 3], '重叠'), ([], '勾选'), ([9], '不存在')):
        with pytest.raises(ValidationError, match=message):
            polish.apply_suggestions(db, run_id, picks)
    assert len(db.revisions(chapter)) == 2


def test_unparseable_or_truncated_suggestions_cannot_be_applied(db):
    chapter = chapter_with(db)
    broken = polish.suggest(db, client('这章写得不错。'), chapter)
    assert broken['suggestions'] is None and db.run(broken['run_id'])['status'] == '结果待修正'
    cut = polish.suggest(db, client(suggestions(('林默嘴角勾起一抹弧度。', '林默笑了。')), finish='length'), chapter)
    assert db.run(cut['run_id'])['status'] == '内容截断'
    for run_id in (broken['run_id'], cut['run_id']):
        with pytest.raises(ValidationError, match='不可用'):
            polish.apply_suggestions(db, run_id, [0])


def test_suggestion_parser_skips_unchanged_items_and_caps_the_list():
    items = [('  ', '空原句')] + [('句子' + str(n), '改' + str(n)) for n in range(40)] + [('不变', '不变')]
    rows = polish.parse_suggestions('说明：' + suggestions(*items))
    assert len(rows) == polish.MAX_SUGGESTIONS and rows[0] == {'原句': '句子0', '改为': '改0', '理由': '套话'}
    assert polish.parse_suggestions(json.dumps({'建议': []})) == []
    with pytest.raises(ValidationError):
        polish.parse_suggestions(json.dumps({'建议': [{'改为': '缺原句'}]}, ensure_ascii=False))


def test_polish_saves_a_candidate_that_must_be_adopted(db):
    chapter = chapter_with(db)
    seen = []
    polished = '　　林默笑了一下。\n　　冷柜门开着，里面是空的。'
    result = polish.polish(db, client(polished, seen), chapter, '去套话')
    system = seen[0]['messages'][0]['content']
    assert polish.POLISH_MODES['去套话'] in system and '只输出润色后的完整正文' in system
    assert json.loads(seen[0]['messages'][1]['content'])['体检发现']
    candidate = db.revision(result['candidate_id'])
    assert candidate['body'] == polished and candidate['source'] == 'AI 候选稿'
    assert db.run(result['run_id'])['candidate_id'] == candidate['id']
    assert db.revision(db.chapter(chapter)['current_revision_id'])['body'] == BODY
    db.adopt_candidate(chapter, candidate['id'], source=polish.ADOPT_SOURCE)
    assert db.revision(db.chapter(chapter)['current_revision_id'])['body'] == polished


def test_light_polish_sends_no_findings_and_custom_needs_instructions(db):
    chapter = chapter_with(db)
    seen = []
    polish.polish(db, client('润色稿', seen), chapter, '轻度润色')
    assert '体检发现' not in json.loads(seen[0]['messages'][1]['content'])
    with pytest.raises(ValidationError, match='要求'):
        polish.polish(db, client('润色稿'), chapter, '自定义要求', '  ')
    with pytest.raises(ValidationError):
        polish.polish(db, client('润色稿'), chapter, '重写成诗')


def test_empty_body_is_rejected_before_calling_the_model(db):
    chapter = chapter_with(db, '')
    with pytest.raises(ValidationError, match='正文'):
        polish.suggest(db, client('{}'), chapter)
    assert db.runs(db.chapter(chapter)['book_id']) == []


def test_changed_paragraphs_and_inline_diff():
    old = '甲段。\n乙段原文。\n丙段。'
    new = '甲段。\n乙段改文。\n丙段。\n丁段。'
    assert polish.changed_paragraphs(old, new) == [{'old': '乙段原文。', 'new': '乙段改文。'}, {'old': '', 'new': '丁段。'}]
    assert polish.inline_diff('乙段原文。', '乙段改文。') == [('same', '乙段'), ('removed', '原'), ('added', '改'),
                                                         ('same', '文。')]
    assert polish.inline_diff('林川不由得站了起来。', '林川从柜台后站起来。') == [
        ('same', '林川'), ('removed', '不由得站了'), ('added', '从柜台后站'), ('same', '起来。')]


def test_book_report_lists_every_chapter_with_its_current_source(db):
    chapter = chapter_with(db)
    book = db.chapter(chapter)['book_id']
    db.create_chapter(book, '第二夜')
    rows = polish.book_report(db, book)
    assert [(r['number'], r['title'], r['hits'], r['source']) for r in rows] == [(1, '第一夜', 6, '手工稿'),
                                                                                 (2, '第二夜', 0, '手工稿')]
