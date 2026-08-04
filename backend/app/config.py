import os
from dotenv import load_dotenv

# Load .env file
load_dotenv(override=True)

PROVIDER = os.getenv("PROVIDER", "mock").lower()

UPSTOX_CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID", "")
UPSTOX_CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET", "")
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "https://mesh-deferred-legendary-ellis.trycloudflare.com/api/auth/callback")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./market_tracker.db")

# A set list of standard high-volume Nifty 50 stocks for Movers & Search
DEFAULT_NIFTY_50 = [
    {"symbol": "RELIANCE", "name": "Reliance Industries Ltd.", "key": "NSE_EQ|INE002A01018"},
    {"symbol": "TCS", "name": "Tata Consultancy Services Ltd.", "key": "NSE_EQ|INE467B01029"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "key": "NSE_EQ|INE040A01034"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel Ltd.", "key": "NSE_EQ|INE397D01024"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank Ltd.", "key": "NSE_EQ|INE090A01021"},
    {"symbol": "INFY", "name": "Infosys Ltd.", "key": "NSE_EQ|INE009A01021"},
    {"symbol": "SBI", "name": "State Bank of India", "key": "NSE_EQ|INE062A01020"},
    {"symbol": "LICI", "name": "Life Insurance Corporation of India", "key": "NSE_EQ|INE001G01021"},
    {"symbol": "ITC", "name": "ITC Ltd.", "key": "NSE_EQ|INE154A01025"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever Ltd.", "key": "NSE_EQ|INE030A01027"},
    {"symbol": "L&T", "name": "Larsen & Toubro Ltd.", "key": "NSE_EQ|INE018A01030"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance Ltd.", "key": "NSE_EQ|INE296A01024"},
    {"symbol": "HCLTECH", "name": "HCL Technologies Ltd.", "key": "NSE_EQ|INE860A01027"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki India Ltd.", "key": "NSE_EQ|INE585B01010"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical Industries Ltd.", "key": "NSE_EQ|INE044A01036"},
    {"symbol": "ADANIENT", "name": "Adani Enterprises Ltd.", "key": "NSE_EQ|INE423A01024"},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank Ltd.", "key": "NSE_EQ|INE237A01028"},
    {"symbol": "TITAN", "name": "Titan Company Ltd.", "key": "NSE_EQ|INE280A01028"},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement Ltd.", "key": "NSE_EQ|INE481G01011"},
    {"symbol": "AXISBANK", "name": "Axis Bank Ltd.", "key": "NSE_EQ|INE238A01034"},
    {"symbol": "NTPC", "name": "NTPC Ltd.", "key": "NSE_EQ|INE733E01010"},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corporation Ltd.", "key": "NSE_EQ|INE213A01029"},
    {"symbol": "ASIANPAINT", "name": "Asian Paints Ltd.", "key": "NSE_EQ|INE021A01026"},
    {"symbol": "ADANIPORTS", "name": "Adani Ports & SEZ Ltd.", "key": "NSE_EQ|INE742F01042"},
    {"symbol": "TATASTEEL", "name": "Tata Steel Ltd.", "key": "NSE_EQ|INE081A01020"},
    {"symbol": "COALINDIA", "name": "Coal India Ltd.", "key": "NSE_EQ|INE522F01014"},
    {"symbol": "M&M", "name": "Mahindra & Mahindra Ltd.", "key": "NSE_EQ|INE101A01026"},
    {"symbol": "POWERGRID", "name": "Power Grid Corporation of India Ltd.", "key": "NSE_EQ|INE752E01010"},
    {"symbol": "JSWSTEEL", "name": "JSW Steel Ltd.", "key": "NSE_EQ|INE019A01030"},
    {"symbol": "Tatamotors", "name": "Tata Motors Ltd.", "key": "NSE_EQ|INE155A01022"},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv Ltd.", "key": "NSE_EQ|INE918I01018"},
    {"symbol": "NESTLEIND", "name": "Nestle India Ltd.", "key": "NSE_EQ|INE239A01016"},
    {"symbol": "GRASIM", "name": "Grasim Industries Ltd.", "key": "NSE_EQ|INE047A01021"},
    {"symbol": "ADANIPOWER", "name": "Adani Power Ltd.", "key": "NSE_EQ|INE814H01011"},
    {"symbol": "TECHM", "name": "Tech Mahindra Ltd.", "key": "NSE_EQ|INE669C01036"},
    {"symbol": "WIPRO", "name": "Wipro Ltd.", "key": "NSE_EQ|INE075A01022"},
    {"symbol": "HINDALCO", "name": "Hindalco Industries Ltd.", "key": "NSE_EQ|INE038A01020"},
    {"symbol": "INDUSINDBK", "name": "IndusInd Bank Ltd.", "key": "NSE_EQ|INE095A01012"},
    {"symbol": "JIOFIN", "name": "Jio Financial Services Ltd.", "key": "NSE_EQ|INE758E01017"},
    {"symbol": "HAL", "name": "Hindustan Aeronautics Ltd.", "key": "NSE_EQ|INE066F01012"},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products Ltd.", "key": "NSE_EQ|INE192A01025"},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance Company Ltd.", "key": "NSE_EQ|INE123W01016"},
    {"symbol": "BRITANNIA", "name": "Britannia Industries Ltd.", "key": "NSE_EQ|INE216A01030"},
    {"symbol": "BPCL", "name": "Bharat Petroleum Corporation Ltd.", "key": "NSE_EQ|INE029A01011"},
    {"symbol": "EICHERMOT", "name": "Eicher Motors Ltd.", "key": "NSE_EQ|INE066A01021"},
    {"symbol": "LTIM", "name": "LTIMindtree Ltd.", "key": "NSE_EQ|INE214B01022"},
    {"symbol": "CIPLA", "name": "Cipla Ltd.", "key": "NSE_EQ|INE059A01026"},
    {"symbol": "DIVISLAB", "name": "Divi's Laboratories Ltd.", "key": "NSE_EQ|INE361B01024"},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals Enterprise Ltd.", "key": "NSE_EQ|INE439A01020"},
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto Ltd.", "key": "NSE_EQ|INE917I01010"}
]
