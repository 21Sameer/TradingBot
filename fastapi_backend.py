#fastapi_backend.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import websocket_fetcher  # ✅ Import the actual module, not just the variable
import requests
from typing import List

# Import the trading engine module so we can read live mutable state
# import paper_trading_engine as pte
import data_fetcher
from fastapi import Body
import re
import websocket_fetcher
from paper_trading_engine import last_ai_decisions, ai_trade, portfolio, latest_price
from paper_trading_engine import PaperTradingEngineSingleton
pte = PaperTradingEngineSingleton.get_instance()
# from shared_state import pte, last_ai_decisions, portfolio

# Import the Binance utils for symbol validation
from binance_utils import filter_valid_binance_symbols
import asyncio


async def process_live_signals(symbols: List[str]):
    """
    Process live trading signals for multiple symbols:
    1. Verify valid Binance pairs
    2. Fetch OHLCV data
    3. Calculate indicators
    4. Get AI predictions
    5. Start paper trades for BUY signals
    """
    try:
        # Filter for valid Binance symbols
        valid_symbols = filter_valid_binance_symbols(symbols)
        if not valid_symbols:
            return {"ok": False, "error": "No valid Binance symbols found"}

        results = []
        for symbol in valid_symbols:
            try:
                # Fetch and store OHLCV data
                df = data_fetcher.get_recent_candles(symbol, interval="1m", limit=1000)
                if df is None or df.empty:
                    print(f"⚠️ No data available for {symbol}")
                    continue

                # Run AI analysis
                parsed = await pte.ai_trade(symbol, df)
                
                # Handle different response formats
                if isinstance(parsed, str):
                    result = {
                        "symbol": symbol,
                        "decision": "HOLD",
                        "confidence": 0,
                        "reason": parsed
                    }
                elif isinstance(parsed, dict):
                    # Try to get decision info from different possible structures
                    if "summary" in parsed and isinstance(parsed["summary"], dict):
                        parsed_data = parsed["summary"].get("parsed", {})
                    else:
                        parsed_data = parsed
                    
                    # Extract decision and confidence safely
                    decision = str(parsed_data.get("decision", "HOLD")).upper()
                    try:
                        confidence = float(parsed_data.get("confidence", 0) or 0)
                    except (ValueError, TypeError):
                        confidence = 0
                    
                    result = {
                        "symbol": symbol,
                        "decision": decision,
                        "confidence": confidence,
                        "reason": str(parsed_data.get("reason", "No reason provided"))
                    }
                else:
                    result = {
                        "symbol": symbol,
                        "decision": "HOLD",
                        "confidence": 0,
                        "reason": "Invalid AI response format"
                    }

                # If AI suggests BUY with good confidence, start paper trade
                if result["decision"] == "BUY" and result["confidence"] >= 70:
                    # Check if trade already exists
                    import trade_history
                    print(f"🔍 Checking for existing trade for {symbol}...")
                    existing_trade = trade_history.get_open_trade(symbol)
                    
                    if not existing_trade:
                        print(f"✅ No existing trade found for {symbol}, attempting to start trade...")
                        trade_result = pte.start_trade_for_symbol(symbol)
                        print(f"🎯 Trade start result for {symbol}: {trade_result}")
                        
                        if isinstance(trade_result, dict):
                            result["trade_started"] = trade_result.get("ok", False)
                            if not trade_result.get("ok"):
                                result["reason"] = trade_result.get("error", "Unknown error starting trade")
                        else:
                            result["trade_started"] = False
                            result["reason"] = "Invalid trade result format"
                            
                        if result.get("trade_started"):
                            print(f"🚀 Successfully auto-started paper trade for {symbol}")
                        else:
                            print(f"❌ Failed to start paper trade for {symbol}: {result.get('reason')}")
                    else:
                        result["trade_started"] = False
                        result["reason"] = f"Trade already open for {symbol}"
                        print(f"⚠️ {result['reason']}")

                results.append(result)
                
            except Exception as e:
                print(f"⚠️ Error processing {symbol}: {e}")
                results.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        return {
            "ok": True,
            "results": results,
            "processed_count": len(results)
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


app = FastAPI()

# ✅ Allow CORS for your HTML frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5501",
        "http://localhost:5501"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Use websocket_fetcher.latest_price directly
@app.get("/prices")
def get_prices():
    return websocket_fetcher.latest_price


# Keep this: Returns JSON dict (what frontend likely expects)
@app.get("/ai_decision")
def get_all_ai_decisions():
    """Return all AI decisions in clean format (for frontend table)."""
    decisions = getattr(pte, "last_ai_decisions", None)
    if not decisions:
        return {"error": "No AI decisions found yet."}

    result = {}

    for sym, sym_data in decisions.items():
        try:
            # Handle string responses
            if isinstance(sym_data, str):
                result[sym] = {
                    "decision": "HOLD",
                    "confidence": 0,
                    "entry": 0,
                    "target": 0,
                    "stop_loss": 0,
                    "reason": sym_data,  # Use the string as reason
                    "timestamp": "N/A"
                }
                continue

            # Handle dict responses
            if not isinstance(sym_data, dict):
                continue

            # Try to get summary, might be nested or direct
            summary = sym_data.get("summary", sym_data)
            if isinstance(summary, str):
                summary = {"parsed": {"reason": summary}}
            elif not isinstance(summary, dict):
                continue

            # Get parsed data, might be nested or direct
            parsed = summary.get("parsed", summary)
            if not isinstance(parsed, dict):
                parsed = {"reason": str(parsed)}

            result[sym] = {
                "decision": str(parsed.get("decision", "HOLD")).upper(),
                "confidence": float(parsed.get("confidence", 0) or 0),
                "entry": float(parsed.get("entry", 0) or 0),
                "target": float(parsed.get("target", 0) or 0),
                "stop_loss": float(parsed.get("stop_loss", 0) or 0),
                "reason": str(parsed.get("reason", "No reason provided.")),
                "timestamp": str(summary.get("timestamp", "N/A"))
            }
        except Exception as e:
            print(f"⚠️ Error processing AI decision for {sym}: {e}")
            # Provide a safe fallback
            result[sym] = {
                "decision": "HOLD",
                "confidence": 0,
                "entry": 0,
                "target": 0,
                "stop_loss": 0,
                "reason": f"Error processing AI data: {str(e)}",
                "timestamp": "N/A"
            }

    if not result:
        return {"error": "AI decisions structure found, but empty."}

    return result


@app.get("/trades")
def get_trades():
    """
    Return open trades only from the database.
    """
    from sqlalchemy import select
    from db import engine, trades

    out = []
    try:
        with engine.connect() as conn:
            # ✅ Fetch only trades with status='OPEN' AND closed_at IS NULL
            stmt = (
                select(trades)
                .where(
                    (trades.c.status == "OPEN") &
                    (trades.c.closed_at.is_(None))
                )
                .order_by(trades.c.id.desc())
            )
            rows = conn.execute(stmt).mappings().all()

        print(f"🔍 DEBUG /trades: Found {len(rows)} open trades")  # ✅ Debug log

        seen_symbols = set()

        for r in rows:
            symbol = r.get("symbol", "").upper()
            
            # Skip duplicates
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)

            entry = float(r.get("entry") or 0)
            size = float(r.get("size") or 0)
            amount = entry * size * 10 if entry and size else 0

            # ✅ Debug each trade
            print(f"   Trade: {symbol} | Status from DB: {r.get('status')} | closed_at: {r.get('closed_at')}")

            out.append({
                "symbol": symbol,
                "action": r.get("side", "BUY").upper(),
                "entry": entry,
                "tp": float(r.get("target") or 0),
                "sl": float(r.get("stop_loss") or 0),
                "amount": amount,
                "price": None,
                "pnl": None,
                "pnl_pct": None,
                "status": "OPEN",  # ✅ HARDCODE "OPEN" since we're only fetching open trades
                "opened_at": str(r.get("opened_at") or ""),
                "closed_at": None  # ✅ Always None for open trades
            })

    except Exception as e:
        print(f"⚠️ Error in /trades endpoint: {e}")
        import traceback
        traceback.print_exc()

    print(f"✅ Returning {len(out)} trades to frontend")  # ✅ Debug log
    return out




# old one ab naya claude wala dal raha ho
# @app.get("/trades")
# def get_trades():
#     """
#     Return open trades only from the database (no in-memory duplicates).
#     """
#     from sqlalchemy import select
#     from db import engine, trades

#     out = []
#     try:
#         with engine.connect() as conn:
#             # Fetch trades that are still open (not closed)
#             stmt = select(trades).where(trades.c.closed_at.is_(None)).order_by(trades.c.id.desc())
#             rows = conn.execute(stmt).mappings().all()

#         seen_symbols = set()  # prevent duplicate entries for same symbol

#         for r in rows:
#             symbol = r.get("symbol", "").upper()
#             if symbol in seen_symbols:
#                 continue  # skip duplicates
#             seen_symbols.add(symbol)

#             entry = float(r.get("entry") or 0)
#             size = float(r.get("size") or 0)
#             amount = entry * size * 10 if entry and size else 0

            
#             # ✅ Only small addition below
#             # ✅ FIX: Check closed_at properly (it's a datetime object or None)
#             closed_at_value = r.get("closed_at")
#             trade_status = "OPEN" if closed_at_value is None else "CLOSED"
            
#             # ✅ Also check the status column directly
#             db_status = r.get("status", "").upper()
#             if db_status:
#                 trade_status = db_status  # Trust the database status field

#             out.append({
#                 "symbol": symbol,
#                 "action": r.get("side", "BUY").upper(),
#                 "entry": entry,
#                 "tp": float(r.get("target") or 0),
#                 "sl": float(r.get("stop_loss") or 0),
#                 "amount": amount,
#                 "price": None,
#                 "pnl": None,
#                 "pnl_pct": None,
#                 "status": trade_status,
#                 "opened_at": str(r.get("opened_at") or ""),
#                 "closed_at": str(closed_at_value) if closed_at_value else None
#             })

#     except Exception as e:
#         print(f"⚠️ Error in /trades endpoint: {e}")

#     return out



# @app.get("/trades")
# def get_trades():
#     raw = pte.portfolio.get("open_trades", []) or []
#     out = []
#     for t in raw:
#         try:
#             symbol = (t.get("symbol") or t.get("symbol", "")).upper()
#             side = t.get("side") or t.get("action") or t.get("side")
#             entry = float(t.get("entry") or 0)
#             tp = float(t.get("target") or t.get("tp") or 0)
#             sl = float(t.get("stop_loss") or t.get("sl") or 0)
#             size = float(t.get("size") or 0)
#             amount = entry * size * 10 if entry and size else 0
#             out.append({
#                 "symbol": symbol,
#                 "action": (side or "").upper(),
#                 "entry": entry or None,
#                 "tp": tp or None,
#                 "sl": sl or None,
#                 "amount": amount or None,
#                 "open": True,
#             })
#         except Exception:
#             out.append(t)
#     return out


@app.get("/balance")
def get_balance():
    return {"balance": pte.portfolio.get("balance",0)}

# Optionally keep this for human-readable ALL decisions (different route)
@app.get("/ai_decisions")
def get_ai_decisions():
    """Return AI decisions in plain, human-readable English sentences."""
    decisions = getattr(pte, "last_ai_decisions", {}) or {}
    if not decisions:
        return "No AI decisions available yet."

    lines = ["AI Market Decisions:\n"]

    for symbol, sym_data in decisions.items():
        try:
            # Handle string responses
            if isinstance(sym_data, str):
                lines.append(f"• {symbol}: {sym_data}")
                continue

            # Handle dict responses
            if not isinstance(sym_data, dict):
                lines.append(f"• {symbol}: Invalid data type")
                continue

            # Try to get summary, might be nested or direct
            summary = sym_data.get("summary", sym_data)
            if isinstance(summary, str):
                lines.append(f"• {symbol}: {summary}")
                continue

            # Get parsed data, might be nested or direct
            parsed = summary.get("parsed", summary) if isinstance(summary, dict) else summary
            if not isinstance(parsed, dict):
                lines.append(f"• {symbol}: {str(parsed)}")
                continue

            # Extract decision info
            decision = str(parsed.get("decision", "N/A")).upper()
            reason = str(parsed.get("reason", "No reason provided."))
            confidence = parsed.get("confidence")
            timestamp = summary.get("timestamp", "N/A") if isinstance(summary, dict) else "N/A"

            line = f"• {symbol}: {decision} — {reason}"
            if confidence is not None:
                try:
                    conf_val = float(confidence)
                    if conf_val > 0:
                        line += f" (Confidence: {conf_val:.1f}%)"
                except (ValueError, TypeError):
                    pass

            if timestamp and timestamp != "N/A":
                line += f" [Updated: {timestamp}]"

            lines.append(line)
        except Exception as e:
            lines.append(f"• {symbol}: Unable to parse AI result ({e}).")

    return "\n".join(lines)

    return "\n".join(lines)





# Keep this for single-symbol (but return JSON instead of string for consistency)
@app.get("/ai_decision/{symbol}")
def get_ai_for_symbol(symbol: str):
    sym = symbol.strip().upper()
    decisions = getattr(pte, "last_ai_decisions", {}) or {}

    if sym not in decisions:
        return {"error": f"No AI decision found for {sym}."}

    sym_data = decisions.get(sym, {})
    try:
        # Handle string responses
        if isinstance(sym_data, str):
            return {
                "decision": "HOLD",
                "confidence": 0,
                "reason": sym_data,
                "timestamp": "N/A"
            }

        # Handle dict responses
        if not isinstance(sym_data, dict):
            return {"error": f"{sym}: Invalid data type"}

        # Try to get summary, might be nested or direct
        summary = sym_data.get("summary", sym_data)
        if isinstance(summary, str):
            return {
                "decision": "HOLD",
                "confidence": 0,
                "reason": summary,
                "timestamp": "N/A"
            }

        # Get parsed data, might be nested or direct
        parsed = summary.get("parsed", summary) if isinstance(summary, dict) else summary
        if not isinstance(parsed, dict):
            return {
                "decision": "HOLD",
                "confidence": 0,
                "reason": str(parsed),
                "timestamp": "N/A"
            }

        # Return normalized response
        return {
            "decision": str(parsed.get("decision", "N/A")).upper(),
            "confidence": float(parsed.get("confidence", 0) or 0),
            "reason": str(parsed.get("reason", "No reason provided.")),
            "timestamp": str(summary.get("timestamp", "N/A") if isinstance(summary, dict) else "N/A")
        }
    except Exception as e:
        return {"error": f"{sym}: Unable to parse AI result ({e})."}
# # ✅ Human-readable AI decisions endpoint





@app.post("/subscribe_symbol")
def subscribe_symbol(payload: dict = Body(...)):
    sym = str(payload.get("symbol", "")).strip()
    if not sym:
        return {"ok": False, "error": "symbol required"}
    websocket_fetcher.add_symbol(sym)
    return {"ok": True, "tracked": websocket_fetcher.get_tracked_symbols()}




@app.post("/start_paper_trade")
async def start_paper_trade(payload: dict = Body(...)):
    """
    When user clicks 'Analyze':
    - Fetches recent data
    - Runs AI model (ai_trade)
    - Returns AI decision but does NOT execute trade automatically
    """
    sym = str(payload.get("symbol", "")).strip()
    if not sym:
        return {"ok": False, "error": "symbol required"}

    websocket_fetcher.add_symbol(sym)

    try:
        # ✅ Step 1 — Fetch latest 1m candles
        from data_fetcher import get_recent_candles
        df = get_recent_candles(sym, interval="1m", limit=1000)
        if df is None or getattr(df, "empty", True):
            return {"ok": False, "error": "No candle data available"}

        # ✅ Step 2 — Run AI model asynchronously
        parsed = await pte.ai_trade(sym, df)

        # ✅ Step 3 — Start paper-trading monitor for this symbol (no trade yet)
        try:
            pte.start_paper_trading_for(sym)
        except Exception as sub_e:
            print(f"⚠️ start_paper_trading_for failed for {sym}: {sub_e}")

        # ✅ Step 4 — Return AI decision cleanly
        return {
            "ok": True,
            "symbol": sym.upper(),
            "decision": parsed,
            "message": "AI analysis complete. Ready for manual trade start."
        }

    except Exception as e:
        return {"ok": False, "error": f"start_paper_trade failed: {e}"}

@app.get("/live_signals")
async def fetch_live_signals():
    """
    Fetch and process signals from external API:
    1. Get signals from external API
    2. Filter for valid Binance pairs
    3. Process each symbol for potential trades
    """
    try:
        # Fetch signals from external API with increased timeout and retries
        print("🔄 Fetching signals from external API...")
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util import Retry
            
            # Create a session with retry strategy
            session = requests.Session()
            retry_strategy = Retry(
                total=3,  # number of retries
                backoff_factor=1,  # wait 1, 2, 4 seconds between retries
                status_forcelist=[500, 502, 503, 504]  # retry on these status codes
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            
            # Make the request with increased timeout
            response = session.get(
                "http://51.75.73.27:5000/api/live_signals", 
                timeout=(15, 30)  # (connect timeout, read timeout)
            )
            response.raise_for_status()
            response_data = response.json()
        except requests.exceptions.Timeout:
            return {
                "ok": False, 
                "error": "API request timed out. The server might be under heavy load or temporarily unavailable. Please try again in a few minutes."
            }
        except requests.exceptions.ConnectionError:
            return {
                "ok": False,
                "error": "Could not connect to the signals API. The server might be down or unreachable. Please try again later."
            }
        
        # Log the raw response for debugging
        print(f"📥 Raw API Response: {response_data}")
        
        if not response_data:
            return {"ok": False, "error": "No signals received from API"}
        
        # Extract signals from the response (either from 'data' or 'signals' array)
        signals_data = (response_data.get('data') or response_data.get('signals', [])) if isinstance(response_data, dict) else []
        print(f"Found {len(signals_data)} signals in response")
            
        # Extract symbols and their signals
        symbols = []
        signals_info = {}  # Store additional signal info for each symbol
        
        for signal in signals_data:
            if isinstance(signal, dict):
                symbol = signal.get('symbol', '').upper()
                if symbol and symbol.endswith('USDT'):
                    signal_type = signal.get('signal', '').upper()
                    try:
                        confidence = float(signal.get('confidence', 0))
                        price = float(signal.get('price', 0))
                        weighted_score = float(signal.get('weighted_score', 0))
                    except (ValueError, TypeError):
                        confidence = 0
                        price = 0
                        weighted_score = 0
                    
                    # Only process BUY signals
                    if signal_type == 'BUY':
                        print(f"Found BUY signal: {symbol} with confidence: {confidence*100:.1f}%")
                        symbols.append(symbol)
                        signals_info[symbol] = {
                            'signal': signal_type,
                            'confidence': confidence,
                            'price': price,
                            'timestamp': signal.get('timestamp', ''),
                            'strategy': signal.get('strategy', ''),
                            'status': signal.get('status', ''),
                            'weighted_score': weighted_score
                        }
                    else:
                        print(f"Skipping {signal_type} signal for {symbol} (only processing BUY signals)")
                        
        # Remove duplicates while preserving order
        symbols = list(dict.fromkeys(symbols))
        
        # Remove duplicates while preserving order
        symbols = list(dict.fromkeys([s for s in symbols if s]))
        
        print(f"📋 Extracted {len(symbols)} unique symbols with signals")
        print(f"Signal details: {signals_info}")
        
        if not symbols:
            return {
                "ok": False, 
                "error": "No valid trading pairs found in signals",
                "raw_response": response_data
            }
            
        # Process the signals
        return await process_live_signals(symbols)
        
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Failed to fetch signals: {str(e)}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post('/analyze_all')
async def analyze_all_symbols(payload: dict = Body(...)):
    """
    Analyze multiple symbols concurrently.
    For each symbol:
      - Fetches recent candles
      - Runs AI (via ai_trade)
      - Automatically starts trade if AI says BUY
      - Skips if trade is already open
    """
    import asyncio
    import random
    from data_fetcher import get_recent_candles
    import trade_history

    try:
        symbols = payload.get("symbols", [])
        if not isinstance(symbols, list) or not symbols:
            return {"ok": False, "error": "symbols list required"}

        results = {}

        # 🔹 Single-symbol worker
        async def analyze_one(sym):
            sym = sym.upper()
            print(f"🔹 Starting AI for {sym}")

            df = get_recent_candles(sym, interval="1m", limit=1000)
            if df is None or getattr(df, "empty", True):
                print(f"⚠️ No data for {sym}")
                return {"decision": "HOLD", "reason": "no_data"}

            parsed = await pte.ai_trade(sym, df)
            decision = parsed.get("decision", "").upper()

            # ✅ Auto-trade logic only for BUY signals
            if decision == "BUY":
                open_trade = trade_history.get_open_trade(sym)
                if open_trade and open_trade.get("closed_at") is None:
                    print(f"⚠️ Skipping {sym} — trade already open.")
                    parsed["trade_started"] = False
                    parsed["reason"] = "already_open"
                else:
                    trade_result = pte.start_trade_for_symbol(sym)
                    parsed["trade_started"] = trade_result.get("ok", False)
                    print(f"🚀 Auto trade started for {sym}")
            else:
                parsed["trade_started"] = False

            return parsed

        # 🔹 Limit concurrency to 5 and add staggered delays
        sem = asyncio.Semaphore(5)

        async def limited_analyze(sym):
            async with sem:
                await asyncio.sleep(random.uniform(0.5, 1.5))  # small delay to avoid overload
                return sym.upper(), await analyze_one(sym)

        # 🔹 Run all in parallel
        results_list = await asyncio.gather(*[limited_analyze(sym) for sym in symbols], return_exceptions=True)

        # 🔹 Collect results
        for res in results_list:
            if isinstance(res, Exception):
                print(f"⚠️ Exception in analyze_all: {res}")
                continue
            sym, parsed = res
            results[sym] = parsed

        print(f"✅ Finished analyzing {len(symbols)} symbols concurrently.")
        return {"ok": True, "results": results}

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return {"ok": False, "error": str(e)}









@app.post("/start_trade")
def start_trade(payload: dict = Body(...)):
    """
    When user clicks 'Start Trade':
    - Uses last AI decision (BUY/SELL)
    - Starts paper trade if AI recommended BUY
    """
    sym = str(payload.get("symbol", "")).strip()
    if not sym:
        return {"ok": False, "error": "symbol required"}

    websocket_fetcher.add_symbol(sym)

    # ✅ Fetch the last AI decision stored in memory
    ai_decision = pte.last_ai_decisions.get(sym.upper(), {}).get("summary", {}).get("parsed", {})
    if not ai_decision:
        return {"ok": False, "error": "No AI decision found for this symbol. Run analysis first."}

    decision = ai_decision.get("decision", "HOLD").upper()

    if decision == "BUY":
        result = pte.start_trade_for_symbol(sym)
        if isinstance(result, dict) and result.get("ok"):
            try:
                pte.start_paper_trading_for(sym)
            except Exception as e:
                print(f"⚠️ start_paper_trading_for failed: {e}")
            return {"ok": True, "trade_started": True, "ai_decision": ai_decision}
        else:
            return {"ok": False, "error": "Failed to start trade"}

    return {"ok": False, "error": f"Trade not started because AI decision = {decision}"}







# -------------------------------------------------------------------
# ✅ Step 3 — Periodic AI Decision Updater
# -------------------------------------------------------------------
import asyncio
import threading
import time
from datetime import datetime
import trade_history


class PeriodicUpdater:
    def __init__(self):
        self._running = True
        self._thread = None
        self._lock = threading.Lock()

    async def _async_update_symbol(self, sym):
        """Handle updates for a single symbol"""
        try:
            # Update timeframes
            for tf in ["1m", "5m", "15m", "1h", "4h"]:
                try:
                    data_fetcher.update_candles_for_symbol(sym, tf)
                except Exception as e:
                    print(f"⚠️ Candle update failed for {sym} ({tf}): {e}")

            # Load recent candles
            df = data_fetcher.get_recent_candles(sym, interval="1m", limit=1000)
            if df is None or df.empty:
                print(f"⚠️ Skipping {sym} — no 1m data available.")
                return

            # Run AI analysis
            parsed = await pte.ai_trade(sym, df)
            print(f"[AI UPDATE] ✅ {sym} analyzed: {parsed.get('decision', 'HOLD')} at {datetime.now().strftime('%H:%M:%S')}")

            # Auto-start trades for BUY signals
            if parsed.get('decision') == 'BUY' and float(parsed.get('confidence', 0)) >= 70:
                print(f"🎯 Found BUY signal for {sym}, checking if we can open trade...")
                
                # Check for existing trade
                import trade_history
                existing = trade_history.get_open_trade(sym)
                if not existing:
                    print(f"✅ No existing trade found for {sym}, starting new trade...")
                    trade_result = pte.start_trade_for_symbol(sym)
                    if isinstance(trade_result, dict) and trade_result.get('ok'):
                        print(f"🚀 Successfully opened trade for {sym}")
                    else:
                        print(f"❌ Failed to open trade for {sym}: {trade_result}")
                else:
                    print(f"⚠️ Trade already exists for {sym}")

        except Exception as e:
            print(f"⚠️ Update failed for {sym}: {e}")

    async def _update_loop(self):
        """Main update loop"""
        while self._running:
            try:
                symbols = websocket_fetcher.get_tracked_symbols()
                print(f"\n🔄 Starting periodic AI update for {len(symbols)} symbols at {datetime.now().strftime('%H:%M:%S')}")

                # Process symbols concurrently with rate limiting
                sem = asyncio.Semaphore(5)  # limit concurrent updates
                async def process_with_limit(sym):
                    async with sem:
                        await self._async_update_symbol(sym)

                await asyncio.gather(*[process_with_limit(sym) for sym in symbols])

            except Exception as e:
                print(f"[Update Error] {e}")

            # Sleep between updates
            try:
                await asyncio.sleep(60 * 5)  # 5 minutes
            except asyncio.CancelledError:
                break

    def start(self):
        """Start the periodic updater in a background thread"""
        async def run_forever():
            while self._running:
                try:
                    await self._update_loop()
                except Exception as e:
                    print(f"[Update Loop Error] {e}")
                    await asyncio.sleep(5)

        def thread_target():
            asyncio.run(run_forever())

        with self._lock:
            if not self._thread:
                self._running = True
                self._thread = threading.Thread(target=thread_target, daemon=True)
                self._thread.start()

    def stop(self):
        """Stop the periodic updater"""
        with self._lock:
            self._running = False
            if self._thread:
                self._thread.join(timeout=1)
                self._thread = None

# Create the periodic updater but DO NOT start it on import.
# Starting background threads at import-time causes interpreter shutdown
# races (prints from daemon threads while shutdown is finalizing).
# The application entrypoint (`app.py`) will start the updater explicitly.
updater = PeriodicUpdater()

# If this module is executed directly, start the updater for local testing.
if __name__ == "__main__":
    updater.start()
# -------------------------------------------------------------------
# End of periodic AI decision updater


@app.get('/trade_history')
def get_trade_history(limit: int = None):
    try:
        items = trade_history.get_history(limit=limit)
        return {'ok': True, 'count': len(items), 'history': items}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
