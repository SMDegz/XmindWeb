import os
import socket
import shutil
import hashlib
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
    file_id = database.insert_file(
        file.filename, parsed["root_title"], uploader, pwd_hash, project, version, remark
    )

    # 逐条插入 topics，建立临时ID -> 数据库ID 映射以正确关联 parent
    topics = parsed["topics"]
    id_map = {}  # 临时 ID -> 数据库 ID
    conn = database.get_conn()
    try:
        for t in topics:
            db_parent = id_map.get(t["parent_id"]) if t["parent_id"] else None
            db_id = database.insert_topic(conn, file_id, db_parent, t["title"], t["depth"], t["path"])
            id_map[t["id"]] = db_id
        conn.commit()
    finally:
        conn.close()

    return {
        "id": file_id,
        "filename": file.filename,
        "root_title": parsed["root_title"],
        "topic_count": len(topics),
    }


@app.get("/api/files")
async def list_files(
    project: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    return database.get_all_files(project=project, search=search)


@app.get("/api/files/{file_id}")
async def get_file_detail(file_id: int):
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    topics = database.get_topics_by_file(file_id)
    return {"file": f, "topics": topics}


class DeleteRequest(BaseModel):
    password: str


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: int, body: DeleteRequest):
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 验证密码：文件密码或管理员密码
    pwd_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if pwd_hash != f.get("password", "") and body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="密码错误")

    # 删除上传文件
    filepath = os.path.join(UPLOAD_DIR, f["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)
    database.delete_file(file_id)
    return {"detail": "已删除"}


@app.get("/api/files/{file_id}/mindmap")
async def get_mindmap(file_id: int):
    """返回 Markmap 可用的 Markdown 文本"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    topics = database.get_topics_by_file(file_id)
    lines = [f"# {f['root_title']}"]
    for t in topics:
        if t["depth"] == 0:
            continue
        indent = "  " * t["depth"]
        lines.append(f"{indent}- {t['title']}")
    markdown = "\n".join(lines)
    return {"markdown": markdown, "root_title": f["root_title"]}


@app.get("/api/files/{file_id}/tree")
async def get_tree(file_id: int):
    """返回树形 JSON 结构"""
    topics = database.get_topics_by_file(file_id)
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
    return root_nodes


@app.get("/api/modules/{file_id}")
async def get_modules(file_id: int, depth: int = 3):
    """返回模块路径列表"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")
    return database.get_modules(file_id, min_depth=1, max_depth=depth)


@app.get("/api/cases/{file_id}/{module_path:path}")
async def get_cases(file_id: int, module_path: str):
    """按模块路径返回用例（该模块下的所有子节点)"""
    f = database.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="文件不存在")

    all_topics = database.get_topics_by_file(file_id)

    matched = [t for t in all_topics if t["path"] == module_path]
    if not matched:
        raise HTTPException(status_code=404, detail=f"未找到模块: {module_path}")

    parent = matched[0]

    result = []
    descendants = [t for t in all_topics if t["path"].startswith(parent["path"] + "/")]
    for d in descendants:
        result.append({
            "title": d["title"],
            "depth": d["depth"] - parent["depth"],
            "path": d["path"],
        })

    return {"module": parent["title"], "path": parent["path"], "cases": result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


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
