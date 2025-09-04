# db.py
import os
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")
# SQLAlchemy θέλει postgresql://
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String(64), unique=True, index=True, nullable=False)
    is_premium = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    alerts = relationship("Alert", back_populates="user")

class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(32), index=True, nullable=False)          # π.χ. BTCUSDT
    rule = Column(String(32), nullable=False)                        # "price_above" | "price_below"
    value = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    cooldown_seconds = Column(Integer, default=900, nullable=False)
    last_fired_at = Column(DateTime, nullable=True)
    last_met = Column(Boolean, nullable=True)                        # ✅ για edge-trigger

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="alerts")

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    provider = Column(String(32), nullable=False)                    # "paypal"
    provider_status = Column(String(64), nullable=True)
    status_internal = Column(String(32), nullable=False)             # ACTIVE | CANCEL_AT_PERIOD_END | CANCELLED
    provider_ref = Column(String(128), nullable=True)                # π.χ. subscription id
    current_period_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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

def init_db():
    Base.metadata.create_all(bind=engine)
