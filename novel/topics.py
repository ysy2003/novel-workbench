"""选题方案与开新书。

选题方案：根据题材报告、该分类的榜单热度和作者偏好，生成 3 个差别明显的方案。
开书草稿：把选中的方案扩写成设定、总纲、写作偏好和第一卷大纲；作者在表单里改完确认后才创建作品。
只借鉴结构、节奏和爽点循环，不用现有作品的书名、人物、专名、桥段，不写同人或借用受版权保护的 IP。
"""
from __future__ import annotations

import json

from novel import analysis, research
from novel.model import load_json
from novel.store import ValidationError, clean_text, positive_int

PROPOSAL_COUNT = 3
PROPOSAL_FIELDS = ('书名', '番茄分类', '一句话卖点', '主角', '金手指或核心设定', '差异化', '开篇三章', '前30章节奏', '风险')
DRAFT_FIELDS = ('书名', '题材', '故事创意', '人物与世界设定', '总纲', '写作偏好', '第一卷')
VOLUME_FIELDS = ('卷名', '起始章', '结束章', '卷大纲')
NO_COPYING = '不要使用现有作品的书名、人物、专有名词和具体桥段，不写同人，也不借用受版权保护的 IP；只借鉴结构、节奏和爽点循环。'
PROPOSAL_DIRECTIVE = (f'你是网络小说策划编辑。根据题材报告、榜单热度和作者偏好，为准备在番茄开新书的作者提出 {PROPOSAL_COUNT} 个选题方案。'
                      '方案之间要有明显差别：至少一个走报告里的主流爽点路线，至少一个走报告里的“差异化空间”。'
                      '书名参考报告里的书名公式，但要是新书名。' + NO_COPYING +
                      '只输出 JSON：{"方案":[{"书名":"","番茄分类":"","一句话卖点":"","主角":"","金手指或核心设定":"",'
                      '"差异化":"","开篇三章":"","前30章节奏":"","风险":""}]}')
DRAFT_DIRECTIVE = ('你是网络小说策划编辑。把选定的选题方案扩写成新书的开书资料，供作者修改后使用。' + NO_COPYING +
                   '只输出 JSON，字段：书名；题材（番茄分类）；故事创意（一句话：谁想要什么，遇到了什么阻碍）；'
                   '人物与世界设定（主角、主要配角，金手指或核心设定的规则与代价，世界里不可违反的规则）；'
                   '总纲（全书主线；暗线与真相，标明“作者用，正文不得提前揭示”；伏笔表，写清埋设章和回收章）；'
                   '写作偏好（视角、句式、每章字数、章末钩子、要避免的套话）；'
                   '第一卷（对象，含 卷名、起始章、结束章、卷大纲，卷大纲写清本卷核心冲突、阶段目标、高潮和卷末钩子）。')


def _inline(value):
    if isinstance(value, dict):
        return '；'.join(f'{key}：{_inline(item)}' for key, item in value.items())
    if isinstance(value, list):
        return '；'.join(_inline(item) for item in value)
    return str(value)


def to_text(value):
    """把草稿里的结构化字段整理成便于编辑的多行文字：小标题写成【标题】，列表逐行列出。"""
    if not isinstance(value, dict):
        return '\n'.join(f'- {_inline(item)}' for item in value) if isinstance(value, list) else str(value)
    lines = []
    for key, item in value.items():
        if isinstance(item, list):
            lines += [f'【{key}】', *(f'- {_inline(part)}' for part in item)]
        else:
            lines.append(f'【{key}】{_inline(item)}')
    return '\n'.join(lines)


def _report(store, report_id):
    rows = store.all('SELECT * FROM reports WHERE id=?', (report_id,))
    if not rows:
        raise ValidationError('找不到这份题材报告。')
    return {**rows[0], 'report': json.loads(rows[0]['report'])}


def category_context(store, category):
    """最新快照里这个分类的头部在读合计和在读最高的几本。"""
    latest = store.all('SELECT MAX(id) AS id FROM rank_snapshots')[0]['id']
    if not latest:
        return {'说明': '还没有榜单快照'}
    rows = [r for r in research.entries(store, latest) if r['category'] == category]
    heat = [{'榜单': f'{h["channel"]}{h["board"]}', '前10在读合计（万）': round((h['total'] or 0) / 10000, 1)}
            for h in research.category_heat(store, latest) if h['category'] == category]
    top = sorted(rows, key=lambda r: -(r['reading'] or 0))[:5]
    return {'统计至': store.all('SELECT stat_date FROM rank_snapshots WHERE id=?', (latest,))[0]['stat_date'],
            '分类热度': heat,
            '头部作品': [{'书名': r['note_title'] or r['detail_title'] or r['title'],
                          '榜单': f'{r["channel"]}{r["board"]}', '在读（万）': round((r['reading'] or 0) / 10000, 1)}
                         for r in top]}


def propose(store, client, report_id, preferences=''):
    report = _report(store, report_id)
    preferences = clean_text(preferences, '作者偏好', 2000)
    payload = {'分类': report['category'], '题材报告': report['report'],
               '榜单热度': category_context(store, report['category']), '作者偏好': preferences or '无'}
    response, elapsed = analysis.call_model(client, [{'role': 'system', 'content': PROPOSAL_DIRECTIVE},
                                                     {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}])
    proposals = analysis.parse_logged(store, '选题方案', response, _parse_proposals)
    with store.connect(True) as c:
        return c.execute('INSERT INTO proposals(report_id,category,preferences,proposals,model,usage,elapsed) '
                         'VALUES(?,?,?,?,?,?,?)',
                         (report_id, report['category'], preferences, json.dumps(proposals, ensure_ascii=False),
                          client.config.model, json.dumps(response.usage), elapsed)).lastrowid


def _parse_proposals(text):
    try:
        items = load_json(text)['方案']
        if not isinstance(items, list) or len(items) != PROPOSAL_COUNT:
            raise ValueError('count')
        if not all(isinstance(item, dict) and all(f in item for f in PROPOSAL_FIELDS) for item in items):
            raise ValueError('fields')
        return [{f: analysis.normalize(item[f], f) for f in PROPOSAL_FIELDS} for item in items]
    except (TypeError, ValueError, KeyError) as error:
        raise ValidationError(f'选题方案格式不完整（需要 {PROPOSAL_COUNT} 个方案且字段齐全），请重新生成。') from error


def proposal_sets(store):
    rows = store.all('SELECT * FROM proposals ORDER BY id DESC')
    for row in rows:
        row['proposals'] = json.loads(row['proposals'])
    return rows


def _parse_draft(text):
    try:
        data = load_json(text)
        if not isinstance(data, dict) or not all(f in data for f in DRAFT_FIELDS):
            raise ValueError('fields')
        volume = data['第一卷']
        if not isinstance(volume, dict) or not all(f in volume for f in VOLUME_FIELDS):
            raise ValueError('volume')
        draft = {f: analysis.normalize(data[f], f) for f in DRAFT_FIELDS if f != '第一卷'}
        start, end = int(str(volume['起始章']).strip()), int(str(volume['结束章']).strip())
        if not 1 <= start <= end:
            raise ValueError('range')
        draft['第一卷'] = {'卷名': analysis.normalize(volume['卷名'], '卷名'), '起始章': start, '结束章': end,
                         '卷大纲': analysis.normalize(volume['卷大纲'], '卷大纲')}
        return draft
    except (TypeError, ValueError) as error:
        raise ValidationError('开书草稿格式不完整，请重新生成。') from error


def draft_book(store, client, proposal_id, index):
    sets = [s for s in proposal_sets(store) if s['id'] == proposal_id]
    if not sets or not 0 <= index < len(sets[0]['proposals']):
        raise ValidationError('找不到这个选题方案。')
    report = _report(store, sets[0]['report_id'])['report']
    payload = {'选题方案': sets[0]['proposals'][index],
               '题材报告要点': {k: report[k] for k in ('共同卖点', '开篇规律', '爽点节奏', '新书建议') if k in report}}
    response, elapsed = analysis.call_model(client, [{'role': 'system', 'content': DRAFT_DIRECTIVE},
                                                     {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}])
    draft = analysis.parse_logged(store, '开书草稿', response, _parse_draft)
    with store.connect(True) as c:
        return c.execute('INSERT INTO book_drafts(proposal_id,idx,draft,model,usage,elapsed) VALUES(?,?,?,?,?,?)',
                         (proposal_id, index, json.dumps(draft, ensure_ascii=False), client.config.model,
                          json.dumps(response.usage), elapsed)).lastrowid


def drafts(store, proposal_id=None):
    query, params = 'SELECT *, idx AS "index" FROM book_drafts', ()
    if proposal_id is not None:
        query, params = query + ' WHERE proposal_id=?', (proposal_id,)
    rows = store.all(query + ' ORDER BY id DESC', params)
    for row in rows:
        row['draft'] = json.loads(row['draft'])
    return rows


def create_book(store, draft_id, *, title, genre, premise, bible, outline, style,
                volume_title, volume_start, volume_end, volume_outline):
    """用作者确认过的草稿内容创建作品和第一卷。"""
    rows = store.all('SELECT * FROM book_drafts WHERE id=?', (draft_id,))
    if not rows:
        raise ValidationError('找不到这份开书草稿。')
    if rows[0]['book_id']:
        raise ValidationError('这份草稿已经开过书了。')
    values = {'title': clean_text(title, '书名', 120, True), 'genre': clean_text(genre, '题材', 120),
              'premise': clean_text(premise, '故事创意', 10000), 'bible': clean_text(bible, '人物与世界设定', 20000),
              'outline': clean_text(outline, '故事大纲', 20000), 'style': clean_text(style, '写作偏好', 4000)}
    volume = (clean_text(volume_title, '卷名', 60, True), positive_int(volume_start, '起始章'),
              positive_int(volume_end, '结束章'), clean_text(volume_outline, '卷大纲', 8000))
    if volume[1] > volume[2]:
        raise ValidationError('起始章不能大于结束章。')
    book_id = store.create_book(values['title'], values['premise'], values['genre'])
    store.update_book(book_id, **values)
    store.save_volume(book_id, 1, *volume)
    with store.connect(True) as c:
        c.execute('UPDATE book_drafts SET book_id=? WHERE id=?', (book_id, draft_id))
    return book_id
