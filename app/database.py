from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import StaticPool

DATABASE_URL = "sqlite:///./data.db"

engine = create_engine(
	DATABASE_URL,
	connect_args={"check_same_thread": False},
	poolclass=StaticPool,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
	pass


def init_db() -> None:
	from . import models  # noqa: F401
	Base.metadata.create_all(bind=engine)
