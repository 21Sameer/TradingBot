#trade_history.py

from datetime import datetime
from sqlalchemy import insert, select, update  # type: ignore
from db import engine, trades


# ✅ Record a newly closed trade into DB
def record_closed_trade(trade: dict, result: dict):
    item = {
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "entry": trade.get("entry"),
        "target": trade.get("target"),
        "stop_loss": trade.get("stop_loss"),
        "size": trade.get("size"),
        "opened_at": trade.get("opened_at"),
        "closed_at": result.get("closed_at") or datetime.utcnow(),
        "exit_price": result.get("exit_price"),
        "pnl": result.get("pnl"),
        "outcome": result.get("outcome"),
        "reason": result.get("reason"),
    }

    with engine.begin() as conn:
        conn.execute(insert(trades).values(**item))
    return item


# ✅ Fetch all trade history (for /trade_history endpoint)
def get_history(limit: int = None):
    with engine.connect() as conn:
        stmt = select(trades).order_by(trades.c.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        rows = conn.execute(stmt).mappings().all()
    return [dict(row) for row in rows]


# ✅ Get most recent open trade for a symbol (still not closed)
def get_open_trade(symbol: str):
    """Check if there's an open trade for the given symbol."""
    from sqlalchemy import select
    symbol = symbol.upper()  # Normalize symbol case
    
    print(f"🔍 Checking for open trade: {symbol}")
    try:
        with engine.connect() as conn:
            stmt = (
                select(trades)
                .where(
                    (trades.c.symbol == symbol)
                    & (trades.c.closed_at.is_(None))
                    & (trades.c.status != "CLOSED")  # Extra safety check
                )
                .order_by(trades.c.id.desc())
            )
            row = conn.execute(stmt).mappings().first()
            
            if row:
                print(f"💡 Found open trade for {symbol}: ID={row['id']}, Status={row['status']}, Opened={row['opened_at']}")
            else:
                print(f"✅ No open trade found for {symbol}")
                
            return dict(row) if row else None
            
    except Exception as e:
        print(f"⚠️ Error checking open trade for {symbol}: {e}")
        return None


# ✅ Create new open trade record (used when a BUY happens)
def open_trade(symbol, side, entry, target, stop_loss, size):
    """
    Open a new paper trade record and deduct initial trading fee (like Binance 0.1%).
    """
    from datetime import datetime
    BUY_FEE_RATE = 0.001  # 0.1% per side

    opened_at = datetime.utcnow()
    symbol = symbol.upper()  # Normalize symbol case

    # First check if trade already exists
    existing = get_open_trade(symbol)
    if existing and existing.get("closed_at") is None:
        print(f"⚠️ Trade already exists for {symbol} and is still open")
        return None

    try:
        # --- Calculate trade value and entry fee ---
        trade_value = entry * size
        entry_fee = trade_value * BUY_FEE_RATE  # fee charged on trade open

        # --- Deduct from portfolio balance (simulate Binance behavior) ---
        import paper_trading_engine as pte
        current_balance = float(pte.portfolio.get("balance", 0))
        new_balance = current_balance - entry_fee
        pte.portfolio["balance"] = round(new_balance, 2)

        print(f"💸 Entry fee {entry_fee:.4f} deducted for {symbol} ({side})")
        print(f"💰 Balance updated: {current_balance:.2f} → {new_balance:.2f}")

        # Update DB portfolio table for persistent tracking
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
        print(f"⚠️ Could not update portfolio balance on open trade: {e}")
        entry_fee = 0.0

    # --- Insert trade record into DB ---
    item = {
        "symbol": symbol,
        "side": side.upper(),  # Normalize side case
        "entry": entry,
        "target": target,
        "stop_loss": stop_loss,
        "size": size,
        "opened_at": opened_at,
        "closed_at": None,
        "status": "OPEN",         # Explicitly set status
        "exit_price": None,
        "pnl": None,
        "outcome": None,
        "fees": entry_fee,
        "reason": None,           # Add reason field for trade context
    }

    try:
        with engine.begin() as conn:
            result = conn.execute(insert(trades).values(**item).returning(trades.c.id))
            trade_id = result.scalar()

        print(f"✅ Trade opened successfully:")
        print(f"   Symbol: {symbol}")
        print(f"   Entry: {entry}")
        print(f"   Target: {target}")
        print(f"   Stop Loss: {stop_loss}")
        print(f"   Size: {size}")
        print(f"   Trade ID: {trade_id}")

        # Verify the trade was created properly
        with engine.connect() as conn:
            verify = conn.execute(
                select(trades).where(trades.c.id == trade_id)
            ).mappings().first()

            if verify:
                print(f"✅ Trade verification successful:")
                print(f"   ID: {verify['id']}")
                print(f"   Symbol: {verify['symbol']}")
                print(f"   Status: {verify['status']}")
                print(f"   Entry: {verify['entry']}")
                print(f"   Opened At: {verify['opened_at']}")
            else:
                print("⚠️ Could not verify trade creation!")

        return trade_id

    except Exception as e:
        print(f"❌ Failed to create trade record: {e}")
        import traceback
        traceback.print_exc()
        return None

    return trade_id



# ✅ Close an existing open trade (updates instead of inserting new row)
def close_trade_by_price(symbol: str, exit_price: float, reason: str):
    from datetime import datetime

    trade = get_open_trade(symbol)
    if not trade:
        print(f"[WARN] ⚠️ No open trade found for {symbol}")
        return None

    entry = float(trade.get("entry", 0))
    side = trade.get("side", "BUY").upper()
    size = float(trade.get("size", 1))

    # --- Binance-style trading fees ---
    BUY_FEE_RATE = 0.001  # 0.1%
    SELL_FEE_RATE = 0.001  # 0.1%

    # --- Calculate gross PnL ---
    pnl = (exit_price - entry) * size
    if side == "SELL":
        pnl = -pnl

    # --- Calculate trade value and total fees ---
    trade_value_entry = entry * size
    trade_value_exit = exit_price * size
    total_fees = (trade_value_entry * BUY_FEE_RATE) + (trade_value_exit * SELL_FEE_RATE)

    # --- Subtract fees from PnL (realistic) ---
    pnl_after_fees = pnl - total_fees

    result = {
        "exit_price": exit_price,
        "pnl": pnl_after_fees,
        "fees": total_fees,
        "outcome": "WIN" if pnl_after_fees >= 0 else "LOSS",
        "reason": reason,
        "closed_at": datetime.utcnow(),
    }

    # ✅ Update DB record for this trade
    with engine.begin() as conn:
        conn.execute(
            update(trades)
            .where(trades.c.id == trade["id"])
            .values(**result, status="CLOSED")  # ✅ add this
        )

    print(
        f"[DB] ✅ Closed trade {symbol.upper()} @ {exit_price} | {result['outcome']} | "
        f"PnL(after fees)={pnl_after_fees:.4f} | Fees={total_fees:.4f} | Reason={reason}"
    )

    # ✅ Update simulated balance
    try:
        import paper_trading_engine as pte
        current_balance = float(pte.portfolio.get("balance", 0))
        new_balance = current_balance + pnl_after_fees
        pte.portfolio["balance"] = round(new_balance, 2)

        print(
            f"💰 Balance updated: {current_balance:.2f} → {new_balance:.2f} "
            f"(Δ {pnl_after_fees:.4f} incl. fees)"
        )

        # (Optional) sync to DB portfolio table
        # from sqlalchemy import update
        from db import portfolio as portfolio_table
        with engine.begin() as conn:
            conn.execute(
                update(portfolio_table).values(
                    balance=new_balance,
                    updated_at=datetime.utcnow()
                )
            )

    except Exception as e:
        print(f"[WARN] Could not update paper balance: {e}")

    return result









# import json
# import threading
# from pathlib import Path
# from datetime import datetime



# from datetime import datetime
# from sqlalchemy import insert, select # type: ignore
# from db import engine, trades

# def record_closed_trade(trade: dict, result: dict):
#     item = {
#         "symbol": trade.get("symbol"),
#         "side": trade.get("side"),
#         "entry": trade.get("entry"),
#         "target": trade.get("target"),
#         "stop_loss": trade.get("stop_loss"),
#         "size": trade.get("size"),
#         "opened_at": trade.get("opened_at"),
#         "closed_at": result.get("closed_at") or datetime.utcnow(),
#         "exit_price": result.get("exit_price"),
#         "pnl": result.get("pnl"),
#         "outcome": result.get("outcome"),
#         "reason": result.get("reason")
#     }

#     with engine.begin() as conn:
#         conn.execute(insert(trades).values(**item))
#     return item

# def get_history(limit: int = None):
#     with engine.connect() as conn:
#         stmt = select(trades).order_by(trades.c.id.desc())
#         if limit:
#             stmt = stmt.limit(limit)
#         rows = conn.execute(stmt).mappings().all()
#     return [dict(row) for row in rows]

# def get_open_trade(symbol: str):
#     """Fetch the most recent open trade for a symbol."""
#     from sqlalchemy import select
#     with engine.connect() as conn:
#         stmt = select(trades).where(
#             (trades.c.symbol == symbol.upper()) &
#             (trades.c.closed_at.is_(None))
#         ).order_by(trades.c.id.desc())
#         row = conn.execute(stmt).mappings().first()
#     return dict(row) if row else None

# def close_trade_by_price(symbol: str, exit_price: float, reason: str):
#     """Close the most recent open trade for a symbol at the given exit price."""
#     from datetime import datetime
#     from paper_trading_engine import close_trade  # ✅ uses your existing close_trade()
    
#     trade = get_open_trade(symbol)
#     if not trade:
#         print(f"[WARN] No open trade found for {symbol}")
#         return None

#     entry = float(trade.get("entry", 0))
#     side = trade.get("side", "BUY").upper()

#     # calculate pnl
#     pnl = (exit_price - entry) * trade.get("size", 1)
#     if side == "SELL":
#         pnl = -pnl  # reverse for short trades

#     result = {
#         "exit_price": exit_price,
#         "pnl": pnl,
#         "outcome": "WIN" if pnl >= 0 else "LOSS",
#         "reason": reason,
#         "closed_at": datetime.utcnow()
#     }

#     print(f"[⚙️ Closing trade] {symbol} | Exit={exit_price} | PnL={pnl:.4f} | Reason={reason}")
#     return close_trade(trade, result)
