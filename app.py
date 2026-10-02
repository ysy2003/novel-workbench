from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from novel.model import ModelClient, ModelConfig, ModelError, parse_memory
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
        section = st.radio('工作区', ['作品设定', '章节工作区', '故事记忆', '存稿与导出', '模型与记录'], key='section')
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


def show_chapters():
    st.title('章节工作区')
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
    st.caption(f'当前配置：超时 {config.timeout:g} 秒；最大输出 {config.max_tokens} token。没有设置价格，暂不估算金额。')
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


{'作品设定': show_settings, '章节工作区': show_chapters, '故事记忆': show_memory,
 '存稿与导出': show_exports, '模型与记录': show_model}[section]()
