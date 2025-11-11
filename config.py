# config.py


import os
from dotenv import load_dotenv

load_dotenv()  # Loads variables from .env file

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Trading settings
DEFAULT_BALANCE = 10000  # Virtual starting balance
SL_PERCENTAGE = 1.0      # Stop-loss percentage
TP_PERCENTAGE = 2.0      # Take-profit percentage

# Data refresh
REFRESH_INTERVAL = 5     # seconds
