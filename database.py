import uuid
import datetime

from sqlalchemy import create_engine, Column, String, DateTime, ForeignKey, Text, text
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"sslmode": settings.DB_SSLMODE},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Wireframe(Base):
    __tablename__ = "wireframes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class File(Base):
    __tablename__ = "files"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    wireframe_id = Column(String, ForeignKey("wireframes.id"), nullable=False)
    original_filename = Column(String)
    stored_path = Column(String)
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChartConfig(Base):
    __tablename__ = "chart_configs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    wireframe_id = Column(String, ForeignKey("wireframes.id"), nullable=False)
    file_id = Column(String, ForeignKey("files.id"))
    sql_query = Column(Text)
    chart_type = Column(String)
    x_axis = Column(String)
    y_axis = Column(String)
    group_by = Column(String)
    title = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    wireframe_id = Column(String, ForeignKey("wireframes.id"), nullable=False)
    role = Column(String, nullable=False)  # user | assistant
    message = Column(Text)
    event_type = Column(String, nullable=False, default="message")  # message | suggestion_selected
    suggestions_json = Column(Text)  # JSON array for assistant suggestions
    metadata_json = Column(Text)  # JSON object for selection/action metadata
    parent_message_id = Column(String)  # Link selection event to assistant message id
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)
    # Lightweight auto-migration for existing deployments.
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE chat_history "
            "ADD COLUMN IF NOT EXISTS event_type VARCHAR NOT NULL DEFAULT 'message'"
        ))
        conn.execute(text(
            "ALTER TABLE chat_history "
            "ADD COLUMN IF NOT EXISTS suggestions_json TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE chat_history "
            "ADD COLUMN IF NOT EXISTS metadata_json TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE chat_history "
            "ADD COLUMN IF NOT EXISTS parent_message_id VARCHAR"
        ))
