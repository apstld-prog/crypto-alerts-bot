# db.py
import os
from contextlib import contextmanager
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, Float,
    DateTime, Text, ForeignKey, func
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import NullPool

# -------------------------------------------------------------------
# DATABASE URL
# -------------------------------------------------------------------
_DB_URL = os.getenv("DATABASE_URL", "").strip()
if not _DB_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Normalize scheme: postgres:// → postgresql://
if _DB_URL.startswith("postgres://"):
    _DB_URL = "postgresql://" + _DB_URL[len("postgres://"):]

# Ensure sslmode=require if missing (Neon typically needs it)
if "sslmode=" not in _DB_URL and _DB_URL.startswith("postgresql://"):
    if "?" in _DB_URL:
        _DB_URL += "&sslmode=require"
    else:
        _DB_URL += "?sslmode=require"

# -------------------------------------------------------------------
# SQLAlchemy Engine options (lightweight, Neon-friendly)
# -------------------------------------------------------------------
# Small pool so the DB can autosuspend between cycles
ENGINE_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "2"))
ENGINE_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "0"))
ENGINE_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "180"))  # seconds
ENGINE_POOL_PRE_PING = True  # detect stale connections early
ENGINE_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))

engine = create_engine(
    _DB_URL,
    pool_size=ENGINE_POOL_SIZE,
    max_overflow=ENGINE_MAX_OVERFLOW,
    pool_pre_ping=ENGINE_POOL_PRE_PING,
    pool_recycle=ENGINE_POOL_RECYCLE,
    pool_timeout=ENGINE_POOL_TIMEOUT,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

# -------------------------------------------------------------------
# MODELS
# -------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String(64), unique=True, index=True, nullable=False)
    is_premium = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    symbol = Column(String(32), index=True, nullable=False)       # e.g. BTCUSDT
    rule = Column(String(32), nullable=False)                     # "price_above" | "price_below"
    value = Column(Float, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    cooldown_seconds = Column(Integer, nullable=False, default=900)
    last_fired_at = Column(DateTime, nullable=True)
    last_met = Column(Boolean, nullable=False, default=False)     # κρατάμε το state για να μη σπαμάρει
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="alerts")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    provider = Column(String(32), nullable=False, default="paypal")       # "paypal"
    provider_ref = Column(String(128), nullable=True)                     # PayPal subscription id
    provider_status = Column(String(64), nullable=True)                   # RAW status from provider
    status_internal = Column(String(64), nullable=False, default="UNKNOWN")  # ACTIVE / CANCELLED / CANCEL_AT_PERIOD_END / UNKNOWN
    current_period_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    extra = Column(Text, nullable=True)

    user = relationship("User", back_populates="subscriptions")

# -------------------------------------------------------------------
# INIT / SESSION
# -------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(bind=engine)

@contextmanager
def session_scope():
    """Provide a transactional scope."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def masked_db_url() -> str:
    """Return masked DB URL for logs (hide password)."""
    try:
        return engine.url.render_as_string(hide_password=True)
    except Exception:
        return str(engine.url)
