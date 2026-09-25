from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema():
    """为既有 SQLite 库补充新增列（幂等轻量迁移）。

    create_all 不会修改已存在的表，这里补齐后续版本新增的列。
    """
    if not settings.DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "capacity_follow_ups" in tables:
            cols = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(capacity_follow_ups)"
                )
            }
            if "last_contact_at" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE capacity_follow_ups "
                    "ADD COLUMN last_contact_at DATETIME"
                )
