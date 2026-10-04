import httpx
import pytest

from novel import research
from novel.store import Store

HIDDEN = ''


@pytest.fixture
def db(tmp_path):
    return Store(tmp_path / 'research.sqlite3')


def item(rank, book_id, title, reading, status='连载中', chapter='第79章 夜班', time='2026-10-04 08:36'):
    return (f'<div class="rank-book-item"><div class="book-item-index"><h1>{rank:02d}</h1><p>-</p></div>'
            f'<img class="book-cover-img " src="x" alt="{title}"/><div class="book-item-text">'
            f'<div class="title"><a href="/page/{book_id}" class="font-x">{title}</a></div>'
            f'<div class="author"><a href="/author-page/1"><span class="font-x">作者</span></a></div>'
            f'<div class="desc abstract font-x">简介</div><div class="book-item-footer">'
            f'<span class="book-item-footer-status">{status}</span>'
            f'<span class="book-item-count">在读：<!-- -->{reading}</span>'
            f'<span class="book-item-footer-last"><a class="chapter" href="/reader/1">最近更新：<!-- -->{chapter}</a></span>'
            f'<span class="book-item-footer-time">{time}</span></div></div></div>')


MENU = ('<a level="2" href="/rank/1_1_751">悬疑灵异</a><a level="2" href="/rank/0_2_747">女频悬疑</a>'
        '<a level="2" href="/rank/1_1_751">悬疑灵异</a>')


def page(items, date='10-04 24:00'):
    return f'<html>{MENU}<p>统计时间截止至<!-- -->{date}</p><div class="muye-rank-book-list">{"".join(items)}</div></html>'


def test_reading_counts_are_parsed_to_numbers():
    assert research.parse_reading('13.4万') == 134000
    assert research.parse_reading('9876') == 9876
    assert research.parse_reading('1.2亿') == 120000000
    assert research.parse_reading('') is None


def test_category_links_map_channel_board_and_name():
    assert research.parse_categories(page([])) == [
        {'path': '/rank/1_1_751', 'channel': '男频', 'board': '新书榜', 'category_id': 751, 'category': '悬疑灵异'},
        {'path': '/rank/0_2_747', 'channel': '女频', 'board': '阅读榜', 'category_id': 747, 'category': '女频悬疑'}]


def test_rank_page_entries_are_read_and_obscured_titles_are_marked():
    stat_date, entries = research.parse_rank_page(page([
        item(1, '111', '夜班守则', '13.4万'), item(2, '222', f'盗墓{HIDDEN}八创雷', '9876', '已完结', '第2285章 终章')]))
    assert stat_date == '10-04 24:00'
    assert entries[0] == {'rank': 1, 'book_id': '111', 'title': '夜班守则', 'title_complete': 1, 'reading': 134000,
                          'status': '连载中', 'latest_chapter': '第79章 夜班', 'updated_at': '2026-10-04 08:36'}
    assert (entries[1]['title'], entries[1]['title_complete'], entries[1]['reading']) == ('盗墓□八创雷', 0, 9876)


def test_fetch_stores_every_category_and_records_failed_pages(db):
    requested = []

    def serve(request):
        requested.append(request.url.path)
        if request.url.path == '/rank':
            return httpx.Response(200, text=page([]))
        if request.url.path == '/rank/1_1_751':
            return httpx.Response(200, text=page([item(1, '111', '夜班守则', '13.4万')]))
        return httpx.Response(500, text='busy')
    snapshot, summary = research.fetch_snapshot(db, httpx.MockTransport(serve), delay=0)
    assert requested == ['/rank', '/rank/1_1_751', '/rank/0_2_747']
    assert (summary['pages'], summary['entries']) == (1, 1)
    assert summary['failed'] == [{'path': '/rank/0_2_747', 'reason': 'HTTP 500'}]
    rows = research.entries(db, snapshot)
    assert [(r['channel'], r['board'], r['category'], r['title'], r['reading']) for r in rows] == [
        ('男频', '新书榜', '悬疑灵异', '夜班守则', 134000)]


def snapshot(db, date, books):
    rows = [{'channel': '男频', 'board': '新书榜', 'category_id': cid, 'category': name, 'rank': rank,
             'book_id': book, 'title': f'书{book}', 'title_complete': 1, 'reading': reading, 'status': '连载中',
             'latest_chapter': f'第{chapter}章', 'updated_at': ''}
            for cid, name, rank, book, reading, chapter in books]
    return research.save_snapshot(db, date, 2, rows, [])


def test_category_heat_sums_reading_and_compares_with_previous_snapshot(db):
    old = snapshot(db, '10-03 24:00', [(751, '悬疑灵异', 1, 'a', 100000, 10), (8, '科幻末世', 1, 'b', 300000, 10)])
    new = snapshot(db, '10-04 24:00', [(751, '悬疑灵异', 1, 'a', 150000, 12), (751, '悬疑灵异', 2, 'c', 50000, 3),
                                       (8, '科幻末世', 1, 'b', 290000, 11)])
    heat = research.category_heat(db, new, old)
    assert [(h['category'], h['total'], h['change']) for h in heat] == [('科幻末世', 290000, -10000),
                                                                         ('悬疑灵异', 200000, 100000)]


def test_rising_books_rank_by_reading_growth_and_flag_new_entries(db):
    old = snapshot(db, '10-03 24:00', [(751, '悬疑灵异', 1, 'a', 100000, 10), (751, '悬疑灵异', 2, 'b', 90000, 40)])
    new = snapshot(db, '10-04 24:00', [(751, '悬疑灵异', 1, 'b', 140000, 43), (751, '悬疑灵异', 2, 'a', 110000, 11),
                                       (751, '悬疑灵异', 3, 'c', 50000, 5)])
    rising = research.rising_books(db, new, old)
    assert [(r['book_id'], r['growth'], r['previous_rank'], r['chapters_gained']) for r in rising[:2]] == [
        ('b', 50000, 2, 3), ('a', 10000, 1, 1)]
    assert rising[2]['book_id'] == 'c' and rising[2]['is_new']


def test_notes_fill_in_readable_titles_for_entries(db):
    shot = snapshot(db, '10-04 24:00', [(751, '悬疑灵异', 1, 'a', 100000, 10)])
    research.save_note(db, 'a', '夜班守则', '规则怪谈,都市', '开篇钩子强')
    assert research.entries(db, shot)[0]['note_title'] == '夜班守则'
    assert research.notes(db)[0]['tags'] == '规则怪谈,都市'


def detail(title='盗墓：八岁创雷法', tags=('悬疑灵异', '盗墓'), words='17.7', unit='万字',
           intro='【盗墓】张海歌穿越盗墓世界，开局被屠村。', chapters=('第1章 乱世绝境', '第2章 归途赐名')):
    return ('<div class="info-name"><h1>' + title + '</h1></div><div class="info-label">'
            '<span class="info-label-yellow">连载中</span>' + ''.join(f'<span class="info-label-grey">{t}</span>' for t in tags)
            + f'</div><div class="info-count"><div class="info-count-word"><span class="detail">{words}</span>'
            f'<span class="text">{unit}</span></div></div><div class="info-last"><a href="/reader/9" class="chapter-item-title">'
            '<span class="info-last-title">最近更新：第2章 归途赐名</span></a></div>'
            f'<div class="page-abstract-content"><p>{intro}</p></div><div class="page-directory-content">'
            '<div class="volume volume_first">第一卷</div><div class="chapter">'
            + ''.join(f'<div class="chapter-item"><a href="/reader/{i}" class="chapter-item-title">{c}</a></div>'
                      for i, c in enumerate(chapters)) + '</div></div>')


def test_book_page_details_are_parsed_without_the_latest_update_link():
    assert research.parse_book_page(detail()) == {
        'title': '盗墓：八岁创雷法', 'status': '连载中', 'tags': ['悬疑灵异', '盗墓'], 'word_count': 177000,
        'intro': '【盗墓】张海歌穿越盗墓世界，开局被屠村。', 'chapters': ['第1章 乱世绝境', '第2章 归途赐名']}
    assert research.parse_book_page(detail(words='9876', unit='字'))['word_count'] == 9876


def test_fetched_details_give_rank_entries_readable_titles_and_record_failures(db):
    shot = snapshot(db, '10-04 24:00', [(751, '悬疑灵异', 1, '111', 100000, 10), (751, '悬疑灵异', 2, '222', 90000, 9)])

    def serve(request):
        if request.url.path == '/page/111':
            return httpx.Response(200, text=detail())
        return httpx.Response(200, text='<html>验证页面</html>')
    summary = research.fetch_details(db, ['111', '222'], httpx.MockTransport(serve), delay=0)
    assert summary['fetched'] == 1
    assert summary['failed'] == [{'book_id': '222', 'reason': '页面里没有书名'}]
    rows = research.entries(db, shot)
    assert [r['detail_title'] for r in rows] == ['盗墓：八岁创雷法', None]
    assert research.details(db, '111')['chapters'] == ['第1章 乱世绝境', '第2章 归途赐名']
