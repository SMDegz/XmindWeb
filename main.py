import json
import socket
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import database
import xmind_parser

app = FastAPI(title="XMind 思维导图查看器")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
TEMPLATE_DIR = BASE_DIR / "templates"

UPLOAD_DIR.mkdir(exist_ok=True)
TEMPLATE_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

VALID_TOPIC_STATUSES = {"", "self_test", "test"}


@app.on_event("startup")
def startup():
    database.init_db()


def _load_sheet_names(raw_sheet_names: str) -> list[str]:
    try:
        sheet_names = json.loads(raw_sheet_names or "[]")
    except Exception:
        return []
    return sheet_names if isinstance(sheet_names, list) else []


def _group_topics_by_sheet(topics: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for topic in topics:
        sheet_index = topic.get("sheet_index", 0) or 0
        grouped.setdefault(sheet_index, []).append(topic)
    return grouped


def _get_non_leaf_topic_ids(topics: list[dict]) -> set[int]:
    return {
        topic["parent_id"]
        for topic in topics
        if topic.get("parent_id") is not None
    }


def _count_leaf_topics(topics: list[dict]) -> int:
    non_leaf_topic_ids = _get_non_leaf_topic_ids(topics)
    return sum(
        1
        for topic in topics
        if topic.get("depth", 0) > 0 and topic.get("id") not in non_leaf_topic_ids
    )


def _build_sheet_stats(sheet_names: list[str], topics_by_sheet: dict[int, list[dict]]) -> list[dict]:
    sheet_indexes = set(topics_by_sheet.keys())
    if sheet_names:
        sheet_indexes.update(range(len(sheet_names)))
    if not sheet_indexes:
        return []

    stats = []
    for sheet_index in range(max(sheet_indexes) + 1):
        topics = topics_by_sheet.get(sheet_index, [])
        stats.append(
            {
                "index": sheet_index,
                "name": sheet_names[sheet_index] if sheet_index < len(sheet_names) else f"画布{sheet_index + 1}",
                "topic_count": _count_leaf_topics(topics),
            }
        )
    return stats


def _serialize_file(file_data: dict, all_topics: Optional[list[dict]] = None) -> dict:
    file_payload = dict(file_data)
    raw_sheet_names = file_payload.get("sheet_names") or "[]"
    sheet_names = _load_sheet_names(raw_sheet_names)
    if all_topics is None:
        all_topics = database.get_topics_by_file(file_payload["id"])
    topics_by_sheet = _group_topics_by_sheet(all_topics)
    sheet_stats = _build_sheet_stats(sheet_names, topics_by_sheet)
    file_payload["sheet_count"] = len(sheet_stats) if sheet_stats else file_payload.get("sheet_count", 1)
    file_payload["total_topic_count"] = sum(stat["topic_count"] for stat in sheet_stats) if sheet_stats else file_payload.get("topic_count", 0)
    file_payload["topic_count"] = file_payload["total_topic_count"]
    file_payload["sheet_names"] = json.dumps(sheet_names, ensure_ascii=False)
    file_payload["sheet_stats"] = sheet_stats
    return file_payload


def _storage_path(stored_filename: str) -> Path:
    return UPLOAD_DIR / stored_filename


def _extract_upload_filename(filename: str) -> str:
    return (filename or "").replace("\\", "/").split("/")[-1]


def _delete_storage_file(stored_filename: str):
    path = _storage_path(stored_filename)
    if path.exists():
        path.unlink()


def _ensure_folder_exists(folder_id: int) -> dict:
    folder = database.get_folder(folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return folder


def _ensure_file_exists(file_id: int) -> dict:
    file_data = database.get_file(file_id)
    if not file_data:
        raise HTTPException(status_code=404, detail="文件不存在")
    return file_data


def _ensure_topic_exists(topic_id: int) -> dict:
    topic = database.get_topic(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="节点不存在")
    return topic


def _validate_folder_name(name: str) -> str:
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="文件夹名称不能为空")
    return clean_name


def _build_markdown_from_topics(root_title: str, topics: list[dict]) -> str:
    by_parent: dict[Optional[int], list[dict]] = {}
    for topic in topics:
        by_parent.setdefault(topic["parent_id"], []).append(topic)

    lines = [f"# {root_title}"]

    def walk(parent_id: Optional[int]):
        for topic in by_parent.get(parent_id, []):
            if topic["depth"] == 0:
                walk(topic["id"])
                continue
            indent = "  " * topic["depth"]
            lines.append(f"{indent}- {topic['title']}")
            walk(topic["id"])

    walk(None)
    return "\n".join(lines)


def _build_tree_nodes(topics: list[dict]) -> list[dict]:
    node_map: dict[int, dict] = {}
    root_nodes: list[dict] = []

    for topic in topics:
        node = {
            "id": topic["id"],
            "title": topic["title"],
            "depth": topic["depth"],
            "path": topic["path"],
            "children": [],
        }
        node_map[topic["id"]] = node
        if topic["parent_id"] is None or topic["depth"] == 0:
            root_nodes.append(node)
        else:
            parent = node_map.get(topic["parent_id"])
            if parent:
                parent["children"].append(node)

    def fill_topic_count(node: dict) -> int:
        if not node["children"]:
            node["topic_count"] = 0
            return 1 if node["depth"] > 0 else 0

        descendant_count = 0
        for child in node["children"]:
            descendant_count += fill_topic_count(child)
        node["topic_count"] = descendant_count
        return descendant_count

    for root in root_nodes:
        fill_topic_count(root)

    return root_nodes


def _get_server_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _build_export_markdown(file_id: int, topic_path: str) -> Optional[str]:
    file_data = database.get_file(file_id)
    if not file_data:
        return None

    all_topics = database.get_topics_by_file(file_id)
    matched = [topic for topic in all_topics if topic["path"] == topic_path]
    if not matched:
        return None

    parent = matched[0]
    descendants = [topic for topic in all_topics if topic["path"].startswith(parent["path"] + "/")]

    lines = [
        f"# {parent['title']}",
        "",
        f"> 文件: {file_data['root_title']} ({file_data['filename']})",
        f"> 模块路径: {parent['path']}",
        f"> 子节点总数: {len(descendants)}",
        "",
        "---",
        "",
    ]

    for topic in descendants:
        rel_depth = topic["depth"] - parent["depth"]
        indent = "  " * rel_depth
        lines.append(f"{indent}- {topic['title']}")

    return "\n".join(lines)


def _build_prompt_markdown(file_id: int, topic_path: str) -> Optional[str]:
    file_data = database.get_file(file_id)
    if not file_data:
        return None

    all_topics = database.get_topics_by_file(file_id)
    matched = [topic for topic in all_topics if topic["path"] == topic_path]
    if not matched:
        return None

    parent = matched[0]
    descendants = [topic for topic in all_topics if topic["path"].startswith(parent["path"] + "/")]

    lines = [
        "请根据以下测试用例列表，检查代码中是否覆盖了这些场景。",
        "对于每个用例，指出：是否已有代码覆盖、覆盖位置、是否存在逻辑漏洞。",
        "",
        "## 模块信息",
        f"- 项目: {file_data['root_title']}",
        f"- 模块: {parent['title']}",
        f"- 路径: {parent['path']}",
        f"- 用例数: {len(descendants)}",
        "",
        "## 测试用例",
        "",
    ]

    for topic in descendants:
        rel_depth = topic["depth"] - parent["depth"]
        indent = "  " * rel_depth
        lines.append(f"{indent}- {topic['title']}")

    lines.extend(
        [
            "",
            "## 要求",
            "1. 逐条检查每个测试用例是否在代码中有对应实现",
            "2. 对于未覆盖的用例，说明可能的风险",
            "3. 对于已覆盖的用例，标注代码位置",
            "4. 总结覆盖率和改进建议",
        ]
    )
    return "\n".join(lines)


class FolderCreateRequest(BaseModel):
    name: str
    parent_id: Optional[int] = None


class FolderRenameRequest(BaseModel):
    name: str


class FolderMoveRequest(BaseModel):
    parent_id: Optional[int] = None


class TopicStatusRequest(BaseModel):
    status: str = ""


class TopicEditRequest(BaseModel):
    title: str


class AddChildRequest(BaseModel):
    title: str


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/server-info")
async def server_info():
    return {"ip": _get_server_ip(), "port": 8000}


@app.get("/api/tree")
async def get_library_tree():
    return database.get_library_tree()


@app.post("/api/folders")
async def create_folder(body: FolderCreateRequest):
    name = _validate_folder_name(body.name)
    if body.parent_id is not None:
        _ensure_folder_exists(body.parent_id)
    try:
        folder_id = database.create_folder(name, body.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"detail": "文件夹已创建", "folder_id": folder_id}


@app.put("/api/folders/{folder_id}/rename")
async def rename_folder(folder_id: int, body: FolderRenameRequest):
    _ensure_folder_exists(folder_id)
    name = _validate_folder_name(body.name)
    try:
        database.rename_folder(folder_id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"detail": "文件夹已重命名"}


@app.put("/api/folders/{folder_id}/move")
async def move_folder(folder_id: int, body: FolderMoveRequest):
    _ensure_folder_exists(folder_id)
    if body.parent_id is not None:
        _ensure_folder_exists(body.parent_id)
    try:
        database.move_folder(folder_id, body.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"detail": "文件夹已移动"}


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: int):
    _ensure_folder_exists(folder_id)
    stored_files = database.get_folder_storage_files(folder_id)
    database.delete_folder(folder_id)
    for file_info in stored_files:
        _delete_storage_file(file_info["stored_filename"])
    return {"detail": "文件夹已删除", "deleted_file_count": len(stored_files)}


@app.post("/api/import")
async def import_xmind_files(
    files: list[UploadFile] = File(...),
    folder_id: Optional[int] = Form(default=None),
):
    if folder_id is not None:
        _ensure_folder_exists(folder_id)

    imported: list[dict] = []
    failed: list[dict] = []

    for upload in files:
        filename = _extract_upload_filename(upload.filename or "")
        if not filename.lower().endswith(".xmind"):
            failed.append({"filename": filename or "未命名文件", "error": "仅支持 .xmind 文件"})
            continue

        content = await upload.read()
        if not content:
            failed.append({"filename": filename, "error": "文件内容为空"})
            continue

        try:
            parsed = xmind_parser.parse_xmind(content)
        except Exception as exc:
            failed.append({"filename": filename, "error": f"解析失败: {exc}"})
            continue

        stored_filename = f"{uuid.uuid4().hex}{Path(filename).suffix or '.xmind'}"
        storage_path = _storage_path(stored_filename)
        sheet_names = json.dumps([sheet["title"] for sheet in parsed["sheets"]], ensure_ascii=False)
        topic_count = sum(max(len(sheet["topics"]) - 1, 0) for sheet in parsed["sheets"])

        conn = database.get_conn()
        try:
            storage_path.write_bytes(content)
            file_id = database.insert_file(
                conn,
                folder_id=folder_id,
                filename=filename,
                stored_filename=stored_filename,
                root_title=parsed["root_title"],
                sheet_names=sheet_names,
                sheet_count=len(parsed["sheets"]),
                topic_count=topic_count,
            )

            for sheet_index, sheet in enumerate(parsed["sheets"]):
                temp_id_to_db_id: dict[int, int] = {}
                child_counter = Counter(
                    topic["parent_id"]
                    for topic in sheet["topics"]
                    if topic["parent_id"] is not None
                )
                sibling_orders: dict[Optional[int], int] = defaultdict(int)

                for topic in sheet["topics"]:
                    temp_parent_id = topic["parent_id"]
                    sort_order = sibling_orders[temp_parent_id]
                    sibling_orders[temp_parent_id] += 1
                    db_parent_id = temp_id_to_db_id.get(temp_parent_id)
                    db_topic_id = database.insert_topic(
                        conn,
                        file_id=file_id,
                        parent_id=db_parent_id,
                        title=topic["title"],
                        depth=topic["depth"],
                        path=topic["path"],
                        sheet_index=sheet_index,
                        sort_order=sort_order,
                        child_count=child_counter.get(topic["id"], 0),
                    )
                    temp_id_to_db_id[topic["id"]] = db_topic_id

            conn.commit()
        except Exception as exc:
            conn.rollback()
            if storage_path.exists():
                storage_path.unlink()
            failed.append({"filename": filename, "error": f"导入失败: {exc}"})
            conn.close()
            continue
        finally:
            conn.close()

        imported.append(
            {
                "id": file_id,
                "filename": filename,
                "root_title": parsed["root_title"],
                "topic_count": topic_count,
                "sheet_count": len(parsed["sheets"]),
                "folder_id": folder_id,
            }
        )

    return {"folder_id": folder_id, "imported": imported, "failed": failed}


@app.get("/api/files")
async def list_files():
    files = []
    for file_data in database.get_all_files():
        files.append(_serialize_file(file_data))
    return files


@app.get("/api/files/{file_id}")
async def get_file_detail(file_id: int, sheet: int = 0):
    file_data = _ensure_file_exists(file_id)
    all_topics = database.get_topics_by_file(file_id)
    serialized_file = _serialize_file(file_data, all_topics)
    topics_by_sheet = _group_topics_by_sheet(all_topics)
    topics = topics_by_sheet.get(sheet, [])
    root_topic = next((topic for topic in topics if topic.get("depth") == 0), None)
    return {
        "file": serialized_file,
        "topics": topics,
        "root_topic": root_topic,
        "sheet_stats": serialized_file.get("sheet_stats")
        if "sheet_stats" in serialized_file
        else _build_sheet_stats(_load_sheet_names(serialized_file.get("sheet_names")), topics_by_sheet),
    }


@app.get("/api/files/{file_id}/mindmap")
async def get_mindmap(file_id: int, sheet: int = 0):
    file_data = _ensure_file_exists(file_id)
    all_topics = database.get_topics_by_file(file_id)
    serialized_file = _serialize_file(file_data, all_topics)
    topics_by_sheet = _group_topics_by_sheet(all_topics)
    topics = topics_by_sheet.get(sheet, [])
    if not topics:
        raise HTTPException(status_code=404, detail="画布无数据")

    sheet_names = _load_sheet_names(serialized_file.get("sheet_names", "[]"))
    root_title = sheet_names[sheet] if sheet < len(sheet_names) else serialized_file["root_title"]
    return {"markdown": _build_markdown_from_topics(root_title, topics), "root_title": root_title}


@app.get("/api/files/{file_id}/tree")
async def get_tree(file_id: int, sheet: int = 0):
    _ensure_file_exists(file_id)
    topics = database.get_topics_by_file(file_id, sheet_index=sheet)
    if not topics:
        raise HTTPException(status_code=404, detail="文件不存在或无数据")
    return _build_tree_nodes(topics)


@app.get("/api/modules/{file_id}")
async def get_modules(file_id: int, depth: int = 3, sheet: int = 0):
    _ensure_file_exists(file_id)
    return database.get_modules(file_id, min_depth=1, max_depth=depth, sheet_index=sheet)


@app.get("/api/cases/{file_id}/{module_path:path}")
async def get_cases(file_id: int, module_path: str, sheet: int = 0):
    _ensure_file_exists(file_id)
    all_topics = database.get_topics_by_file(file_id, sheet_index=sheet)
    matched = [topic for topic in all_topics if topic["path"] == module_path]
    if not matched:
        raise HTTPException(status_code=404, detail=f"未找到模块: {module_path}")

    parent = matched[0]
    descendants = [topic for topic in all_topics if topic["path"].startswith(parent["path"] + "/")]
    non_leaf_topic_ids = _get_non_leaf_topic_ids(all_topics)
    result = [
        {
            "title": topic["title"],
            "depth": topic["depth"] - parent["depth"],
            "path": topic["path"],
        }
        for topic in descendants
    ]
    case_count = sum(1 for topic in descendants if topic["id"] not in non_leaf_topic_ids)
    return {"module": parent["title"], "path": parent["path"], "cases": result, "case_count": case_count}


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: int):
    file_storage = database.get_file_storage(file_id)
    if not file_storage:
        raise HTTPException(status_code=404, detail="文件不存在")
    database.delete_file(file_id)
    _delete_storage_file(file_storage["stored_filename"])
    return {"detail": "已删除"}


@app.get("/api/files/{file_id}/download")
async def download_file(file_id: int):
    file_storage = database.get_file_storage(file_id)
    if not file_storage:
        raise HTTPException(status_code=404, detail="文件不存在")

    path = _storage_path(file_storage["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="源文件不存在")

    return FileResponse(
        path=str(path),
        filename=file_storage["filename"],
        media_type="application/octet-stream",
    )


@app.get("/api/comments/{file_id}")
async def get_comments(file_id: int):
    _ensure_file_exists(file_id)
    return database.get_comments_by_file(file_id)


@app.post("/api/comments/{file_id}")
async def create_comment(file_id: int, request: Request):
    _ensure_file_exists(file_id)
    body = await request.json()
    topic_id = body.get("topic_id")
    author = (body.get("author") or "").strip() or "匿名"
    content = (body.get("content") or "").strip()
    if not topic_id or not content:
        raise HTTPException(status_code=400, detail="缺少 topic_id 或 content")
    comment_id = database.insert_comment(file_id, topic_id, author, content)
    return {"id": comment_id, "topic_id": topic_id, "author": author, "content": content}


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: int):
    database.delete_comment(comment_id)
    return {"detail": "已删除"}


@app.put("/api/topics/{topic_id}")
async def edit_topic(topic_id: int, body: TopicEditRequest):
    topic = _ensure_topic_exists(topic_id)
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="标题不能为空")

    old_title = topic["title"]
    old_path = topic["path"]
    if "/" in old_path:
        new_path = old_path.rsplit("/" + old_title, 1)[0] + "/" + new_title
    else:
        new_path = new_title

    conn = database.get_conn()
    conn.execute("UPDATE topics SET title = ?, path = ? WHERE id = ?", (new_title, new_path, topic_id))
    conn.execute(
        "UPDATE topics SET path = REPLACE(path, ?, ?) WHERE path LIKE ?",
        (old_path + "/", new_path + "/", old_path + "/%"),
    )
    conn.commit()
    conn.close()
    return {"detail": "已更新", "id": topic_id, "title": new_title}


@app.get("/api/topics/{topic_id}/children")
async def get_topic_children(topic_id: int):
    _ensure_topic_exists(topic_id)
    return database.get_topic_children(topic_id)


@app.post("/api/topics/{topic_id}/children")
async def add_child(topic_id: int, body: AddChildRequest):
    topic = _ensure_topic_exists(topic_id)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    new_id = database.add_child_topic(topic["file_id"], topic_id, title)
    return {"detail": "已添加", "id": new_id, "title": title}


@app.put("/api/topics/{topic_id}/status")
async def update_topic_status(topic_id: int, body: TopicStatusRequest):
    topic = _ensure_topic_exists(topic_id)
    status = (body.status or "").strip()
    if status not in VALID_TOPIC_STATUSES:
        raise HTTPException(status_code=400, detail="无效的节点状态")
    database.update_topic_status(topic_id, status)
    updated_topic = dict(topic)
    updated_topic["status"] = status
    return {"detail": "节点状态已更新", "topic": updated_topic}


@app.get("/api/export/{file_id}/{topic_path:path}")
async def export_topic(file_id: int, topic_path: str, format: str = Query("md")):
    _ensure_file_exists(file_id)
    if format == "prompt":
        content = _build_prompt_markdown(file_id, topic_path)
    else:
        content = _build_export_markdown(file_id, topic_path)

    if content is None:
        raise HTTPException(status_code=404, detail=f"未找到节点: {topic_path}")
    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
