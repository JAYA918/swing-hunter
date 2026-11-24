# Swing Hunter AI

Automatic NSE F&O swing option buying stock scanner (backend + API for the iOS app).

## What it does
- Scans all current NSE F&O stocks (live list from NSE)
- Top 5 bullish and Top 5 bearish stocks
- CE / PE / WAIT signal with score out of 100
- RSI(14), EMA 20/50, volume expansion, 20-day breakout
- Entry, Stop Loss, Target 1, Target 2 (on the underlying stock)
- Auto scan Mon-Fri at 9:00 AM and 4:00 PM IST
- Saves every scan in a SQLite database (history)

## Real data only
If market data cannot be fetched or is stale, no scan is saved and no signals are shown. Nothing is simulated.
Data source is yfinance (delayed daily candles). For live data, replace `fetch_prices()` in `main.py` with a broker API (Kite, Upstox, Dhan, Angel One).

## Run locally
```
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Deploy on Render (free)
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Optional environment variable: `API_KEY` (protects manual scan)

## API
| Endpoint | Purpose |
|---|---|
| `GET /api/latest` | Latest scan with Top 5 bullish and bearish |
| `GET /api/history` | List of saved scans |
| `GET /api/scan/{id}` | One saved scan |
| `POST /api/scan` | Start a scan now (send `x-api-key` header if API_KEY is set) |
| `GET /docs` | Interactive API page |

## Disclaimer
Educational tool. Not investment advice. Options trading carries high risk.
