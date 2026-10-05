from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import json

from novel import analysis, research, shelf, topics
from novel.store import Store


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app(tmp_path, monkeypatch):
    path = tmp_path / 'ui.sqlite3'
    monkeypatch.setenv('NOVEL_DB_PATH', str(path))
    monkeypatch.setenv('NOVEL_API_KEY', '')
    at = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=15).run()
    assert not at.exception
    return at, Store(path)


def button(at, label):
    return next(b for b in at.button if b.label == label)


def new_book(at):
    at.text_input('welcome_create_title').input('雨夜来信')
    at.text_area('welcome_create_premise').input('修灯人收到一封没有署名的信。')
    button(at, '创建作品').click().run()
    assert not at.exception


def test_create_edit_finalize_continue_export_and_reopen(app):
    at, db = app
    new_book(at)
    at.radio('section').set_value('章节工作区').run()
    at.text_input('new_chapter_title').input('旧灯')
    at.text_area('new_chapter_plan').input('发现一封信，决定出门。')
    button(at, '创建章节').click().run()
    assert not at.exception
    at.text_area('edit_body_1').input('林川左臂受伤，仍然走入了雨中。').run()
    assert db.revision(db.chapter(1)['current_revision_id'])['body'] == '林川左臂受伤，仍然走入了雨中。'
    next(t for t in at.text_area if t.label == '本章摘要').input('林川受伤后仍出门寻找线索。')
    button(at, '确认记忆并定稿').click().run()
    assert not at.exception
    assert db.chapter(1)['finalized_revision_id']
    at.text_input('new_chapter_title').input('来客')
    at.text_area('new_chapter_plan').input('一个陌生人来到修灯铺。')
    button(at, '创建章节').click().run()
    assert not at.exception
    assert at.selectbox('chapter_pick_1').value == 2
    assert db.context(2)['recent_summaries'][0]['summary'] == '林川受伤后仍出门寻找线索。'
    at.radio('section').set_value('存稿与导出').run()
    assert not at.exception
    button(at, '准备整书定稿 TXT 压缩包').click().run()
    assert not at.exception
    assert at.session_state['export_1']['data'][:2] == b'PK'
    at.radio('section').set_value('章节工作区').run()
    at.selectbox('chapter_pick_1').select(1).run()
    assert at.text_area('edit_body_1').value == '林川左臂受伤，仍然走入了雨中。'


def test_sidebar_can_create_second_book_and_all_sections_render(app):
    at, db = app
    new_book(at)
    at.text_input('sidebar_create_title').input('第二本故事')
    button(at, '创建作品').click().run()
    assert not at.exception
    assert len(db.books()) == 2
    assert at.selectbox('book_pick').value == 2
    for section in ('故事记忆', '存稿与导出', '模型与记录', '作品设定'):
        at.radio('section').set_value(section).run()
        assert not at.exception, section


def test_editing_settings_after_finalize_does_not_block_next_chapter(app):
    at, db = app
    new_book(at)
    at.radio('section').set_value('章节工作区').run()
    at.text_input('new_chapter_title').input('旧灯')
    button(at, '创建章节').click().run()
    at.text_area('edit_body_1').input('林川左臂受伤。').run()
    next(t for t in at.text_area if t.label == '本章摘要').input('林川受伤。')
    button(at, '确认记忆并定稿').click().run()
    assert db.chapter(1)['finalized_revision_id']
    at.radio('section').set_value('作品设定').run()
    at.text_area('settings_bible_1').input('林川怕水。')
    button(at, '保存设定').click().run()
    assert not at.exception
    assert db.book(1)['bible'] == '林川怕水。'
    at.radio('section').set_value('章节工作区').run()
    at.text_input('new_chapter_title').input('来客')
    button(at, '创建章节').click().run()
    assert not at.exception
    assert at.selectbox('chapter_pick_1').value == 2
    assert not any('前面的章节' in i.value for i in at.info)


def test_refinalizing_old_chapter_only_flags_later_chapters(app):
    at, db = app
    new_book(at)
    at.radio('section').set_value('章节工作区').run()
    for number, title in ((1, '旧灯'), (2, '来客')):
        at.text_input('new_chapter_title').input(title)
        button(at, '创建章节').click().run()
        at.text_area(f'edit_body_{number}').input(f'第{number}章正文。').run()
        next(t for t in at.text_area if t.label == '本章摘要').input(f'第{number}章摘要')
        button(at, '确认记忆并定稿').click().run()
        assert db.chapter(number)['finalized_revision_id']
    at.selectbox('chapter_pick_1').select(1).run()
    at.text_area('edit_body_1').input('第1章正文，改了错字。').run()
    button(at, '确认记忆并定稿').click().run()
    assert not at.exception
    assert db.memory(1)[1]['stale'] == 1
    at.text_input('new_chapter_title').input('归来')
    button(at, '创建章节').click().run()
    assert not at.exception
    assert not any('前面的章节' in i.value for i in at.info)
    assert any('待复核' in i.value for i in at.info)
    at.radio('section').set_value('存稿与导出').run()
    button(at, '准备整书定稿 TXT 压缩包').click().run()
    assert not at.exception
    assert at.session_state['export_1']['data'][:2] == b'PK'
    assert any('待复核' in w.value for w in at.warning)


def test_indented_body_is_saved_once_with_indent(app):
    at, db = app
    new_book(at)
    at.radio('section').set_value('章节工作区').run()
    at.text_input('new_chapter_title').input('旧灯')
    button(at, '创建章节').click().run()
    at.text_area('edit_body_1').input('\u3000\u3000林川推开门。\n\u3000\u3000雨停了。').run()
    assert db.revision(db.chapter(1)['current_revision_id'])['body'] == '\u3000\u3000林川推开门。\n\u3000\u3000雨停了。'
    saved = len(db.revisions(1))
    button(at, '保存当前稿').click().run()
    assert not at.exception
    assert len(db.revisions(1)) == saved


def test_outline_section_saves_volume_and_creates_chapters_from_beats(app):
    at, db = app
    new_book(at)
    at.radio('section').set_value('大纲').run()
    assert not at.exception
    at.text_input('volume_title_1_new').input('第一夜')
    at.text_area('volume_outline_1_new').input('林默入职，活过第一周。')
    button(at, '保存分卷').click().run()
    assert not at.exception
    assert [(v['number'], v['title'], v['start_chapter'], v['end_chapter']) for v in db.volumes(1)] == [(1, '第一夜', 1, 30)]
    db.save_beats(1, 1, 30, [{'number': 1, 'title': '第一条规则', 'brief': '入职第一夜'},
                             {'number': 2, 'title': '灯牌', 'brief': '不关灯牌'}])
    at.run()
    assert any('细纲只领先' in w.value for w in at.warning)
    at.radio('section').set_value('章节工作区').run()
    button(at, '按细纲创建第 1 章：第一条规则').click().run()
    assert not at.exception
    revision = db.revision(db.chapter(1)['current_revision_id'])
    assert (revision['title'], revision['plan']) == ('第一条规则', '入职第一夜')
    assert any(b.label == '按细纲创建第 2 章：灯牌' for b in at.button)


def test_autowrite_section_shows_how_far_it_can_write_and_needs_a_model(app):
    at, db = app
    new_book(at)
    db.save_volume(1, 1, '第一夜', 1, 30, '')
    db.save_beats(1, 1, 30, [{'number': n, 'title': f'细纲{n}', 'brief': '要点'} for n in (1, 2, 3)])
    at.radio('section').set_value('自动连写').run()
    assert not at.exception
    assert any('现有连续细纲 3 章，不够时会自动续写' in c.value for c in at.caption)
    assert button(at, '开始自动连写').disabled


def test_autowrite_without_beats_can_start_only_when_auto_beats_is_on(app):
    at, db = app
    new_book(at)
    db.save_volume(1, 1, '第一夜', 1, 30, '')
    at.radio('section').set_value('自动连写').run()
    assert at.checkbox('auto_beats_1').value is True
    assert not any('请先到“大纲”续写细纲' in i.value for i in at.info)
    at.checkbox('auto_beats_1').uncheck().run()
    assert not at.exception
    assert any('请先到“大纲”续写细纲' in i.value for i in at.info)


def test_research_section_shows_snapshots_and_saves_title_notes(app):
    at, db = app
    new_book(at)
    at.radio('section').set_value('选题研究').run()
    assert not at.exception
    assert any('还没有榜单数据' in i.value for i in at.info)
    row = {'channel': '男频', 'board': '新书榜', 'category_id': 751, 'category': '悬疑灵异', 'rank': 1,
           'book_id': '111', 'title': '盗墓□八创雷', 'title_complete': 0, 'reading': 134000, 'status': '连载中',
           'latest_chapter': '第79章 夜班', 'updated_at': '2026-10-04 08:36'}
    research.save_snapshot(db, '10-03 24:00', 1, [{**row, 'reading': 100000}], [])
    research.save_snapshot(db, '10-04 24:00', 1, [row], [])
    at.run()
    assert not at.exception
    assert any('统计至 10-04 24:00' in c.value for c in at.caption)
    at.text_input('note_title_111').input('盗墓：八卦创雷')
    button(at, '保存标注').click().run()
    assert not at.exception
    assert research.notes(db)[0]['title'] == '盗墓：八卦创雷'


def test_research_breakdown_and_report_tabs_show_saved_cards_and_reports(app):
    at, db = app
    new_book(at)
    row = {'channel': '男频', 'board': '新书榜', 'category_id': 751, 'category': '悬疑灵异', 'rank': 1,
           'book_id': '111', 'title': '盗墓□八创雷', 'title_complete': 0, 'reading': 134000, 'status': '连载中',
           'latest_chapter': '第79章', 'updated_at': ''}
    research.save_snapshot(db, '10-04 24:00', 1, [row], [])
    research.save_details(db, '111', {'title': '盗墓：八岁创雷法', 'status': '连载中', 'tags': ['盗墓'],
                                      'word_count': 177000, 'intro': '简介', 'chapters': ['第1章 乱世绝境']})
    card = {field: '内容' for field in analysis.FIELDS}
    with db.connect(True) as c:
        c.execute('INSERT INTO analyses(book_id,title,category,source,sample_chars,card) VALUES(?,?,?,?,?,?)',
                  ('111', '盗墓：八岁创雷法', '悬疑灵异', '简介 + 目录 1 章', 0, json.dumps(card, ensure_ascii=False)))
        c.execute('INSERT INTO reports(category,card_ids,report) VALUES(?,?,?)',
                  ('悬疑灵异', '[1]', json.dumps({field: '规律' for field in analysis.REPORT_FIELDS}, ensure_ascii=False)))
    at.radio('section').set_value('选题研究').run()
    assert not at.exception
    assert any(b.label == '抓取这一页的书籍详情' for b in at.button)
    assert any('盗墓：八岁创雷法' in e.label for e in at.expander)
    assert any('悬疑灵异' in m.value and '题材报告' in m.value for m in at.markdown)


def test_topic_tab_creates_a_new_book_from_an_edited_draft(app):
    at, db = app
    new_book(at)
    proposal = {field: f'{field}内容' for field in topics.PROPOSAL_FIELDS}
    draft = {'书名': '夜班规则簿', '题材': '悬疑脑洞', '故事创意': '创意', '人物与世界设定': '设定', '总纲': '总纲',
             '写作偏好': '短句', '第一卷': {'卷名': '第一夜', '起始章': 1, '结束章': 30, '卷大纲': '卷纲'}}
    with db.connect(True) as c:
        report = c.execute('INSERT INTO reports(category,card_ids,report) VALUES(?,?,?)',
                           ('悬疑脑洞', '[1,2]', json.dumps({f: '内容' for f in analysis.REPORT_FIELDS}, ensure_ascii=False))).lastrowid
        proposal_id = c.execute('INSERT INTO proposals(report_id,category,proposals) VALUES(?,?,?)',
                                (report, '悬疑脑洞', json.dumps([proposal] * 3, ensure_ascii=False))).lastrowid
        draft_id = c.execute('INSERT INTO book_drafts(proposal_id,idx,draft) VALUES(?,?,?)',
                             (proposal_id, 0, json.dumps(draft, ensure_ascii=False))).lastrowid
    research.save_snapshot(db, '10-04 24:00', 0, [], [])
    at.radio('section').set_value('选题研究').run()
    assert not at.exception
    assert any(e.label.startswith('方案 1') for e in at.expander)
    at.text_input(f'draft_title_{draft_id}').input('作者改过的书名')
    button(at, '用这份草稿创建作品').click().run()
    assert not at.exception
    assert [b['title'] for b in db.books()][0] == '作者改过的书名'
    assert db.volumes(db.books()[0]['id'])[0]['title'] == '第一夜'
    assert at.radio('section').value == '作品设定'


def test_shelf_lists_books_records_platform_stats_and_opens_a_book(app):
    at, db = app
    new_book(at)
    second = db.create_book('第二本书')
    at.radio('section').set_value('书架').run()
    assert not at.exception
    assert any('2 本书' in c.value for c in at.caption)
    at.number_input('stats_metric_0').set_value(120)
    button(at, '保存数据').click().run()
    assert not at.exception
    assert shelf.stats(db, 1)[0]['metrics'] == {'在读人数': 120}
    at.selectbox('shelf_open').select(second)
    button(at, '打开这本书').click().run()
    assert not at.exception
    assert at.selectbox('book_pick').value == second and at.radio('section').value == '作品设定'


def test_settings_form_does_not_overwrite_another_windows_changes(app):
    at, db = app
    new_book(at)
    db.update_book(1, title='另一窗口改过的标题', premise='另一窗口的创意', genre='', bible='', style='', outline='')
    at.text_input('settings_title_1').input('旧窗口试图保存')
    button(at, '保存设定').click().run()
    assert not at.exception
    assert db.book(1)['title'] == '另一窗口改过的标题'
    assert any('其他窗口更新' in e.value for e in at.error)
