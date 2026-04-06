from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class XMindFile(BaseModel):
    id: int
    filename: str
    upload_time: str
    root_title: str

    class Config:
        from_attributes = True


class TopicNode(BaseModel):
    id: int
    file_id: int
    parent_id: Optional[int] = None
    title: str
    depth: int
    path: str

    class Config:
        from_attributes = True


class ModuleInfo(BaseModel):
    path: str
    title: str
    depth: int
    case_count: int


class CaseItem(BaseModel):
    title: str
    path: str
    children: list["CaseItem"] = []

    class Config:
        from_attributes = True
