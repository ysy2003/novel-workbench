from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

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


def test_settings_form_does_not_overwrite_another_windows_changes(app):
    at, db = app
    new_book(at)
    db.update_book(1, title='另一窗口改过的标题', premise='另一窗口的创意', genre='', bible='', style='', outline='')
    at.text_input('settings_title_1').input('旧窗口试图保存')
    button(at, '保存设定').click().run()
    assert not at.exception
    assert db.book(1)['title'] == '另一窗口改过的标题'
    assert any('其他窗口更新' in e.value for e in at.error)
