"""chat.sqlite3 as the bridge uses it: its tables, message de-duplication, and what a group said since she last looked."""
import sqlite3
import time

from . import prompts
from .agents.state import recent_history
from .message_policy import speaker_message


def open_database(path, owner):
    """Open chat.sqlite3 and create or upgrade the bridge's own tables; old databases upgrade in place."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, time REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS history (role TEXT, content TEXT)")
    # Preserve pre-upgrade history exclusively for the owner's private chat.
    if "conversation" not in {row[1] for row in db.execute("PRAGMA table_info(history)")}:
        db.execute("ALTER TABLE history ADD COLUMN conversation TEXT")
        db.execute("UPDATE history SET conversation=?", (f"private:{int(owner)}",))
    db.execute("CREATE TABLE IF NOT EXISTS threads (conversation TEXT PRIMARY KEY, thread_id TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS conversation_models (conversation TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS bootstrap (thread_id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS old_threads (conversation TEXT, thread_id TEXT, time REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS group_context (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, content TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS group_context_group ON group_context(group_id, id)")
    db.execute("CREATE TABLE IF NOT EXISTS group_context_cursors ("
               "conversation TEXT NOT NULL, profile TEXT NOT NULL, session_id TEXT NOT NULL, "
               "last_id INTEGER NOT NULL, PRIMARY KEY(conversation,profile,session_id))")
    db.commit()
    return db


class Store:
    """Needs self.db, self.cfg, self.conversation() and self.agent_for()."""

    def first_delivery(self, key):
        """False when this QQ message was already handled; ids are kept for 7 days."""
        if self.db.execute("SELECT 1 FROM seen WHERE id=?", (key,)).fetchone():
            return False
        self.db.execute("INSERT INTO seen VALUES (?, ?)", (key, time.time()))
        self.db.execute("DELETE FROM seen WHERE time < ?", (time.time() - 7 * 86400,))
        self.db.commit()
        return True

    def remember_group(self, source, text):
        cursor = self.db.execute("INSERT INTO group_context (group_id, content) VALUES (?, ?)",
                                 (source.target, speaker_message(source, text[:1000], self.cfg['owner_qq'])))
        context_id = cursor.lastrowid
        self.db.execute("DELETE FROM group_context WHERE group_id=? AND id NOT IN "
                        "(SELECT id FROM group_context WHERE group_id=? ORDER BY id DESC LIMIT 100)",
                        (source.target, source.target))
        self.db.commit()
        return context_id

    def group_reference(self, source, context_id, thread=None):
        if source.kind != 'group' or context_id is None:
            return ''
        agent = self.agent_for(source)
        thread = thread or agent.session_id(self.conversation(source)) or ''
        last_id = self.db.execute("SELECT COALESCE(MAX(last_id),0) FROM group_context_cursors "
                                  "WHERE conversation=? AND profile=? AND session_id IN (?, '')",
                                  (self.conversation(source), agent.name, thread)).fetchone()[0]
        rows = self.db.execute("SELECT content FROM group_context WHERE group_id=? AND id>? AND id<? "
                               "ORDER BY id DESC LIMIT 40", (source.target, last_id, context_id)).fetchall()
        messages, size = [], 0
        for (content,) in rows:
            if size + len(content) > 12000:
                break
            messages.append(content)
            size += len(content)
        if not messages:
            return ''
        return prompts.group_context(list(reversed(messages)))

    def advance_group_context(self, source, thread, context_id):
        if source.kind != 'group' or context_id is None:
            return
        self.db.execute("INSERT INTO group_context_cursors VALUES (?,?,?,?) "
                        "ON CONFLICT(conversation,profile,session_id) DO UPDATE "
                        "SET last_id=MAX(last_id,excluded.last_id)",
                        (self.conversation(source), self.agent_for(source).name, thread, context_id))

    def reset_group_context(self, source, context_id=None):
        if source.kind != 'group':
            return
        key, profile = self.conversation(source), self.agent_for(source).name
        latest = context_id if context_id is not None else self.db.execute(
            "SELECT COALESCE(MAX(id),0) FROM group_context WHERE group_id=?", (source.target,)).fetchone()[0]
        self.db.execute("DELETE FROM group_context_cursors WHERE conversation=? AND profile=?", (key, profile))
        # An empty session ID is a floor for future sessions after explicit /new.
        self.db.execute("INSERT INTO group_context_cursors VALUES (?,?,?,?)", (key, profile, '', latest))

    def turn_text(self, source, thread, text, context_id):
        """Prefix old-bridge history (until the thread is bootstrapped) and unseen group context."""
        if not self.db.execute("SELECT 1 FROM bootstrap WHERE thread_id=?", (thread,)).fetchone():
            history = recent_history(self.db, self.conversation(source))
            if history:
                text = prompts.history_import(history, text)
        return self.group_reference(source, context_id, thread) + text
