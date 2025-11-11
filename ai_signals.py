
# ai_signals.py
import requests
import re
import json
import pandas as pd
import time
from config import GROQ_API_KEY
from indicators import calculate_indicators, get_multi_timeframe_indicators
# from paper_trading_engine import parse_ai_response

# ------------------------------------------------------------
# 🔹 AI Cooldown Control
# ------------------------------------------------------------
_last_ai_call = {}
_AI_COOLDOWN_SECONDS = 5.0  # Prevent hitting rate limit (increased from 2.5)

def wait_for_symbol_cooldown(symbol: str):
    """Ensure cooldown per symbol before making AI request."""
    global _last_ai_call
    symbol = symbol.lower()
    now = time.time()
    last = _last_ai_call.get(symbol, 0)
    elapsed = now - last

    if elapsed < _AI_COOLDOWN_SECONDS:
        wait = _AI_COOLDOWN_SECONDS - elapsed
        print(f"⏳ Waiting {wait:.1f}s before next AI call for {symbol}...")
        time.sleep(wait)

    _last_ai_call[symbol] = time.time()

# ------------------------------------------------------------
# 🔹 Groq API retry mechanism
# ------------------------------------------------------------
def call_ai_with_retry(payload, headers, retries=10):
    """Call Groq API with backoff on rate limits or network issues."""
    groq_url = "https://api.groq.com/openai/v1/chat/completions"

    for attempt in range(retries):
        try:
            response = requests.post(groq_url, json=payload, headers=headers, timeout=50)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError:
            if response.status_code == 429:
                wait = (attempt + 1) * 20
                print(f"⚠️ AI rate limited (429). Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"❌ HTTP Error {response.status_code}: {response.text[:200]}")
                time.sleep(5)
        except requests.exceptions.RequestException as e:
            print(f"⚠️ AI network error: {e}. Retrying in 5s...")
            time.sleep(5)

    raise Exception("❌ AI request failed after multiple retries.")

# ------------------------------------------------------------
# 🔹 AI Data Cache (for speed)
# ------------------------------------------------------------
_cached_indicators = {}   # {symbol: {"timestamp": ts, "multi_tf": {...}}}
_CACHE_TTL = 30           # seconds

def get_cached_multi_tf(symbol: str):
    """Return cached multi-timeframe indicators if still fresh (with timestamp fix)."""
    import pandas as pd
    global _cached_indicators
    now = time.time()

    # ✅ Use cache if still valid
    if symbol in _cached_indicators:
        ts = _cached_indicators[symbol]["timestamp"]
        if now - ts < _CACHE_TTL:
            print(f"⚡ Using cached indicators for {symbol.upper()}")
            return _cached_indicators[symbol]["multi_tf"]

    # 🆕 Fetch fresh indicators if cache expired or missing
    multi_tf = get_multi_timeframe_indicators(symbol)

    # 🧹 Sanitize timestamps for each timeframe DataFrame
    for tf, data in multi_tf.items():
        try:
            if isinstance(data, pd.DataFrame) and "timestamp" in data.columns:
                # Convert timestamps safely, remove bad rows, drop timezone
                data["timestamp"] = pd.to_datetime(data["timestamp"], errors="coerce", utc=True)
                data = data.dropna(subset=["timestamp"])
                data["timestamp"] = data["timestamp"].dt.tz_localize(None)
                data = data.sort_values("timestamp").reset_index(drop=True)
                multi_tf[tf] = data
        except Exception as e:
            print(f"⚠️ Timestamp fix failed in {tf} for {symbol.upper()}: {e}")

    _cached_indicators[symbol] = {"timestamp": now, "multi_tf": multi_tf}
    return multi_tf


def invalidate_cache(symbol: str):
    """Force refresh cache for a specific symbol."""
    global _cached_indicators
    _cached_indicators.pop(symbol.lower(), None)
    print(f"🧹 Cache invalidated for {symbol.upper()}")

# ------------------------------------------------------------
# 🔹 AI Analysis Core
# ------------------------------------------------------------
def ai_analysis_with_groq(symbol, recent_candles):
    """
    Send OHLCV + indicators to Groq AI using prompt (via REST API)
    and return structured trading decision JSON with trend bias and stronger confidence logic.
    """
    if not isinstance(recent_candles, pd.DataFrame):
        try:
            recent_candles = pd.DataFrame(recent_candles)
        except Exception:
            recent_candles = pd.DataFrame()

    # ✅ Step 1 — Get multi-timeframe indicators (cached for 30s)
    try:
        multi_tf = get_cached_multi_tf(symbol)
        indicators = multi_tf.get("1m", {})  # fallback for base logic
    except Exception as e:
        multi_tf = {}
        indicators = {"rsi": None, "macd": None, "ema_9": None, "ema_21": None}
        print(f"⚠️ Multi-timeframe indicator calc failed for {symbol}: {e}")

    # ✅ Step 2 — Build trend bias summary
    try:
        higher_tf_trends = {
            tf: v.get("trend", "neutral") for tf, v in multi_tf.items() if tf in ["5m", "15m", "1h", "4h"]
        }
        trend_bias = "; ".join([f"{tf}:{trend}" for tf, trend in higher_tf_trends.items()]) or "neutral"
    except Exception:
        trend_bias = "neutral"

    # ✅ Step 3 — Build enhanced AI prompt
    prompt = f"""
    You are an elite crypto analyst. Analyze {symbol.upper()} using the provided data, indicators, and multi-timeframe trends.
    Decide only BUY or SELL — never HOLD. Always output valid JSON and reasoning.

    HIGHER-TIMEFRAME TREND BIAS:
    {trend_bias}

    INDICATORS (1m base):
    {indicators}

    Recent closes: {[float(x) for x in recent_candles['close'].tail(5).values] if len(recent_candles) > 0 else 'N/A'}
    RULES FOR EVALUATION:
    1️⃣ Align with higher-timeframe trend bias.
    2️⃣ RSI:
    - <40 → bullish bias (+1)
    - >60 → bearish bias (-1)
    3️⃣ MACD:
    - Positive → bullish (+1)
    - Negative → bearish (-1)
    4️⃣ EMA & VWAP:
    - Price > EMA_9 and VWAP → bullish (+1)
    - Price < EMA_9 and VWAP → bearish (-1)
    5️⃣ Stochastic:
    - <30 → bullish (+1)
    - >70 → bearish (-1)

    SCORING LOGIC:
    Sum all signals (+1 or -1).

    Confidence logic:
    - |score| == 0 → 55
    - |score| == 1 → 65
    - |score| == 2 → 75
    - |score| == 3 → 85
    - |score| >= 4 → 95
    Then add a small random variation of ±3 for realism.
    Ensure final confidence is between 50–98.

    ATR = {indicators.get('atr', 0.001)}
    Entry = current close price.
    BUY → target = entry + (2.5 × ATR), stop_loss = entry − (1.2 × ATR)
    SELL → target = entry − (2.5 × ATR), stop_loss = entry + (1.2 × ATR)

    OUTPUT REQUIREMENTS:
    Return ONLY valid JSON, nothing else.
    JSON must include:
    {{
    "decision": "BUY" or "SELL",
    "confidence": number (0–100),
    "entry": float,
    "target": float,
    "stop_loss": float,
    "reason": "short English explanation"
    }}
    """
    # ✅ Step 4 — Send to Groq
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "You are a precise crypto trading AI that always responds in valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 400,
    }

    try:
        wait_for_symbol_cooldown(symbol)
        data = call_ai_with_retry(payload, headers)

        if not data or "choices" not in data or not data["choices"]:
            print(f"⚠️ Groq returned empty response for {symbol}")
            return {
                "decision": "HOLD",
                "confidence": 0,
                "entry": 0,
                "target": 0,
                "stop_loss": 0,
                "reason": "Groq returned empty response"
            }

        content = data["choices"][0]["message"]["content"].strip()
        print(f"🧠 Raw Groq output for {symbol}:\n{content}\n")

        # ✅ Try JSON parsing first
        try:
            json_obj = json.loads(content)
            return {
                "decision": str(json_obj.get("decision", "HOLD")).upper(),
                "confidence": float(json_obj.get("confidence", 0)),
                "entry": float(json_obj.get("entry", 0)),
                "target": float(json_obj.get("target", 0)),
                "stop_loss": float(json_obj.get("stop_loss", 0)),
                "reason": str(json_obj.get("reason", "")).strip() or "no_reason"
            }

        except Exception:
            # ⚠️ If still not JSON — fallback gracefully using lazy import
            print(f"⚠️ AI returned non-JSON output for {symbol}. Using fallback parser.")
            try:
                # Lazy import to prevent circular dependency
                from paper_trading_engine import parse_ai_response
                return parse_ai_response(content, symbol)
            except Exception as inner_e:
                print(f"❌ Failed to call parse_ai_response for {symbol}: {inner_e}")
                return {
                    "decision": "HOLD",
                    "confidence": 0,
                    "entry": 0,
                    "target": 0,
                    "stop_loss": 0,
                    "reason": f"parse_ai_response_failed: {inner_e}"
                }

    except Exception as e:
        print(f"❌ Groq AI call failed for {symbol}: {e}")
        return {
            "decision": "HOLD",
            "confidence": 0,
            "entry": 0,
            "target": 0,
            "stop_loss": 0,
            "reason": f"AI request error: {e}"
        }
