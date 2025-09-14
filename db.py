# db.py
# SQLAlchemy setup & models for the crypto alerts bot.

import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

# Recommended engine settings for Render/PG
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    Base.metadata.create_all(bind=engine)


# ───────────────────────── Models ─────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # Keep telegram_id as string because TG ids can be large and we sometimes stringify
    telegram_id = Column(String(64), unique=True, nullable=False, index=True)
    is_premium = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # e.g. BTCUSDT
    symbol = Column(String(32), nullable=False, index=True)

    # "price_above" | "price_below"
    rule = Column(String(32), nullable=False)

    # threshold value
    value = Column(Float, nullable=False)

    enabled = Column(Boolean, nullable=False, server_default=text("true"))

    # cooldown seconds between firings
    cooldown_seconds = Column(Integer, nullable=False, server_default=text("900"))

    # last time alert actually fired
    last_fired_at = Column(DateTime, nullable=True)

    # last condition evaluation result
    last_met = Column(Boolean, nullable=True)

    # NEW: per-user sequential number A1, A2, ...
    user_seq = Column(Integer, nullable=True, index=True)

    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    user = relationship("User", back_populates="alerts")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # e.g. "paypal"
    provider = Column(String(32), nullable=False, server_default=text("'paypal'"))
    # external subscription id (from PayPal etc.)
    provider_sub_id = Column(String(128), nullable=True, index=True)

    # "ACTIVE" | "CANCEL_AT_PERIOD_END" | "CANCELLED" | etc.
    status_internal = Column(String(64), nullable=True, index=True)

    # Optional metadata
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

    user = relationship("User", back_populates="subscriptions")
