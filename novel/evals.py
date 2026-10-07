"""评测框架：用固定的示例故事跑真实的规划、初稿、记忆提取流程，按可重复的规则打分，比较提示词或模型的改动。

每个样本在独立的临时数据库里运行，不碰作者的书库。原始输出存在 data/evals/<标签>.jsonl（不入库），
报告写到 evals/reports/<标签>.md。规则检查不调用模型；只有 run 命令会调用模型并产生费用。

用法：
    python -m novel.evals cases                       列出用例（不调用模型）
    python -m novel.evals run --label baseline        跑全部用例，每个默认重复 2 次，结束后写报告
    python -m novel.evals report baseline             重新生成报告
    python -m novel.evals compare baseline v2         对比两次评测
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from novel import polish
from novel.model import ModelError, parse_memory
from novel.store import Store, ValidationError
from novel.writer import Writer, prompt_version, tail_text

ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = ROOT / 'evals'
RESULTS_DIR = ROOT / 'data' / 'evals'
REPORTS_DIR = SUITE_DIR / 'reports'
LABEL = re.compile(r'^[A-Za-z0-9_.-]{1,40}$')
CHECK_KEYS = {'leaks', 'names', 'future', 'memory_required', 'memory_forbidden'}
OPENING_CHARS = 200
SHINGLE = 5
OVERLAP_LIMIT = 0.3
MAX_FACTS = 15
I = '　　'

# 规则检查：（键，名称，是否通过）。全部通过才算这个样本通过。
CHECKS = (
    ('no_leak', '暗线未泄露', lambda m: not m['leaks'] and not m['author_note']),
    ('names', '人名一致', lambda m: not m['name_variants']),
    ('no_future', '没提前写后续', lambda m: not m['future']),
    ('opening', '开头不复述', lambda m: m['opening_overlap'] < OVERLAP_LIMIT),
    ('length', '字数在范围内', lambda m: 0.5 <= m['length_ratio'] <= 2),
    ('memory_format', '记忆格式正确', lambda m: m['memory_ok'] and m['memory_facts'] <= MAX_FACTS),
    ('memory_recall', '记忆覆盖', lambda m: not m['memory_missing']),
    ('memory_truth', '记忆没有剧透', lambda m: not m['memory_forbidden']),
)
# 数值指标：（键，名称，方向）。方向 lower 表示越小越好，higher 越大越好，None 不判断好坏。
NUMBERS = (
    ('length_ratio', '字数 / 目标', None),
    ('opening_overlap', '开头与上一章结尾的重合', 'lower'),
    ('cliche_density', '每千字套话', 'lower'),
    ('fragments_per_k', '每千字碎句', 'lower'),
    ('memory_facts', '记忆条数', None),
    ('memory_recall', '记忆覆盖率', 'higher'),
)
EVIDENCE = {'no_leak': lambda m: '、'.join(m['leaks'] + (['作者备注'] if m['author_note'] else [])),
            'names': lambda m: '、'.join(m['name_variants']),
            'no_future': lambda m: '、'.join(m['future']),
            'opening': lambda m: f'重合 {m["opening_overlap"]}',
            'length': lambda m: f'{m["chars"]} 字，目标的 {m["length_ratio"]} 倍',
            'memory_format': lambda m: f'{m["memory_facts"]} 条' if m['memory_ok'] else '解析失败',
            'memory_recall': lambda m: '缺 ' + '；'.join(m['memory_missing']),
            'memory_truth': lambda m: '、'.join(m['memory_forbidden'])}


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def body_of(chapter):
    return '\n'.join(I + p for p in chapter['paragraphs'])


def load(directory=SUITE_DIR):
    """读取并校验 stories/*.json 和 cases.json，返回（用例列表，{故事 ID: 故事}）。"""
    directory = Path(directory)
    stories = {}
    for path in sorted((directory / 'stories').glob('*.json')):
        story = json.loads(path.read_text('utf-8'))
        _require({'id', 'book', 'volume', 'beats', 'chapters'} <= set(story), f'{path.name} 缺少故事字段。')
        stories[story['id']] = story
    cases = json.loads((directory / 'cases.json').read_text('utf-8'))
    seen = set()
    for case in cases:
        name = case.get('id')
        _require(name and name not in seen, f'用例 ID 缺失或重复：{name}')
        seen.add(name)
        story = stories.get(case.get('story'))
        _require(story, f'用例 {name} 引用的故事 {case.get("story")} 不存在。')
        number = case.get('chapter')
        _require(isinstance(number, int) and any(b['number'] == number for b in story['beats']),
                 f'用例 {name} 要写的第 {number} 章没有细纲。')
        _require(len(story['chapters']) >= number - 1,
                 f'用例 {name} 需要前 {number - 1} 章正文，故事只有 {len(story["chapters"])} 章。')
        checks = case.get('checks', {})
        _require(set(checks) <= CHECK_KEYS, f'用例 {name} 有未知的检查项：{sorted(set(checks) - CHECK_KEYS)}')
        _require(isinstance(checks.get('names', {}), dict), f'用例 {name} 的检查项 names 需要是“规范名: [错误写法]”。')
        _require(all(isinstance(item, list) for item in checks.get('memory_required', [])),
                 f'用例 {name} 的检查项 memory_required 每一项需要是关键词列表。')
    return cases, stories


def seed(store, story, case):
    """在空数据库里建好设定、分卷、细纲和前面已定稿的章节，再按细纲建好要写的那一章。"""
    book = story['book']
    book_id = store.create_book(book['title'], book['premise'], book['genre'])
    store.update_book(book_id, **{k: book[k] for k in ('title', 'premise', 'genre', 'bible', 'style', 'outline')})
    volume = story['volume']
    store.save_volume(book_id, 1, volume['title'], volume['start'], volume['end'], volume['outline'])
    store.save_beats(book_id, volume['start'], volume['end'], story['beats'])
    previous = ''
    for chapter in story['chapters'][:case['chapter'] - 1]:
        chapter_id = store.create_chapter(book_id, chapter['title'], chapter.get('plan', ''))
        previous = body_of(chapter)
        revision = store.save_revision(chapter_id, chapter['title'], chapter.get('plan', ''), previous,
                                       store.chapter(chapter_id)['current_revision_id'])
        store.finalize(chapter_id, revision, chapter['summary'], chapter['facts'], store.book(book_id)['context_version'])
    beat = next(b for b in story['beats'] if b['number'] == case['chapter'])
    return book_id, store.create_chapter(book_id, beat['title'], beat['brief']), previous


def contains(text, keyword):
    """关键词可以用 | 写几种说法，任一出现即算出现。"""
    return any(part in text for part in keyword.split('|'))


def shingles(text):
    text = re.sub(r'\s', '', text)
    return {text[i:i + SHINGLE] for i in range(len(text) - SHINGLE + 1)}


def opening_overlap(draft, previous_body):
    """正文开头 200 字里，有多大比例的 5 字片段出现在上一章结尾里；高说明开头在复述上一章。"""
    opening = shingles(re.sub(r'\s', '', draft)[:OPENING_CHARS])
    if not opening or not previous_body.strip():
        return 0.0
    return round(len(opening & shingles(tail_text(previous_body))) / len(opening), 2)


def measure(checks, draft, memory, previous_body, words):
    chars = polish.count_chars(draft)
    style = polish.check(draft)
    facts = memory['facts'] if memory else []
    fact_texts = [f['entity'] + f['attribute'] + f['value'] for f in facts]
    memory_text = '\n'.join(fact_texts + ([memory['summary']] if memory else []))
    required = checks.get('memory_required', [])
    missing = [' + '.join(item) for item in required
               if not any(all(contains(text, keyword) for keyword in item) for text in fact_texts)]
    return {'chars': chars, 'length_ratio': round(chars / words, 2),
            'leaks': [k for k in checks.get('leaks', []) if contains(draft, k)],
            'author_note': '作者备注' in draft,
            'name_variants': sorted({v for variants in checks.get('names', {}).values() for v in variants
                                     if v in draft + memory_text}),
            'future': [k for k in checks.get('future', []) if contains(draft, k)],
            'opening_overlap': opening_overlap(draft, previous_body),
            'cliche_density': style['density'],
            'fragments_per_k': polish.per_thousand(len(style['sentences']['fragments']), chars),
            'memory_ok': memory is not None, 'memory_facts': len(facts),
            'memory_recall': round(1 - len(missing) / len(required), 2) if required else 1.0,
            'memory_missing': missing,
            # 只算正文里没有的：正文已经写了的，算在“暗线未泄露”或“没提前写后续”上，不重复扣分
            'memory_forbidden': [k for k in checks.get('memory_forbidden', [])
                                 if contains(memory_text, k) and not contains(draft, k)]}


def evaluate(metrics):
    return {key: bool(passes(metrics)) for key, _, passes in CHECKS}


class StepFailed(Exception):
    pass


def _step(store, writer, record, chapter_id, task, words, allowed=('成功',)):
    result = writer.run(chapter_id, task, '', words)
    run = store.run(result['run_id'])
    usage = json.loads(run['usage'])
    record['steps'][task] = {'status': run['status'], 'elapsed': round(run['elapsed'], 1),
                             'prompt_tokens': usage.get('prompt_tokens', 0),
                             'completion_tokens': usage.get('completion_tokens', 0)}
    record['outputs'][task] = run['output']
    if run['status'] not in allowed:
        raise StepFailed(f'{run["status"]}：{run["error"]}' if run['error'] else run['status'])
    return result


def run_sample(case, story, client, words, repeat=1):
    record = {'case': case['id'], 'story': story['id'], 'chapter': case['chapter'], 'focus': case.get('focus', ''),
              'repeat': repeat, 'words': words, 'steps': {}, 'outputs': {}, 'error': None,
              'metrics': None, 'checks': {}, 'passed': False}
    with tempfile.TemporaryDirectory(prefix='novel-eval-') as workdir:
        store = Store(Path(workdir) / 'eval.sqlite3')
        _, chapter_id, previous = seed(store, story, case)
        writer = Writer(store, client)
        step, draft, memory = 'plan', None, None
        try:
            plan = _step(store, writer, record, chapter_id, 'plan', words)['text']
            current = store.revision(store.chapter(chapter_id)['current_revision_id'])
            store.save_revision(chapter_id, current['title'], plan, current['body'], current['id'])
            step = 'draft'
            candidate = _step(store, writer, record, chapter_id, 'draft', words)['candidate_id']
            store.adopt_candidate(chapter_id, candidate)
            draft = store.revision(candidate)['body']
            record['outputs']['draft'] = draft
            step = 'memory'
            memory = _step(store, writer, record, chapter_id, 'memory', words, ('成功', '结果待修正')).get('memory')
        except (ModelError, ValidationError, StepFailed) as error:
            record['error'] = {'step': step, 'message': str(error)}
    if draft is not None:
        record['metrics'] = measure(case.get('checks', {}), draft, memory, previous, words)
        record['checks'] = evaluate(record['metrics'])
    record['passed'] = record['error'] is None and bool(record['checks']) and all(record['checks'].values())
    return record


def rescore(records, cases, stories):
    """按当前的用例和规则，用保存的原始输出重新打分；规则改了，旧结果也能放在同一把尺子下比较。"""
    cases = {case['id']: case for case in cases}
    result = []
    for record in records:
        case = cases.get(record['case'])
        draft = record['outputs'].get('draft')
        if case is None or record['metrics'] is None or draft is None:
            result.append(record)
            continue
        story = stories[case['story']]
        previous = body_of(story['chapters'][case['chapter'] - 2]) if case['chapter'] > 1 else ''
        try:
            memory = parse_memory(record['outputs'].get('memory', ''))
        except ValidationError:
            memory = None
        metrics = measure(case.get('checks', {}), draft, memory, previous, record['words'])
        checks = evaluate(metrics)
        result.append({**record, 'metrics': metrics, 'checks': checks,
                       'passed': record['error'] is None and all(checks.values())})
    return result


def results_path(label, results_dir=RESULTS_DIR):
    _require(isinstance(label, str) and LABEL.match(label) and label not in ('.', '..'),
             '标签只能用字母、数字、点、下划线和连字符，最长 40 个字符。')
    return Path(results_dir) / f'{label}.jsonl'


def run_eval(label, client, cases, stories, repeat=2, words=1200, workers=3, results_dir=RESULTS_DIR, progress=print):
    """逐个样本运行并追加写入 JSONL；同一个标签不覆盖。"""
    path = results_path(label, results_dir)
    _require(not path.exists(), f'标签 {label} 已经有评测结果，请换一个标签。')
    client.config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {'label': label, 'prompt_version': prompt_version(), 'model': client.config.model,
            'plan_reasoning': client.config.plan_reasoning or '默认',
            'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    jobs = [(case, n) for case in cases for n in range(1, repeat + 1)]
    lock = threading.Lock()

    def work(job):
        case, n = job
        started = time.monotonic()
        record = {**meta, **run_sample(case, stories[case['story']], client, words, n)}
        with lock:
            with path.open('a', encoding='utf-8') as file:
                file.write(json.dumps(record, ensure_ascii=False) + '\n')
            failed = [label for key, label, _ in CHECKS if not record['checks'].get(key, True)]
            outcome = '通过' if record['passed'] else ('出错：' + record['error']['message'] if record['error']
                                                      else '未通过：' + '、'.join(failed))
            progress(f'{case["id"]} #{n} · {round(time.monotonic() - started)} 秒 · {outcome}')
        return record

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(work, jobs))


def load_results(label, results_dir=RESULTS_DIR):
    path = results_path(label, results_dir)
    _require(path.exists(), f'没有找到标签 {label} 的评测结果。')
    return [json.loads(line) for line in path.read_text('utf-8').splitlines() if line.strip()]


def prices():
    """每百万 tokens 的输入、输出价格（元），从 NOVEL_PRICE_INPUT / NOVEL_PRICE_OUTPUT 读取；没填就不算费用。"""
    try:
        values = [float(os.environ[name]) for name in ('NOVEL_PRICE_INPUT', 'NOVEL_PRICE_OUTPUT')]
        return values if all(v >= 0 for v in values) else None
    except (KeyError, ValueError):
        return None


def summarize(records):
    total = len(records)
    measured = [r for r in records if r['metrics']]
    steps = {}
    for record in records:
        for task, step in record['steps'].items():
            row = steps.setdefault(task, {'count': 0, 'elapsed': 0.0, 'prompt_tokens': 0, 'completion_tokens': 0})
            row['count'] += 1
            for key in ('elapsed', 'prompt_tokens', 'completion_tokens'):
                row[key] += step[key]
    price = prices()
    tokens = [sum(s[key] for s in steps.values()) for key in ('prompt_tokens', 'completion_tokens')]
    return {'total': total, 'passed': sum(r['passed'] for r in records),
            'errors': sum(r['error'] is not None for r in records),
            'checks': {key: sum(r['checks'].get(key, False) for r in records) for key, _, _ in CHECKS},
            'numbers': {key: round(sum(r['metrics'][key] for r in measured) / len(measured), 2) if measured else None
                        for key, _, _ in NUMBERS},
            'steps': steps, 'tokens': tokens,
            'cost': round((tokens[0] * price[0] + tokens[1] * price[1]) / 1e6, 4) if price else None}


def _meta(records):
    first = records[0] if records else {}
    return {key: first.get(key, '') for key in ('label', 'prompt_version', 'model', 'plan_reasoning', 'started_at', 'words')}


def render_report(records):
    meta, summary = _meta(records), summarize(records)
    total = summary['total']
    lines = [f'# 评测报告：{meta["label"]}', '',
             f'- 时间：{meta["started_at"]}；提示词版本 `{meta["prompt_version"]}`；模型 {meta["model"]}；'
             f'规划思考强度 {meta["plan_reasoning"]}；目标字数 {meta["words"]}',
             f'- 样本 {total} 个（{len({r["case"] for r in records})} 个用例），全部检查通过 {summary["passed"]} 个，'
             f'中途出错 {summary["errors"]} 个',
             '- 按当前的用例和规则打分（`report` / `compare` 会用保存的原始输出重新打分）', '',
             '## 规则检查', '', '| 检查 | 通过 |', '| --- | --- |',
             f'| 全部通过 | {summary["passed"]}/{total} |']
    lines += [f'| {name} | {summary["checks"][key]}/{total} |' for key, name, _ in CHECKS]
    lines += ['', '## 数值指标（平均）', '', '| 指标 | 平均 |', '| --- | --- |']
    lines += [f'| {name} | {summary["numbers"][key]} |' for key, name, _ in NUMBERS]
    lines += ['', '## 用时与用量', '', '| 步骤 | 次数 | 平均耗时（秒） | 输入 tokens | 输出 tokens |',
              '| --- | --- | --- | --- | --- |']
    lines += [f'| {task} | {s["count"]} | {round(s["elapsed"] / s["count"], 1)} | {s["prompt_tokens"]} | '
              f'{s["completion_tokens"]} |' for task, s in summary['steps'].items()]
    cost = f'约 {summary["cost"]} 元' if summary['cost'] is not None else '未填写价格（NOVEL_PRICE_INPUT / NOVEL_PRICE_OUTPUT），没有估算'
    lines += ['', f'费用：{cost}。', '', '## 按用例', '', '| 用例 | 关注点 | 全部通过 | 未通过的检查 |', '| --- | --- | --- | --- |']
    for case in dict.fromkeys(r['case'] for r in records):
        rows = [r for r in records if r['case'] == case]
        failed = sorted({name for r in rows for key, name, _ in CHECKS if not r['checks'].get(key, True)}
                        | {f'{r["error"]["step"]} 出错' for r in rows if r['error']})
        lines.append(f'| {case} | {rows[0]["focus"]} | {sum(r["passed"] for r in rows)}/{len(rows)} | '
                     f'{"、".join(failed) or "—"} |')
    lines += ['', '## 未通过明细', '']
    details = []
    for r in sorted(records, key=lambda r: (r['case'], r['repeat'])):
        if r['error']:
            details.append(f'- {r["case"]} #{r["repeat"]} · {r["error"]["step"]} 出错：{r["error"]["message"]}')
        for key, name, _ in CHECKS:
            if not r['checks'].get(key, True):
                details.append(f'- {r["case"]} #{r["repeat"]} · {name}：{EVIDENCE[key](r["metrics"])}')
    lines += details or ['全部通过。']
    return '\n'.join(lines) + '\n'


def _change(a, b, direction):
    if a is None or b is None or a == b:
        return '—'
    delta = f'{b - a:+.2f}'
    if direction is None:
        return delta
    better = b < a if direction == 'lower' else b > a
    return ('✅ 变好' if better else '⚠️ 变差') + f'（{delta}）'


def render_compare(a_records, b_records):
    a, b = summarize(a_records), summarize(b_records)
    ma, mb = _meta(a_records), _meta(b_records)
    rate = lambda passed, total: passed / total if total else 0
    verdict = lambda pa, ta, pb, tb: ('—' if rate(pa, ta) == rate(pb, tb)
                                      else '✅ 变好' if rate(pb, tb) > rate(pa, ta) else '⚠️ 变差')
    lines = [f'# 评测对比：{ma["label"]} → {mb["label"]}', '',
             f'- 提示词版本 `{ma["prompt_version"]}` → `{mb["prompt_version"]}`；模型 {ma["model"]} → {mb["model"]}；'
             f'规划思考强度 {ma["plan_reasoning"]} → {mb["plan_reasoning"]}', '',
             '| 检查 | ' + ma['label'] + ' | ' + mb['label'] + ' | 变化 |', '| --- | --- | --- | --- |',
             f'| 全部通过 | {a["passed"]}/{a["total"]} | {b["passed"]}/{b["total"]} | '
             f'{verdict(a["passed"], a["total"], b["passed"], b["total"])} |']
    lines += [f'| {name} | {a["checks"][key]}/{a["total"]} | {b["checks"][key]}/{b["total"]} | '
              f'{verdict(a["checks"][key], a["total"], b["checks"][key], b["total"])} |' for key, name, _ in CHECKS]
    lines += ['', '| 指标 | ' + ma['label'] + ' | ' + mb['label'] + ' | 变化 |', '| --- | --- | --- | --- |']
    lines += [f'| {name} | {a["numbers"][key]} | {b["numbers"][key]} | '
              f'{_change(a["numbers"][key], b["numbers"][key], direction)} |' for key, name, direction in NUMBERS]
    lines += ['', '| 用量 | ' + ma['label'] + ' | ' + mb['label'] + ' |', '| --- | --- | --- |',
              f'| 输入 tokens | {a["tokens"][0]} | {b["tokens"][0]} |',
              f'| 输出 tokens | {a["tokens"][1]} | {b["tokens"][1]} |',
              f'| 费用（元） | {a["cost"] if a["cost"] is not None else "—"} | {b["cost"] if b["cost"] is not None else "—"} |']
    return '\n'.join(lines) + '\n'


def write_report(label, text, reports_dir=REPORTS_DIR):
    path = Path(reports_dir) / f'{label}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, 'utf-8')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m novel.evals', description='小说工作台评测')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('cases', help='列出用例（不调用模型）')
    run = commands.add_parser('run', help='调用模型跑评测，结束后写报告')
    run.add_argument('--label', required=True)
    run.add_argument('--cases', default='', help='只跑这些用例，逗号分隔')
    run.add_argument('--repeat', type=int, default=2)
    run.add_argument('--words', type=int, default=1200)
    run.add_argument('--workers', type=int, default=3)
    report = commands.add_parser('report', help='重新生成某次评测的报告')
    report.add_argument('label')
    compare = commands.add_parser('compare', help='对比两次评测')
    compare.add_argument('base')
    compare.add_argument('new')
    args = parser.parse_args(argv)
    try:
        cases, stories = load()
        if args.command == 'cases':
            print(f'{len(cases)} 个用例，{len(stories)} 个故事：')
            for case in cases:
                print(f'  {case["id"]}：《{stories[case["story"]]["book"]["title"]}》第 {case["chapter"]} 章 · {case.get("focus", "")}')
            return 0
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)  # 模型配置和价格
        if args.command == 'run':
            from novel.model import ModelClient, ModelConfig
            picked = [c for c in cases if not args.cases or c['id'] in args.cases.split(',')]
            _require(picked, '没有匹配的用例。')
            records = run_eval(args.label, ModelClient(ModelConfig.from_env()), picked, stories,
                               args.repeat, args.words, args.workers)
            print('报告：' + str(write_report(args.label, render_report(records))))
            return 0
        if args.command == 'report':
            records = rescore(load_results(args.label), cases, stories)
            print('报告：' + str(write_report(args.label, render_report(records))))
            return 0
        text = render_compare(rescore(load_results(args.base), cases, stories),
                              rescore(load_results(args.new), cases, stories))
        print('报告：' + str(write_report(f'{args.base}-vs-{args.new}', text)))
        return 0
    except ValidationError as error:
        print(f'错误：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
