
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, String, Integer, Boolean, DateTime, ForeignKey, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

def _normalize_database_url(raw: Optional[str]) -> str:
    if not raw:
        return "sqlite:///./local.db"
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url

def _build_engine(url: str):
    try:
        return create_engine(url, pool_pre_ping=True)
    except Exception as e:
        raise RuntimeError(
            "Invalid DATABASE_URL format. Expected like postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require"
        )

DATABASE_URL_RAW = os.getenv("DATABASE_URL")
DATABASE_URL = _normalize_database_url(DATABASE_URL_RAW)

engine = _build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))                 # 'paypal'
    provider_status: Mapped[str] = mapped_column(String(64))          # raw PayPal status
    status_internal: Mapped[str] = mapped_column(String(32))          # ACTIVE | CANCEL_AT_PERIOD_END | CANCELLED
    provider_ref: Mapped[Optional[str]] = mapped_column(String(128))  # PayPal subscription id
    current_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[Optional[User]] = relationship(User)

class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    symbol: Mapped[str] = mapped_column(String(32))  # e.g., BTCUSDT
    rule: Mapped[str] = mapped_column(String(32))    # price_above | price_below
    value: Mapped[float] = mapped_column(Float)      # threshold
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=900)  # 15m
    last_fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship(User)

def init_db():
    Base.metadata.create_all(bind=engine)

@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
