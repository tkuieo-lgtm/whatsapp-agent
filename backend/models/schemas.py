from pydantic import BaseModel
from typing import Optional, Any, Dict
from datetime import datetime


class IncomingMessage(BaseModel):
    sender: str
    message: str
    timestamp: Optional[datetime] = None
    # Group context
    is_group: bool = False
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    group_sender: Optional[str] = None
    group_sender_name: Optional[str] = None
    # Media (voice notes)
    message_type: str = "text"   # "text" | "audio"
    media_data: Optional[str] = None   # base64-encoded audio
    media_mime: Optional[str] = None


class EmailRuleCreate(BaseModel):
    name: str
    conditions: Dict[str, Any]
    actions: Dict[str, Any]
    is_active: bool = True


class EmailRuleUpdate(BaseModel):
    name: Optional[str] = None
    conditions: Optional[Dict[str, Any]] = None
    actions: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class SettingUpdate(BaseModel):
    value: Any
