
from sqlalchemy import text
from db import session_scope

SQL = '''
CREATE TABLE IF NOT EXISTS user_settings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    pump_live BOOLEAN NOT NULL DEFAULT FALSE,
    pump_threshold_percent INTEGER NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_settings_user_id ON user_settings(user_id);
'''

if __name__ == "__main__":
    print("[migrate user_settings] starting…")
    with session_scope() as s:
        s.execute(text(SQL))
    print("[migrate user_settings] ok ✅")
