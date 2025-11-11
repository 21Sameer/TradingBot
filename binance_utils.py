import requests
import pandas as pd
from typing import List, Dict
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Cache the symbols list to avoid frequent API calls
_cached_symbols = None
_last_cache_time = 0
_CACHE_TTL = 7200  # 2 hour cache

# Configure retry strategy
retry_strategy = Retry(
    total=3,  # number of retries
    backoff_factor=1,  # wait 1, 2, 4 seconds between retries
    status_forcelist=[429, 418, 500, 502, 503, 504]  # status codes to retry on
)
# Create session with retry strategy
session = requests.Session()
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

# Add headers to identify application
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)',
    'Accept': 'application/json'
}

def get_binance_symbols() -> List[str]:
    """
    Get list of valid Binance trading pairs.
    Uses caching to avoid frequent API calls.
    """
    global _cached_symbols, _last_cache_time
    
    current_time = time.time()
    
    # Return cached results if still valid
    if _cached_symbols and (current_time - _last_cache_time) < _CACHE_TTL:
        return _cached_symbols
    
    try:
        # Fetch exchange info from Binance with retry and proper headers
        response = session.get(
            'https://api.binance.com/api/v3/exchangeInfo',
            timeout=60,
            headers=headers
        )
        response.raise_for_status()
        
        # Extract valid USDT trading pairs
        data = response.json()
        symbols = [
            symbol['symbol'] 
            for symbol in data['symbols']
            if symbol['status'] == 'TRADING' 
            and symbol['symbol'].endswith('USDT')
            and symbol['isSpotTradingAllowed']
        ]
        
        # If we got no symbols, something is wrong
        if not symbols:
            raise Exception("No trading pairs found in response")
        
        # Update cache
        _cached_symbols = symbols
        _last_cache_time = current_time
        
        print(f"✅ Fetched {len(symbols)} valid Binance USDT pairs")
        return symbols
        
    except Exception as e:
        print(f"⚠️ Error fetching Binance symbols: {e}")
        # Return cached symbols if available, otherwise empty list
        return _cached_symbols if _cached_symbols else []

def filter_valid_binance_symbols(symbols: List[str]) -> List[str]:
    """
    Filter a list of symbols to only include valid Binance trading pairs.
    """
    valid_symbols = get_binance_symbols()
    valid_symbols_set = set(valid_symbols)
    
    # Filter and standardize symbols
    filtered = [
        sym.upper() 
        for sym in symbols 
        if sym.upper() in valid_symbols_set
    ]
    
    print(f"✅ Filtered {len(filtered)} valid Binance pairs from {len(symbols)} input symbols")
    return filtered