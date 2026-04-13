"""XMind 8 (.xmind) 文件解析器

XMind 8 格式本质是 ZIP 包，内含 content.xml（XML 格式的思维导图数据）。
解析流程：ZIP -> content.xml -> ElementTree -> 递归提取 topic 节点
"""

import zipfile
import io
import re
import xml.etree.ElementTree as ET
from typing import Optional

# XMind XML 命名空间
NS = {
    "xmap": "urn:xmind:xmap:xmlns:content:2.0",
    "svg": "http://www.w3.org/2000/svg",
}

PLACEHOLDER_SHEET_TITLE_RE = re.compile(
    r"^(?:画布|sheet|canvas)\s*\d+(?:\s*(?:copy|副本))?$",
    re.IGNORECASE,
)
PLACEHOLDER_CONTENT_TITLES = {
    "中心主题",
    "中央主题",
    "central topic",
    "topic",
    "root topic",
}


def parse_xmind(content: bytes) -> dict:
    """解析 XMind 文件，返回 {root_title, sheets: [{title, topics}], markdown, topic_count}

    支持多标签页：一个 XMind 文件可包含多个 sheet。
    root_title 取第一个标签页标题，sheets 包含所有标签页数据。
    """
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        xml_content = zf.read("content.xml")

    root = ET.fromstring(xml_content)
    sheets_el = root.findall("xmap:sheet", NS)
    if not sheets_el:
        raise ValueError("无法找到 sheet 节点")

    sheets = []
    for idx, sheet in enumerate(sheets_el):
        root_topic = sheet.find("xmap:topic", NS)
        if root_topic is None:
            continue
        sheet_title_el = sheet.find("xmap:title", NS)
        sheet_title = _normalize_title(sheet_title_el.text if sheet_title_el is not None else "")
        topics = []
        _walk_topic(root_topic, topics, parent_id=None, depth=0, path="")
        title = resolve_sheet_title(sheet_title, topics, idx)
        sheets.append({"title": title, "topics": topics})

    if not sheets:
        raise ValueError("无法找到有效的画布数据")

    # 兼容：root_title 和 markdown 取第一个标签页
    first = sheets[0]
    markdown = _topics_to_markdown(first["title"], first["topics"])
    total_topics = sum(len(s["topics"]) for s in sheets)

    return {
        "root_title": first["title"],
        "sheets": sheets,
        "markdown": markdown,
        "topic_count": total_topics,
    }


def resolve_sheet_title(sheet_title: str, topics: list[dict], index: int) -> str:
    preferred_title = _normalize_title(sheet_title)
    root_title = ""
    second_level_title = ""

    for topic in topics:
        title = _normalize_title(topic.get("title", ""))
        if not title:
            continue
        if topic.get("depth") == 0 and not root_title:
            root_title = title
        elif topic.get("depth") == 1 and not second_level_title:
            second_level_title = title
        if root_title and second_level_title:
            break

    if preferred_title and not _is_placeholder_sheet_title(preferred_title):
        return preferred_title
    if second_level_title:
        return second_level_title
    if root_title and not _is_placeholder_content_title(root_title):
        return root_title
    if root_title:
        return root_title
    if preferred_title:
        return preferred_title
    return f"画布{index + 1}"


def _normalize_title(text: str) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


def _is_placeholder_sheet_title(title: str) -> bool:
    title = _normalize_title(title)
    return bool(title) and bool(PLACEHOLDER_SHEET_TITLE_RE.match(title))


def _is_placeholder_content_title(title: str) -> bool:
    title = _normalize_title(title)
    return not title or title.lower() in PLACEHOLDER_CONTENT_TITLES or _is_placeholder_sheet_title(title)


def _get_title(topic: ET.Element) -> str:
    """获取 topic 的 title 文本"""
    title_el = topic.find("xmap:title", NS)
    if title_el is not None:
        return _normalize_title(title_el.text or "")
    return ""


def _walk_topic(
    topic: ET.Element,
    result: list[dict],
    parent_id: Optional[int],
    depth: int,
    path: str,
):
    """递归遍历 topic 树"""
    title = _get_title(topic)
    topic_id = len(result) + 1  # 1-based 临时 ID，入库后由数据库分配
    current_path = f"{path}/{title}" if path else title

    result.append({
        "id": topic_id,
        "parent_id": parent_id,
        "title": title,
        "depth": depth,
        "path": current_path,
    })

    # 遍历子节点
    children_el = topic.find("xmap:children", NS)
    if children_el is not None:
        for topics_el in children_el.findall("xmap:topics", NS):
            for child_topic in topics_el.findall("xmap:topic", NS):
                _walk_topic(child_topic, result, topic_id, depth + 1, current_path)


def _topics_to_markdown(root_title: str, topics: list[dict]) -> str:
    """将 topics 列表转为 Markdown 层级文本（供 Markmap 渲染）"""
    lines = [f"# {root_title}"]
    for t in topics:
        if t["depth"] == 0:
            continue  # 跳过根节点（已作为 h1）
        indent = "  " * t["depth"]
        lines.append(f"{indent}- {t['title']}")
    return "\n".join(lines)
