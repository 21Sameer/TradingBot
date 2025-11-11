#db.py
from sqlalchemy import create_engine, Column, String, Float, DateTime, Integer, JSON, MetaData, Table # type: ignore
from sqlalchemy.dialects.postgresql import JSONB # type: ignore
from datetime import datetime,timezone
import psycopg2 # type: ignore

# --- CONFIG ---
DB_URL = "postgresql://neondb_owner:npg_VHRrdlZ9ngO2@ep-icy-leaf-afjwtqdg-pooler.c-2.us-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

engine = create_engine(
    DB_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,  # Recycle connections after 30 minutes
    connect_args={
        "sslmode": "require",
        "connect_timeout": 10,
        "application_name": "paper_trading_backend",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5
    }
)
metadata = MetaData()

# --- TABLES ---
trades = Table(
    "trades", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String),
    Column("side", String),
    Column("entry", Float),
    Column("target", Float),
    Column("stop_loss", Float),
    Column("size", Float),
    Column("opened_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
    Column("closed_at", DateTime),
    Column("exit_price", Float),
    Column("pnl", Float),
    Column("outcome", String),
    Column("reason", String),
    Column("fees", Float, default=0.0),
    Column("status", String, default="OPEN"),  # ✅ NEW COLUMN
    
)

portfolio = Table(
    "portfolio", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("balance", Float, default=10000.0),
    Column("open_trades", JSONB),
    Column("updated_at",DateTime(timezone=True),default=lambda: datetime.now(timezone.utc),onupdate=lambda: datetime.now(timezone.utc),
)
)

ai_decisions = Table(
    "ai_decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String),
    Column("decision", String),
    Column("confidence", Float),
    Column("reason", String),
    Column("entry", Float),
    Column("target", Float),
    Column("stop_loss", Float),
    Column("timestamp",DateTime(timezone=True),default=lambda: datetime.now(timezone.utc),)
)

# Optional: store raw candle data
candles = Table(
    "candles", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String),
    Column("interval", String),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("volume", Float),
    Column("timestamp", DateTime)
)

def init_db():
    metadata.create_all(engine)
