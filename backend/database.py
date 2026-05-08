import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings

logger = logging.getLogger(__name__)


def _async_url(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


engine = create_async_engine(_async_url(settings.database_url), echo=False, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


class Base(DeclarativeBase):
    pass


class EmailRule(Base):
    __tablename__ = "email_rules"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, nullable=False)
    conditions = Column(JSONB, nullable=False, default=dict)
    actions = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self):
        return {
            "id": str(self.id), "name": self.name,
            "conditions": self.conditions, "actions": self.actions,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PendingAction(Base):
    __tablename__ = "pending_actions"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    type = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc) + timedelta(minutes=30),
    )

    def to_dict(self):
        return {
            "id": str(self.id), "type": self.type, "payload": self.payload,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class ConversationHistory(Base):
    __tablename__ = "conversation_history"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ActionLog(Base):
    __tablename__ = "action_log"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    action_type = Column(String, nullable=False)
    details = Column(JSONB)
    status = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self):
        return {
            "id": str(self.id), "action_type": self.action_type,
            "details": self.details, "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(JSONB)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self):
        return {"key": self.key, "value": self.value}


class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    text = Column(String, nullable=False)
    remind_at = Column(DateTime(timezone=True), nullable=False)
    sent = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self):
        return {
            "id": str(self.id), "text": self.text,
            "remind_at": self.remind_at.isoformat() if self.remind_at else None,
            "sent": self.sent,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Memory(Base):
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    source = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_referenced = Column(DateTime(timezone=True), server_default=func.now())


class VoicePreference(Base):
    __tablename__ = "voice_preferences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    context_type = Column(String(50))
    used_voice = Column(Boolean)
    user_feedback = Column(String(20))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GroupInteraction(Base):
    __tablename__ = "group_interactions"
    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    group_id = Column(String, nullable=False)
    sender = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    response = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


async def verify_tables() -> bool:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("[DB] All tables verified / created.")
        return True
    except Exception as e:
        logger.error(f"[DB] Failed to initialise database: {e}")
        print(f"\n{'='*60}\n❌  Database error: {e}\nCheck DATABASE_URL in .env\n{'='*60}\n")
        return False
