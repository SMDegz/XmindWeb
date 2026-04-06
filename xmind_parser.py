"""XMind 8 (.xmind) 文件解析器

XMind 8 格式本质是 ZIP 包，内含 content.xml（XML 格式的思维导图数据）。
解析流程：ZIP -> content.xml -> ElementTree -> 递归提取 topic 节点
"""

import zipfile
import io
import xml.etree.ElementTree as ET
from typing import Optional

# XMind XML 命名空间
NS = {
    "xmap": "urn:xmind:xmap:xmlns:content:2.0",
    "svg": "http://www.w3.org/2000/svg",
}


def parse_xmind(content: bytes) -> dict:
    """解析 XMind 文件，返回 {root_title, topics, markdown}"""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        xml_content = zf.read("content.xml")

    root = ET.fromstring(xml_content)
    sheet = root.find("xmap:sheet", NS)
    if sheet is None:
        raise ValueError("无法找到 sheet 节点")

    root_topic = sheet.find("xmap:topic", NS)
    if root_topic is None:
        raise ValueError("无法找到根 topic 节点")

    root_title = _get_title(root_topic) or "未命名"

    # 递归提取所有 topic
    topics = []
    _walk_topic(root_topic, topics, parent_id=None, depth=0, path="")

    # 生成 Markdown
    markdown = _topics_to_markdown(root_title, topics)

    return {
        "root_title": root_title,
        "topics": topics,
        "markdown": markdown,
    }


def _get_title(topic: ET.Element) -> str:
    """获取 topic 的 title 文本"""
    title_el = topic.find("xmap:title", NS)
    if title_el is not None:
        text = (title_el.text or "").strip()
        # 将换行替换为空格，避免破坏 markdown/markmap 格式
        text = text.replace("\n", " ").replace("\r", " ")
        # 合并多余空格
        while "  " in text:
            text = text.replace("  ", " ")
        return text.strip()
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
