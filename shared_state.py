# shared_state.py
from paper_trading_engine import PaperTradingEngineSingleton

# One global singleton for the entire app
pte = PaperTradingEngineSingleton.get_instance()

# These just alias to shared internal state
last_ai_decisions = pte.last_ai_decisions
portfolio = pte.portfolio
