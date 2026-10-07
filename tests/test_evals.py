import json

import httpx
import pytest

from novel import evals, writer
from novel.model import ModelClient, ModelConfig
from novel.store import Store, ValidationError
from novel.writer import TASKS

I = '　　'
STORY = {
    'id': 'mini',
    'book': {'title': '小岛', 'premise': '守塔员发现值班表多了一个名字。', 'genre': '悬疑', 'bible': '【周屿】守塔员。',
             'style': '短句。', 'outline': '【作者用】林望是第一任守塔员。'},
    'volume': {'title': '第一卷', 'start': 1, 'end': 30, 'outline': '查名字。'},
    'beats': [{'number': 1, 'title': '上岛', 'brief': '周屿上岛。'},
              {'number': 2, 'title': '签名', 'brief': '林望在值班表上签名。'},
              {'number': 3, 'title': '铁皮箱', 'brief': '铁皮箱里是工作证。'}],
    'chapters': [{'title': '上岛', 'paragraphs': ['船靠岸的时候，雾已经起来了。', '老周把值班表塞给周屿，转身上了船。'],
                  'summary': '周屿上岛接班。', 'facts': [{'entity': '周屿', 'attribute': '身份', 'value': '守塔员'}]}],
}
CASE = {'id': 'mini-2', 'story': 'mini', 'chapter': 2, 'focus': '测试用例',
        'checks': {'leaks': ['第一任守塔员'], 'names': {'周屿': ['周宇']}, 'future': ['工作证'],
                   'memory_required': [['林望', '签名|签字']], 'memory_forbidden': ['第一任', '坠塔']}}
CLEAN = I + '灯室里没有人。' + I + '值班表上，林望的签名还没干。周屿把笔放回抽屉。' * 20
MEMORY = {'summary': '林望在值班表上签名。', 'facts': [{'entity': '林望', 'attribute': '状态', 'value': '在值班表上签名'}]}


def client(draft=CLEAN, memory=MEMORY, finish='stop', seen=None):
    def serve(request):
        body = json.loads(request.content)
        system = body['messages'][0]['content']
        if seen is not None:
            seen.append(body)
        if TASKS['plan'] in system:
            text, end = '场景一：周屿上灯室。', 'stop'
        elif TASKS['draft'] in system:
            text, end = draft, finish
        else:
            text, end = json.dumps(memory, ensure_ascii=False), 'stop'
        return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': end}],
                                         'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}})
    return ModelClient(ModelConfig('https://example.test/chat/completions', 'm', 'k', plan_reasoning='low'),
                       httpx.MockTransport(serve))


def write_suite(directory, cases=(CASE,), stories=(STORY,)):
    (directory / 'stories').mkdir(parents=True, exist_ok=True)
    for story in stories:
        (directory / 'stories' / f'{story["id"]}.json').write_text(json.dumps(story, ensure_ascii=False), 'utf-8')
    (directory / 'cases.json').write_text(json.dumps(list(cases), ensure_ascii=False), 'utf-8')
    return directory


def test_repository_cases_are_valid():
    cases, stories = evals.load()
    assert len(cases) >= 6 and len(stories) >= 3
    assert {case['story'] for case in cases} <= set(stories)


@pytest.mark.parametrize('change, message', [
    ({'story': 'nope'}, '故事'), ({'chapter': 3}, '前'), ({'chapter': 9}, '细纲'),
    ({'checks': {'typo': []}}, '检查项'), ({'checks': {'names': ['周宇']}}, 'names')])
def test_invalid_cases_are_rejected(tmp_path, change, message):
    write_suite(tmp_path, cases=[{**CASE, **change}])
    with pytest.raises(ValidationError, match=message):
        evals.load(tmp_path)


def test_seed_builds_finalized_prior_chapters_and_the_target_chapter(tmp_path):
    store = Store(tmp_path / 'seed.sqlite3')
    book, chapter, previous = evals.seed(store, STORY, CASE)
    chapters = store.chapters(book)
    assert [(c['number'], bool(c['finalized_revision_id'])) for c in chapters] == [(1, True), (2, False)]
    assert store.revision(store.chapter(chapter)['current_revision_id'])['plan'] == '林望在值班表上签名。'
    assert previous.startswith(I + '船靠岸')
    assert store.context(chapter)['facts'][0]['value'] == '守塔员'


def test_measure_flags_leaks_names_future_and_memory():
    draft = '周宇上了灯室。他想起林望是第一任守塔员。作者备注：别写。铁皮箱里有工作证。' * 3
    memory = {'summary': '周宇查到第一任守塔员。', 'facts': [{'entity': '周宇', 'attribute': '知晓', 'value': '林望坠塔'}]}
    metrics = evals.measure(CASE['checks'], draft, memory, '', 1200)
    assert metrics['leaks'] == ['第一任守塔员'] and metrics['author_note']
    assert metrics['name_variants'] == ['周宇'] and metrics['future'] == ['工作证']
    assert metrics['memory_missing'] == ['林望 + 签名|签字'] and metrics['memory_recall'] == 0
    assert metrics['memory_forbidden'] == ['坠塔']  # “第一任”正文里已经写了，只算在“暗线未泄露”上
    checks = evals.evaluate(metrics)
    assert [key for key, ok in checks.items() if not ok] == ['no_leak', 'names', 'no_future', 'length',
                                                             'memory_recall', 'memory_truth']


def test_opening_overlap_detects_a_restated_previous_ending():
    previous = I + '老周把值班表塞给周屿，转身上了船。船开走了，雾越来越大。'
    assert evals.opening_overlap(I + '老周把值班表塞给周屿，转身上了船。周屿上楼。', previous) >= 0.5
    assert evals.opening_overlap(I + '灯室在塔顶，一百二十级台阶。', previous) == 0
    assert evals.opening_overlap('任何开头', '') == 0


def test_run_writes_one_record_per_sample_with_prompt_version(tmp_path):
    seen = []
    records = evals.run_eval('base', client(seen=seen), [CASE], {'mini': STORY}, repeat=2, words=300, workers=2,
                             results_dir=tmp_path, progress=lambda line: None)
    assert [r['repeat'] for r in sorted(records, key=lambda r: r['repeat'])] == [1, 2]
    record = records[0]
    assert record['passed'] and record['error'] is None and all(record['checks'].values())
    assert record['prompt_version'] == writer.prompt_version() and record['plan_reasoning'] == 'low'
    assert record['steps']['draft'] == {'status': '成功', 'elapsed': record['steps']['draft']['elapsed'],
                                        'prompt_tokens': 100, 'completion_tokens': 50}
    assert record['outputs']['draft'].startswith(I + '灯室里没有人')
    saved = evals.load_results('base', tmp_path)
    assert len(saved) == 2 and {r['label'] for r in saved} == {'base'}
    assert sum(TASKS['memory'] in body['messages'][0]['content'] for body in seen) == 2


def test_failed_step_is_recorded_and_the_sample_fails(tmp_path):
    record = evals.run_eval('cut', client(finish='length'), [CASE], {'mini': STORY}, repeat=1, words=300,
                            results_dir=tmp_path, progress=lambda line: None)[0]
    assert record['error']['step'] == 'draft' and '内容截断' in record['error']['message']
    assert not record['passed'] and record['metrics'] is None


def test_unparseable_memory_fails_the_format_check_without_stopping(tmp_path):
    record = evals.run_eval('mem', client(memory='不是 JSON'), [CASE], {'mini': STORY}, repeat=1, words=300,
                            results_dir=tmp_path, progress=lambda line: None)[0]
    assert record['error'] is None and record['steps']['memory']['status'] == '结果待修正'
    assert not record['checks']['memory_format'] and not record['passed']


def test_labels_are_checked_and_never_overwritten(tmp_path):
    evals.run_eval('base', client(), [CASE], {'mini': STORY}, repeat=1, words=300, results_dir=tmp_path,
                   progress=lambda line: None)
    for label in ('base', '../x', ''):
        with pytest.raises(ValidationError):
            evals.run_eval(label, client(), [CASE], {'mini': STORY}, repeat=1, words=300, results_dir=tmp_path,
                           progress=lambda line: None)


def test_report_and_comparison_show_pass_rates_failures_and_changes(tmp_path, monkeypatch):
    run = lambda label, model: evals.run_eval(label, model, [CASE], {'mini': STORY}, repeat=2, words=300,
                                              results_dir=tmp_path, progress=lambda line: None)
    good = run('good', client())
    bad = run('bad', client(draft=CLEAN + '他知道林望是第一任守塔员。'))
    monkeypatch.setenv('NOVEL_PRICE_INPUT', '2')
    monkeypatch.setenv('NOVEL_PRICE_OUTPUT', '8')
    report = evals.render_report(bad)
    assert '# 评测报告：bad' in report and '| 暗线未泄露 | 0/2 |' in report
    assert 'mini-2 #1 · 暗线未泄露：第一任守塔员' in report
    assert '0.0' in report and '元' in report
    comparison = evals.render_compare(good, bad)
    assert '| 暗线未泄露 | 2/2 | 0/2 | ⚠️ 变差 |' in comparison
    assert '| 全部通过 | 2/2 | 0/2 | ⚠️ 变差 |' in comparison


def test_reports_rescore_saved_outputs_with_the_current_rules(tmp_path):
    records = evals.run_eval('old', client(), [CASE], {'mini': STORY}, repeat=1, words=300, results_dir=tmp_path,
                             progress=lambda line: None)
    assert records[0]['passed']
    stricter = {**CASE, 'checks': {**CASE['checks'], 'future': ['值班表']}}
    rescored = evals.rescore(records, [stricter], {'mini': STORY})
    assert not rescored[0]['passed'] and rescored[0]['metrics']['future'] == ['值班表']
    assert rescored[0]['outputs'] == records[0]['outputs']
    unknown = evals.rescore(records, [], {'mini': STORY})
    assert unknown[0]['checks'] == records[0]['checks']


def test_prompt_version_follows_the_prompt_text(monkeypatch):
    before = writer.prompt_version()
    assert before == writer.prompt_version() and len(before) == 8
    monkeypatch.setitem(TASKS, 'draft', TASKS['draft'] + '再加一句。')
    assert writer.prompt_version() != before


def test_writer_records_the_prompt_version_on_each_run(tmp_path):
    store = Store(tmp_path / 'w.sqlite3')
    _, chapter, _ = evals.seed(store, STORY, CASE)
    result = writer.Writer(store, client()).run(chapter, 'plan', '', 300)
    assert store.run(result['run_id'])['prompt_version'] == writer.prompt_version()


def test_cases_command_lists_cases_without_a_model(capsys):
    assert evals.main(['cases']) == 0
    out = capsys.readouterr().out
    assert 'lighthouse-4' in out and '用例' in out
