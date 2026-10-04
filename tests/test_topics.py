import json

import httpx
import pytest

from novel import analysis, research, topics
from novel.model import ModelClient, ModelConfig
from novel.store import Store, ValidationError


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'topics.sqlite3')


def proposal(title):
    return {'书名': title, '番茄分类': '悬疑脑洞', '一句话卖点': '夜班店员靠规则活下来', '主角': '缺钱的应届生',
            '金手指或核心设定': '能看见规则的代价', '差异化': '弱金手指解谜', '开篇三章': '入职、违规、反杀',
            '前30章节奏': '每 3 章一条规则', '风险': '爽点偏慢'}


DRAFT = {'书名': '夜班规则簿', '题材': '悬疑脑洞', '故事创意': '缺钱的应届生在夜班里靠规则活下去。',
         '人物与世界设定': '【主角】……', '总纲': '【全书主线】……【作者用】……', '写作偏好': '第三人称，短句',
         '第一卷': {'卷名': '第一夜', '起始章': 1, '结束章': 30, '卷大纲': '活过第一周'}}


def client(texts, seen=None):
    texts = list(texts)

    def serve(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        text = texts.pop(0) if len(texts) > 1 else texts[0]
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
    return ModelClient(ModelConfig('https://example.test/chat/completions', 'm', 'k'), httpx.MockTransport(serve))


def as_json(data):
    return json.dumps(data, ensure_ascii=False)


def seed_research(db):
    research.save_snapshot(db, '10-04 24:00', 1, [
        {'channel': '男频', 'board': '新书榜', 'category_id': 1, 'category': '悬疑脑洞', 'rank': 1, 'book_id': '111',
         'title': '什么叫我成了□世级怪谈？', 'title_complete': 0, 'reading': 504000, 'status': '连载中',
         'latest_chapter': '第79章', 'updated_at': ''}], [])
    research.save_details(db, '111', {'title': '什么叫我成了灭世级怪谈？', 'status': '连载中', 'tags': ['规则怪谈'],
                                      'word_count': 170000, 'intro': '简介', 'chapters': ['第1章']})
    with db.connect(True) as c:
        return c.execute('INSERT INTO reports(category,card_ids,report) VALUES(?,?,?)',
                         ('悬疑脑洞', '[1,2]', as_json({field: f'{field}内容' for field in analysis.REPORT_FIELDS}))).lastrowid


def propose(db, texts=None, seen=None, preferences='不写同人'):
    report = seed_research(db)
    texts = texts or [as_json({'方案': [proposal('甲'), proposal('乙'), proposal('丙')]})]
    return topics.propose(db, client(texts, seen), report, preferences=preferences)


def test_proposals_use_the_report_heat_and_preferences(db):
    seen = []
    proposal_id = propose(db, seen=seen)
    sent = json.loads(seen[0]['messages'][1]['content'])
    assert sent['题材报告']['差异化空间'] == '差异化空间内容'
    assert sent['作者偏好'] == '不写同人'
    assert sent['榜单热度']['头部作品'][0] == {'书名': '什么叫我成了灭世级怪谈？', '榜单': '男频新书榜', '在读（万）': 50.4}
    saved = topics.proposal_sets(db)[0]
    assert saved['id'] == proposal_id and [p['书名'] for p in saved['proposals']] == ['甲', '乙', '丙']


def test_proposals_need_the_requested_count_and_fields(db):
    with pytest.raises(ValidationError):
        propose(db, texts=[as_json({'方案': [proposal('甲'), proposal('乙')]})])
    broken = {k: v for k, v in proposal('丙').items() if k != '风险'}
    with pytest.raises(ValidationError):
        propose(db, texts=[as_json({'方案': [proposal('甲'), proposal('乙'), broken]})])
    assert topics.proposal_sets(db) == []


def test_prompts_ask_for_variety_and_forbid_copying_existing_works():
    for directive in (topics.PROPOSAL_DIRECTIVE, topics.DRAFT_DIRECTIVE):
        assert '不要使用现有作品' in directive and '同人' in directive
    assert '差异化空间' in topics.PROPOSAL_DIRECTIVE


def test_draft_expands_only_the_chosen_proposal(db):
    proposal_id = propose(db)
    seen = []
    draft_id = topics.draft_book(db, client([as_json(DRAFT)], seen), proposal_id, 1)
    sent = json.loads(seen[0]['messages'][1]['content'])
    assert sent['选题方案']['书名'] == '乙'
    draft = topics.drafts(db, proposal_id)[0]
    assert draft['id'] == draft_id and draft['index'] == 1
    assert draft['draft']['第一卷'] == {'卷名': '第一夜', '起始章': 1, '结束章': 30, '卷大纲': '活过第一周'}


def test_draft_volume_range_must_be_numbers(db):
    proposal_id = propose(db)
    bad = {**DRAFT, '第一卷': {**DRAFT['第一卷'], '起始章': '开头'}}
    with pytest.raises(ValidationError):
        topics.draft_book(db, client([as_json(bad)]), proposal_id, 0)


def fields_from(draft):
    volume = draft['第一卷']
    return {'title': draft['书名'], 'genre': draft['题材'], 'premise': draft['故事创意'], 'bible': draft['人物与世界设定'],
            'outline': draft['总纲'], 'style': draft['写作偏好'], 'volume_title': volume['卷名'],
            'volume_start': volume['起始章'], 'volume_end': volume['结束章'], 'volume_outline': volume['卷大纲']}


def test_creating_a_book_from_a_draft_sets_up_settings_and_first_volume_once(db):
    proposal_id = propose(db)
    draft_id = topics.draft_book(db, client([as_json(DRAFT)]), proposal_id, 0)
    fields = {**fields_from(DRAFT), 'title': '作者改过的书名'}
    book_id = topics.create_book(db, draft_id, **fields)
    book = db.book(book_id)
    assert (book['title'], book['genre'], book['style']) == ('作者改过的书名', '悬疑脑洞', '第三人称，短句')
    assert [(v['title'], v['start_chapter'], v['end_chapter']) for v in db.volumes(book_id)] == [('第一夜', 1, 30)]
    assert topics.drafts(db, proposal_id)[0]['book_id'] == book_id
    with pytest.raises(ValidationError, match='已经'):
        topics.create_book(db, draft_id, **fields)


def test_failed_draft_output_is_kept_for_diagnosis(db):
    proposal_id = propose(db)
    with pytest.raises(ValidationError):
        topics.draft_book(db, client(['不是 JSON']), proposal_id, 0)
    assert analysis.failures(db)[0]['task'] == '开书草稿'


def test_structured_draft_fields_become_readable_multiline_text():
    value = {'主角': '程晚照，入殓师', '主要配角': ['韩劲松', {'姓名': '纪云枝', '身份': '纸扎铺老板'}]}
    assert topics.to_text(value) == '【主角】程晚照，入殓师\n【主要配角】\n- 韩劲松\n- 姓名：纪云枝；身份：纸扎铺老板'
    assert topics.to_text('已经是文字') == '已经是文字'
