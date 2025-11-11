# indicators.py
import pandas as pd
import numpy as np


def calculate_indicators(df, latest_price=None):
    """
    Calculate multiple technical indicators for trading.
    """

    # Append latest price as last candle if provided
    if latest_price is not None:
        new_row = pd.DataFrame([{
            'open': latest_price,
            'high': latest_price,
            'low': latest_price,
            'close': latest_price,
            'volume': 0
        }])
        df = pd.concat([df, new_row], ignore_index=True)

    # Ensure numeric columns
    for col in ['open','high','low','close','volume']:
        df[col] = df[col].astype(float)

    indicators = {}

    # --- EMA ---
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    indicators['ema_9'] = df['ema_9'].iloc[-1]
    indicators['ema_21'] = df['ema_21'].iloc[-1]
    # NEW: EMA slope (momentum)
    indicators['ema_slope'] = df['ema_9'].iloc[-1] - df['ema_9'].iloc[-5] if len(df) > 5 else 0

    # --- RSI ---
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    indicators['rsi'] = rsi.iloc[-1]

    # --- MACD ---
    ema_12 = df['close'].ewm(span=12, adjust=False).mean()
    ema_26 = df['close'].ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line
    indicators['macd'] = macd_line.iloc[-1]
    indicators['macd_signal'] = signal_line.iloc[-1]
    indicators['macd_hist'] = macd_hist.iloc[-1]
    # NEW: MACD slope
    indicators['macd_slope'] = macd_line.iloc[-1] - macd_line.iloc[-5] if len(macd_line) > 5 else 0

    # --- Bollinger Bands ---
    sma_20 = df['close'].rolling(20).mean()
    std_20 = df['close'].rolling(20).std()
    indicators['bb_upper'] = sma_20.iloc[-1] + 2 * std_20.iloc[-1]
    indicators['bb_middle'] = sma_20.iloc[-1]
    indicators['bb_lower'] = sma_20.iloc[-1] - 2 * std_20.iloc[-1]

    # --- ATR ---
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    indicators['atr'] = atr.iloc[-1]
    # NEW: ATR trend (volatility expansion or contraction)
    indicators['atr_trend'] = atr.iloc[-1] - atr.iloc[-5] if len(atr) > 5 else 0

    # --- VWAP ---
    cumulative_volume = df['volume'].cumsum()
    cumulative_vwap = (df['close'] * df['volume']).cumsum()
    indicators['vwap'] = (cumulative_vwap / cumulative_volume).iloc[-1]

   # --- Stochastic Oscillator ---
    try:
        low_14 = df['low'].rolling(14).min()
        high_14 = df['high'].rolling(14).max()
        denominator = high_14.iloc[-1] - low_14.iloc[-1]

        if denominator == 0 or np.isnan(denominator):
            indicators['stochastic'] = 50.0  # neutral value
        else:
            indicators['stochastic'] = (
                (df['close'].iloc[-1] - low_14.iloc[-1]) / denominator * 100
            )
    except Exception as e:
        print(f"⚠️ Stochastic calc failed: {e}")
        indicators['stochastic'] = 50.0  # fallback neutral

    # --- CCI (Commodity Channel Index) ---
    tp = (df['high'] + df['low'] + df['close']) / 3
    sma_tp = tp.rolling(20).mean()
    mean_dev = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    try:
        denom = 0.015 * mean_dev.iloc[-1]
        if denom == 0 or np.isnan(denom):
            indicators['cci'] = 0
        else:
            indicators['cci'] = (tp.iloc[-1] - sma_tp.iloc[-1]) / denom
    except Exception:
        indicators['cci'] = 0

    return indicators


def evaluate_trend(df, indicators=None):
    """
    Enhanced trend evaluation using more dynamic factors.
    """
    try:
        if indicators is None:
            indicators = calculate_indicators(df)
    except Exception:
        return {'trend': 'NEUTRAL', 'score': 0.0, 'votes': {}, 'reason': 'indicators_unavailable'}

    votes = {}
    score = 0.0

    # EMA cross
    ema9 = float(indicators.get('ema_9') or 0)
    ema21 = float(indicators.get('ema_21') or 0)
    if ema9 > ema21:
        votes['ema_cross_up'] = True
        score += 1.0
    elif ema9 < ema21:
        votes['ema_cross_down'] = True
        score -= 1.0

    # EMA slope
    ema_slope = float(indicators.get('ema_slope') or 0)
    if ema_slope > 0:
        votes['ema_slope_up'] = True
        score += 0.6
    elif ema_slope < 0:
        votes['ema_slope_down'] = True
        score -= 0.6

    # MACD histogram & slope
    macd_hist = float(indicators.get('macd_hist') or 0)
    macd_slope = float(indicators.get('macd_slope') or 0)
    if macd_hist > 0 and macd_slope > 0:
        votes['macd_momentum_up'] = True
        score += 0.8
    elif macd_hist < 0 and macd_slope < 0:
        votes['macd_momentum_down'] = True
        score -= 0.8

    # ATR trend (volatility expansion helps trend)
    atr_trend = float(indicators.get('atr_trend') or 0)
    if atr_trend > 0:
        votes['atr_expanding'] = True
        score += 0.4
    elif atr_trend < 0:
        votes['atr_contracting'] = True
        score -= 0.2

    # RSI levels
    rsi = float(indicators.get('rsi') or 50)
    if rsi < 35:
        votes['rsi_oversold'] = True
        score += 0.5
    elif rsi > 65:
        votes['rsi_overbought'] = True
        score -= 0.5

    # Price vs VWAP
    vwap = float(indicators.get('vwap') or 0)
    price = float(df['close'].iloc[-1])
    if vwap and price > vwap:
        votes['price_above_vwap'] = True
        score += 0.4
    elif vwap and price < vwap:
        votes['price_below_vwap'] = True
        score -= 0.4

    # CCI bias
    cci = float(indicators.get('cci') or 0)
    if cci > 100:
        votes['cci_pos'] = True
        score += 0.3
    elif cci < -100:
        votes['cci_neg'] = True
        score -= 0.3

    # Final decision
    trend = 'NEUTRAL'
    if score >= 1.8:
        trend = 'UP'
    elif score <= -1.8:
        trend = 'DOWN'

    reason = ', '.join([k.replace('_', ' ') for k, v in votes.items() if v]) or 'no clear bias'

    return {'trend': trend, 'score': round(score, 3), 'votes': votes, 'reason': reason}



def get_multi_timeframe_indicators(symbol: str):
    from data_fetcher import get_recent_candles

    """
    Fetch candles for multiple timeframes and compute indicators for each.
    Returns a dictionary like:
    {
        "1m": {"trend": "UP", "score": 2.1, ...},
        "5m": {"trend": "UP", "score": 1.4, ...},
        "15m": {"trend": "DOWN", "score": -2.0, ...},
        ...
    }
    """
    timeframes = ["1m", "5m", "15m", "1h", "4h"]
    results = {}

    for tf in timeframes:
        try:
            df = get_recent_candles(symbol, tf, limit=1000)
            if df is None or df.empty:
                print(f"⚠️ Skipping {symbol} {tf}: no candles")
                continue

            indicators = calculate_indicators(df)
            trend_info = evaluate_trend(df, indicators)
            results[tf] = trend_info
        except Exception as e:
            print(f"❌ Error calculating {tf} indicators for {symbol}: {e}")

    return results
