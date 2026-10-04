"""自动连写：按细纲新建章节，依次规划、写初稿、检查、提取记忆并定稿；任何一步不通过就停下。

只写已定稿章节之后的新章节，从不改动已有章节。停下时，已完成的章节保留，出问题的那一章留给作者处理。
"""
from __future__ import annotations

import re
import time

from novel.model import ModelError
from novel.store import ValidationError

AUTO_SOURCE = '自动连写'
MAX_CHAPTERS = 10
LOOKAHEAD = 4  # 本章加后面 3 章：写作时会参考后续 3 章细纲
BEAT_BATCH = 10


class Stop(Exception):
    def __init__(self, step, reason):
        super().__init__(reason)
        self.step = step


def count_chars(text):
    return len(re.sub(r'\s', '', text))


def check_draft(text, target_words):
    problems = []
    chars = count_chars(text)
    if not target_words * 0.5 <= chars <= target_words * 2:
        problems.append(f'正文约 {chars} 字，不在目标 {target_words} 字的一半到两倍之间')
    if '作者备注' in text:
        problems.append('正文里出现了“作者备注”，可能泄露了只给作者看的内容')
    return problems


def capacity(store, book_id):
    """下一章章号、从它开始连续有细纲的章数、尚未定稿的章节。"""
    chapters = store.chapters(book_id)
    start = max((ch['number'] for ch in chapters), default=0) + 1
    beats = {b['number'] for b in store.beats(book_id)}
    available = 0
    while start + available in beats:
        available += 1
    return {'next': start, 'available': available,
            'unfinished': [ch['number'] for ch in chapters if not ch['finalized_revision_id']]}


def consecutive_beats(numbers, start):
    count = 0
    while start + count in numbers:
        count += 1
    return count


def _top_up_beats(store, writer, book_id, number, state):
    """本章起连续细纲不足 LOOKAHEAD 章时续写一批并直接采用（只填空位）。"""
    numbers = {b['number'] for b in store.beats(book_id)}
    ahead = consecutive_beats(numbers, number)
    later = sorted(n for n in numbers if n > number + ahead)
    if later:  # 中间有空洞：续写只会接在最后面，补不上
        if not ahead:
            raise Stop('续写细纲', f'第 {number} 章没有细纲，但后面已有第 {later[0]} 章的细纲；'
                                  f'自动续写只能接在最后面，请到“大纲”补上第 {number} 章。')
        return
    if ahead >= LOOKAHEAD or (state['top_up_failed'] and ahead):
        return
    try:
        generated = writer.run_beats(book_id, BEAT_BATCH)
        run = store.run(generated['run_id'])
        if not generated['candidates']:
            raise ValidationError(f'{run["status"]}：{run["error"]}' if run['error'] else run['status'])
        store.adopt_beats(book_id, generated['candidates'], run['context_version'])
        state['beats'].append({'start': generated['start'], 'end': generated['end']})
    except (ModelError, ValidationError) as error:
        if not ahead:
            raise Stop('续写细纲', str(error)) from None
        state['top_up_failed'] = True
        state['warnings'].append(f'写第 {number} 章前续写细纲没有成功（{error}）；本章已有细纲，继续写，本次不再重试。')


def auto_chapters(store, book_id):
    """定稿版本仍是自动连写结果的章节，即作者还没改过的。"""
    return store.all('SELECT ch.id, ch.number, r.title, r.body, m.facts FROM chapters ch '
                     'JOIN revisions r ON r.id=ch.finalized_revision_id '
                     'LEFT JOIN memories m ON m.chapter_id=ch.id '
                     'WHERE ch.book_id=? AND r.source=? ORDER BY ch.number', (book_id, AUTO_SOURCE))


def _run(store, writer, chapter_id, task, target_words, step):
    try:
        result = writer.run(chapter_id, task, '', target_words)
    except (ModelError, ValidationError) as error:
        raise Stop(step, str(error)) from None
    run = store.run(result['run_id'])
    if run['status'] != '成功':
        raise Stop(step, f'{run["status"]}：{run["error"]}' if run['error'] else run['status'])
    return result


def _write_chapter(store, writer, book_id, beat, target_words, progress):
    number, started = beat['number'], time.monotonic()
    step = '创建章节'
    try:
        progress(number, step)
        chapter_id = store.create_chapter(book_id, beat['title'], beat['brief'])
        step = '规划本章'
        progress(number, step)
        plan = _run(store, writer, chapter_id, 'plan', target_words, step)['text']
        current = store.revision(store.chapter(chapter_id)['current_revision_id'])
        store.save_revision(chapter_id, current['title'], plan, current['body'], current['id'], source=AUTO_SOURCE)
        step = '生成初稿'
        progress(number, step)
        candidate = store.revision(_run(store, writer, chapter_id, 'draft', target_words, step)['candidate_id'])
        step = '检查初稿'
        progress(number, step)
        problems = check_draft(candidate['body'], target_words)
        if problems:
            raise Stop(step, '；'.join(problems))
        store.adopt_candidate(chapter_id, candidate['id'], source=AUTO_SOURCE)
        step = '提取记忆'
        progress(number, step)
        memory = _run(store, writer, chapter_id, 'memory', target_words, step).get('memory')
        if not memory:
            raise Stop(step, '记忆提取结果格式不完整')
        step = '定稿'
        progress(number, step)
        store.finalize(chapter_id, store.chapter(chapter_id)['current_revision_id'], memory['summary'],
                       memory['facts'], store.book(book_id)['context_version'])
    except ValidationError as error:
        raise Stop(step, str(error)) from None
    return {'chapter_id': chapter_id, 'number': number, 'title': beat['title'],
            'chars': count_chars(candidate['body']), 'facts': len(memory['facts']),
            'elapsed': time.monotonic() - started}


def autowrite(store, writer, book_id, count, target_words=2000, on_progress=None, auto_beats=False):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_CHAPTERS:
        raise ValidationError(f'一次连写 1–{MAX_CHAPTERS} 章。')
    cap = capacity(store, book_id)
    if cap['unfinished']:
        raise ValidationError(f'第 {cap["unfinished"][0]} 章还没定稿。自动连写只接着已定稿的章节往后写，请先处理它。')
    if not auto_beats and cap['available'] < count:
        raise ValidationError(f'从第 {cap["next"]} 章起只有 {cap["available"]} 章连续的细纲，'
                              '请先到“大纲”续写细纲，或减少连写章数。')
    writer.client.config.validate()
    progress = on_progress or (lambda number, step: None)
    state = {'beats': [], 'warnings': [], 'top_up_failed': False}
    result = {'done': [], 'stopped': None, 'beats': state['beats'], 'warnings': state['warnings']}
    for number in range(cap['next'], cap['next'] + count):
        try:
            if auto_beats:
                progress(number, '检查细纲')
                _top_up_beats(store, writer, book_id, number, state)
            beat = next((b for b in store.beats(book_id) if b['number'] == number), None)
            if not beat:
                raise Stop('续写细纲', f'第 {number} 章没有细纲。')
            result['done'].append(_write_chapter(store, writer, book_id, beat, target_words, progress))
        except Stop as stop:
            result['stopped'] = {'number': number, 'step': stop.step, 'reason': str(stop)}
            break
    return result
