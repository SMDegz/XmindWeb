import os
import sqlite3
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "xmind.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    if _needs_schema_rebuild(conn):
        _rebuild_schema(conn)
    else:
        _create_schema(conn)
    conn.commit()
    conn.close()


def _needs_schema_rebuild(conn: sqlite3.Connection) -> bool:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    if "files" not in tables or "topics" not in tables:
        return False

    file_columns = _table_columns(conn, "files")
    topic_columns = _table_columns(conn, "topics")
    required_file_columns = {
        "folder_id",
        "stored_filename",
        "sheet_names",
        "sheet_count",
        "topic_count",
    }
    required_topic_columns = {"status", "sort_order", "child_count"}
    return not required_file_columns.issubset(file_columns) or not required_topic_columns.issubset(topic_columns)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _rebuild_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        DROP TABLE IF EXISTS comments;
        DROP TABLE IF EXISTS topics;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS folders;
        """
    )
    _create_schema(conn)


def _create_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES folders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER,
            filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            upload_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            root_title TEXT NOT NULL,
            sheet_names TEXT NOT NULL DEFAULT '[]',
            sheet_count INTEGER NOT NULL DEFAULT 1,
            topic_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            parent_id INTEGER,
            title TEXT NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL DEFAULT '',
            sheet_index INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            child_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_id) REFERENCES topics(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics(id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_unique_sibling
        ON folders(IFNULL(parent_id, 0), name);

        CREATE INDEX IF NOT EXISTS idx_folders_parent_id ON folders(parent_id);
        CREATE INDEX IF NOT EXISTS idx_files_folder_id ON files(folder_id);
        CREATE INDEX IF NOT EXISTS idx_topics_file_sheet_parent ON topics(file_id, sheet_index, parent_id);
        CREATE INDEX IF NOT EXISTS idx_topics_parent_id ON topics(parent_id);
        CREATE INDEX IF NOT EXISTS idx_topics_path ON topics(path);
        CREATE INDEX IF NOT EXISTS idx_comments_topic_id ON comments(topic_id);
        """
    )


def _folder_name_exists(
    conn: sqlite3.Connection,
    name: str,
    parent_id: Optional[int],
    exclude_id: Optional[int] = None,
) -> bool:
    query = "SELECT 1 FROM folders WHERE name = ? AND "
    params: list[object] = [name]
    if parent_id is None:
        query += "parent_id IS NULL"
    else:
        query += "parent_id = ?"
        params.append(parent_id)

    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)

    return conn.execute(query, params).fetchone() is not None


def get_folder(folder_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT id, parent_id, name, created_at, updated_at
        FROM folders
        WHERE id = ?
        """,
        (folder_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def create_folder(name: str, parent_id: Optional[int]) -> int:
    conn = get_conn()
    if parent_id is not None and get_folder(parent_id) is None:
        conn.close()
        raise ValueError("目标父目录不存在")

    if _folder_name_exists(conn, name, parent_id):
        conn.close()
        raise ValueError("同级目录下已存在同名文件夹")

    cursor = conn.execute(
        """
        INSERT INTO folders (parent_id, name, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        """,
        (parent_id, name),
    )
    folder_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return folder_id


def rename_folder(folder_id: int, name: str):
    conn = get_conn()
    folder = conn.execute(
        "SELECT id, parent_id FROM folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    if folder is None:
        conn.close()
        raise ValueError("文件夹不存在")

    if _folder_name_exists(conn, name, folder["parent_id"], exclude_id=folder_id):
        conn.close()
        raise ValueError("同级目录下已存在同名文件夹")

    conn.execute(
        """
        UPDATE folders
        SET name = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (name, folder_id),
    )
    conn.commit()
    conn.close()


def get_descendant_folder_ids(folder_id: int) -> set[int]:
    conn = get_conn()
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM folders WHERE id = ?
            UNION ALL
            SELECT f.id
            FROM folders f
            INNER JOIN subtree s ON f.parent_id = s.id
        )
        SELECT id FROM subtree
        """,
        (folder_id,),
    ).fetchall()
    conn.close()
    return {row["id"] for row in rows}


def move_folder(folder_id: int, parent_id: Optional[int]):
    conn = get_conn()
    folder = conn.execute(
        "SELECT id, parent_id, name FROM folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    if folder is None:
        conn.close()
        raise ValueError("文件夹不存在")

    if parent_id == folder_id:
        conn.close()
        raise ValueError("文件夹不能移动到自己下面")

    if parent_id is not None:
        parent = conn.execute(
            "SELECT id FROM folders WHERE id = ?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            conn.close()
            raise ValueError("目标父目录不存在")

        descendants = get_descendant_folder_ids(folder_id)
        if parent_id in descendants:
            conn.close()
            raise ValueError("文件夹不能移动到自己的子目录下面")

    if _folder_name_exists(conn, folder["name"], parent_id, exclude_id=folder_id):
        conn.close()
        raise ValueError("目标目录下已存在同名文件夹")

    conn.execute(
        """
        UPDATE folders
        SET parent_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (parent_id, folder_id),
    )
    conn.commit()
    conn.close()


def get_folder_storage_files(folder_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM folders WHERE id = ?
            UNION ALL
            SELECT f.id
            FROM folders f
            INNER JOIN subtree s ON f.parent_id = s.id
        )
        SELECT id, stored_filename, filename
        FROM files
        WHERE folder_id IN (SELECT id FROM subtree)
        """,
        (folder_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_folder(folder_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    conn.commit()
    conn.close()


def get_library_tree() -> dict:
    conn = get_conn()
    folder_rows = conn.execute(
        """
        SELECT id, parent_id, name, created_at, updated_at
        FROM folders
        ORDER BY name COLLATE NOCASE, id
        """
    ).fetchall()
    file_rows = conn.execute(
        """
        SELECT id, folder_id, filename, root_title, upload_time, sheet_count, topic_count
        FROM files
        ORDER BY root_title COLLATE NOCASE, filename COLLATE NOCASE, id
        """
    ).fetchall()
    conn.close()

    folder_map: dict[int, dict] = {}
    for row in folder_rows:
        node = dict(row)
        node["children"] = []
        node["files"] = []
        node["total_file_count"] = 0
        folder_map[node["id"]] = node

    root_folders: list[dict] = []
    root_files: list[dict] = []

    for folder in folder_map.values():
        parent_id = folder["parent_id"]
        if parent_id is not None and parent_id in folder_map:
            folder_map[parent_id]["children"].append(folder)
        else:
            root_folders.append(folder)

    for file_row in file_rows:
        file_data = dict(file_row)
        folder_id = file_data["folder_id"]
        if folder_id is not None and folder_id in folder_map:
            folder_map[folder_id]["files"].append(file_data)
        else:
            root_files.append(file_data)

    def decorate(folder: dict) -> int:
        total = len(folder["files"])
        for child in folder["children"]:
            total += decorate(child)
        folder["total_file_count"] = total
        return total

    total_files = len(root_files)
    for folder in root_folders:
        total_files += decorate(folder)

    return {
        "folders": root_folders,
        "files": root_files,
        "folder_count": len(folder_map),
        "file_count": total_files,
    }


def get_all_files() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            id,
            folder_id,
            filename,
            root_title,
            upload_time,
            sheet_count,
            topic_count
        FROM files
        ORDER BY upload_time DESC, id DESC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def insert_file(
    conn: sqlite3.Connection,
    folder_id: Optional[int],
    filename: str,
    stored_filename: str,
    root_title: str,
    sheet_names: str,
    sheet_count: int,
    topic_count: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO files (
            folder_id, filename, stored_filename, upload_time,
            root_title, sheet_names, sheet_count, topic_count
        )
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """,
        (folder_id, filename, stored_filename, root_title, sheet_names, sheet_count, topic_count),
    )
    return cursor.lastrowid


def get_file(file_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT
            id, folder_id, filename, stored_filename, upload_time,
            root_title, sheet_names, sheet_count, topic_count
        FROM files
        WHERE id = ?
        """,
        (file_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_file_storage(file_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, stored_filename, filename FROM files WHERE id = ?",
        (file_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_file(file_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()


def insert_topic(
    conn: sqlite3.Connection,
    file_id: int,
    parent_id: Optional[int],
    title: str,
    depth: int,
    path: str,
    sheet_index: int,
    sort_order: int,
    child_count: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO topics (
            file_id, parent_id, title, depth, path,
            sheet_index, sort_order, child_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, parent_id, title, depth, path, sheet_index, sort_order, child_count),
    )
    return cursor.lastrowid


def _topic_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["has_children"] = (data.get("child_count") or 0) > 0
    return data


def get_sheet_topic_counts(file_id: int) -> dict[int, int]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT sheet_index, SUM(CASE WHEN depth > 0 THEN 1 ELSE 0 END) AS topic_count
        FROM topics
        WHERE file_id = ?
        GROUP BY sheet_index
        ORDER BY sheet_index
        """,
        (file_id,),
    ).fetchall()
    conn.close()
    return {row["sheet_index"]: row["topic_count"] for row in rows}


def get_topics_by_file(file_id: int, sheet_index: Optional[int] = None) -> list[dict]:
    conn = get_conn()
    if sheet_index is None:
        rows = conn.execute(
            """
            SELECT
                id, file_id, parent_id, title, depth, path,
                sheet_index, status, sort_order, child_count
            FROM topics
            WHERE file_id = ?
            ORDER BY sheet_index, sort_order, id
            """,
            (file_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                id, file_id, parent_id, title, depth, path,
                sheet_index, status, sort_order, child_count
            FROM topics
            WHERE file_id = ? AND sheet_index = ?
            ORDER BY sort_order, id
            """,
            (file_id, sheet_index),
        ).fetchall()
    conn.close()
    return [_topic_to_dict(row) for row in rows]


def get_topics_by_path(file_id: int, path_prefix: str, sheet_index: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            id, file_id, parent_id, title, depth, path,
            sheet_index, status, sort_order, child_count
        FROM topics
        WHERE file_id = ? AND sheet_index = ? AND path LIKE ?
        ORDER BY sort_order, id
        """,
        (file_id, sheet_index, path_prefix + "%"),
    ).fetchall()
    conn.close()
    return [_topic_to_dict(row) for row in rows]


def get_modules(file_id: int, min_depth: int = 1, max_depth: int = 3, sheet_index: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT DISTINCT path, title, depth
        FROM topics
        WHERE file_id = ? AND sheet_index = ? AND depth BETWEEN ? AND ?
        ORDER BY path
        """,
        (file_id, sheet_index, min_depth, max_depth),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_root_topic(file_id: int, sheet_index: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT
            id, file_id, parent_id, title, depth, path,
            sheet_index, status, sort_order, child_count
        FROM topics
        WHERE file_id = ? AND sheet_index = ? AND depth = 0
        ORDER BY id
        LIMIT 1
        """,
        (file_id, sheet_index),
    ).fetchone()
    conn.close()
    return _topic_to_dict(row) if row else None


def get_topic(topic_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT
            id, file_id, parent_id, title, depth, path,
            sheet_index, status, sort_order, child_count
        FROM topics
        WHERE id = ?
        """,
        (topic_id,),
    ).fetchone()
    conn.close()
    return _topic_to_dict(row) if row else None


def get_topic_children(topic_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            id, file_id, parent_id, title, depth, path,
            sheet_index, status, sort_order, child_count
        FROM topics
        WHERE parent_id = ?
        ORDER BY sort_order, id
        """,
        (topic_id,),
    ).fetchall()
    conn.close()
    return [_topic_to_dict(row) for row in rows]


def update_topic_title(topic_id: int, title: str):
    conn = get_conn()
    conn.execute("UPDATE topics SET title = ? WHERE id = ?", (title, topic_id))
    conn.commit()
    conn.close()


def add_child_topic(file_id: int, parent_id: int, title: str) -> int:
    parent = get_topic(parent_id)
    if not parent:
        return 0

    conn = get_conn()
    next_sort_order_row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM topics WHERE parent_id = ?",
        (parent_id,),
    ).fetchone()
    sort_order = next_sort_order_row["next_order"] if next_sort_order_row else 0
    path = f"{parent['path']}/{title}"
    cursor = conn.execute(
        """
        INSERT INTO topics (
            file_id, parent_id, title, depth, path,
            sheet_index, sort_order, child_count, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, '')
        """,
        (
            file_id,
            parent_id,
            title,
            parent["depth"] + 1,
            path,
            parent.get("sheet_index", 0) or 0,
            sort_order,
        ),
    )
    conn.execute(
        "UPDATE topics SET child_count = child_count + 1 WHERE id = ?",
        (parent_id,),
    )
    topic_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return topic_id


def update_topic_status(topic_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE topics SET status = ? WHERE id = ?",
        (status, topic_id),
    )
    conn.commit()
    conn.close()


def insert_comment(file_id: int, topic_id: int, author: str, content: str) -> int:
    conn = get_conn()
    cursor = conn.execute(
        """
        INSERT INTO comments (file_id, topic_id, author, content, created_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (file_id, topic_id, author, content),
    )
    comment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return comment_id


def get_comments_by_file(file_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            c.id,
            c.topic_id,
            c.file_id,
            c.author,
            c.content,
            c.created_at,
            t.sheet_index,
            t.title AS topic_title,
            t.path AS topic_path
        FROM comments c
        LEFT JOIN topics t ON t.id = c.topic_id
        WHERE c.file_id = ?
        ORDER BY COALESCE(t.sheet_index, 0), c.topic_id, c.created_at
        """,
        (file_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_comment(comment_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
