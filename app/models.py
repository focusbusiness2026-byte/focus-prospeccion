from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Role(str, enum.Enum):
    FOCUS_ADMIN = "focus_admin"
    FOCUS_OPERATOR = "focus_operator"
    CLIENT = "client"


class SearchStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    quota: Mapped["Quota"] = relationship(back_populates="client", uselist=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(32), default=Role.CLIENT.value)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Quota(Base):
    __tablename__ = "quotas"

    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), primary_key=True)
    launches_total: Mapped[int] = mapped_column(Integer, default=0)
    launches_consumed: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    client: Mapped[Client] = relationship(back_populates="quota")

    @property
    def launches_available(self) -> int:
        return max(0, self.launches_total - self.launches_consumed)


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default=SearchStatus.PENDING.value, index=True)
    source_mode: Mapped[str] = mapped_column(String(32), default="fixture")
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    quota_charged: Mapped[bool] = mapped_column(Boolean, default=True)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Prospect(Base):
    __tablename__ = "prospects"
    __table_args__ = (UniqueConstraint("client_id", "dedupe_key", name="uq_client_prospect"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    legal_name: Mapped[str] = mapped_column(String(220))
    commercial_name: Mapped[str] = mapped_column(String(220))
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(160), nullable=True)
    employees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revenue_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    classification: Mapped[str] = mapped_column(String(24), default="red")
    score_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SearchResult(Base):
    __tablename__ = "search_results"
    __table_args__ = (UniqueConstraint("search_id", "prospect_id", name="uq_search_prospect"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    search_id: Mapped[str] = mapped_column(ForeignKey("search_jobs.id"), index=True)
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
