# websocket_fetcher.py

import websocket
import json
import threading
import time
from db import engine, candles
from sqlalchemy import insert # type: ignore
import pandas as pd
from datetime import datetime
from typing import Dict, Set
import logging
import requests
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Global state
latest_price: Dict[str, float] = {}
_tracked_symbols: Set[str] = set()
_tracked_lock = threading.Lock()
_ws_instances: Dict[str, websocket.WebSocketApp] = {}

def on_message(ws, message):
    """Handle incoming WebSocket messages."""
    try:
        # Parse the message
        data = json.loads(message)
        symbol = data.get('s', '').lower()
        price = float(data.get('p', 0))
        ts = int(data.get('T', datetime.utcnow().timestamp() * 1000))
        
        if not symbol or not price:
            logger.warning(f"Invalid price data received: symbol={symbol}, price={price}")
            return
            
        # Update latest price with validation and stale check
        old_price = latest_price.get(symbol)
        if old_price == price:
            # Check if price has been the same for too long
            now = time.time()
            last_update = getattr(ws, '_last_price_change', 0)
            if now - last_update > 10:  # If same price for 10+ seconds
                # Force refresh from REST API
                fresh_price = refresh_price(symbol)
                if fresh_price != price:
                    price = fresh_price
                    logger.warning(f"Stale price detected for {symbol}, refreshed: {old_price} -> {price}")
            else:
                logger.debug(f"Same price received for {symbol}: {price}")
        else:
            # Update last price change timestamp
            setattr(ws, '_last_price_change', time.time())
            
        latest_price[symbol] = price
        logger.info(f"Price updated for {symbol}: {old_price} -> {price}")
        
        # Prepare candle data
        candle = {
            'symbol': symbol,
            'interval': '1m',
            'open': price,
            'high': price,
            'low': price,
            'close': price,
            'volume': float(data.get('q', 0)),
            'timestamp': datetime.utcfromtimestamp(ts/1000)
        }
        
        # Insert into database
        try:
            with engine.begin() as conn:
                conn.execute(insert(candles).values(**candle))
            logger.debug(f"Inserted candle for {symbol}: {price}")
        except Exception as e:
            logger.error(f"DB insert error for {symbol}: {e}")

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
    except Exception as e:
        logger.error(f"WebSocket message error: {e}")

def on_error(ws, error):
    """Handle WebSocket errors."""
    logger.error(f"WebSocket Error: {error}")
    
def on_close(ws, close_status_code, close_msg):
    """Handle WebSocket closure."""
    symbol = getattr(ws, '_symbol', 'unknown')
    logger.warning(f"WebSocket closed for {symbol}: {close_status_code} - {close_msg}")
    
def on_open(ws):
    """Handle WebSocket connection opening."""
    symbol = getattr(ws, '_symbol', 'unknown')
    logger.info(f"WebSocket opened for {symbol}")

def start_ws(symbol: str) -> None:
    """Start a WebSocket connection for a symbol with proper error handling."""
    symbol = symbol.lower()
    ws_url = f"wss://stream.binance.com:9443/ws/{symbol}@trade"
    backoff = 1
    max_backoff = 60
    
    def _run_websocket():
        try:
            logger.info(f"Starting WebSocket for {symbol}")
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open
            )
            _ws_instances[symbol] = ws
            # Run in a non-blocking way
            ws.run_forever(reconnect=3)
        except Exception as e:
            logger.error(f"WebSocket error for {symbol}: {e}")
            return False
        return True

    while True:
        if _run_websocket():
            backoff = 1  # Reset backoff on successful connection
        else:
            logger.warning(f"Reconnecting {symbol} in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

def add_symbol(symbol: str) -> None:
    """Add a symbol to track with proper thread management."""
    if not symbol:
        return
        
    sym = symbol.strip().lower()
    with _tracked_lock:
        if sym in _tracked_symbols:
            logger.info(f"Symbol {sym} already being tracked")
            return
        _tracked_symbols.add(sym)
        
    try:
        thread = threading.Thread(
            target=start_ws,
            args=(sym,),
            daemon=True,
            name=f"ws-{sym}"
        )
        thread.start()
        logger.info(f"Started tracking {sym}")
    except Exception as e:
        logger.error(f"Error starting thread for {sym}: {e}")
        with _tracked_lock:
            _tracked_symbols.remove(sym)

def get_tracked_symbols() -> list:
    """Get list of currently tracked symbols thread-safely."""
    with _tracked_lock:
        return list(_tracked_symbols)

def refresh_price(symbol: str) -> float:
    """Force refresh price from Binance REST API if websocket price seems stale."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        price = float(data['price'])
        
        # Update latest_price with fresh data
        symbol_lower = symbol.lower()
        old_price = latest_price.get(symbol_lower)
        latest_price[symbol_lower] = price
        
        logger.info(f"Price refreshed for {symbol}: {old_price} -> {price}")
        return price
    except Exception as e:
        logger.error(f"Failed to refresh price for {symbol}: {e}")
        return latest_price.get(symbol.lower(), 0)

def validate_prices():
    """Periodically validate all tracked prices and refresh if stale."""
    while True:
        try:
            with _tracked_lock:
                symbols = list(_tracked_symbols)
            
            for symbol in symbols:
                try:
                    current_price = latest_price.get(symbol)
                    if current_price:
                        # Force refresh prices every minute as a safety check
                        fresh_price = refresh_price(symbol)
                        if abs(fresh_price - current_price) > 0.00001:  # Accounting for float precision
                            logger.warning(f"Price mismatch for {symbol}: WS={current_price}, REST={fresh_price}")
                            latest_price[symbol] = fresh_price
                except Exception as e:
                    logger.error(f"Price validation failed for {symbol}: {e}")
            
            time.sleep(60)  # Run validation every minute
        except Exception as e:
            logger.error(f"Price validation error: {e}")
            time.sleep(60)

def run_ws(symbols: list = None) -> None:
    """Initialize WebSocket connections for multiple symbols."""
    if not symbols:
        logger.info("No initial symbols to track")
        return
        
    logger.info(f"Starting WebSocket tracking for {len(symbols)} symbols")
    for s in symbols:
        if s and s.strip():
            add_symbol(s)
    
    # Start price validation thread
    validation_thread = threading.Thread(
        target=validate_prices,
        daemon=True,
        name="price-validator"
    )
    validation_thread.start()

