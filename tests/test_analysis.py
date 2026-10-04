import json
import sqlite3

import httpx
import pytest

from novel import analysis, research
from novel.model import ModelClient, ModelConfig, ModelError
from novel.store import Store, ValidationError

SAMPLE = '独一无二的样本原文标记甲乙丙。' + '林川推开门，雨停了。' * 200
CARD = {'为什么火': '规则怪谈正热，开篇就有生存压力', '书名套路': '“规则”加身份', '简介写法': '先抛规则再抛悬念',
        '开篇钩子': '第一句就让主角面临失业和死亡威胁', '核心卖点': '规则怪谈加推理',
        '设定亮出': '第1章结尾亮出守则', '主角人设': '缺钱、冷静、记性好', '冲突结构': '守则真假与生存压力',
        '爽点分布': [{'章节': '第2章', '类型': '识破假规则', '内容': '没有关灯牌而活下来'}],
        '章末钩子': ['即时危险', '新线索'], '节奏': '每章验证一条规则', '可借鉴': ['每章一条规则的验证结构'],
        '不可照搬': ['守则具体条文', '人物名字']}


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'analysis.sqlite3')


def client(text=None, status=200, seen=None):
    def serve(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, text='error')
        content = text if text is not None else json.dumps(CARD, ensure_ascii=False)
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
    config = ModelConfig('https://example.test/chat/completions', 'writing-model', 'test-secret')
    return ModelClient(config, httpx.MockTransport(serve))


def analyze(db, model, text=SAMPLE):
    return analysis.analyze(db, model, title='夜班守则', text=text, source='第1–3章正文',
                            category='悬疑脑洞', book_id='111')


def test_card_is_saved_without_the_sample_text(db, tmp_path):
    seen = []
    card_id = analyze(db, client(seen=seen))
    card = analysis.cards(db)[0]
    assert card['id'] == card_id and card['title'] == '夜班守则' and card['sample_chars'] == len(SAMPLE)
    assert card['card']['核心卖点'] == '规则怪谈加推理'
    assert '独一无二的样本原文标记' in seen[0]['messages'][1]['content']
    dump = '\n'.join(sqlite3.connect(tmp_path / 'analysis.sqlite3').iterdump())
    assert '独一无二的样本原文标记' not in dump


def test_prompt_limits_the_model_to_the_sample():
    assert '只依据' in analysis.DIRECTIVE and '不可照搬' in analysis.DIRECTIVE


def test_card_parser_requires_every_field_and_accepts_code_fences():
    text = '```json\n' + json.dumps({**CARD, '额外': 1}, ensure_ascii=False) + '\n```'
    assert analysis.parse_card(text)['节奏'] == '每章验证一条规则'
    with pytest.raises(ValidationError):
        analysis.parse_card(json.dumps({k: v for k, v in CARD.items() if k != '节奏'}, ensure_ascii=False))


def test_bad_output_or_failed_request_saves_nothing(db):
    with pytest.raises(ValidationError):
        analyze(db, client(text='这本书很好看。'))
    with pytest.raises(ModelError):
        analyze(db, client(status=500))
    assert analysis.cards(db) == []


def test_sample_must_be_present_and_not_too_long(db):
    with pytest.raises(ValidationError):
        analyze(db, client(), text='  ')
    with pytest.raises(ValidationError):
        analyze(db, client(), text='字' * (analysis.MAX_SAMPLE + 1))


def test_cards_can_be_deleted(db):
    card_id = analyze(db, client())
    analysis.delete_card(db, card_id)
    assert analysis.cards(db) == []


REPORT = {'共同卖点': ['开局即危机'], '书名公式': ['题材：主角做了什么，震惊谁'], '人设模板': ['冷静的穿越者'],
          '开篇规律': '第1章绝境、第2章获得依靠', '爽点节奏': '每 2–3 章一次', '差异化空间': ['女性视角的怪谈'],
          '新书建议': ['前 3 章亮出核心设定']}


def store_details(db, book_id='111'):
    research.save_details(db, book_id, {'title': '盗墓：八岁创雷法', 'status': '连载中', 'tags': ['盗墓', '穿越'],
                                        'word_count': 177000, 'intro': '张海歌穿越盗墓世界，开局被屠村。',
                                        'chapters': ['第1章 乱世绝境，琪师收徒', '第2章 归途赐名，三年团宠']})


def test_book_analysis_reads_details_and_keeps_extra_sample_out_of_storage(db, tmp_path):
    store_details(db)
    seen = []
    analysis.analyze_book(db, client(seen=seen), '111', category='悬疑灵异', extra_text='补充样本标记丁戊己。正文……')
    sent = seen[0]['messages'][1]['content']
    assert '开局被屠村' in sent and '第2章 归途赐名，三年团宠' in sent and '补充样本标记丁戊己' in sent
    card = analysis.cards(db)[0]
    assert (card['book_id'], card['title'], card['category']) == ('111', '盗墓：八岁创雷法', '悬疑灵异')
    assert '目录 2 章' in card['source'] and card['card']['为什么火']
    dump = '\n'.join(sqlite3.connect(tmp_path / 'analysis.sqlite3').iterdump())
    assert '补充样本标记丁戊己' not in dump


def test_book_analysis_needs_fetched_details(db):
    with pytest.raises(ValidationError, match='详情'):
        analysis.analyze_book(db, client(), '999')


def test_category_report_compares_cards_of_one_category(db):
    store_details(db, '111')
    store_details(db, '222')
    analysis.analyze_book(db, client(), '111', category='悬疑灵异')
    analysis.analyze_book(db, client(), '222', category='悬疑灵异')
    seen = []
    analysis.category_report(db, client(text=json.dumps(REPORT, ensure_ascii=False), seen=seen), '悬疑灵异')
    sent = json.loads(seen[0]['messages'][1]['content'])
    assert len(sent['分析卡']) == 2
    report = analysis.reports(db)[0]
    assert report['category'] == '悬疑灵异' and report['report']['差异化空间'] == ['女性视角的怪谈']


def test_category_report_needs_at_least_two_cards(db):
    store_details(db)
    analysis.analyze_book(db, client(), '111', category='悬疑灵异')
    with pytest.raises(ValidationError, match='至少'):
        analysis.category_report(db, client(), '悬疑灵异')


def test_card_parser_tolerates_objects_numbers_nested_lists_and_nulls():
    loose = {**CARD, '设定亮出': {'章节': '第1章', '方式': '规则纸条'}, '节奏': 3,
             '爽点分布': [['第2章', '识破假规则']], '书名套路': None}
    card = analysis.parse_card(json.dumps(loose, ensure_ascii=False))
    assert card['设定亮出'] == {'章节': '第1章', '方式': '规则纸条'}
    assert card['节奏'] == '3'
    assert card['爽点分布'] == ['第2章 · 识破假规则']
    assert card['书名套路'] == '材料未体现'
