from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path


class ValidationError(ValueError):
    pass


class Conflict(ValidationError):
    pass


def trim(value, keep_indent=False):
    """去掉首尾空白；keep_indent 时只去掉开头的空行，保留首段的全角缩进。"""
    if keep_indent:
        return re.sub(r'^(?:[^\S\n]*\n)+', '', value).rstrip()
    return value.strip()


def clean_text(value, label, maximum=100000, required=False, keep_indent=False):
    if not isinstance(value, str):
        raise ValidationError(f'{label}必须是文字。')
    value = trim(value, keep_indent)
    if required and not value:
        raise ValidationError(f'请填写{label}。')
    if len(value) > maximum:
        raise ValidationError(f'{label}不能超过 {maximum} 个字符。')
    return value


def validate_memory(summary, facts):
    summary = clean_text(summary, '本章摘要', 4000, True)
    if not isinstance(facts, list) or len(facts) > 100:
        raise ValidationError('每章记忆必须是列表，最多 100 条。')
    result, keys = [], set()
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {'entity', 'attribute', 'value'}:
            raise ValidationError('每条记忆需要对象、属性、事实三个字段。')
        item = {k: clean_text(fact[k], {'entity': '对象', 'attribute': '属性', 'value': '事实'}[k],
                              1000 if k == 'value' else 120, True) for k in fact}
        key = (item['entity'], item['attribute'])
        if key in keys:
            raise ValidationError('同一对象的同一属性每章只保留一条当前事实。')
        keys.add(key)
        result.append(item)
    return summary, result


def safe_filename(value):
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', value).strip('. ')
    return value[:80] or '未命名'


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS books(
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, premise TEXT NOT NULL DEFAULT '',
                genre TEXT NOT NULL DEFAULT '', bible TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT '', outline TEXT NOT NULL DEFAULT '',
                context_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS chapters(
                id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
                number INTEGER NOT NULL, current_revision_id INTEGER, finalized_revision_id INTEGER,
                UNIQUE(book_id,number));
            CREATE TABLE IF NOT EXISTS revisions(
                id INTEGER PRIMARY KEY, chapter_id INTEGER NOT NULL REFERENCES chapters(id),
                title TEXT NOT NULL, plan TEXT NOT NULL, body TEXT NOT NULL,
                source TEXT NOT NULL, base_revision_id INTEGER REFERENCES revisions(id),
                context_version INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS memories(
                chapter_id INTEGER PRIMARY KEY REFERENCES chapters(id),
                revision_id INTEGER NOT NULL REFERENCES revisions(id),
                summary TEXT NOT NULL, facts TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
                chapter_id INTEGER REFERENCES chapters(id), revision_id INTEGER REFERENCES revisions(id),
                context_version INTEGER NOT NULL, task TEXT NOT NULL, model TEXT NOT NULL,
                endpoint TEXT NOT NULL, status TEXT NOT NULL DEFAULT '运行中',
                prompt TEXT NOT NULL, prompt_version TEXT NOT NULL DEFAULT '1',
                output TEXT NOT NULL DEFAULT '', usage TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '', elapsed REAL NOT NULL DEFAULT 0,
                candidate_id INTEGER REFERENCES revisions(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS publications(
                id INTEGER PRIMARY KEY, chapter_id INTEGER NOT NULL REFERENCES chapters(id),
                revision_id INTEGER NOT NULL REFERENCES revisions(id),
                status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            PRAGMA user_version=1;
            ''')

    @contextmanager
    def connect(self, write=False):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def one(self, sql, params=()):
        with self.connect() as c:
            row = c.execute(sql, params).fetchone()
            if row is None:
                raise ValidationError('找不到这条记录。')
            return dict(row)

    def all(self, sql, params=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, params)]

    def books(self):
        return self.all('SELECT * FROM books ORDER BY id DESC')

    def book(self, book_id):
        return self.one('SELECT * FROM books WHERE id=?', (book_id,))

    def create_book(self, title, premise='', genre=''):
        values = (clean_text(title, '书名', 120, True), clean_text(premise, '故事创意', 10000),
                  clean_text(genre, '题材', 120))
        with self.connect(True) as c:
            return c.execute('INSERT INTO books(title,premise,genre) VALUES(?,?,?)', values).lastrowid

    def update_book(self, book_id, *, title, premise, genre, bible, style, outline, expected_version=None):
        values = [clean_text(v, name, limit, name == '书名') for v, name, limit in
                  [(title, '书名', 120), (premise, '故事创意', 10000), (genre, '题材', 120),
                   (bible, '人物与世界设定', 20000), (style, '文风', 4000), (outline, '故事大纲', 20000)]]
        with self.connect(True) as c:
            old = c.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            if not old:
                raise ValidationError('作品不存在。')
            if expected_version is not None and old['context_version'] != expected_version:
                raise Conflict('作品设定已在其他窗口更新。请先刷新并核对。')
            if all(old[k] == v for k, v in zip(('title', 'premise', 'genre', 'bible', 'style', 'outline'), values)):
                return
            # 记忆来自定稿正文，设定变化不改变已发生的剧情，所以不标记待复核；
            # 版本号递增只让基于旧设定生成的候选稿失效。
            c.execute('UPDATE books SET title=?,premise=?,genre=?,bible=?,style=?,outline=?, '
                      'context_version=context_version+1 WHERE id=?', (*values, book_id))

    def chapters(self, book_id):
        return self.all('SELECT ch.*,r.title,r.plan,r.body,m.stale FROM chapters ch '
                        'JOIN revisions r ON r.id=ch.current_revision_id '
                        'LEFT JOIN memories m ON m.chapter_id=ch.id '
                        'WHERE ch.book_id=? ORDER BY ch.number', (book_id,))

    def chapter(self, chapter_id):
        return self.one('SELECT * FROM chapters WHERE id=?', (chapter_id,))

    def revision(self, revision_id):
        return self.one('SELECT * FROM revisions WHERE id=?', (revision_id,))

    def revisions(self, chapter_id):
        return self.all('SELECT * FROM revisions WHERE chapter_id=? ORDER BY id DESC', (chapter_id,))

    def create_chapter(self, book_id, title, plan=''):
        title, plan = clean_text(title, '章节标题', 160, True), clean_text(plan, '章节计划', 12000)
        with self.connect(True) as c:
            book = c.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            if not book:
                raise ValidationError('作品不存在。')
            number = c.execute('SELECT COALESCE(MAX(number),0)+1 FROM chapters WHERE book_id=?', (book_id,)).fetchone()[0]
            chapter = c.execute('INSERT INTO chapters(book_id,number) VALUES(?,?)', (book_id, number)).lastrowid
            revision = c.execute('INSERT INTO revisions(chapter_id,title,plan,body,source,context_version) '
                                 'VALUES(?,?,?,?,?,?)', (chapter, title, plan, '', '手工稿', book['context_version'])).lastrowid
            c.execute('UPDATE chapters SET current_revision_id=? WHERE id=?', (revision, chapter))
            return chapter

    def _save(self, c, chapter_id, title, plan, body, expected, source):
        title = clean_text(title, '章节标题', 160, True)
        plan, body = clean_text(plan, '章节计划', 12000), clean_text(body, '正文', 100000, keep_indent=True)
        chapter = c.execute('SELECT * FROM chapters WHERE id=?', (chapter_id,)).fetchone()
        if not chapter:
            raise ValidationError('章节不存在。')
        if chapter['current_revision_id'] != expected:
            raise Conflict('当前稿已更新，未覆盖你的输入。请复制当前输入，刷新后合并。')
        version = c.execute('SELECT context_version FROM books WHERE id=?', (chapter['book_id'],)).fetchone()[0]
        revision = c.execute('INSERT INTO revisions(chapter_id,title,plan,body,source,base_revision_id,context_version) '
                             'VALUES(?,?,?,?,?,?,?)', (chapter_id, title, plan, body, source, expected, version)).lastrowid
        c.execute('UPDATE chapters SET current_revision_id=? WHERE id=?', (revision, chapter_id))
        return revision

    def save_revision(self, chapter_id, title, plan, body, expected):
        with self.connect(True) as c:
            return self._save(c, chapter_id, title, plan, body, expected, '手工稿')

    def restore(self, chapter_id, revision_id, expected):
        old = self.revision(revision_id)
        if old['chapter_id'] != chapter_id:
            raise ValidationError('不能恢复其他章节的版本。')
        if old['source'] == 'AI 候选稿':
            raise ValidationError('AI 候选稿请在写作助手中采用；过期候选稿需要手动合并。')
        with self.connect(True) as c:
            return self._save(c, chapter_id, old['title'], old['plan'], old['body'], expected, f'恢复自版本 {revision_id}')

    def add_candidate(self, chapter_id, title, plan, body, base_revision, context_version):
        if self.revision(base_revision)['chapter_id'] != chapter_id:
            raise ValidationError('候选稿与原章节不匹配。')
        title = clean_text(title, '章节标题', 160, True)
        body = clean_text(body, '正文', 100000, True, keep_indent=True)
        plan = clean_text(plan, '章节计划', 12000)
        with self.connect(True) as c:
            return c.execute('INSERT INTO revisions(chapter_id,title,plan,body,source,base_revision_id,context_version) '
                             'VALUES(?,?,?,?,?,?,?)', (chapter_id, title, plan, body, 'AI 候选稿',
                                                      base_revision, context_version)).lastrowid

    def adopt_candidate(self, chapter_id, candidate_id):
        candidate = self.revision(candidate_id)
        if candidate['chapter_id'] != chapter_id or candidate['source'] != 'AI 候选稿':
            raise ValidationError('候选稿不属于本章。')
        with self.connect(True) as c:
            version = c.execute('SELECT b.context_version FROM books b JOIN chapters ch ON b.id=ch.book_id '
                                'WHERE ch.id=?', (chapter_id,)).fetchone()[0]
            if candidate['context_version'] != version:
                raise Conflict('生成后故事设定或正式记忆已改变，请重新生成或手动合并候选稿。')
            return self._save(c, chapter_id, candidate['title'], candidate['plan'], candidate['body'],
                              candidate['base_revision_id'], '采纳 AI 候选稿')

    def memory(self, book_id):
        rows = self.all('SELECT m.*,ch.number FROM memories m JOIN chapters ch ON ch.id=m.chapter_id '
                        'WHERE ch.book_id=? ORDER BY ch.number', (book_id,))
        for row in rows:
            row['facts'] = json.loads(row['facts'])
        return rows

    @staticmethod
    def _check_previous(c, chapter):
        gap = c.execute('SELECT ch.number FROM chapters ch LEFT JOIN memories m ON ch.id=m.chapter_id '
                        'WHERE ch.book_id=? AND ch.number<? AND '
                        '(ch.finalized_revision_id IS NULL OR m.chapter_id IS NULL) LIMIT 1',
                        (chapter['book_id'], chapter['number'])).fetchone()
        if gap:
            raise ValidationError(f'请先处理前面的章节：第 {gap[0]} 章尚未定稿。')

    def finalize(self, chapter_id, revision_id, summary, facts, expected_context):
        summary, facts = validate_memory(summary, facts)
        with self.connect(True) as c:
            chapter = c.execute('SELECT * FROM chapters WHERE id=?', (chapter_id,)).fetchone()
            if not chapter or chapter['current_revision_id'] != revision_id:
                raise Conflict('正文版本已变化，请针对当前稿重新确认摘要与记忆。')
            revision = c.execute('SELECT * FROM revisions WHERE id=?', (revision_id,)).fetchone()
            if not revision['body'].strip():
                raise ValidationError('正文为空，不能定稿。')
            version = c.execute('SELECT context_version FROM books WHERE id=?', (chapter['book_id'],)).fetchone()[0]
            if version != expected_context:
                raise Conflict('设定或记忆已改变，请刷新后重新核对定稿内容。')
            self._check_previous(c, chapter)
            old = c.execute('SELECT * FROM memories WHERE chapter_id=?', (chapter_id,)).fetchone()
            changed = not old or (old['revision_id'], old['summary'], json.loads(old['facts'])) != (revision_id, summary, facts)
            c.execute('INSERT INTO memories(chapter_id,revision_id,summary,facts,stale) VALUES(?,?,?,?,0) '
                      'ON CONFLICT(chapter_id) DO UPDATE SET revision_id=excluded.revision_id, '
                      'summary=excluded.summary,facts=excluded.facts,stale=0',
                      (chapter_id, revision_id, summary, json.dumps(facts, ensure_ascii=False)))
            c.execute('UPDATE chapters SET finalized_revision_id=? WHERE id=?', (revision_id, chapter_id))
            # 待复核只是提醒：后续章节照常续写、定稿和导出。原样重新确认不连锁标记。
            if changed:
                c.execute('UPDATE memories SET stale=1 WHERE chapter_id IN '
                          '(SELECT id FROM chapters WHERE book_id=? AND number>?)',
                          (chapter['book_id'], chapter['number']))
            c.execute('UPDATE books SET context_version=context_version+1 WHERE id=?', (chapter['book_id'],))

    def context(self, chapter_id):
        chapter = self.chapter(chapter_id)
        with self.connect() as c:
            self._check_previous(c, chapter)
        book = self.book(chapter['book_id'])
        current = {}
        memories = [m for m in self.memory(book['id']) if m['number'] < chapter['number']]
        for memory in memories:
            for fact in memory['facts']:
                current[(fact['entity'], fact['attribute'])] = {
                    **fact, 'chapter_number': memory['number'], 'revision_id': memory['revision_id']}
        previous = self.all('SELECT ch.number, r.id AS revision_id, r.body FROM chapters ch '
                            'JOIN revisions r ON r.id=ch.finalized_revision_id '
                            'WHERE ch.book_id=? AND ch.number<? ORDER BY ch.number DESC LIMIT 1',
                            (book['id'], chapter['number']))
        return {'book': book, 'chapter_number': chapter['number'], 'facts': list(current.values()),
                'previous_chapter': previous[0] if previous else None,
                'recent_summaries': [{'chapter_number': m['number'], 'summary': m['summary'],
                                      'revision_id': m['revision_id']} for m in memories[-6:]],
                'source_chapters': [m['number'] for m in memories],
                'stale_chapters': [m['number'] for m in memories if m['stale']],
                'context_version': book['context_version']}

    def record_publication(self, chapter_id, revision_id, status, note=''):
        if status not in ('已导出', '已存平台草稿', '审核中', '定时发布', '已发布', '退回修改'):
            raise ValidationError('未知发布状态。')
        if self.revision(revision_id)['chapter_id'] != chapter_id:
            raise ValidationError('发布记录与章节不匹配。')
        with self.connect(True) as c:
            return c.execute('INSERT INTO publications(chapter_id,revision_id,status,note) VALUES(?,?,?,?)',
                             (chapter_id, revision_id, status, clean_text(note, '备注', 2000))).lastrowid

    def publications(self, book_id):
        return self.all('SELECT p.*,ch.number FROM publications p JOIN chapters ch ON ch.id=p.chapter_id '
                        'WHERE ch.book_id=? ORDER BY p.id DESC', (book_id,))

    def export_book(self, book_id):
        chapters = self.chapters(book_id)
        selected = [ch for ch in chapters if ch['finalized_revision_id']]
        if not selected:
            raise ValidationError('还没有定稿章节。')
        buffer, records = io.BytesIO(), []
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for ch in selected:
                rev = self.revision(ch['finalized_revision_id'])
                text = self.chapter_txt(ch['number'], rev)
                archive.writestr(f'{ch["number"]:04d}_{safe_filename(rev["title"])}.txt', text.encode('utf-8-sig'))
                records.append({'chapter_id': ch['id'], 'revision_id': rev['id'], 'status': '已导出'})
        return buffer.getvalue(), records

    @staticmethod
    def chapter_txt(number, revision):
        return f'第{number}章 {revision["title"]}\n\n{trim(revision["body"], keep_indent=True)}\n'

    def backup_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'backup.sqlite3'
            with self.connect() as source, sqlite3.connect(dest) as target:
                source.backup(target)
            return dest.read_bytes()

    def start_run(self, book_id, chapter_id, revision_id, context_version, task, model, endpoint, prompt):
        with self.connect(True) as c:
            return c.execute('INSERT INTO runs(book_id,chapter_id,revision_id,context_version,task,model,endpoint,prompt) '
                             'VALUES(?,?,?,?,?,?,?,?)', (book_id, chapter_id, revision_id, context_version,
                                                        task, model, endpoint, json.dumps(prompt, ensure_ascii=False))).lastrowid

    def finish_run(self, run_id, status, output='', usage=None, error='', elapsed=0):
        with self.connect(True) as c:
            c.execute('UPDATE runs SET status=?,output=?,usage=?,error=?,elapsed=? WHERE id=?',
                      (status, output, json.dumps(usage or {}), error, elapsed, run_id))

    def recover_runs(self):
        with self.connect(True) as c:
            c.execute("UPDATE runs SET status='已中断',error='应用重启；请核对用量后手动重试。' WHERE status='运行中'")

    def link_candidate(self, run_id, revision_id):
        with self.connect(True) as c:
            c.execute('UPDATE runs SET candidate_id=? WHERE id=?', (revision_id, run_id))

    def runs(self, book_id, chapter_id=None):
        query = 'SELECT * FROM runs WHERE book_id=?'
        params = [book_id]
        if chapter_id is not None:
            query += ' AND chapter_id=?'
            params.append(chapter_id)
        return self.all(query + ' ORDER BY id DESC', params)
