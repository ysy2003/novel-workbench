from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from novel.autowrite import BEAT_BATCH, LOOKAHEAD, MAX_CHAPTERS, auto_chapters, autowrite, capacity
from novel import analysis, research, topics
from novel.model import ModelClient, ModelConfig, ModelError, parse_beats, parse_memory
from novel.store import Store, ValidationError, safe_filename, trim
from novel.writer import RECENT_CHAPTERS, TASKS, Writer


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=False)
st.set_page_config(page_title='灯下 · 小说工作台', page_icon='✍️', layout='wide')
st.markdown('''<style>
.block-container{max-width:1350px;padding-top:2.3rem;padding-bottom:3rem}
h1{font-weight:650!important;letter-spacing:-.03em}h2,h3{font-weight:600!important}
[data-testid="stSidebar"]{border-right:1px solid #dfe5da}
[data-testid="stMetric"]{padding:12px 16px;border:1px solid #dfe5da;border-radius:12px;background:#fffefb}
[data-testid="stTextArea"] textarea{line-height:1.85!important}
div[data-testid="stAlert"]{border-radius:10px}
</style>''', unsafe_allow_html=True)


@st.cache_resource
def open_store(path):
    store = Store(path)
    store.recover_runs()
    return store


db_path = os.getenv('NOVEL_DB_PATH', str(ROOT / 'data' / 'novels.sqlite3'))
store = open_store(str(Path(db_path).expanduser().resolve()))
try:
    config = ModelConfig.from_env()
except ValidationError as error:
    st.error(str(error))
    config = ModelConfig()


def notice(message):
    st.session_state['notice'] = message


def count_chars(text):
    return len(re.sub(r'\s', '', text))


def stale_hint(numbers):
    shown = '、'.join(map(str, numbers[:5])) + ('…' if len(numbers) > 5 else '')
    return f'{len(numbers)} 章记忆待复核（第 {shown} 章）'


def book_creation(key):
    with st.form(key, clear_on_submit=True):
        title = st.text_input('书名', placeholder='先给故事一个名字', key=f'{key}_title')
        genre = st.text_input('题材（可稍后填写）', placeholder='例如：都市悬疑', key=f'{key}_genre')
        premise = st.text_area('一句话创意', placeholder='谁想要什么，遇到了什么阻碍？', key=f'{key}_premise')
        if st.form_submit_button('创建作品', type='primary'):
            try:
                book_id = store.create_book(title, premise, genre)
                st.session_state['pending_book'] = book_id
                notice('作品已创建。可以先补充设定，也可以直接开始第一章。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))


if 'pending_book' in st.session_state:
    st.session_state['book_pick'] = st.session_state.pop('pending_book')
    st.session_state['section'] = '作品设定'

with st.sidebar:
    st.markdown('## 灯下')
    st.caption('个人小说工作台 · 番茄连载')
    books = store.books()
    if books:
        book_names = {b['id']: b['title'] for b in books}
        if st.session_state.get('book_pick') not in book_names:
            st.session_state['book_pick'] = books[0]['id']
        book_id = st.selectbox('当前作品', list(book_names), format_func=book_names.get, key='book_pick')
        section = st.radio('工作区', ['作品设定', '大纲', '章节工作区', '自动连写', '故事记忆', '存稿与导出', '选题研究', '模型与记录'], key='section')
        st.divider()
        with st.expander('＋ 新建作品'):
            book_creation('sidebar_create')
    else:
        st.caption('从一个故事想法开始。')
    st.divider()
    st.caption('稿件保存在本机 SQLite 数据库。')
    st.caption('模型已配置' if config.ready else '模型未配置 · 可以先写作与保存')

if 'notice' in st.session_state:
    st.success(st.session_state.pop('notice'))

if not books:
    st.caption('从灵感到第一章')
    st.title('写下你的下一个故事。')
    st.write('整理设定，逐章创作，保留每一次修改。你来决定哪些文字成为正式故事。')
    left, right = st.columns([1.4, 1], gap='large')
    with left:
        book_creation('welcome_create')
    with right:
        st.markdown('### 你的创作流程')
        st.write('**01 · 建立故事**\n\n写下创意、人物与世界规则。')
        st.write('**02 · 逐章打磨**\n\n规划情节，编辑正文，再选择是否采用 AI 候选稿。')
        st.write('**03 · 定稿与连载**\n\n确认本章记忆，导出稿件，记录番茄发布进度。')
        st.info('还没有模型 API 也能开书、写作、定稿和导出。')
    st.stop()

book = store.book(book_id)
chapters = store.chapters(book_id)
st.caption(f'我的书架 / {book["title"]}')


def editor_keys(chapter_id):
    return {name: f'edit_{name}_{chapter_id}' for name in ('title', 'plan', 'body', 'base')}


def reset_editor(chapter_id):
    for key in editor_keys(chapter_id).values():
        st.session_state.pop(key, None)
    st.session_state.pop(f'edit_error_{chapter_id}', None)


def save_editor(chapter_id):
    keys = editor_keys(chapter_id)
    try:
        base = st.session_state[keys['base']]
        old = store.revision(base)
        values = [st.session_state[keys[name]] for name in ('title', 'plan', 'body')]
        if any(trim(value, name == 'body') != old[name] for value, name in zip(values, ('title', 'plan', 'body'))):
            new = store.save_revision(chapter_id, *values, base)
            st.session_state[keys['base']] = new
        elif store.chapter(chapter_id)['current_revision_id'] != base:
            raise ValidationError('此章节在另一个窗口有新版本。请复制输入后刷新核对。')
        st.session_state.pop(f'edit_error_{chapter_id}', None)
        return True
    except ValidationError as error:
        st.session_state[f'edit_error_{chapter_id}'] = str(error)
        return False


def show_settings():
    st.title('作品设定')
    st.write('人物、世界规则和故事走向，都可以边写边完善。')
    fields = {name: f'settings_{name}_{book_id}' for name in ('title', 'genre', 'premise', 'bible', 'outline', 'style')}
    base_key = f'settings_base_{book_id}'
    if fields['title'] not in st.session_state:
        for name, key in fields.items():
            st.session_state[key] = book[name]
        st.session_state[base_key] = book['context_version']
    with st.form(f'settings_{book_id}'):
        title = st.text_input('书名', key=fields['title'])
        genre = st.text_input('题材', key=fields['genre'], placeholder='尚未确定可以留空')
        premise = st.text_area('故事创意', key=fields['premise'], height=100)
        bible = st.text_area('人物与世界设定', key=fields['bible'], height=210,
                             placeholder='人物的欲望、关系、能力边界；世界里不可违反的规则。')
        outline = st.text_area('故事大纲', key=fields['outline'], height=180,
                               placeholder='开端、主要转折、高潮与结局。')
        style = st.text_area('写作偏好', key=fields['style'], height=100,
                             placeholder='叙述视角、语言节奏、喜欢或避免的表达。')
        st.caption('修改设定不会改动已定稿章节和故事记忆，可以随时调整。新设定若与前文冲突，请修改并重新定稿相应章节。')
        if st.form_submit_button('保存设定', type='primary'):
            try:
                store.update_book(book_id, title=title, premise=premise, genre=genre, bible=bible,
                                  style=style, outline=outline, expected_version=st.session_state[base_key])
                st.session_state[base_key] = store.book(book_id)['context_version']
                notice('设定已保存。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))


def show_assistant(chapter_id, revision):
    labels = {'plan': '规划本章', 'draft': '生成初稿', 'rewrite': '按意见改写', 'review': '一致性检查', 'memory': '提取本章记忆'}
    task = st.selectbox('要做什么', list(labels), format_func=labels.get, key=f'task_{chapter_id}')
    instructions = st.text_area('本次要求', placeholder='例如：把冲突放在开头；保留结尾悬念。', key=f'instructions_{chapter_id}')
    target = st.number_input('目标正文字数', min_value=200, max_value=12000, value=2000, step=200,
                             key=f'target_{chapter_id}')
    passage = ''
    if task == 'rewrite':
        passage = st.text_area('只重写这一段（可选）', placeholder='粘贴当前正文中要改写的原文；留空则改写整章。',
                               key=f'passage_{chapter_id}')
    writer = Writer(store, ModelClient(config))
    try:
        _, _, context, messages = writer.prepare(chapter_id, task, instructions, int(target), passage)
        ready = True
    except ValidationError as error:
        st.info(str(error))
        ready, context, messages = False, None, None
    if messages:
        if context['stale_chapters']:
            st.info(f'{stale_hint(context["stale_chapters"])}，生成时仍会参考。可在“故事记忆”中核对。')
        with st.expander('本次会用到的故事资料'):
            st.caption(f'来自 {len(context["source_chapters"])} 个定稿章节；发送 {len(context["sent_facts"])} / '
                       f'{len(context["facts"])} 条事实（本章提到的对象和最近 {RECENT_CHAPTERS} 章的更新）。')
            volume = context['volume']
            volume_label = f'第 {volume["number"]} 卷《{volume["title"]}》' if volume else '未分卷'
            st.caption(f'本卷：{volume_label}；本章细纲：{"有" if context["beat"] else "无"}；'
                       f'后续细纲 {len(context["next_beats"])} 章。')
            if context['previous_ending']:
                st.caption(f'附上第 {context["previous_ending"]["chapter_number"]} 章定稿结尾 '
                           f'{len(context["previous_ending"]["text"])} 字，用于衔接。')
            st.json(json.loads(messages[1]['content']), expanded=False)
    if not config.ready:
        st.caption('配置模型后即可使用。请到“模型与记录”查看接入方法。')
    else:
        st.caption(f'将发送上面的资料到 {config.endpoint}，使用 {config.model}。')
    if st.button('开始' + labels[task], type='primary', disabled=not (config.ready and ready), key=f'generate_{chapter_id}'):
        if save_editor(chapter_id):
            try:
                with st.spinner('正在生成；当前稿已保存…'):
                    result = writer.run(chapter_id, task, instructions, int(target), passage)
                notice('结果已保存，请在下方查看。')
                st.rerun()
            except (ValidationError, ModelError) as error:
                st.error(str(error))
    runs = store.runs(book_id, chapter_id)
    if not runs:
        return
    st.divider()
    run = runs[0]
    st.caption(f'最近结果 · {labels.get(run["task"], run["task"])} · {run["status"]} · 依据版本 {run["revision_id"]}')
    if run['error']:
        st.warning(run['error'])
    if run['output']:
        st.text_area('生成结果', value=run['output'], height=220, disabled=True, key=f'run_output_{run["id"]}')
    current = store.chapter(chapter_id)
    same_source = run['revision_id'] == current['current_revision_id'] and run['context_version'] == store.book(book_id)['context_version']
    if run['status'] == '成功' and run['task'] == 'plan' and same_source:
        if st.button('采用这个章节计划', key=f'accept_plan_{run["id"]}'):
            try:
                r = store.revision(current['current_revision_id'])
                store.save_revision(chapter_id, r['title'], run['output'], r['body'], r['id'])
                reset_editor(chapter_id)
                st.rerun()
            except ValidationError as error:
                st.error(str(error))
    if run['task'] in ('draft', 'rewrite') and run['status'] == '成功' and run['candidate_id']:
        candidate = store.revision(run['candidate_id'])
        if st.button('采用最近候选稿', key=f'accept_draft_{candidate["id"]}'):
            try:
                store.adopt_candidate(chapter_id, candidate['id'])
                reset_editor(chapter_id)
                notice('候选稿已成为当前稿；原版本仍在历史中。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))


def show_finalize(chapter_id, revision):
    memories = {m['chapter_id']: m for m in store.memory(book_id)}
    old = memories.get(chapter_id)
    runs = store.runs(book_id, chapter_id)
    extraction = next((r for r in runs if r['task'] == 'memory' and r['status'] == '成功'
                       and r['revision_id'] == revision['id'] and r['context_version'] == book['context_version']), None)
    seed = {'summary': old['summary'], 'facts': old['facts']} if old else {'summary': '', 'facts': []}
    seed_id = 'manual'
    if extraction:
        seed = parse_memory(extraction['output'])
        seed_id = str(extraction['id'])
    if old and old['stale']:
        st.warning('前文已重新定稿，本章记忆待复核。核对正文和记忆后再次确认即可；不影响继续写后面的章节。')
    st.caption('写下本章真正发生的事。可以手工填写，也可以先用写作助手提取，再在这里修订。')
    key = f'final_{chapter_id}_{revision["id"]}_{book["context_version"]}_{seed_id}'
    with st.form(key):
        summary = st.text_area('本章摘要', value=seed['summary'], height=130)
        frame = pd.DataFrame(seed['facts'], columns=['entity', 'attribute', 'value']).rename(
            columns={'entity': '对象', 'attribute': '属性', 'value': '事实'})
        facts = st.data_editor(frame, num_rows='dynamic', hide_index=True, width='stretch',
                               column_config={name: st.column_config.TextColumn(name) for name in ('对象', '属性', '事实')})
        st.caption('例如：对象“林川”，属性“左臂伤势”，事实“尚未痊愈，不能持剑”。无新增事实时可留空。')
        if st.form_submit_button('确认记忆并定稿', type='primary'):
            rows = []
            for row in facts.fillna('').to_dict('records'):
                if any(str(v).strip() for v in row.values()):
                    rows.append({'entity': str(row['对象']), 'attribute': str(row['属性']), 'value': str(row['事实'])})
            try:
                store.finalize(chapter_id, revision['id'], summary, rows, book['context_version'])
                notice('本章已定稿，故事记忆已更新。现在可以继续下一章。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))


def show_history(chapter_id, revision):
    revisions = store.revisions(chapter_id)
    options = {r['id']: r for r in revisions}
    picked = st.selectbox('历史版本', list(options),
                          format_func=lambda n: f'版本 {n} · {options[n]["source"]} · {options[n]["created_at"]}',
                          key=f'history_{chapter_id}')
    chosen = options[picked]
    st.caption(chosen['title'])
    st.text_area('版本正文', value=chosen['body'], height=250, disabled=True, key=f'preview_revision_{picked}')
    if chosen['source'] == 'AI 候选稿':
        st.caption('候选稿请从写作助手采用；过期候选可参考内容手动合并。')
    if st.button('恢复为新的当前稿', disabled=picked == revision['id'] or chosen['source'] == 'AI 候选稿', key=f'restore_{chapter_id}'):
        try:
            store.restore(chapter_id, picked, revision['id'])
            reset_editor(chapter_id)
            notice('已恢复为新版本，原有版本全部保留。')
            st.rerun()
        except ValidationError as error:
            st.error(str(error))


BEATS_AHEAD = 5


def show_beat_progress(quiet=False):
    if not store.volumes(book_id):
        return
    last_chapter = max((ch['number'] for ch in chapters), default=0)
    last_beat = max((b['number'] for b in store.beats(book_id)), default=0)
    progress = f'已建到第 {last_chapter} 章，细纲写到第 {last_beat} 章'
    if last_beat - last_chapter >= BEATS_AHEAD:
        if not quiet:
            st.caption(progress + '。')
        return
    message = f'细纲只领先 {max(last_beat - last_chapter, 0)} 章（{progress}）'
    if quiet:
        st.caption(message + '，可到“大纲”续写细纲。')
    else:
        st.warning(message + '，建议续写细纲。')


def beat_rows(frame):
    rows = []
    for row in frame.fillna('').to_dict('records'):
        if not any(str(value).strip() for value in row.values()):
            continue
        try:
            number = int(row['章号'])
        except (TypeError, ValueError):
            raise ValidationError('章号必须是数字。') from None
        rows.append({'number': number, 'title': str(row['标题']), 'brief': str(row['要点'])})
    return rows


def beat_frame(rows):
    return pd.DataFrame(rows, columns=['number', 'title', 'brief']).rename(
        columns={'number': '章号', 'title': '标题', 'brief': '要点'})


def show_volume_editor(volumes):
    by_number = {v['number']: v for v in volumes}
    picked = st.selectbox('分卷', [*by_number, 'new'], key=f'volume_pick_{book_id}',
                          format_func=lambda n: '＋ 新建分卷' if n == 'new' else
                          f'第 {n} 卷 · {by_number[n]["title"]}（第 {by_number[n]["start_chapter"]}–{by_number[n]["end_chapter"]} 章）')
    if picked == 'new':
        last = volumes[-1] if volumes else None
        start = last['end_chapter'] + 1 if last else 1
        volume = {'number': last['number'] + 1 if last else 1, 'title': '', 'start_chapter': start,
                  'end_chapter': start + 29, 'outline': ''}
    else:
        volume = by_number[picked]
    keys = {name: f'volume_{name}_{book_id}_{picked}' for name in ('number', 'title', 'start', 'end', 'outline')}
    with st.form(f'volume_form_{book_id}_{picked}'):
        a, b, c, d = st.columns([1, 2, 1, 1])
        number = a.number_input('卷号', min_value=1, step=1, value=volume['number'], key=keys['number'])
        title = b.text_input('卷名', value=volume['title'], key=keys['title'])
        start = c.number_input('起始章', min_value=1, step=1, value=volume['start_chapter'], key=keys['start'])
        end = d.number_input('结束章', min_value=1, step=1, value=volume['end_chapter'], key=keys['end'])
        outline = st.text_area('卷大纲', value=volume['outline'], height=160, key=keys['outline'],
                               placeholder='本卷核心冲突、阶段目标、高潮、卷末钩子。')
        if st.form_submit_button('保存分卷', type='primary'):
            try:
                store.save_volume(book_id, int(number), title, int(start), int(end), outline)
                st.session_state['pending_volume'] = int(number)
                notice('分卷已保存。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))
    if picked == 'new':
        return None
    if st.button('删除这一卷（细纲保留）', key=f'delete_volume_{book_id}_{picked}'):
        store.delete_volume(book_id, picked)
        st.session_state['pending_volume'] = 'new'
        notice('分卷已删除，这一卷范围内的细纲仍然保留。')
        st.rerun()
    return volume


def show_beat_editor(volume):
    st.markdown(f'### 第 {volume["number"]} 卷细纲')
    rows = [b for b in store.beats(book_id) if volume['start_chapter'] <= b['number'] <= volume['end_chapter']]
    version = book['context_version']
    with st.form(f'beats_form_{book_id}_{volume["number"]}_{version}'):
        edited = st.data_editor(beat_frame(rows), num_rows='dynamic', hide_index=True, width='stretch',
                                key=f'beats_editor_{book_id}_{volume["number"]}_{version}',
                                column_config={'章号': st.column_config.NumberColumn('章号', min_value=1, step=1, width='small'),
                                               '标题': st.column_config.TextColumn('标题', width='medium'),
                                               '要点': st.column_config.TextColumn('要点', width='large')})
        st.caption('可以直接修改、增删行；保存后替换本卷范围内的细纲。')
        if st.form_submit_button('保存细纲'):
            try:
                store.save_beats(book_id, volume['start_chapter'], volume['end_chapter'], beat_rows(edited))
                notice('细纲已保存。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))


def show_beat_generator():
    st.markdown('### AI 续写细纲')
    count = st.number_input('续写章数', min_value=1, max_value=30, value=10, step=1, key=f'beat_count_{book_id}')
    instructions = st.text_area('本次要求（可选）', key=f'beat_instructions_{book_id}',
                                placeholder='例如：第 5 章安排一次小高潮。')
    try:
        start, end, target = store.beat_window(book_id, int(count))
        st.caption(f'将生成第 {start}–{end} 章细纲（第 {target["number"]} 卷《{target["title"]}》），'
                   '参考总纲、本卷大纲、本卷已有细纲和最近 6 章摘要。结果先作为候选，采用后才写入。')
        window_ok = True
    except ValidationError as error:
        st.info(str(error))
        window_ok = False
    if not config.ready:
        st.caption('配置模型后即可使用。请到“模型与记录”查看接入方法。')
    if st.button('开始续写细纲', type='primary', disabled=not (config.ready and window_ok), key=f'run_beats_{book_id}'):
        try:
            with st.spinner('正在生成细纲…'):
                Writer(store, ModelClient(config)).run_beats(book_id, int(count), instructions)
            notice('细纲候选已生成，请在下方查看。')
            st.rerun()
        except (ValidationError, ModelError) as error:
            st.error(str(error))
    run = next((r for r in store.runs(book_id) if r['task'] == 'beats'), None)
    if not run:
        return
    st.caption(f'最近一次续写 · {run["status"]} · {run["created_at"]}')
    if run['error']:
        st.warning(run['error'])
    if run['status'] != '成功':
        if run['output']:
            st.text_area('模型返回', run['output'], height=200, disabled=True, key=f'beats_output_{run["id"]}')
        return
    candidates = parse_beats(run['output'])
    st.dataframe(beat_frame(candidates), hide_index=True, width='stretch')
    existing = {b['number']: b for b in store.beats(book_id)}
    if all(existing.get(c['number']) == c for c in candidates):
        st.caption('这批细纲已采用，可以在上方表格里继续修改。')
    elif run['context_version'] != book['context_version']:
        st.info('生成后大纲、设定或记忆已改变，这批候选已过期。可以参考内容手动填写，或重新生成。')
    elif st.button('采用这些细纲', type='primary', key=f'adopt_beats_{run["id"]}'):
        try:
            store.adopt_beats(book_id, candidates, run['context_version'])
            notice('细纲已采用。')
            st.rerun()
        except ValidationError as error:
            st.error(str(error))


def show_outline():
    st.title('大纲')
    st.write('总纲（全书主线、暗线、伏笔表）在“作品设定 → 故事大纲”里维护；这里管理分卷大纲和每章细纲。')
    if 'pending_volume' in st.session_state:
        st.session_state[f'volume_pick_{book_id}'] = st.session_state.pop('pending_volume')
    show_beat_progress()
    volume = show_volume_editor(store.volumes(book_id))
    if volume:
        show_beat_editor(volume)
    st.divider()
    show_beat_generator()


def show_chapters():
    st.title('章节工作区')
    next_number = max((ch['number'] for ch in chapters), default=0) + 1
    next_beat = next((b for b in store.beats(book_id) if b['number'] == next_number), None)
    if next_beat and st.button(f'按细纲创建第 {next_number} 章：{next_beat["title"]}', type='primary',
                               key=f'from_beat_{book_id}_{next_number}'):
        try:
            st.session_state[f'chapter_pick_{book_id}'] = store.create_chapter(book_id, next_beat['title'], next_beat['brief'])
            notice('章节已按细纲创建，计划里是细纲要点，可以用“规划本章”扩成场景计划。')
            st.rerun()
        except ValidationError as error:
            st.error(str(error))
    show_beat_progress(quiet=True)
    with st.expander('＋ 新建章节', expanded=not chapters):
        with st.form(f'new_chapter_{book_id}', clear_on_submit=True):
            title = st.text_input('新章节标题', placeholder='例如：雨夜来信', key='new_chapter_title')
            plan = st.text_area('本章计划', placeholder='本章发生什么？人物作出什么选择？结尾留下什么？', key='new_chapter_plan')
            if st.form_submit_button('创建章节', type='primary'):
                try:
                    new_id = store.create_chapter(book_id, title, plan)
                    st.session_state[f'chapter_pick_{book_id}'] = new_id
                    notice('章节已创建。')
                    st.rerun()
                except ValidationError as error:
                    st.error(str(error))
    if not chapters:
        st.info('先创建第一章，写几句计划就可以开始。')
        return
    lookup = {ch['id']: ch for ch in chapters}
    chapter_id = st.selectbox('当前章节', list(lookup),
                              format_func=lambda n: f'第 {lookup[n]["number"]} 章 · {lookup[n]["title"]}',
                              key=f'chapter_pick_{book_id}')
    chapter = store.chapter(chapter_id)
    revision = store.revision(chapter['current_revision_id'])
    keys = editor_keys(chapter_id)
    if keys['title'] not in st.session_state:
        for field in ('title', 'plan', 'body'):
            st.session_state[keys[field]] = revision[field]
        st.session_state[keys['base']] = revision['id']
    left, right = st.columns([1.65, 1], gap='large')
    with left:
        st.text_input('章节标题', key=keys['title'], on_change=save_editor, args=(chapter_id,))
        st.text_area('章节计划', height=125, key=keys['plan'], on_change=save_editor, args=(chapter_id,))
        st.text_area('正文', height=550, key=keys['body'], on_change=save_editor, args=(chapter_id,),
                     placeholder='从第一个动作或第一句对话开始……')
        st.caption('离开输入框时保存新版本；写完也可以点击保存。浏览器关闭前请确认下方已保存状态。')
        if st.button('保存当前稿', type='primary', key=f'save_{chapter_id}'):
            if save_editor(chapter_id):
                notice('当前稿已保存。')
                st.rerun()
        if f'edit_error_{chapter_id}' in st.session_state:
            st.error(st.session_state[f'edit_error_{chapter_id}'])
        else:
            st.caption(f'已保存 · 版本 {st.session_state[keys["base"]]} · 约 {count_chars(st.session_state[keys["body"]])} 字')
        if chapter['finalized_revision_id']:
            suffix = '（当前稿有后续修改）' if chapter['current_revision_id'] != chapter['finalized_revision_id'] else ''
            st.caption(f'正式定稿：版本 {chapter["finalized_revision_id"]}{suffix}')
    revision = store.revision(store.chapter(chapter_id)['current_revision_id'])
    with right:
        assistant, finalize, history = st.tabs(['写作助手', '定稿与记忆', '历史版本'])
        with assistant:
            show_assistant(chapter_id, revision)
        with finalize:
            show_finalize(chapter_id, revision)
        with history:
            show_history(chapter_id, revision)


def show_autowrite():
    st.title('自动连写')
    st.write('按细纲从下一章开始连续写作：每章自动规划、写初稿、检查、提取记忆并定稿。'
             '任何一步不通过就停下，出问题的那一章留给你处理。已有章节不会被改动。')
    cap = capacity(store, book_id)
    auto_beats = st.checkbox('细纲不够时自动续写', value=True, key=f'auto_beats_{book_id}',
                             help=f'每写一章前，如果从这一章起连续细纲不足 {LOOKAHEAD} 章，就续写 {BEAT_BATCH} 章细纲并直接采用；'
                                  '只填空位，不覆盖已有细纲，也不超出分卷。')
    if cap['unfinished']:
        st.warning(f'第 {cap["unfinished"][0]} 章还没定稿。先在“章节工作区”定稿后才能连写。')
    elif not cap['available'] and not auto_beats:
        st.info(f'第 {cap["next"]} 章还没有细纲。请先到“大纲”续写细纲，或打开“细纲不够时自动续写”。')
    else:
        st.caption(f'可以从第 {cap["next"]} 章开始；现有连续细纲 {cap["available"]} 章'
                   + ('，不够时会自动续写。' if auto_beats else '。'))
    limit = MAX_CHAPTERS if auto_beats else max(1, min(MAX_CHAPTERS, cap['available']))
    count = st.number_input('连写章数', min_value=1, max_value=limit, value=min(3, limit), step=1,
                            key=f'auto_count_{book_id}_{auto_beats}')
    target = st.number_input('每章目标字数', min_value=500, max_value=12000, value=2000, step=100,
                             key=f'auto_target_{book_id}')
    st.caption('检查标准：字数在目标的一半到两倍之间，正文不出现“作者备注”，模型输出完整，记忆格式正确。'
               '每章约 1–1.5 分钟；运行期间不要关闭或刷新页面，中途断开时已完成的章节会保留。')
    if not config.ready:
        st.caption('配置模型后即可使用。请到“模型与记录”查看接入方法。')
    ready = config.ready and not cap['unfinished'] and (auto_beats or cap['available'] > 0)
    if st.button('开始自动连写', type='primary', disabled=not ready, key=f'auto_run_{book_id}'):
        last = cap['next'] + int(count) - 1
        with st.status(f'正在连写第 {cap["next"]}–{last} 章…', expanded=True) as status:
            try:
                result = autowrite(store, Writer(store, ModelClient(config)), book_id, int(count), int(target),
                                   lambda number, step: st.write(f'第 {number} 章：{step}'), auto_beats=auto_beats)
                stopped = result['stopped']
                status.update(label=f'第 {stopped["number"]} 章在“{stopped["step"]}”停下' if stopped
                              else f'完成 {len(result["done"])} 章', state='error' if stopped else 'complete')
                st.session_state[f'auto_result_{book_id}'] = result
            except (ValidationError, ModelError) as error:
                status.update(label='没有开始', state='error')
                st.error(str(error))
        if f'auto_result_{book_id}' in st.session_state:
            st.rerun()
    result = st.session_state.get(f'auto_result_{book_id}')
    if result:
        st.markdown('#### 最近一次连写')
        if not result['stopped']:
            st.success(f'完成 {len(result["done"])} 章，全部通过检查并定稿。')
        if result['done']:
            st.dataframe([{'章节': f'第 {d["number"]} 章', '标题': d['title'], '字数': d['chars'],
                           '记忆条数': d['facts'], '耗时（秒）': round(d['elapsed'])} for d in result['done']],
                         hide_index=True, width='stretch')
        if result.get('beats'):
            st.caption('本次自动续写了细纲：' + '、'.join(f'第 {b["start"]}–{b["end"]} 章' for b in result['beats'])
                       + '，可以到“大纲”抽查或修改。')
        for warning in result.get('warnings', []):
            st.warning(warning)
        if result['stopped']:
            stopped = result['stopped']
            st.warning(f'第 {stopped["number"]} 章停在“{stopped["step"]}”：{stopped["reason"]}。'
                       '这一章已经建好，请到“章节工作区”手动处理并定稿，之后可以继续连写。')
    st.markdown('### 自动连写的章节')
    written = auto_chapters(store, book_id)
    if not written:
        st.caption('暂无。你修改并重新定稿后，章节会从这里移除。')
        return
    st.caption('这些章节的定稿还是自动连写的原样。需要修改的，到“章节工作区”改完重新定稿；后面的章节会标为待复核，只作提醒。')
    for chapter in written:
        with st.expander(f'第 {chapter["number"]} 章 · {chapter["title"]} · 约 {count_chars(chapter["body"])} 字'):
            st.text_area('正文', chapter['body'], height=320, disabled=True, key=f'auto_body_{chapter["id"]}')


def wan(value):
    return None if value is None else round(value / 10000, 1)


def entry_title(row):
    return row.get('note_title') or row.get('detail_title') or row['title']


def flat(value):
    if isinstance(value, dict):
        return '；'.join(f'{key}：{flat(item)}' for key, item in value.items())
    if isinstance(value, list):
        return '；'.join(flat(item) for item in value)
    return str(value)


def show_fields(data, fields):
    for field in fields:
        value = data.get(field)
        if isinstance(value, list):
            st.markdown(f'**{field}**')
            for item in value:
                st.markdown('- ' + (' · '.join(flat(v) for v in item.values()) if isinstance(item, dict) else flat(item)))
        else:
            st.markdown(f'**{field}**：{flat(value)}')


def show_breakdown(rows):
    st.caption('先抓取一页榜单（10 本）的书籍详情：书名、标签、字数、简介和完整目录都是正常文字。'
               'AI 根据详情生成分析卡，也可以附上你合法拿到的正文或阅读笔记；补充样本不会保存。')
    a, b, c = st.columns(3)
    channel = a.selectbox('频道', ['男频', '女频'], key='bd_channel')
    board = b.selectbox('榜单', ['新书榜', '阅读榜'], key='bd_board')
    categories = sorted({r['category'] for r in rows if r['channel'] == channel and r['board'] == board})
    if not categories:
        st.info('这份快照里没有这个榜单。')
        return
    category = c.selectbox('分类', categories, key=f'bd_category_{channel}_{board}')
    books = [r for r in rows if (r['channel'], r['board'], r['category']) == (channel, board, category)]
    carded = {card['book_id'] for card in analysis.cards(store)}
    st.dataframe([{'排名': r['rank'], '书名': entry_title(r), '在读（万）': wan(r['reading']),
                   '详情': '已抓取' if r['detail_title'] else '未抓取', '分析卡': '有' if r['book_id'] in carded else ''}
                  for r in books], hide_index=True, width='stretch')
    if st.button('抓取这一页的书籍详情', key=f'bd_fetch_{category}'):
        with st.status('正在抓取书籍详情…', expanded=True) as status:
            summary = research.fetch_details(store, [r['book_id'] for r in books],
                                             progress=lambda done, total, _: status.update(label=f'正在抓取 {done}/{total}'))
            status.update(label=f'抓取 {summary["fetched"]} 本，失败 {len(summary["failed"])} 本',
                          state='complete' if not summary['failed'] else 'error')
        st.rerun()
    ready = [r for r in books if r['detail_title']]
    todo = [r for r in ready if r['book_id'] not in carded]
    if not config.ready:
        st.caption('配置模型后才能生成分析卡。')
    if st.button(f'为还没有分析卡的 {len(todo)} 本生成分析卡', type='primary', key=f'bd_batch_{category}',
                 disabled=not (config.ready and todo)):
        outcomes = []
        with st.status('正在生成分析卡…', expanded=True) as status:
            client = ModelClient(config)
            for index, r in enumerate(todo, 1):
                status.update(label=f'正在分析 {index}/{len(todo)}：{entry_title(r)}')
                try:
                    analysis.analyze_book(store, client, r['book_id'], category=category)
                    outcomes.append(f'完成：{entry_title(r)}')
                except (ValidationError, ModelError) as error:
                    outcomes.append(f'失败：{entry_title(r)}（{error}）')
                st.write(outcomes[-1])
            status.update(label='分析完成', state='complete')
        st.session_state[f'bd_outcomes_{category}'] = outcomes
        st.rerun()
    outcomes = st.session_state.get(f'bd_outcomes_{category}', [])
    failures = [line for line in outcomes if line.startswith('失败')]
    if outcomes:
        st.caption(f'上次批量分析：成功 {len(outcomes) - len(failures)} 本，失败 {len(failures)} 本。失败的可以再点一次重试。')
        for line in failures:
            st.warning(line)
    if ready:
        with st.expander('附补充样本分析单本（可选）'):
            lookup = {r['book_id']: r for r in ready}
            picked = st.selectbox('书', list(lookup), format_func=lambda n: entry_title(lookup[n]), key=f'bd_one_{category}')
            source = st.text_input('样本说明', placeholder='例如：第 1–3 章正文 / 我的阅读笔记', key=f'bd_source_{category}')
            extra = st.text_area('补充样本', height=200, key=f'bd_extra_{category}',
                                 placeholder='粘贴你合法拿到的正文，或你自己写的阅读笔记。只发送给模型，不会保存。')
            if st.button('分析这一本', disabled=not config.ready, key=f'bd_run_one_{category}'):
                try:
                    with st.spinner('正在分析…'):
                        analysis.analyze_book(store, ModelClient(config), picked, category=category,
                                              extra_text=extra, extra_source=source)
                    notice('分析卡已生成。')
                    st.rerun()
                except (ValidationError, ModelError) as error:
                    st.error(str(error))
    st.markdown(f'### {category} 的分析卡')
    for card in [c for c in analysis.cards(store) if c['category'] == category]:
        with st.expander(f'{card["title"]} · {card["source"]}'):
            show_fields(card['card'], analysis.FIELDS)
            if st.button('删除这张分析卡', key=f'bd_delete_{card["id"]}'):
                analysis.delete_card(store, card['id'])
                st.rerun()


def show_reports():
    cards = analysis.cards(store)
    categories = sorted({c['category'] for c in cards if c['category']})
    if not categories:
        st.info('还没有分析卡。先在“拆书分析”里给一个分类生成分析卡。')
        return
    category = st.selectbox('分类', categories, key='report_category')
    books = {c['book_id'] or c['id'] for c in cards if c['category'] == category}
    st.caption(f'{category} 有 {len(books)} 本书的分析卡。题材报告横向对比这些书：共同卖点、书名公式、人设模板、开篇规律、'
               '爽点节奏、差异化空间和新书建议。')
    if st.button('生成题材报告', type='primary', key='report_run', disabled=not (config.ready and len(books) >= 2)):
        try:
            with st.spinner('正在生成题材报告…'):
                analysis.category_report(store, ModelClient(config), category)
            notice('题材报告已生成。')
            st.rerun()
        except (ValidationError, ModelError) as error:
            st.error(str(error))
    for index, report in enumerate(r for r in analysis.reports(store) if r['category'] == category):
        st.markdown(f'### {category} 题材报告 · {report["created_at"][:16].replace("T", " ")} UTC · {len(report["card_ids"])} 本')
        show_fields(report['report'], analysis.REPORT_FIELDS)
        if index == 0:
            st.divider()


def show_research():
    st.title('选题研究')
    st.write('抓取番茄排行榜的公开数据，看哪类题材热、哪本新书在冲。书名、作者、简介用了字体反爬，这里不解码，'
             '残缺的字显示为 □；感兴趣的书点链接在浏览器里看，再到“书名标注”里记下来。')
    shots = research.snapshots(store)
    if st.button('抓取最新榜单', type='primary', key='fetch_rank'):
        with st.status('正在抓取番茄排行榜…', expanded=True) as status:
            try:
                _, summary = research.fetch_snapshot(
                    store, progress=lambda done, total, name: status.update(label=f'正在抓取 {done}/{total}：{name}'))
                status.update(label=f'完成：{summary["pages"]} 页、{summary["entries"]} 条，失败 {len(summary["failed"])} 页',
                              state='complete' if not summary['failed'] else 'error')
                if shots and shots[0]['stat_date'] == summary['stat_date']:
                    notice('这份快照与上一份统计日期相同。排行榜每天下午 3 点前更新上一日的数据。')
                st.rerun()
            except research.ResearchError as error:
                status.update(label='抓取失败', state='error')
                st.error(str(error))
    st.caption('共 74 个榜单页，每页间隔 2 秒，约 3 分钟；只抓公开榜单页，不抓正文。')
    if not shots:
        st.info('还没有榜单数据。点上面的按钮抓取一次。')
        return
    labels = {s['id']: f'统计至 {s["stat_date"]} · 抓取于 {s["fetched_at"][:16].replace("T", " ")} UTC' for s in shots}
    current = st.selectbox('快照', list(labels), format_func=labels.get, key='rank_snapshot')
    shot = next(s for s in shots if s['id'] == current)
    failed = json.loads(shot['failed'])
    st.caption(f'统计至 {shot["stat_date"]}：{shot["pages"]} 页、{shot["entries"]} 条'
               + (f'；{len(failed)} 页抓取失败' if failed else '') + '。')
    older = [s['id'] for s in shots if s['id'] < current and s['stat_date'] != shot['stat_date']]
    previous = st.selectbox('对比', [None, *older], key=f'rank_compare_{current}',
                            index=1 if older else 0, format_func=lambda n: '不对比' if n is None else labels[n])
    heat_tab, rising_tab, all_tab, breakdown_tab, report_tab, topics_tab, notes_tab = st.tabs(
        ['题材热度', '新书冲榜', '全部榜单', '拆书分析', '题材报告', '选题方案', '书名标注'])
    rows = research.entries(store, current)
    with heat_tab:
        board = st.radio('榜单', ['新书榜', '阅读榜'], horizontal=True, key='heat_board')
        st.caption('按分类合计前 10 名的在读人数。新书榜更能看出当下什么题材在起量。')
        st.dataframe([{'频道': h['channel'], '分类': h['category'], '前10在读合计（万）': wan(h['total']),
                       '变化（万）': wan(h['change']), '变化%': h['change_pct']}
                      for h in research.category_heat(store, current, previous) if h['board'] == board],
                     hide_index=True, width='stretch')
    with rising_tab:
        board = st.radio('榜单', ['新书榜', '阅读榜'], horizontal=True, key='rising_board')
        if previous is None:
            st.info('需要另一天的快照才能计算增长。下面先按在读人数排列。')
            ranked = sorted((r for r in rows if r['board'] == board), key=lambda r: -(r['reading'] or 0))
            ranked = [{**r, 'growth': None, 'previous_rank': None, 'chapters_gained': None, 'is_new': False} for r in ranked]
        else:
            ranked = [r for r in research.rising_books(store, current, previous) if r['board'] == board]
        st.dataframe([{'分类': r['category'], '排名': r['rank'], '上次排名': '新上榜' if r['is_new'] else r['previous_rank'],
                       '书名': entry_title(r), '在读（万）': wan(r['reading']), '增长（万）': wan(r['growth']),
                       '新增章节': r['chapters_gained'], '最新章节': r['latest_chapter'],
                       '链接': f'{research.BASE}/page/{r["book_id"]}'} for r in ranked[:50]],
                     hide_index=True, width='stretch',
                     column_config={'链接': st.column_config.LinkColumn('链接', display_text='打开')})
    with all_tab:
        a, b, c = st.columns(3)
        channel = a.selectbox('频道', ['男频', '女频'], key='all_channel')
        board = b.selectbox('榜单', ['新书榜', '阅读榜'], key='all_board')
        categories = sorted({r['category'] for r in rows if r['channel'] == channel and r['board'] == board})
        category = c.selectbox('分类', categories, key=f'all_category_{channel}_{board}') if categories else None
        st.dataframe([{'排名': r['rank'], '书名': entry_title(r), '标签': r['note_tags'], '在读（万）': wan(r['reading']),
                       '状态': r['status'], '最新章节': r['latest_chapter'], '更新时间': r['updated_at'],
                       '链接': f'{research.BASE}/page/{r["book_id"]}'}
                      for r in rows if (r['channel'], r['board'], r['category']) == (channel, board, category)],
                     hide_index=True, width='stretch',
                     column_config={'链接': st.column_config.LinkColumn('链接', display_text='打开')})
    with breakdown_tab:
        show_breakdown(rows)
    with report_tab:
        show_reports()
    with topics_tab:
        show_topics()
    with notes_tab:
        books = {}
        for r in rows:
            books.setdefault(r['book_id'], r)
        if not books:
            st.info('这份快照里没有书目。')
            return
        picked = st.selectbox('选择一本书', list(books), key='note_book', format_func=lambda n:
                              f'{books[n]["channel"]}{books[n]["board"]} · {books[n]["category"]} · 第 {books[n]["rank"]} 名 · {entry_title(books[n])}')
        book_row = books[picked]
        st.caption(f'在浏览器里打开 {research.BASE}/page/{picked} 查看完整书名和简介。')
        with st.form(f'note_form_{picked}'):
            title = st.text_input('书名', value=book_row['note_title'] or '', key=f'note_title_{picked}')
            tags = st.text_input('标签', value=book_row['note_tags'] or '', key=f'note_tags_{picked}',
                                 placeholder='例如：盗墓, 系统, 无敌流')
            note = st.text_area('备注', value=book_row['note'] or '', key=f'note_text_{picked}',
                                placeholder='卖点、开篇钩子、为什么能火')
            if st.form_submit_button('保存标注', type='primary'):
                try:
                    research.save_note(store, picked, title, tags, note)
                    notice('标注已保存。')
                    st.rerun()
                except ValidationError as error:
                    st.error(str(error))
        saved = research.notes(store)
        if saved:
            st.dataframe([{'书名': n['title'], '标签': n['tags'], '备注': n['note'],
                           '链接': f'{research.BASE}/page/{n["book_id"]}'} for n in saved],
                         hide_index=True, width='stretch',
                         column_config={'链接': st.column_config.LinkColumn('链接', display_text='打开')})


def show_draft_form(draft):
    data, volume = draft['draft'], draft['draft']['第一卷']
    key = lambda name: f'draft_{name}_{draft["id"]}'
    st.markdown(f'#### 开书草稿（可修改）')
    with st.form(f'draft_form_{draft["id"]}'):
        title = st.text_input('书名', value=data['书名'], key=key('title'))
        genre = st.text_input('题材', value=data['题材'], key=key('genre'))
        premise = st.text_area('故事创意', value=topics.to_text(data['故事创意']), height=80, key=key('premise'))
        bible = st.text_area('人物与世界设定', value=topics.to_text(data['人物与世界设定']), height=240, key=key('bible'))
        outline = st.text_area('总纲（主线、暗线、伏笔表）', value=topics.to_text(data['总纲']), height=240, key=key('outline'))
        style = st.text_area('写作偏好', value=topics.to_text(data['写作偏好']), height=100, key=key('style'))
        a, b, c = st.columns([2, 1, 1])
        volume_title = a.text_input('第一卷卷名', value=flat(volume['卷名']), key=key('volume_title'))
        start = b.number_input('起始章', min_value=1, step=1, value=volume['起始章'], key=key('volume_start'))
        end = c.number_input('结束章', min_value=1, step=1, value=volume['结束章'], key=key('volume_end'))
        volume_outline = st.text_area('第一卷大纲', value=topics.to_text(volume['卷大纲']), height=140, key=key('volume_outline'))
        st.caption('创建后进入“作品设定”。之后到“大纲”续写细纲，再用“自动连写”或逐章写作。')
        if st.form_submit_button('用这份草稿创建作品', type='primary'):
            try:
                new_id = topics.create_book(store, draft['id'], title=title, genre=genre, premise=premise, bible=bible,
                                            outline=outline, style=style, volume_title=volume_title,
                                            volume_start=int(start), volume_end=int(end), volume_outline=volume_outline)
                st.session_state['pending_book'] = new_id
                notice(f'《{title}》已创建，第一卷也建好了。下一步到“大纲”续写细纲。')
                st.rerun()
            except ValidationError as error:
                st.error(str(error))


def show_topics():
    reports = analysis.reports(store)
    if not reports:
        st.info('还没有题材报告。先在“拆书分析”生成分析卡，再在“题材报告”里生成报告。')
        return
    labels = {r['id']: f'{r["category"]} · {r["created_at"][:16].replace("T", " ")} UTC · {len(r["card_ids"])} 本' for r in reports}
    report_id = st.selectbox('依据的题材报告', list(labels), format_func=labels.get, key='topic_report')
    preferences = st.text_area('你的偏好（可选）', key='topic_preferences', height=80,
                               placeholder='例如：想写女主；可以有金手指但要有代价；不写同人；每天能更 4000 字')
    st.caption(f'生成 {topics.PROPOSAL_COUNT} 个差别明显的方案：至少一个走主流爽点路线，至少一个走报告里的差异化空间。'
               '只借鉴结构和节奏，不用现有作品的书名、人物和桥段。')
    if st.button(f'生成 {topics.PROPOSAL_COUNT} 个选题方案', type='primary', disabled=not config.ready, key='topic_run'):
        try:
            with st.spinner('正在生成选题方案…'):
                topics.propose(store, ModelClient(config), report_id, preferences)
            notice('选题方案已生成。')
            st.rerun()
        except (ValidationError, ModelError) as error:
            st.error(str(error))
    sets = [s for s in topics.proposal_sets(store) if s['report_id'] == report_id]
    if not sets:
        return
    current = sets[0]
    if current['preferences']:
        st.caption(f'最近一组方案的偏好：{current["preferences"]}')
    for index, proposal in enumerate(current['proposals']):
        with st.expander(f'方案 {index + 1}：{flat(proposal["书名"])}', expanded=index == 0):
            show_fields(proposal, topics.PROPOSAL_FIELDS[1:])
            if st.button('用这个方案生成开书草稿', disabled=not config.ready, key=f'topic_draft_{current["id"]}_{index}'):
                try:
                    with st.spinner('正在生成开书草稿…'):
                        topics.draft_book(store, ModelClient(config), current['id'], index)
                    notice('开书草稿已生成，在下方修改后创建作品。')
                    st.rerun()
                except (ValidationError, ModelError) as error:
                    st.error(str(error))
    for draft in topics.drafts(store, current['id']):
        if draft['book_id']:
            st.caption(f'方案 {draft["index"] + 1} 的草稿已经开书。')
            continue
        st.caption(f'基于方案 {draft["index"] + 1}：{flat(current["proposals"][draft["index"]]["书名"])}')
        show_draft_form(draft)
        break


def show_memory():
    st.title('故事记忆')
    st.write('每条记忆都来自你确认过的定稿。前文重新定稿后，后续章节会标为待复核；这只是提醒，不影响继续写作。')
    memories = store.memory(book_id)
    if not memories:
        st.info('还没有正式记忆。完成第一章后，在“定稿与记忆”里确认摘要和事实。')
    for memory in memories:
        label = '待复核' if memory['stale'] else '已确认'
        with st.expander(f'第 {memory["number"]} 章 · {label} · 来源版本 {memory["revision_id"]}', expanded=bool(memory['stale'])):
            st.write(memory['summary'])
            if memory['facts']:
                st.dataframe(pd.DataFrame(memory['facts']).rename(columns={'entity': '对象', 'attribute': '属性', 'value': '事实'}),
                             hide_index=True, width='stretch')
    st.caption(f'续写时发送本章计划、正文和近期摘要里提到的对象的事实，加上最近 {RECENT_CHAPTERS} 章更新的事实、最近 6 章摘要，以及上一章定稿结尾。')


def show_exports():
    st.title('存稿与导出')
    total = sum(count_chars(ch['body']) for ch in chapters)
    finalized = [ch for ch in chapters if ch['finalized_revision_id']]
    a, b, c = st.columns(3)
    a.metric('已建章节', len(chapters))
    b.metric('定稿章节', len(finalized))
    c.metric('当前稿字数', f'{total:,}')
    st.caption('字数按去除空白后的字符估算；番茄后台的统计可能不同。')
    st.markdown('### 导出稿件')
    st.write('下载 TXT 后，将章节标题与正文分别填入番茄后台。发布状态由你根据平台结果记录。')
    stale = [ch['number'] for ch in finalized if ch['stale']]
    if stale:
        st.warning(f'{stale_hint(stale)}。仍可导出，发布前建议核对。')
    if st.button('准备整书定稿 TXT 压缩包', disabled=not finalized):
        try:
            blob, records = store.export_book(book_id)
            st.session_state[f'export_{book_id}'] = {'data': blob, 'records': records,
                                                     'version': book['context_version']}
        except ValidationError as error:
            st.error(str(error))
    bundle = st.session_state.get(f'export_{book_id}')
    if bundle:
        if bundle['version'] != book['context_version']:
            st.info('定稿或设定已更新，请重新准备压缩包。')
        else:
            if st.download_button('下载整书定稿 TXT', bundle['data'], file_name=f'{safe_filename(book["title"])}_定稿.zip',
                                  mime='application/zip', key=f'zip_{book_id}'):
                for record in bundle['records']:
                    store.record_publication(record['chapter_id'], record['revision_id'], '已导出', '整书定稿 TXT')
                notice('已记录导出版本；这不代表平台已发布。')
    if chapters:
        lookup = {ch['id']: ch for ch in chapters}
        chapter_id = st.selectbox('单章导出', list(lookup),
                                  format_func=lambda n: f'第 {lookup[n]["number"]} 章 · {lookup[n]["title"]}', key='export_chapter')
        chapter = lookup[chapter_id]
        mode = st.radio('稿件版本', ['当前稿', '定稿'], horizontal=True)
        revision_id = chapter['current_revision_id'] if mode == '当前稿' else chapter['finalized_revision_id']
        if revision_id:
            revision = store.revision(revision_id)
            if mode == '定稿' and chapter['stale']:
                st.warning('本章记忆待复核，请在发布前核对正文。')
            st.text_area('可复制的正文', value=revision['body'], height=170, disabled=False,
                         key=f'export_text_{revision_id}')
            st.caption('上方仅供复制；修改请返回章节工作区。')
            txt = store.chapter_txt(chapter['number'], revision).encode('utf-8-sig')
            if st.download_button('下载本章 TXT', txt,
                                  file_name=f'{chapter["number"]:04d}_{safe_filename(revision["title"])}_{mode}.txt',
                                  mime='text/plain', key=f'single_{revision_id}'):
                store.record_publication(chapter_id, revision_id, '已导出', f'单章{mode}')
            with st.form('publication'):
                status = st.selectbox('番茄后台状态', ['已存平台草稿', '审核中', '定时发布', '已发布', '退回修改'])
                note = st.text_input('备注', placeholder='可填写平台章节链接、实际排期或退回原因')
                if st.form_submit_button('记录平台状态'):
                    store.record_publication(chapter_id, revision_id, status, note)
                    notice('已记录你确认的平台状态。')
                    st.rerun()
        else:
            st.info('本章还没有定稿版本。')
    records = store.publications(book_id)
    if records:
        st.markdown('### 发布记录')
        st.dataframe(pd.DataFrame(records)[['number', 'revision_id', 'status', 'note', 'created_at']].rename(
            columns={'number': '章节', 'revision_id': '稿件版本', 'status': '状态', 'note': '备注', 'created_at': '记录时间（UTC）'}),
            hide_index=True, width='stretch')
    st.divider()
    if st.button('准备全部作品数据库备份'):
        st.session_state['backup'] = store.backup_bytes()
    if 'backup' in st.session_state:
        st.download_button('下载数据库备份', st.session_state['backup'], file_name='novels-backup.sqlite3',
                           mime='application/octet-stream')
        st.caption('备份包含全部作品、版本、记忆和调用记录，不包含 API 密钥。恢复前先关闭应用并保留原数据库副本。')


def show_model():
    st.title('模型与调用记录')
    if config.ready:
        st.success('配置已就绪。实际连通性与中文写作效果需要使用你的模型服务验证。')
        st.write(f'模型：{config.model}')
        st.write(f'接口：{config.endpoint}')
    else:
        st.info('模型尚未配置。编辑与保存功能可以照常使用。')
    st.write('将项目中的 `.env.example` 复制为 `.env`，在本机填写下列配置后重启应用。')
    st.code('NOVEL_API_URL=服务商完整的 Chat Completions 请求地址\nNOVEL_MODEL=模型名称\nNOVEL_API_KEY=你的密钥', language='bash')
    st.caption('当前适配 Chat Completions 协议；其他协议需增加适配器。密钥不写入数据库。每次生成前可以查看将发送的资料。')
    st.caption(f'当前配置：超时 {config.timeout:g} 秒；最大输出 {config.max_tokens} token；'
               f'规划本章的思考强度 {config.plan_reasoning or "默认"}（NOVEL_PLAN_REASONING）。没有设置价格，暂不估算金额。')
    failed = analysis.failures(store, 10)
    if failed:
        with st.expander(f'选题研究的模型输出解析失败记录（最近 {len(failed)} 条）'):
            st.caption('分析卡、题材报告、选题方案、开书草稿的输出没能按格式解析时，原始输出保存在这里，方便查原因。')
            for item in failed:
                st.markdown(f'**{item["task"]}** · {item["created_at"][:16].replace("T", " ")} UTC · {item["error"]}')
                st.text_area('原始输出', item['output'], height=160, disabled=True, key=f'failure_{item["id"]}')
    st.markdown('### 调用记录')
    runs = store.runs(book_id)
    if not runs:
        st.write('还没有模型调用记录。')
        return
    rows = []
    for run in runs:
        usage = json.loads(run['usage'])
        rows.append({'任务': run['id'], '类型': run['task'], '状态': run['status'], '模型': run['model'],
                     '输入 token': usage.get('prompt_tokens'), '输出 token': usage.get('completion_tokens'),
                     '耗时（秒）': round(run['elapsed'], 1), '时间（UTC）': run['created_at']})
    st.dataframe(rows, hide_index=True, width='stretch')
    selected = st.selectbox('查看一次调用', [r['id'] for r in runs])
    run = next(r for r in runs if r['id'] == selected)
    if run['error']:
        st.warning(run['error'])
    with st.expander('当时发送的故事资料'):
        st.json(json.loads(run['prompt']), expanded=False)
    st.text_area('返回内容', run['output'], height=260, disabled=True, key=f'audit_output_{selected}')


{'作品设定': show_settings, '大纲': show_outline, '章节工作区': show_chapters, '自动连写': show_autowrite, '故事记忆': show_memory,
 '存稿与导出': show_exports, '选题研究': show_research, '模型与记录': show_model}[section]()
