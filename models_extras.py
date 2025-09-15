
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, text
from db import engine
from sqlalchemy.orm import sessionmaker

BaseExtra = declarative_base()
SessionLocalExtra = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class UserSettings(BaseExtra):
    __tablename__ = "user_settings"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    pump_live = Column(Boolean, nullable=False, server_default=text("false"))
    pump_threshold_percent = Column(Integer, nullable=True)  # user override
    created_at = Column(DateTime, nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime, nullable=False, server_default=text("NOW()"))

def init_extras():
    BaseExtra.metadata.create_all(bind=engine)
