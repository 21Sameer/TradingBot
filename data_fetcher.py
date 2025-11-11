
# data_fetcher.py
import pandas as pd
import requests
from datetime import datetime, timedelta
from sqlalchemy import insert, delete, select, func
from db import engine, candles
import time

# 🔹 Local cache to avoid Binance API spamming
_cached_candles = {}
_CACHE_TTL = 120  # 2 minutes
BINANCE_API = "https://api.binance.com/api/v3/klines"


# ------------------------------------------------------------
# 🔹 Fetch OHLCV with retry + cache
# ------------------------------------------------------------
def fetch_binance_candles(symbol, interval="1m", limit=1000, retries=3):
    """Fetch OHLCV data from Binance (with retry, cache, and safety)."""
    key = f"{symbol.lower()}_{interval}"
    now = time.time()
    df = pd.DataFrame()

    # ✅ Use cache if recent
    if key in _cached_candles:
        ts, cached_df = _cached_candles[key]
        if now - ts < _CACHE_TTL:
            print(f"⚡ Using cached candles for {symbol.upper()} ({interval})")
            return cached_df.copy()

    for attempt in range(retries):
        try:
            url = f"{BINANCE_API}?symbol={symbol.upper()}&interval={interval}&limit={limit}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            raw = resp.json()

            df = pd.DataFrame(raw, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "num_trades", "taker_base_vol",
                "taker_quote_vol", "ignore"
            ])
            df["timestamp"] = pd.to_datetime(df["close_time"], unit="ms", errors="coerce")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df[["timestamp", "open", "high", "low", "close", "volume"]].dropna()

            if df.empty:
                raise ValueError("Empty DataFrame received")

            _cached_candles[key] = (now, df)
            print(f"✅ Binance candles fetched successfully for {symbol.upper()} ({interval}, {len(df)} rows)")
            return df

        except Exception as e:
            wait = 2 ** attempt
            print(f"⏳ Binance fetch failed for {symbol.upper()} ({interval}): {e}. Retrying in {wait}s...")
            time.sleep(wait)

    if key in _cached_candles:
        print(f"⚠️ Using stale cached candles for {symbol.upper()} ({interval}) after retries failed.")
        return _cached_candles[key][1].copy()

    print(f"❌ Binance timeout for {symbol.upper()} ({interval}), retry later.")
    return pd.DataFrame()


# ------------------------------------------------------------
# 🔹 Incremental update (new candles only)
# ------------------------------------------------------------
def update_candles_for_symbol(symbol: str, interval="1m"):
    """Fetch only new candles for a symbol and append to DB efficiently."""
    symbol = symbol.lower()

    try:
        with engine.connect() as conn:
            last_ts = conn.execute(
                select(func.max(candles.c.timestamp))
                .where(candles.c.symbol == symbol)
                .where(candles.c.interval == interval)
            ).scalar()

        # --- First time? Fetch full batch ---
        if not last_ts:
            print(f"📉 No candles found for {symbol.upper()} ({interval}), fetching initial batch...")
            df = fetch_binance_candles(symbol, interval, limit=1000)
            if not df.empty:
                insert_candles_to_db(symbol, df, interval)
            return

        # --- Incremental fetch since last timestamp ---
        start_time = int((pd.to_datetime(last_ts) + timedelta(milliseconds=1)).timestamp() * 1000)
        url = f"{BINANCE_API}?symbol={symbol.upper()}&interval={interval}&startTime={start_time}&limit=1000"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()

        if not raw:
            print(f"⏸️ No new candles for {symbol.upper()} ({interval})")
            return

        df_new = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base_vol",
            "taker_quote_vol", "ignore"
        ])
        df_new["timestamp"] = pd.to_datetime(df_new["close_time"], unit="ms", errors="coerce")
        for col in ["open", "high", "low", "close", "volume"]:
            df_new[col] = pd.to_numeric(df_new[col], errors="coerce")
        df_new = df_new[["timestamp", "open", "high", "low", "close", "volume"]].dropna()

        insert_candles_to_db(symbol, df_new, interval)
        print(f"🟢 Inserted {len(df_new)} new candles for {symbol.upper()} ({interval})")

    except Exception as e:
        print(f"⚠️ Incremental update failed for {symbol} ({interval}): {e}")


# ------------------------------------------------------------
# 🔹 Insert helper
# ------------------------------------------------------------
def insert_candles_to_db(symbol, df, interval):
    """Insert new candles into the Neon DB safely."""
    if df.empty:
        return
    try:
        records = [
            {
                "symbol": symbol.lower(),
                "interval": interval,
                "timestamp": row.timestamp,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
            }
            for _, row in df.iterrows()
        ]
        with engine.begin() as conn:
            conn.execute(insert(candles), records)
        print(f"✅ {len(records)} {interval} candles inserted for {symbol.upper()}")
    except Exception as e:
        print(f"⚠️ Insert failed for {symbol.upper()} ({interval}): {e}")


# ------------------------------------------------------------
# 🔹 Backfill multiple timeframes (initial setup)
# ------------------------------------------------------------
def backfill_all_timeframes(symbol):
    """Bootstrap all intervals (1m–4h) for a symbol."""
    for interval in ["1m", "5m", "15m", "1h", "4h"]:
        update_candles_for_symbol(symbol, interval)


# ------------------------------------------------------------
# 🔹 Ensure symbol has enough data before AI/trading
# ------------------------------------------------------------
def ensure_symbol_data(symbol: str):
    """Ensure the DB has enough data; otherwise, backfill it."""
    symbol = symbol.lower()
    try:
        with engine.connect() as conn:
            q = select(candles.c).where(candles.c.symbol == symbol)
            rows = conn.execute(q).fetchall()

        if rows and len(rows) > 100:
            print(f"✅ {symbol.upper()} already has {len(rows)} candles in DB")
            return

        print(f"⚠️ {symbol.upper()} missing or incomplete — backfilling now...")
        backfill_all_timeframes(symbol)

    except Exception as e:
        print(f"❌ ensure_symbol_data() failed for {symbol}: {e}")
        backfill_all_timeframes(symbol)


# ------------------------------------------------------------
# 🔹 Periodic cleanup (keep last few days only)
# ------------------------------------------------------------
def cleanup_old_candles(symbol: str, interval: str, days_to_keep: int = 3):
    """Delete candles older than N days for this symbol+interval."""
    try:
        cutoff = datetime.utcnow() - timedelta(days=days_to_keep)
        with engine.begin() as conn:
            result = conn.execute(
                delete(candles)
                .where(candles.c.symbol == symbol.lower())
                .where(candles.c.interval == interval)
                .where(candles.c.timestamp < cutoff)
            )
        print(f"🧹 Deleted {result.rowcount} old {interval} candles for {symbol.upper()} (> {days_to_keep} days old)")
    except Exception as e:
        print(f"⚠️ Cleanup failed for {symbol.upper()} {interval}: {e}")

# ------------------------------------------------------------
# 🔹 Fetch recent candles (DB first, then Binance)
# ------------------------------------------------------------
def get_recent_candles(symbol: str, interval: str = "1m", limit: int = 1000):
    """
    Fetch recent OHLCV candles for a symbol.
    1️⃣ Try database first (for speed)
    2️⃣ If missing or incomplete, fetch from Binance REST API
    """
    import pandas as pd
    symbol = symbol.lower()

    try:
        # --- Try loading from database first
        query = (
            candles.select()
            .where(candles.c.symbol == symbol)
            .where(candles.c.interval == interval)
            .order_by(candles.c.timestamp.desc())
            .limit(limit)
        )
        with engine.connect() as conn:
            rows = conn.execute(query).fetchall()

        if rows and len(rows) > 20:
            df = pd.DataFrame(rows, columns=candles.c.keys())
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
            print(f"📊 Loaded {len(df)} {interval} candles for {symbol.upper()} from DB")
            return df

        # --- Fallback to Binance if not enough data
        print(f"⚠️ No/insufficient candles in DB for {symbol.upper()} ({interval}). Fetching from Binance...")
        return fetch_binance_candles(symbol, interval, limit)

    except Exception as e:
        print(f"❌ get_recent_candles() failed for {symbol.upper()}: {e}")
        return fetch_binance_candles(symbol, interval, limit)
