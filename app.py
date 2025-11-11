# app.py
import threading
import uvicorn
import asyncio
import time
import websocket_fetcher
from datetime import datetime, timezone
from paper_trading_engine import run_paper_trading
from fastapi_backend_v2 import app  # Updated to v2
from db import init_db
from paper_trading_engine import ai_worker
from paper_trading_engine import PaperTradingEngineSingleton
pte = PaperTradingEngineSingleton.get_instance()


# ✅ Reset old in-memory trades on backend startup
pte.portfolio["open_trades"] = []
print("🧹 Cleared old open_trades cache on startup")


# ------------------------------------------------------------

def sanitize_dataframe_timestamps(df):
    """Fix and clean timestamp columns to prevent mktime errors."""
    import pandas as pd
    import numpy as np

    if df is None or df.empty:
        return df

    # Detect timestamp column
    ts_col = None
    for col in ["timestamp", "open_time", "time", "Date"]:
        if col in df.columns:
            ts_col = col
            break
    if not ts_col:
        return df

    try:
        # Convert to datetime safely
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
        df = df.dropna(subset=[ts_col])

        # Drop obviously invalid timestamps (ancient or far future)
        df = df[
            (df[ts_col].dt.year > 2000)
            & (df[ts_col].dt.year < 2100)
        ]

        # Remove timezone info
        df[ts_col] = df[ts_col].dt.tz_localize(None)

        # Optional: sort by timestamp to avoid misordered data
        df = df.sort_values(ts_col).reset_index(drop=True)

    except Exception as e:
        print(f"⚠️ Timestamp sanitation failed: {e}")

    return df

# Initialize database connection
init_db()

# ✅ Auto-close stale open trades on backend startup
from sqlalchemy import update
from db import engine, trades
from datetime import datetime, timezone

with engine.begin() as conn:
    result = conn.execute(
        update(trades)
        .where(trades.c.closed_at.is_(None))
        .values(
            closed_at=datetime.now(timezone.utc),
            status="CLOSED",  # ✅ MUST SET STATUS
            outcome="LOSS",   # ✅ MUST SET OUTCOME
            reason="backend_restart_cleanup"
        )
    )
    print(f"🧹 Cleaned {result.rowcount} stale DB trades on startup")




# ------------------------------------------------------------
# 🔹 Configure tracked symbols (empty by default; frontend subscribes)
# ------------------------------------------------------------
raw_symbols = []  # can be populated later dynamically
symbols = [s.strip().lower() for s in raw_symbols if s and s.strip()]

# ------------------------------------------------------------
# 🔹 Handle uncaught thread exceptions
# ------------------------------------------------------------
def _thread_excepthook(args):
    
    """Global exception hook for threads (Python 3.8+)."""
    try:
        print(f"⚠️ Uncaught exception in thread {args.thread.name}: {args.exc_type.__name__}: {args.exc_value}")
    except Exception:
        print("⚠️ Uncaught exception in thread (unable to format args)")

threading.excepthook = _thread_excepthook

# ------------------------------------------------------------
# 🔹 Start all background systems
# ------------------------------------------------------------
from data_fetcher import get_recent_candles
from paper_trading_engine import ai_trade
import time
import pandas as pd

def _run_ai_loop():
    """Background AI analysis loop."""
    while True:
        try:
            tracked = websocket_fetcher.get_tracked_symbols()
            if tracked:
                for symbol in tracked:
                    try:
                        df = get_recent_candles(symbol, interval="1m", limit=1000)
                        if df is not None and not df.empty:
                            df = sanitize_dataframe_timestamps(df)
                            loop = asyncio.new_event_loop()
                            loop.run_until_complete(ai_trade(symbol, df))
                            loop.close()
                    except Exception as e:
                        print(f"⚠️ AI loop error for {symbol}: {e}")
            time.sleep(60)  # Wait 60 seconds between iterations
        except Exception as e:
            print(f"⚠️ Main AI loop error: {e}")
            time.sleep(10)

def start_all():
    """Start all background systems including WebSocket and trading threads."""
    print("🔄 Starting background systems...")
    
    # Start with an empty list of symbols - they'll be added dynamically
    websocket_fetcher.run_ws([])
    
    # Give WebSocket time to initialize
    time.sleep(2)
    
    try:
        # Start AI loop in background
        print("🤖 Starting AI analysis loop...")
        ai_thread = threading.Thread(
            target=_run_ai_loop,
            daemon=True,
            name="ai-loop"
        )
        ai_thread.start()
        print("✅ AI loop thread started")
        
        return True
    except Exception as e:
        print(f"❌ Error in start_all: {e}")
        return False

# ------------------------------------------------------------
# ============================================================
# 🔹 Configure static file serving (BEFORE main())
# ============================================================
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

# Path to your public folder
public_dir = os.path.join(os.path.dirname(__file__), "public")

# Serve index.html at root "/"
@app.get("/", include_in_schema=False)
def root():
    """Serve the frontend at the root path."""
    return FileResponse(os.path.join(public_dir, "index.html"), media_type="text/html")

# Serve static files from /static (CSS, JS, etc.)
if os.path.exists(public_dir):
    app.mount("/static", StaticFiles(directory=public_dir), name="static")
    print(f"✅ Static files mounted from {public_dir}")
else:
    print(f"⚠️ Public directory not found: {public_dir}")

# ============================================================
# 🔹 Entry point
# ============================================================
# ------------------------------------------------------------


def main():
    """Main entry point for the application."""
    try:
        print("🚀 Starting trading system...")
        
        # Start background systems first
        if not start_all():
            print("❌ Failed to start background systems")
            return
            
        print("✅ Background systems started successfully")
        print("🌐 Starting FastAPI server on http://127.0.0.1:8080")
        
        # Run the FastAPI application
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8080,
            reload=False,
            access_log=False,
            log_level="info",
            timeout_keep_alive=30
        )
        
    except KeyboardInterrupt:
        print("\n👋 Shutting down gracefully...")
    except Exception as e:
        print(f"❌ Fatal error: {str(e)}")
        raise


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

# Path to your public folder
public_dir = os.path.join(os.path.dirname(__file__), "public")

# Serve static files
app.mount("/public", StaticFiles(directory=public_dir), name="public")

# Serve index.html at root
@app.get("/")
def root():
    return FileResponse(os.path.join(public_dir, "index.html"))




if __name__ == "__main__":
    main()
