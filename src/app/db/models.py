from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    source_chat: Mapped[str | None] = mapped_column(String(255))
    sender: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), default="text")
    reply_to_text: Mapped[str | None] = mapped_column(Text)
    raw_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    # AI processing results
    category: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[str | None] = mapped_column(String(16))
    extracted_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_people: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    extracted_topic: Mapped[str | None] = mapped_column(String(512))
    ai_response: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Status lifecycle: raw → pending_ai → processed → notified → archived
    status: Mapped[str] = mapped_column(String(32), default="raw")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tasks: Mapped[list["Task"]] = relationship(back_populates="message")

    __table_args__ = (
        Index("idx_messages_status", "status"),
        Index("idx_messages_status_created", "status", created_at.desc()),
        Index("idx_messages_source_created", "source", created_at.desc()),
        Index("idx_messages_created_at", created_at.desc()),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[Message | None] = relationship(back_populates="tasks")

    __table_args__ = (
        Index("idx_tasks_active", "is_done", "due_date", postgresql_where=(~is_done)),
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_reminders_pending", "remind_at", postgresql_where=(~sent)),
    )


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    monitored_chats: Mapped[dict] = mapped_column(JSONB, default=list)
    ignored_chats: Mapped[dict] = mapped_column(JSONB, default=list)
    quiet_hours: Mapped[dict] = mapped_column(JSONB, default=dict)
    important_people: Mapped[dict] = mapped_column(JSONB, default=list)
    digest_time: Mapped[str] = mapped_column(String(5), default="09:00")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Kyiv")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatDailySummary(Base):
    __tablename__ = "chat_daily_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary_date: Mapped[date] = mapped_column(Date, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("chat_name", "summary_date", name="uq_chat_daily_summary"),
        Index("idx_chat_summary_date", "summary_date"),
    )
