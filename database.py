import sqlite3
import os
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "xmind.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            upload_time TEXT NOT NULL,
            root_title TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            parent_id INTEGER,
            title TEXT NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_topics_file_id ON topics(file_id);
        CREATE INDEX IF NOT EXISTS idx_topics_path ON topics(path);
        CREATE INDEX IF NOT EXISTS idx_comments_topic_id ON comments(topic_id);
    """)
    conn.commit()
    conn.close()


def insert_file(filename: str, root_title: str) -> int:
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO files (filename, upload_time, root_title) VALUES (?, datetime('now'), ?)",
        (filename, root_title),
    )
    file_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return file_id


def insert_topic(conn: sqlite3.Connection, file_id: int, parent_id, title: str, depth: int, path: str) -> int:
    cursor = conn.execute(
        "INSERT INTO topics (file_id, parent_id, title, depth, path) VALUES (?, ?, ?, ?, ?)",
        (file_id, parent_id, title, depth, path),
    )
    return cursor.lastrowid


def get_all_files() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, filename, upload_time, root_title FROM files ORDER BY upload_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_file(file_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, filename, upload_time, root_title FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_topics_by_file(file_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, file_id, parent_id, title, depth, path FROM topics WHERE file_id = ? ORDER BY id",
        (file_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_topics_by_path(file_id: int, path_prefix: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, file_id, parent_id, title, depth, path FROM topics WHERE file_id = ? AND path LIKE ? ORDER BY id",
        (file_id, path_prefix + "%"),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_modules(file_id: int, min_depth: int = 1, max_depth: int = 3) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT path, title, depth FROM topics WHERE file_id = ? AND depth BETWEEN ? AND ? ORDER BY path",
        (file_id, min_depth, max_depth),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_file(file_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()


# ==================== Comments ====================


def insert_comment(file_id: int, topic_id: int, author: str, content: str) -> int:
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO comments (file_id, topic_id, author, content, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
        (file_id, topic_id, author, content),
    )
    comment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return comment_id


def get_comments_by_file(file_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, topic_id, file_id, author, content, created_at FROM comments WHERE file_id = ? ORDER BY topic_id, created_at",
        (file_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_comment(comment_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
