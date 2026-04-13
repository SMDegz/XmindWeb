import os
import socket
import shutil
import hashlib
import json
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import database
import xmind_parser

app = FastAPI(title="XMind 思维导图查看器")

BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

ADMIN_PASSWORD = "456123.Zz"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True)
templates = Jinja2Templates(directory=TEMPLATE_DIR)


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
        stats.append({
            "index": sheet_index,
            "name": sheet_names[sheet_index] if sheet_index < len(sheet_names) else f"画布{sheet_index + 1}",
            "topic_count": _count_leaf_topics(topics),
        })
    return stats


def _normalize_file_sheet_meta(file_id: int, file_data: dict, all_topics: Optional[list[dict]] = None):
    file_data = dict(file_data)
    original_root_title = file_data.get("root_title") or ""
    original_sheet_names = file_data.get("sheet_names") or "[]"
    stored_sheet_names = _load_sheet_names(original_sheet_names)

    if all_topics is None:
        all_topics = database.get_topics_by_file(file_id)
    topics_by_sheet = _group_topics_by_sheet(all_topics)

    sheet_indexes = set(topics_by_sheet.keys())
    if stored_sheet_names:
        sheet_indexes.update(range(len(stored_sheet_names)))

    if not sheet_indexes:
        return file_data, stored_sheet_names, topics_by_sheet

    resolved_sheet_names = []
    for sheet_index in range(max(sheet_indexes) + 1):
        stored_name = stored_sheet_names[sheet_index] if sheet_index < len(stored_sheet_names) else ""
        resolved_sheet_names.append(
            xmind_parser.resolve_sheet_title(stored_name, topics_by_sheet.get(sheet_index, []), sheet_index)
        )

    root_title = resolved_sheet_names[0] if resolved_sheet_names else original_root_title
    sheet_names_json = json.dumps(resolved_sheet_names, ensure_ascii=False)
    file_data["root_title"] = root_title
    file_data["sheet_names"] = sheet_names_json
    return file_data, resolved_sheet_names, topics_by_sheet


# ==================== 页面 ====================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    files = database.get_all_files()
    return templates.TemplateResponse("index.html", {"request": request, "files": files})


# ==================== API ====================

@app.post("/api/upload")
async def upload_xmind(
    file: UploadFile = File(...),
    uploader: str = Form(default=""),
    password: str = Form(default=""),
    project: str = Form(default=""),
    version: str = Form(default=""),
    remark: str = Form(default=""),
):
    if not file.filename or not file.filename.endswith(".xmind"):
        raise HTTPException(status_code=400, detail="请上传 .xmind 文件")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    # 解析 XMind
    try:
        parsed = xmind_parser.parse_xmind(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {str(e)}")

    # 保存原始文件
    filepath = os.path.join(UPLOAD_DIR, file.filename)
    with open(filepath, "wb") as f:
        f.write(content)

    # 寘意存储密码（简单哈希)
    pwd_hash = hashlib.sha256(password.encode()).hexdigest() if password else ""

    # 存入数据库
    sheet_names = json.dumps([s["title"] for s in parsed["sheets"]], ensure_ascii=False)
    file_id = database.insert_file(
        file.filename, parsed["root_title"], uploader, pwd_hash, project, version, remark, sheet_names
    )

    # 逐条插入 topics，每个 sheet 单独处理
    total_topics = 0
    conn = database.get_conn()
    try:
        for sheet_idx, sheet in enumerate(parsed["sheets"]):
            id_map = {}  # 临时 ID -> 数据库 ID (per sheet)
            for t in sheet["topics"]:
                db_parent = id_map.get(t["parent_id"]) if t["parent_id"] else None
                db_id = database.insert_topic(conn, file_id, db_parent, t["title"], t["depth"], t["path"], sheet_idx)
                id_map[t["id"]] = db_id
            total_topics += len(sheet["topics"])
        conn.commit()
    finally:
        conn.close()

    return {
        "id": file_id,
        "filename": file.filename,
        "root_title": parsed["root_title"],
        "topic_count": total_topics,
        "sheet_count": len(parsed["sheets"]),
    }


@app.get("/api/files")
async def list_files(
    project: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    return database.get_all_files(project=project, search=search)


@app.get("/api/files/{file_id}")
async def get_file_detail(file_id: int, sheet: int = 0):
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    all_topics = database.get_topics_by_file(file_id)
    f, sheet_names, topics_by_sheet = _normalize_file_sheet_meta(file_id, f, all_topics)
    sheet_stats = _build_sheet_stats(sheet_names, topics_by_sheet)
    f["sheet_count"] = len(sheet_stats)
    f["total_topic_count"] = sum(stat["topic_count"] for stat in sheet_stats)
    topics = topics_by_sheet.get(sheet, [])
    return {"file": f, "topics": topics, "sheet_stats": sheet_stats}


class DeleteRequest(BaseModel):
    password: str


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: int, body: DeleteRequest):
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 验证密码：文件密码或管理员密码
    stored_password = database.get_file_password(file_id) or ""
    pwd_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if pwd_hash != stored_password and body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="密码错误")

    # 删除上传文件
    filepath = os.path.join(UPLOAD_DIR, f["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)
    database.delete_file(file_id)
    return {"detail": "已删除"}


@app.get("/api/files/{file_id}/mindmap")
async def get_mindmap(file_id: int, sheet: int = 0):
    """返回 Markmap 可用的 Markdown 文本"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    all_topics = database.get_topics_by_file(file_id)
    f, sheet_names, topics_by_sheet = _normalize_file_sheet_meta(file_id, f, all_topics)
    topics = topics_by_sheet.get(sheet, [])
    if not topics:
        raise HTTPException(status_code=404, detail="画布无数据")
    root_title = sheet_names[sheet] if sheet < len(sheet_names) else f["root_title"]

    # Build tree structure to ensure correct order (children follow parent)
    by_parent: dict[int | None, list] = {}
    for t in topics:
        by_parent.setdefault(t["parent_id"], []).append(t)
    lines = [f"# {root_title}"]

    def walk(parent_id):
        for t in by_parent.get(parent_id, []):
            if t["depth"] == 0:
                walk(t["id"])
                continue
            indent = "  " * t["depth"]
            lines.append(f"{indent}- {t['title']}")
            walk(t["id"])

    walk(None)
    markdown = "\n".join(lines)
    return {"markdown": markdown, "root_title": root_title}


@app.get("/api/files/{file_id}/tree")
async def get_tree(file_id: int, sheet: int = 0):
    """返回树形 JSON 结构"""
    topics = database.get_topics_by_file(file_id, sheet_index=sheet)
    if not topics:
        raise HTTPException(status_code=404, detail="文件不存在或无数据")

    node_map = {}
    root_nodes = []
    for t in topics:
        node = {"id": t["id"], "title": t["title"], "depth": t["depth"], "path": t["path"], "children": []}
        node_map[t["id"]] = node
        if t["parent_id"] is None or t["depth"] == 0:
            root_nodes.append(node)
        else:
            parent = node_map.get(t["parent_id"])
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

    for node in root_nodes:
        fill_topic_count(node)
    return root_nodes


@app.get("/api/modules/{file_id}")
async def get_modules(file_id: int, depth: int = 3, sheet: int = 0):
    """返回模块路径列表"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    return database.get_modules(file_id, min_depth=1, max_depth=depth, sheet_index=sheet)


@app.get("/api/cases/{file_id}/{module_path:path}")
async def get_cases(file_id: int, module_path: str, sheet: int = 0):
    """按模块路径返回用例（该模块下的所有子节点)"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")

    all_topics = database.get_topics_by_file(file_id, sheet_index=sheet)

    matched = [t for t in all_topics if t["path"] == module_path]
    if not matched:
        raise HTTPException(status_code=404, detail=f"未找到模块: {module_path}")

    parent = matched[0]

    result = []
    descendants = [t for t in all_topics if t["path"].startswith(parent["path"] + "/")]
    non_leaf_topic_ids = _get_non_leaf_topic_ids(all_topics)
    for d in descendants:
        result.append({
            "title": d["title"],
            "depth": d["depth"] - parent["depth"],
            "path": d["path"],
        })

    case_count = sum(1 for d in descendants if d["id"] not in non_leaf_topic_ids)
    return {"module": parent["title"], "path": parent["path"], "cases": result, "case_count": case_count}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


# ==================== Export API ====================


def _get_server_ip() -> str:
    """获取本机局域网 IP（非 127.0.0.1））"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/api/server-info")
async def server_info():
    """返回服务器 IP 和端口信息"""
    return {"ip": _get_server_ip(), "port": 8000}


def _build_export_markdown(file_id: int, topic_path: str, relative_depth: bool = True) -> str:
    """构建带元信息的结构化 Markdown"""
    f = database.get_file(file_id)
    if not f:
        return None

    all_topics = database.get_topics_by_file(file_id)

    matched = [t for t in all_topics if t["path"] == topic_path]
    if not matched:
        return None

    parent = matched[0]

    descendants = [t for t in all_topics if t["path"].startswith(parent["path"] + "/")]

    lines = []
    lines.append(f"# {parent['title']}")
    lines.append("")
    lines.append(f"> 文件: {f['root_title']} ({f['filename']})")
    lines.append(f"> 模块路径: {parent['path']}")
    lines.append(f"> 子节点总数: {len(descendants)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for d in descendants:
        rel_depth = d["depth"] - parent["depth"]
        indent = "  " * rel_depth
        lines.append(f"{indent}- {d['title']}")

    return "\n".join(lines)


def _build_prompt_markdown(file_id: int, topic_path: str) -> str:
    """构建 AI 提示词模板"""
    f = database.get_file(file_id)
    if not f:
        return None

    all_topics = database.get_topics_by_file(file_id)

    matched = [t for t in all_topics if t["path"] == topic_path]
    if not matched:
        return None

    parent = matched[0]
    descendants = [t for t in all_topics if t["path"].startswith(parent["path"] + "/")]

    lines = []
    lines.append("请根据以下测试用例列表，检查代码中是否覆盖了这些场景。")
    lines.append("对于每个用例,指出:是否已有代码覆盖、覆盖位置、是否存在逻辑漏洞.")
    lines.append("")
    lines.append("## 模块信息")
    lines.append(f"- 项目: {f['root_title']}")
    lines.append(f"- 模块: {parent['title']}")
    lines.append(f"- 路径: {parent['path']}")
    lines.append(f"- 用例数: {len(descendants)}")
    lines.append("")
    lines.append("## 测试用例")
    lines.append("")

    for d in descendants:
        rel_depth = d["depth"] - parent["depth"]
        indent = "  " * rel_depth
        lines.append(f"{indent}- {d['title']}")

    lines.append("")
    lines.append("## 要求")
    lines.append("1. 逐条检查每个测试用例是否在代码中有对应实现")
    lines.append("2. 对于未覆盖的用例,说明可能的风险")
    lines.append("3. 对于已覆盖的用例,标注代码位置")
    lines.append("4. 总结覆盖率和改进建议")

    return "\n".join(lines)


@app.get("/api/export/{file_id}/{topic_path:path}")
async def export_topic(file_id: int, topic_path: str, format: str = "md"):
    """导出节点及其子树为 Markdown 或 AI Prompt"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")

    if format == "prompt":
        content = _build_prompt_markdown(file_id, topic_path)
    else:
        content = _build_export_markdown(file_id, topic_path)

    if content is None:
        raise HTTPException(status_code=404, detail=f"未找到节点: {topic_path}")

    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")


# ==================== Comments API ====================


@app.get("/api/comments/{file_id}")
async def get_comments(file_id: int):
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    return database.get_comments_by_file(file_id)


@app.post("/api/comments/{file_id}")
async def create_comment(file_id: int, request: Request):
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    body = await request.json()
    topic_id = body.get("topic_id")
    author = body.get("author", "").strip()
    content = body.get("content", "").strip()
    if not topic_id or not content:
        raise HTTPException(status_code=400, detail="缺少 topic_id 或 content")
    if not author:
        author = "匿名"
    comment_id = database.insert_comment(file_id, topic_id, author, content)
    return {"id": comment_id, "topic_id": topic_id, "author": author, "content": content}


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: int):
    database.delete_comment(comment_id)
    return {"detail": "已删除"}


# ==================== Topic Edit API ====================


class TopicEditRequest(BaseModel):
    title: str


@app.put("/api/topics/{topic_id}")
async def edit_topic(topic_id: int, body: TopicEditRequest):
    topic = database.get_topic(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="节点不存在")
    old_title = topic["title"]
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    # Update title and path
    old_path = topic["path"]
    new_path = old_path.rsplit("/" + old_title, 1)[0] + "/" + new_title if "/" in old_path else new_title
    conn = database.get_conn()
    conn.execute("UPDATE topics SET title = ?, path = ? WHERE id = ?", (new_title, new_path, topic_id))
    # Update descendant paths
    conn.execute("UPDATE topics SET path = REPLACE(path, ?, ?) WHERE path LIKE ?",
                 (old_path + "/", new_path + "/", old_path + "/%"))
    conn.commit()
    conn.close()
    return {"detail": "已更新", "id": topic_id, "title": new_title}


class AddChildRequest(BaseModel):
    title: str


@app.post("/api/topics/{topic_id}/children")
async def add_child(topic_id: int, body: AddChildRequest):
    topic = database.get_topic(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="父节点不存在")
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    new_id = database.add_child_topic(topic["file_id"], topic_id, title)
    return {"detail": "已添加", "id": new_id, "title": title}
