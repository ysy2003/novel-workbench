import json
import zipfile
from io import BytesIO

import pytest

from novel.store import Store, Conflict, ValidationError


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'novels.sqlite3')


def story(db):
    book = db.create_book('夜行灯', premise='一个修灯人寻找失踪的姐姐。')
    chapter = db.create_chapter(book, '旧灯', '收到一封信')
    current = db.chapter(chapter)['current_revision_id']
    revision = db.save_revision(chapter, '旧灯', '收到一封信', '林川左臂受伤。', current)
    return book, chapter, revision


def finalize(db, book, chapter, revision, value='受伤'):
    db.finalize(chapter, revision, '林川在修灯时受伤。',
                [{'entity': '林川', 'attribute': '左臂', 'value': value}],
                db.book(book)['context_version'])


def test_persistence_and_non_destructive_restore(db):
    book, chapter, revision = story(db)
    newer = db.save_revision(chapter, '雨夜', '寻找线索', '林川收起了信。', revision)
    restored = db.restore(chapter, revision, newer)
    assert restored not in (revision, newer)
    reopened = Store(db.path)
    assert reopened.revision(newer)['body'] == '林川收起了信。'
    assert reopened.revision(restored)['body'] == '林川左臂受伤。'
    assert reopened.chapter(chapter)['current_revision_id'] == restored


def test_stale_editor_cannot_overwrite_newer_revision(db):
    _, chapter, revision = story(db)
    newer = db.save_revision(chapter, '旧灯', '', '新稿', revision)
    with pytest.raises(Conflict):
        db.save_revision(chapter, '旧灯', '', '过期窗口的稿子', revision)
    assert db.chapter(chapter)['current_revision_id'] == newer


def test_ai_candidate_does_not_overwrite_draft_or_enter_memory(db):
    book, chapter, revision = story(db)
    candidate = db.add_candidate(chapter, '旧灯', '计划', '未采纳的 AI 稿', revision,
                                 db.book(book)['context_version'])
    assert db.chapter(chapter)['current_revision_id'] == revision
    assert db.memory(book) == []
    db.save_revision(chapter, '旧灯', '', '手工修改', revision)
    with pytest.raises(Conflict):
        db.adopt_candidate(chapter, candidate)
    with pytest.raises(ValidationError):
        db.restore(chapter, candidate, db.chapter(chapter)['current_revision_id'])


def test_finalize_updates_only_confirmed_facts_and_blocks_gaps(db):
    book, chapter, revision = story(db)
    second = db.create_chapter(book, '第二夜', '')
    second_rev = db.save_revision(second, '第二夜', '', '林川望向窗外。',
                                  db.chapter(second)['current_revision_id'])
    with pytest.raises(ValidationError, match='前面的章节'):
        finalize(db, book, second, second_rev)
    finalize(db, book, chapter, revision)
    assert db.memory(book)[0]['revision_id'] == revision
    assert db.context(second)['facts'][0]['value'] == '受伤'
    db.save_revision(chapter, '旧灯', '', '尚未定稿：左臂已经痊愈。', revision)
    assert db.context(second)['facts'][0]['value'] == '受伤'


def test_refinalization_flags_later_memory_without_blocking(db):
    book, chapter, revision = story(db)
    finalize(db, book, chapter, revision)
    second = db.create_chapter(book, '次日', '')
    second_rev = db.save_revision(second, '次日', '', '林川仍无法使剑。',
                                  db.chapter(second)['current_revision_id'])
    finalize(db, book, second, second_rev)
    third = db.create_chapter(book, '第三夜', '')
    replacement = db.save_revision(chapter, '旧灯', '', '林川右臂受伤。', revision)
    db.finalize(chapter, replacement, '右臂受伤',
                [{'entity': '林川', 'attribute': '右臂', 'value': '受伤'}],
                db.book(book)['context_version'])
    assert db.memory(book)[1]['stale'] == 1
    context = db.context(third)
    assert context['stale_chapters'] == [2]
    assert {fact['attribute'] for fact in context['facts']} == {'右臂', '左臂'}
    assert db.context(second)['facts'][0]['attribute'] == '右臂'
    assert db.revision(second_rev)['body'] == '林川仍无法使剑。'


def serial(db, count):
    book = db.create_book('连载')
    ids = []
    for number in range(1, count + 1):
        chapter = db.create_chapter(book, f'第{number}夜')
        revision = db.save_revision(chapter, f'第{number}夜', '', f'林川走过第{number}条街。',
                                    db.chapter(chapter)['current_revision_id'])
        db.finalize(chapter, revision, f'第{number}条街',
                    [{'entity': '林川', 'attribute': '位置', 'value': f'第{number}条街'}],
                    db.book(book)['context_version'])
        ids.append(chapter)
    return book, ids


def fix_typo_and_refinalize(db, book, chapter):
    revision = db.save_revision(chapter, '改过错字', '', '林川走过街口。', db.chapter(chapter)['current_revision_id'])
    db.finalize(chapter, revision, '改后的摘要', [], db.book(book)['context_version'])


def reconfirm(db, book, chapter):
    memory = next(m for m in db.memory(book) if m['chapter_id'] == chapter)
    db.finalize(chapter, memory['revision_id'], memory['summary'], memory['facts'], db.book(book)['context_version'])


def stale_numbers(db, book):
    return [m['number'] for m in db.memory(book) if m['stale']]


def test_later_chapter_can_finalize_while_earlier_memory_awaits_review(db):
    book, (first, _) = serial(db, 2)
    fix_typo_and_refinalize(db, book, first)
    third = db.create_chapter(book, '第3夜')
    revision = db.save_revision(third, '第3夜', '', '林川回家。', db.chapter(third)['current_revision_id'])
    finalize(db, book, third, revision)
    assert stale_numbers(db, book) == [2]


def test_reconfirming_unchanged_memory_does_not_flag_later_chapters(db):
    book, (first, second, third) = serial(db, 3)
    fix_typo_and_refinalize(db, book, first)
    assert stale_numbers(db, book) == [2, 3]
    reconfirm(db, book, third)
    reconfirm(db, book, second)
    assert stale_numbers(db, book) == []


def test_batch_export_includes_chapters_awaiting_review(db):
    book, (first, _) = serial(db, 2)
    fix_typo_and_refinalize(db, book, first)
    blob, records = db.export_book(book)
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        assert len(archive.namelist()) == 2
    assert len(records) == 2


def test_settings_change_keeps_finalized_memory_usable(db):
    book, chapter, revision = story(db)
    finalize(db, book, chapter, revision)
    second = db.create_chapter(book, '次日', '')
    db.update_book(book, title='夜行灯', premise='一个修灯人寻找失踪的姐姐。', genre='悬疑',
                   bible='林川怕水。', style='', outline='姐姐在第十章出现。')
    assert db.memory(book)[0]['stale'] == 0
    assert db.context(second)['facts'][0]['value'] == '受伤'


def test_settings_change_still_rejects_candidates_generated_before_it(db):
    book, chapter, revision = story(db)
    candidate = db.add_candidate(chapter, '旧灯', '', 'AI 稿', revision, db.book(book)['context_version'])
    db.update_book(book, title='夜行灯', premise='一个修灯人寻找失踪的姐姐。', genre='',
                   bible='林川怕水。', style='', outline='')
    with pytest.raises(Conflict):
        db.adopt_candidate(chapter, candidate)


def test_invalid_or_stale_memory_commit_is_atomic(db):
    book, chapter, revision = story(db)
    version = db.book(book)['context_version']
    with pytest.raises(ValidationError):
        db.finalize(chapter, revision, '摘要', [{'entity': '林川'}], version)
    assert db.chapter(chapter)['finalized_revision_id'] is None
    assert db.memory(book) == []
    db.update_book(book, title='夜行灯', premise='更改后的故事', genre='', bible='', style='', outline='')
    with pytest.raises(Conflict):
        db.finalize(chapter, revision, '摘要', [], version)


def test_books_are_isolated_and_cross_chapter_restore_is_rejected(db):
    book, chapter, revision = story(db)
    finalize(db, book, chapter, revision)
    other = db.create_book('另一部书')
    other_chapter = db.create_chapter(other, '第一章', '')
    assert db.context(other_chapter)['facts'] == []
    with pytest.raises(ValidationError):
        db.restore(other_chapter, revision, db.chapter(other_chapter)['current_revision_id'])


def test_exports_use_finalized_versions_in_numeric_order_and_keep_status_distinct(db):
    book, chapter, revision = story(db)
    finalize(db, book, chapter, revision)
    edited = db.save_revision(chapter, '新标题', '', '新草稿不应该导出为定稿。', revision)
    blob, records = db.export_book(book)
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        names = archive.namelist()
        assert names == ['0001_旧灯.txt']
        assert archive.read(names[0]).decode('utf-8-sig') == '第1章 旧灯\n\n林川左臂受伤。\n'
    assert records[0]['revision_id'] == revision
    assert records[0]['status'] == '已导出'
    record = db.record_publication(chapter, revision, '已发布', '番茄后台确认')
    assert db.publications(book)[0]['id'] == record
    assert db.chapter(chapter)['current_revision_id'] == edited


def test_current_facts_resolve_changes_in_chapter_order(db):
    book, chapter, revision = story(db)
    finalize(db, book, chapter, revision)
    second = db.create_chapter(book, '康复', '')
    second_rev = db.save_revision(second, '康复', '', '三个月后，左臂痊愈。',
                                  db.chapter(second)['current_revision_id'])
    finalize(db, book, second, second_rev, '痊愈')
    third = db.create_chapter(book, '归来', '')
    context = db.context(third)
    assert context['facts'] == [{'entity': '林川', 'attribute': '左臂', 'value': '痊愈',
                                 'chapter_number': 2, 'revision_id': second_rev}]


def test_backup_is_a_reopenable_database(db, tmp_path):
    book, chapter, revision = story(db)
    snapshot = tmp_path / 'restored.sqlite3'
    snapshot.write_bytes(db.backup_bytes())
    assert Store(snapshot).revision(revision)['body'] == '林川左臂受伤。'


def test_ten_chapters_export_in_numeric_order_with_latest_state(db):
    book = db.create_book('十章流程验证')
    for number in range(1, 11):
        chapter = db.create_chapter(book, f'灯{number}')
        revision = db.save_revision(chapter, f'灯{number}', '继续寻找', f'林川来到第{number}座城。',
                                    db.chapter(chapter)['current_revision_id'])
        if number > 1:
            assert db.context(chapter)['facts'][0]['value'] == f'第{number - 1}座城'
        db.finalize(chapter, revision, f'来到第{number}座城',
                    [{'entity': '林川', 'attribute': '位置', 'value': f'第{number}座城'}],
                    db.book(book)['context_version'])
    blob, _ = db.export_book(book)
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        assert [name[:4] for name in archive.namelist()] == [f'{n:04d}' for n in range(1, 11)]
