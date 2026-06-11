from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings


def _engine_kwargs(database_url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        kwargs["connect_args"] = {"connect_timeout": 3}
    return kwargs


engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
