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
            root_title TEXT NOT NULL,
            uploader TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            version TEXT NOT NULL DEFAULT '',
            remark TEXT NOT NULL DEFAULT ''
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

    # Migrate: add new columns if they don't exist (for existing databases)
    cursor = conn.execute("PRAGMA table_info(files)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    for col, definition in [
        ("uploader", "TEXT NOT NULL DEFAULT ''"),
        ("password", "TEXT NOT NULL DEFAULT ''"),
        ("project", "TEXT NOT NULL DEFAULT ''"),
        ("version", "TEXT NOT NULL DEFAULT ''"),
        ("remark", "TEXT NOT NULL DEFAULT ''"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} {definition}")

    conn.commit()
    conn.close()


def insert_file(filename: str, root_title: str, uploader: str = "",
                password: str = "", project: str = "",
                version: str = "", remark: str = "") -> int:
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO files (filename, upload_time, root_title, uploader, password, project, version, remark) "
        "VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?)",
        (filename, root_title, uploader, password, project, version, remark),
    )
    file_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return file_id


def get_all_files(project: str = None, uploader: str = None, search: str = None) -> list[dict]:
    conn = get_conn()
    query = "SELECT id, filename, upload_time, root_title, uploader, project, version, remark FROM files WHERE 1=1"
    params = []
    if project:
        query += " AND project = ?"
        params.append(project)
    if uploader:
        query += " AND uploader = ?"
        params.append(uploader)
    if search:
        query += " AND (root_title LIKE ? OR project LIKE ? OR remark LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    query += " ORDER BY upload_time DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_file(file_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, filename, upload_time, root_title, uploader, project, version, remark FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_file_password(file_id: int) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT password FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.close()
    return row["password"] if row else None


def get_all_projects() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT project FROM files WHERE project != '' ORDER BY project"
    ).fetchall()
    conn.close()
    return [r["project"] for r in rows]


def get_all_uploaders() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT uploader FROM files WHERE uploader != '' ORDER BY uploader"
    ).fetchall()
    conn.close()
    return [r["uploader"] for r in rows]


def insert_topic(conn: sqlite3.Connection, file_id: int, parent_id, title: str, depth: int, path: str) -> int:
    cursor = conn.execute(
        "INSERT INTO topics (file_id, parent_id, title, depth, path) VALUES (?, ?, ?, ?, ?)",
        (file_id, parent_id, title, depth, path),
    )
    return cursor.lastrowid


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
