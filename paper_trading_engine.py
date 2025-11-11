#paper_trading_engine.py
import re
from datetime import datetime
from ai_signals import ai_analysis_with_groq
from websocket_fetcher import latest_price, get_tracked_symbols, refresh_price
import data_fetcher
from indicators import calculate_indicators, evaluate_trend
import threading
import trade_history
import ta # type: ignore # if not already imported
import json
from sqlalchemy import insert, select, update # type: ignore
from db import engine, portfolio, ai_decisions
from trade_history import record_closed_trade
from db import init_db
# init_db()
from db import engine, trades
import asyncio
from collections import defaultdict

_last_ai_call_time = defaultdict(lambda: 0)
_RATE_LIMIT_DELAY = 10  # seconds between AI calls per symbol
ai_queue = asyncio.Queue()



from db import engine, portfolio as portfolio_table  # <-- add this at top ideally


# 🧾 Portfolio structure
portfolio = {
    "balance": 10000.0,
    "open_trades": []
}

# 🧠 Store latest AI result for FastAPI endpoint
last_ai_decisions = {}

# manual_trades: symbols for which paper trading has been started by user
manual_trades = set()
manual_lock = threading.Lock()

def start_paper_trading_for(symbol: str):
    """Starts paper trading monitor for a symbol (only one thread per symbol)."""
    sym = symbol.strip().upper()
    with manual_lock:
        if sym in manual_trades:
            print(f"⚠️ Paper trading already active for {sym}")
            return {"ok": False, "error": "already active"}

        manual_trades.add(sym)

    print(f"🚀 Starting paper trading thread for {sym}")
    t = threading.Thread(
        target=run_paper_trading,
        args=(sym,),
        daemon=True,
        name=f"paper-{sym}"
    )
    t.start()
    return {"ok": True, "started": sym}


# 🧩 --- Utility: Parse AI response into a clean dict ---
# 🧩 --- Utility: Parse AI response into a clean dict ---
import re, json

def safe_eval_number(val):
    """Safely evaluate simple math expressions like '445.8 + (2.5 * 0.001)'."""
    try:
        if isinstance(val, (int, float)):
            return float(val)
        expr = str(val)
        # allow only safe characters (digits, ., +, -, *, /, (), spaces)
        if re.match(r'^[\d.+\-*/()\s]+$', expr):
            return round(float(eval(expr)), 6)
    except Exception:
        pass
    return 0.0

import re, json, ast



import re
import json
import ast
from paper_trading_engine import latest_price  # Assuming this is already imported

def parse_ai_response(text, symbol=None):
    """
    Parse AI model response into a clean, usable trading signal.
    Supports:
      ✅ JSON (string or object)
      ✅ Python dict-style responses
      ✅ Math in strings like 445.8 + (2.5 * 0.001)
      ✅ Handles Markdown code blocks like ```json
    """
    symbol = (symbol or "btcusdt").lower()
    entry_price = latest_price.get(symbol, 0) or 0

    # 🧠 STEP 1: Direct dict support (already structured)
    if isinstance(text, dict):
        return {
            "decision": str(text.get("decision", "HOLD")).upper(),
            "confidence": float(text.get("confidence", 0)),
            "entry": float(text.get("entry", entry_price)),
            "target": float(text.get("target", 0)),
            "stop_loss": float(text.get("stop_loss", 0)),
            "reason": str(text.get("reason", "")),
        }

    try:
        raw_str = str(text).strip()

        # 🧩 New: Extract inner content if wrapped in Markdown code block
        json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_str, re.DOTALL | re.IGNORECASE)
        if json_match:
            raw_str = json_match.group(1).strip()

        # 🧩 Fix mathematical expressions like: 445.8 + (2.5 * 0.001)
        def eval_math(m):
            expr = f"{m.group(1)} {m.group(2)} ({m.group(3)})"
            try:
                return str(round(eval(expr), 6))
            except Exception:
                return m.group(0)

        math_fixed = re.sub(r"(\d+(?:\.\d+)?)\s*([+\-*/])\s*\(([^)]+)\)", eval_math, raw_str)

        # 🧩 Fix simple math like: 445.8 + 0.0025
        def eval_simple(m):
            expr = f"{m.group(1)} {m.group(2)} {m.group(3)}"
            try:
                return str(round(eval(expr), 6))
            except Exception:
                return m.group(0)

        math_fixed = re.sub(r"(\d+(?:\.\d+)?)\s*([+\-*/])\s*(\d+(?:\.\d+)?)", eval_simple, math_fixed)

        # 🧩 Normalize quotes for JSON parsing
        math_fixed = math_fixed.replace("'", '"')

        # Try to parse JSON, fallback to literal_eval
        try:
            data = json.loads(math_fixed)
        except json.JSONDecodeError:
            data = ast.literal_eval(math_fixed)

        # ✅ Return clean structured response
        return {
            "decision": str(data.get("decision", "HOLD")).upper(),
            "confidence": float(data.get("confidence", 0)),
            "entry": float(data.get("entry", entry_price)),
            "target": float(data.get("target", 0)),
            "stop_loss": float(data.get("stop_loss", 0)),
            "reason": str(data.get("reason", "")),
        }

    except Exception as e:
        print(f"⚠️ parse_ai_response failed for {symbol.upper()}: {e}")
        print(f"🔍 Raw AI output:\n{text}\n")

    # 🧩 Fallback (safety)
    return {
        "decision": "HOLD",
        "confidence": 0,
        "entry": entry_price,
        "target": entry_price,
        "stop_loss": entry_price,
        "reason": "AI fallback parsed",
    }



import time
from datetime import datetime

async def call_ai_with_rate_limit(symbol, df):
    """Async wrapper that respects rate limit per symbol."""
    global _last_ai_call_time

    now = time.time()
    elapsed = now - _last_ai_call_time[symbol]

    # ⏳ Enforce delay between AI calls for each symbol
    if elapsed < _RATE_LIMIT_DELAY:
        wait_time = _RATE_LIMIT_DELAY - elapsed
        print(f"⏳ Waiting {wait_time:.1f}s before next AI call for {symbol}...")
        await asyncio.sleep(wait_time)

    # Run your AI function safely (moves it to a background thread)
    try:
        result = await asyncio.to_thread(ai_analysis_with_groq, symbol, df)
        _last_ai_call_time[symbol] = time.time()
        return result
    except Exception as e:
        print(f"❌ AI call failed for {symbol}: {e}")
        return {"decision": "HOLD", "confidence": 0, "reason": f"AI error: {e}"}


async def ai_worker():
    """Continuously processes AI requests from the queue."""
    while True:
        symbol, df, callback = await ai_queue.get()
        try:
            ai_response = await call_ai_with_rate_limit(symbol, df)
            if callback:
                callback(ai_response)
        except Exception as e:
            print(f"⚠️ AI worker error for {symbol}: {e}")
        finally:
            ai_queue.task_done()



import asyncio
import time
from datetime import datetime

async def ai_trade(symbol, df):
    """
    Improved AI orchestration for a single symbol.
    - Uses cached multi-timeframe indicators (1m,5m,15m,1h,4h) and calls AI ONCE per minute.
    - Applies sanity checks (confidence threshold, trend alignment).
    - Ensures spot-mode safety: do NOT SELL if no open BUY position.
    - Writes final per-interval/parsing state into last_ai_decisions.
    """
    global last_ai_decisions, portfolio, latest_price

    symbol_u = symbol.upper()
    last_ai_decisions.setdefault(symbol_u, {"summary": {}})

    # ✅ 0) Basic guard
    if df is None or (hasattr(df, "empty") and df.empty):
        print(f"⚠️ ai_trade skipped for {symbol_u}: no 1m data")
        return {"decision": "HOLD", "reason": "no_1m_data"}

    # ✅ 1) Compute indicators (fast local calc)
    try:
        indicators_1m = calculate_indicators(df)
    except Exception as e:
        print(f"⚠️ Indicators calc failed for {symbol_u}: {e}")
        indicators_1m = {}

    # ✅ 2) Multi-timeframe indicator gathering
    multi_tf = {}
    try:
        from data_fetcher import get_recent_candles
        tfs = ["1m", "5m", "15m", "1h", "4h"]
        for tf in tfs:
            try:
                df_tf = get_recent_candles(symbol, interval=tf, limit=1000)
                if df_tf is not None and not getattr(df_tf, "empty", True):
                    multi_tf[tf] = evaluate_trend(df_tf, indicators=calculate_indicators(df_tf))
            except Exception as e:
                multi_tf[tf] = {"trend": "NEUTRAL", "score": 0.0, "reason": f"tf_error:{e}"}
    except Exception as e:
        print(f"⚠️ Multi-timeframe gather failed for {symbol_u}: {e}")
        multi_tf = {}

    # ✅ 3) Cached AI decision
    now = time.time()
    cache_ttl = 60  # seconds
    parsed = {"decision": "HOLD", "confidence": 0, "reason": "initial"}
    last_decision = last_ai_decisions[symbol_u].get("summary", {})

    try:
        ts_str = last_decision.get("timestamp", "1970-01-01 00:00:00")
        last_ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        last_ts = 0

    if (now - last_ts) < cache_ttl and "parsed" in last_decision:
        print(f"⚡ Using cached AI decision for {symbol_u}")
        parsed = last_decision["parsed"]
        ai_response = last_decision.get("raw", "")
    else:
        print(f"🤖 Queueing Groq AI call for {symbol_u} (rate-limited async)...")
        try:
            ai_response = await call_ai_with_rate_limit(symbol, df)
            if ai_response:
                parsed = parse_ai_response(ai_response, symbol)
            else:
                parsed = {"decision": "HOLD", "confidence": 0, "reason": "empty_ai_response"}
        except Exception as e:
            print(f"⚠️ AI call failed for {symbol_u}: {e}")
            parsed = {"decision": "HOLD", "confidence": 0, "reason": f"ai_exception:{e}"}

    # ✅ 4) Safety filters
    decision = str(parsed.get("decision", "HOLD")).upper()
    confidence = float(parsed.get("confidence", 0) or 0)

    open_trade = None
    try:
        import trade_history
        open_trade = trade_history.get_open_trade(symbol)
    except Exception:
        pass

    if decision == "SELL" and not open_trade:
        parsed["reason"] = f"{parsed.get('reason', '')} | ignored_sell_no_position"
        parsed["decision"] = "HOLD"
        print(f"⚠️ Ignoring SELL for {symbol_u} because no open BUY position exists.")

    if decision == "BUY" and confidence < 70:
        parsed["reason"] = f"{parsed.get('reason', '')} | low_conf_{confidence}"
        parsed["decision"] = "HOLD"
        print(f"⚠️ Suppressing low-confidence BUY for {symbol_u} (conf={confidence})")

    # ✅ 5) Record summary (for frontend)
    trend_summary = "; ".join(
        [f"{tf}:{v.get('trend','?')}({v.get('score',0)})" for tf, v in multi_tf.items()]
    ) if multi_tf else "no_multi_tf"

    last_ai_decisions[symbol_u] = {
        "summary": {
            "raw": ai_response if 'ai_response' in locals() else "",
            "parsed": parsed,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trend_summary": trend_summary,
        }
    }

    print(f"✅ Saved AI decision for {symbol_u}: {parsed.get('decision')} ({parsed.get('confidence')}%)")
    return parsed


def start_trade_for_symbol(sym):
    """
    Start a paper trade for a given symbol using the most recent AI decision.
    Includes ATR-based SL/TP if available.
    """
    print(f"\n🔄 Starting trade process for {sym}...")
    sym = sym.upper()  # Normalize symbol case
    
    # Step 1: Subscribe to price feed
    from websocket_fetcher import add_symbol, latest_price
    print(f"📡 Adding {sym} to websocket feed...")
    add_symbol(sym)

    # Step 2: Verify AI decision exists
    print(f"🧠 Checking AI decision for {sym}...")
    ai_data = last_ai_decisions.get(sym, {})
    print(f"Available AI data keys: {list(ai_data.keys())}")
    if not ai_data:
        return {"ok": False, "error": f"No AI decision found for {sym}"}

    # Step 3: Check portfolio and global state
    global portfolio
    if "balance" not in portfolio:
        return {"ok": False, "error": "Portfolio not initialized"}

    try:
        ai_data = last_ai_decisions.get(sym.upper(), {}).get("summary", {})
        if not ai_data or "parsed" not in ai_data:
            return {"ok": False, "error": "No AI decision available for this symbol"}

        parsed = ai_data["parsed"]
        decision = parsed.get("decision", "").upper()
        conf = parsed.get("confidence", 0)
        # ✅ Ensure WebSocket started and wait for a live price update
        from websocket_fetcher import add_symbol, latest_price
        

        live_price = None
        for _ in range(10):  # wait up to ~5 seconds for first price update
            live_price = latest_price.get(sym.lower())
            if live_price:
                break
            print(f"⏳ Waiting for live price update for {sym}...")
            time.sleep(0.5)

        # ✅ ADD THIS DEBUG PRINT HERE
        print(f"💾 latest_price keys currently active: {list(latest_price.keys())}")

        if not live_price:
            try:
                from data_fetcher import get_current_price
                live_price = get_current_price(sym)
            except Exception:
                live_price = None

        if not live_price:
            print(f"❌ Cannot get live price for {sym}")
            return {"ok": False, "error": "Cannot get current live price"}

        print(f"✅ Using LIVE entry price for {sym}: {live_price}")
        entry = float(live_price)

        
        # Get AI's TP/SL suggestions but recalculate based on live price
        ai_target = float(parsed.get("target") or 0)
        ai_stop_loss = float(parsed.get("stop_loss") or 0)

        # --- Fetch fresh candles and ATR for dynamic SL/TP ---
        atr = None
        try:
            df = data_fetcher.get_recent_candles(sym, interval="1m", limit=1000)
            inds = calculate_indicators(df)
            atr = float(inds.get("atr", 0)) if isinstance(inds, dict) else 0
            
            # Calculate volatility percentage from recent price action
            if not df.empty:
                recent_high = df['high'].iloc[-20:].max()
                recent_low = df['low'].iloc[-20:].min()
                price_range_pct = (recent_high - recent_low) / recent_low
            else:
                price_range_pct = 0.01  # default 1% if no data
                
        except Exception as e:
            print(f"⚠️ Error calculating ATR: {e}")
            atr = 0
            price_range_pct = 0.01

        # --- Only open trade if AI suggests BUY or SELL ---
        if decision not in ["BUY", "SELL"]:
            return {"ok": False, "info": f"No trade opened (decision={decision})"}

        # Calculate dynamic TP/SL using ATR-based risk management
        if atr and atr > 0:
            # ATR Multipliers for dynamic risk/reward
            RISK_MULT = 1.5    # How many ATR for stop loss
            REWARD_MULT = 3.0  # How many ATR for take profit (2:1 reward-to-risk)
            
            if decision == "BUY":
                stop_loss = entry - (RISK_MULT * atr)   # Risk 1.5 ATR on downside
                target = entry + (REWARD_MULT * atr)    # Target 3.0 ATR on upside
            else:  # SELL orders
                stop_loss = entry + (RISK_MULT * atr)   # Risk 1.5 ATR on upside
                target = entry - (REWARD_MULT * atr)    # Target 3.0 ATR on downside
        else:
            # Fallback to price range if ATR not available
            volatility = price_range_pct or 0.01  # Use 1% as minimum volatility
            if decision == "BUY":
                stop_loss = entry * (1 - volatility)     # Risk one range width
                target = entry * (1 + (volatility * 2))  # Target 2x range width
            else:  # SELL orders
                stop_loss = entry * (1 + volatility)     # Risk one range width
                target = entry * (1 - (volatility * 2))  # Target 2x range width

        # Override with AI's values if they seem reasonable
        if ai_target > 0 and ai_stop_loss > 0:
            target = ai_target
            stop_loss = ai_stop_loss
            print(f"✅ Using AI's TP/SL levels")

        # ✅ ALWAYS ensure TP/SL are different from entry price
        MIN_RISK_PCT = 0.005   # Minimum 0.5% risk
        MIN_REWARD_PCT = 0.01  # Minimum 1.0% reward
        
        # Get price volatility from recent candles
        volatility_pct = 0.01  # default 1%
        try:
            if not df.empty:
                recent_high = df['high'].iloc[-20:].max()
                recent_low = df['low'].iloc[-20:].min()
                volatility_pct = (recent_high - recent_low) / recent_low
                volatility_pct = max(volatility_pct, MIN_RISK_PCT)  # At least minimum risk
        except Exception as e:
            print(f"⚠️ Volatility calculation failed, using default: {e}")
            
        # Primary calculation with ATR if available
        if atr and atr > 0:
            atr_pct = atr / entry
            risk_pct = max(atr_pct * 1.5, MIN_RISK_PCT)      # At least minimum risk
            reward_pct = max(atr_pct * 3.0, MIN_REWARD_PCT)   # At least minimum reward
            
            if decision == "BUY":
                stop_loss = entry * (1 - risk_pct)
                target = entry * (1 + reward_pct)
            else:  # SELL
                stop_loss = entry * (1 + risk_pct)
                target = entry * (1 - reward_pct)
                
        # Fallback to volatility-based calculation
        else:
            risk_pct = max(volatility_pct * 0.5, MIN_RISK_PCT)    # Half volatility or minimum
            reward_pct = max(volatility_pct * 1.0, MIN_REWARD_PCT) # Full volatility or minimum
            
            if decision == "BUY":
                stop_loss = entry * (1 - risk_pct)
                target = entry * (1 + reward_pct)
            else:  # SELL
                stop_loss = entry * (1 + risk_pct)
                target = entry * (1 - reward_pct)

        # Final validation - force minimum distances
        if abs((target - entry) / entry) < MIN_REWARD_PCT or abs((stop_loss - entry) / entry) < MIN_RISK_PCT:
            print(f"⚠️ Warning: Adjusting TP/SL to minimum distances for {sym}")
            if decision == "BUY":
                stop_loss = entry * (1 - MIN_RISK_PCT)
                target = entry * (1 + MIN_REWARD_PCT)
            else:
                stop_loss = entry * (1 + MIN_RISK_PCT)
                target = entry * (1 - MIN_REWARD_PCT)

        print(f"📊 {sym} Risk Management:")
        print(f"   Entry: {entry:.8f}")
        print(f"   Target: {target:.8f} ({((target-entry)/entry*100):.2f}%)")
        print(f"   Stop Loss: {stop_loss:.8f} ({((stop_loss-entry)/entry*100):.2f}%)")
        print(f"   ATR: {atr if atr else 'N/A'}")
        print(f"   Volatility: {volatility_pct*100:.2f}%")
        
        print(f"✅ Final TP/SL levels: Entry={entry:.4f}, TP={target:.4f} ({((target-entry)/entry*100):.2f}%), SL={stop_loss:.4f} ({((stop_loss-entry)/entry*100):.2f}%)")

        TRADE_VALUE_USD = 100  # fixed trade value per position
        size = TRADE_VALUE_USD / entry  # entry is your coin's price


        # --- ✅ Binance-style fees (0.1% per side) ---
        TRADE_FEE_RATE = 0.001  # 0.1%
        trade_value = entry * size
        entry_fee = trade_value * TRADE_FEE_RATE  # cost at opening

        # --- ✅ Deduct entry cost + fee from portfolio balance ---
        try:
            import paper_trading_engine as pte
            current_balance = float(pte.portfolio.get("balance", 0))
            total_cost = trade_value + entry_fee
            
            if current_balance < total_cost:
                print(f"⚠️ Insufficient balance: Need {total_cost:.2f}, Have {current_balance:.2f}")
                return {"ok": False, "error": "Insufficient balance"}

            new_balance = current_balance - total_cost
            pte.portfolio["balance"] = round(new_balance, 2)
            print(f"💸 Balance updated: {current_balance:.2f} → {new_balance:.2f} (Fee {entry_fee:.4f})")

            # (Optional) sync to DB portfolio table
            from sqlalchemy import update
            from db import portfolio as portfolio_table
            with engine.begin() as conn:
                conn.execute(
                    update(portfolio_table).values(
                        balance=new_balance,
                        updated_at=datetime.utcnow()
                    )
                )
        except Exception as e:
            print(f"[WARN] Balance update failed: {e}")

        # --- Create trade object safely ---
        trade = {
            "symbol": sym,
            "side": decision,
            "entry": entry,
            "target": target,
            "stop_loss": stop_loss,
            "size": size,
            "opened_at": str(datetime.now()),
        }

        # --- Record trade in DB instead of just portfolio ---
        try:
            open_trade = trade_history.get_open_trade(sym)
            if open_trade and open_trade.get("closed_at") is None:
                print(f"⚠️ Trade for {sym} already open, skipping new entry.")
                return {"ok": False, "error": "Trade already open"}

            trade_id = trade_history.open_trade(
                symbol=sym.upper(),
                side=decision,
                entry=entry,
                target=target,
                stop_loss=stop_loss,
                size=size,
            )
            print(f"[TRADE OPENED - DB] {sym.upper()} {decision} @ {entry} | TP={target} SL={stop_loss} (id={trade_id})")
            
            # ✅ START MONITORING THREAD (ADD THIS BLOCK)
            try:
                start_paper_trading_for(sym)
                print(f"✅ Monitoring thread started for {sym.upper()}")
            except Exception as monitor_err:
                print(f"❌ Failed to start monitoring thread: {monitor_err}")
                import traceback
                traceback.print_exc()
            # ✅ END OF NEW BLOCK

        except Exception as db_err:
            print(f"[DB INSERT ERROR] {db_err}")
            if "open_trades" not in portfolio:
                portfolio["open_trades"] = []
            portfolio["open_trades"].append({
                "symbol": sym,
                "side": decision,
                "entry": entry,
                "target": target,
                "stop_loss": stop_loss,
                "size": size,
                "opened_at": datetime.utcnow(),
            })
            print(f"[TRADE OPENED - MEM] {sym} {decision} @ {entry} | TP={target} SL={stop_loss}")

        return {"ok": True, "trade": trade}

    except Exception as e:
        print(f"[TRADE ERROR] {e}")
        return {"ok": False, "error": str(e)}



from db import engine, portfolio as portfolio_table
import trade_history
from datetime import datetime


def close_trade(trade, result):
    """Handles closing logic and Neon DB sync."""
    global portfolio

    try:
        # --- Correct balance update for spot paper trading ---
        exit_price = result.get("exit_price", 0)
        reason = result.get("reason", "")
        size = trade.get("size", 0)
        entry = trade.get("entry", 0)
        TRADE_FEE_RATE = 0.001

        gross_pnl = (exit_price - entry) * size
        entry_fee = entry * size * TRADE_FEE_RATE
        exit_fee = exit_price * size * TRADE_FEE_RATE
        net_pnl = gross_pnl - entry_fee - exit_fee

        result["pnl"] = net_pnl
        result["outcome"] = "win" if gross_pnl > 0 else "loss"  # outcome based on gross, or net as preferred

        # Add back the exit value minus exit fee (entry cost already deducted at open)
        portfolio["balance"] += exit_price * size - exit_fee
        print(f"💸 Balance updated on close: added {exit_price * size - exit_fee:.2f} (net PnL {net_pnl:.2f})")

        # --- Record locally for backup (optional) ---
        try:
            trade_history.record_closed_trade(trade, result)
        except Exception as local_err:
            print(f"[Local Record Error] {local_err}")

        # --- Remove from open trades safely ---
        if trade in portfolio.get("open_trades", []):
            portfolio["open_trades"].remove(trade)

        # --- ✅ Update Neon Postgres ---
        with engine.begin() as conn:
            # Update existing trade
            conn.execute(
                update(trades)
                .where(
                    (trades.c.symbol == trade.get("symbol")) &
                    (trades.c.closed_at.is_(None))
                )
                .values(
                    closed_at=result.get("closed_at", datetime.utcnow()),
                    exit_price=result.get("exit_price"),
                    pnl=result.get("pnl"),
                    outcome=result.get("outcome"),
                    reason=result.get("reason"),
                )
            )

            # Update portfolio table
            conn.execute(
                update(portfolio_table)
                .values(
                    balance=portfolio["balance"],
                    open_trades=portfolio["open_trades"],
                    updated_at=datetime.utcnow(),
                )
            )

        print(f"[✅ TRADE CLOSED] {trade['symbol']} | PnL={result.get('pnl')} | Outcome={result.get('outcome')}")
        return {"ok": True}

    except Exception as e:
        print("[❌ DB Sync Error - close_trade]", e)
        return {"ok": False, "error": str(e)} 




def run_paper_trading(symbol):
    """
    Paper trading monitor - watches open trades and closes on TP/SL/SELL signal.
    Does NOT open new trades - that happens in start_trade_for_symbol().
    """
    global portfolio, latest_price, last_ai_decisions
    import time
    from datetime import datetime
    
    symbol_u = symbol.upper()
    symbol_l = symbol.lower()
    print(f"🚀 Starting paper trading monitor for {symbol_u}")
    monitor_start_time = datetime.now()
    
    # Set up imports
    import trade_history
    from data_fetcher import get_recent_candles
    
    def get_live_price():
        """Helper to get current price from multiple sources with stale check"""
        price = latest_price.get(symbol_l)
        last_trade = trade_history.get_open_trade(symbol_u)
        
        # Check if price seems stale (hasn't changed from entry price)
        if last_trade and price and abs(price - last_trade.get('entry', 0)) < 0.000001:
            print(f"⚠️ Price seems stale for {symbol_u}, refreshing...")
            try:
                fresh_price = refresh_price(symbol_u)
                if fresh_price and fresh_price != price:
                    print(f"💫 Price refreshed: {price} -> {fresh_price}")
                    price = fresh_price
            except Exception as e:
                print(f"⚠️ Refresh failed: {e}")
        
        # If still no price, try one more refresh
        if not price:
            try:
                price = refresh_price(symbol_l)
                print(f"🔄 Forced price refresh: {price}")
            except Exception as e:
                print(f"⚠️ Failed to get current price: {e}")
                price = None
                
        if price:
            print(f"📊 Current price for {symbol_u}: {price}")
            
        return price
    
    def verify_trade_status(trade_info):
        """Verify trade is properly recorded in DB"""
        try:
            from sqlalchemy import select
            from db import engine, trades
            
            with engine.connect() as conn:
                verify = conn.execute(
                    select(trades)
                    .where(trades.c.id == trade_info['id'])
                ).mappings().first()
                
                if verify:
                    status_ok = (
                        verify['status'] == 'OPEN' and
                        verify['closed_at'] is None
                    )
                    if not status_ok:
                        print(f"⚠️ Trade status verification failed:")
                        print(f"   Expected: status=OPEN, closed_at=None")
                        print(f"   Found: status={verify['status']}, closed_at={verify['closed_at']}")
                        return False
                return bool(verify)
        except Exception as e:
            print(f"⚠️ Trade verification error: {e}")
            return False

    print(f"✅ Monitor setup complete for {symbol_u}")
    print(f"⏰ Started at: {monitor_start_time}")
# idhar change kia ha 
    while True:
            try:
                # ===== 1️⃣ GET LIVE PRICE =====
                try:
                    price = get_live_price()
                    if price is None:
                        print(f"⏳ Waiting for price data for {symbol_u}...")
                        time.sleep(2)
                        continue
                except Exception as e:
                    print(f"⚠️ Error getting live price for {symbol_u}: {e}")
                    time.sleep(2)
                    continue

                # ===== 2️⃣ CHECK FOR OPEN TRADE =====
                try:
                    open_pos = trade_history.get_open_trade(symbol_u)
                    if open_pos:
                        verified = verify_trade_status(open_pos)
                        if not verified:
                            print(f"⚠️ Trade verification failed for {symbol_u}")
                            time.sleep(2)
                            continue
                except Exception as e:
                    print(f"⚠️ Error checking open trade for {symbol_u}: {e}")
                    time.sleep(2)
                    continue

                if not open_pos or open_pos.get("closed_at"):
                    time.sleep(2)
                    continue

                # ===== 3️⃣ MONITOR OPEN BUY POSITION =====
                if open_pos.get("side", "").upper() == "BUY":
                    entry = float(open_pos.get("entry", 0))
                    target = float(open_pos.get("target", 0))
                    stop_loss = float(open_pos.get("stop_loss", 0))
                    
                    # Calculate current P&L
                    pnl = (price - entry) / entry * 100
                    
                    # 🔍 MONITORING INFO (every 5 seconds)
                    if time.time() % 5 < 1:
                        print(f"📊 {symbol_u} Monitor:")
                        print(f"   Price: {price:.6f}")
                        print(f"   Entry: {entry:.6f}")
                        print(f"   TP: {target:.6f} ({((target-entry)/entry*100):.2f}%)")
                        print(f"   SL: {stop_loss:.6f} ({((stop_loss-entry)/entry*100):.2f}%)")
                        print(f"   Current P&L: {pnl:.2f}%")
                    
                    # ===== 🎯 CHECK TAKE-PROFIT =====
                    if target > 0 and price >= target:
                        print(f"🎯 TARGET HIT! {symbol_u}:")
                        print(f"   Price: {price:.6f} >= TP: {target:.6f}")
                        print(f"   P&L: {pnl:.2f}%")
                        try:
                            trade_history.close_trade_by_price(
                                symbol=symbol_u,
                                exit_price=price,
                                reason=f"target_hit_pnl_{pnl:.2f}pct"
                            )
                            print(f"✅ Trade closed at target!")
                        except Exception as e:
                            print(f"❌ Failed to close at target: {e}")
                            import traceback
                            traceback.print_exc()
                        time.sleep(2)
                        continue

                    # ===== 🛑 CHECK STOP-LOSS =====
                    if stop_loss > 0 and price <= stop_loss:
                        print(f"🛑 STOP LOSS HIT! {symbol_u}:")
                        print(f"   Price: {price:.6f} <= SL: {stop_loss:.6f}")
                        print(f"   P&L: {pnl:.2f}%")
                        try:
                            trade_history.close_trade_by_price(
                                symbol=symbol_u,
                                exit_price=price,
                                reason=f"stop_loss_hit_pnl_{pnl:.2f}pct"
                            )
                            print(f"✅ Trade closed at stop loss!")
                        except Exception as e:
                            print(f"❌ Failed to close at SL: {e}")
                            import traceback
                            traceback.print_exc()
                        time.sleep(2)
                        continue

                    # ===== 📉 CHECK AI SELL SIGNAL =====
                    try:
                        ai_decision = last_ai_decisions.get(symbol_u, {}).get("summary", {}).get("parsed", {})
                        decision = ai_decision.get("decision", "HOLD").upper()
                        confidence = float(ai_decision.get("confidence", 0))
                        
                        if decision == "SELL" and confidence >= 75:
                            print(f"📉 AI SELL SIGNAL! {symbol_u}:")
                            print(f"   Confidence: {confidence}%")
                            print(f"   Current P&L: {pnl:.2f}%")
                            try:
                                trade_history.close_trade_by_price(
                                    symbol=symbol_u,
                                    exit_price=price,
                                    reason=f"ai_sell_signal_conf_{confidence}_pnl_{pnl:.2f}pct"
                                )
                                print(f"✅ Trade closed on AI sell signal!")
                            except Exception as e:
                                print(f"❌ Failed to close on AI sell: {e}")
                                import traceback
                                traceback.print_exc()
                            time.sleep(2)
                            continue
                    except Exception as e:
                        if time.time() % 30 < 1:  # Log AI check errors every 30s
                            print(f"⚠️ AI check failed: {e}")
                    # ⏱️ Loop delay (dynamic based on market volatility)
                    time.sleep(1)
                
            except Exception as outer_e:
                print(f"⚠️ Outer loop error for {symbol_u}: {outer_e}")
                import traceback
                traceback.print_exc()
                print(f"🔄 Restarting monitor loop for {symbol_u} in 5 seconds...")
                time.sleep(5)



# --- Singleton accessor ---
class PaperTradingEngineSingleton:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            import paper_trading_engine as pte

            # ✅ Ensure portfolio is initialized once
            if not hasattr(pte, "portfolio") or not pte.portfolio:
                pte.portfolio = {
                    "balance": 10000.0,
                    "open_trades": []
                }
                print("✅ Initialized portfolio with $10,000 balance")

            cls._instance = pte

        return cls._instance