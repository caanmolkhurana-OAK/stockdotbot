import os
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import pandas as pd
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

ACTIVE_ALERTS = []

VALID_TIMEFRAMES = {
    "5m": "5d",
    "15m": "5d",
    "45m": "1mo",
    "1h": "1mo",
    "4h": "3mo",
    "1d": "6mo"
}

def get_currency_symbol(ticker: str) -> str:
    """Returns $ for international symbols/forex/commodities and ₹ for Indian stocks."""
    if ticker.endswith(".NS") or ticker.endswith(".BO") or ticker.startswith("^NSE") or ticker.startswith("^BSE"):
        return "₹"
    return "$"

def get_fetch_ticker(ticker: str, timeframe: str) -> str:
    """Uses GC=F for intraday Spot Gold requests to bypass Yahoo Finance API limits."""
    if ticker in ["XAUUSD=X", "XAUUSD"] and timeframe in ["5m", "15m", "45m"]:
        return "GC=F"
    return ticker

def format_volume(volume: float) -> str:
    """Formats raw numbers into financial notation (K, M)."""
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"{volume / 1_000:.1f}K"
    return f"{int(volume)}"

def calculate_rsi(close_series: pd.Series, period: int = 14) -> float:
    """Calculates standard 14-period Relative Strength Index (RSI)."""
    if len(close_series) < period + 1:
        return 50.0
    
    delta = close_series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.iloc[1:period+1].mean()
    avg_loss = loss.iloc[1:period+1].mean()

    for i in range(period + 1, len(close_series)):
        avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period

    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)

# Lightweight HTTP server to satisfy Render Free Web Service health check
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive with 60-SMA Volume Multiple Tracking!")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Welcome to @stockdotbot!*\n\n"
        "Set an alert:\n"
        "`/alert <TICKER> <TRIGGER_PRICE> <TIMEFRAME> [TARGET_PRICE]`\n\n"
        "Examples:\n"
        "• `/alert TATAMOTORS 980 15m 1020`\n"
        "• `/alert XAUUSD=X 2650 15m 2700`\n\n"
        "Check alerts: `/list` | Clear all: `/clear`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        raw_ticker = context.args[0].upper()
        
        is_global_asset = ("=" in raw_ticker or "XAU" in raw_ticker or "USD" in raw_ticker or "^" in raw_ticker or raw_ticker.endswith(".BO"))
        if not is_global_asset and not raw_ticker.endswith(".NS"):
            ticker = raw_ticker + ".NS"
        else:
            ticker = raw_ticker

        target_price = float(context.args[1])
        tf = context.args[2].lower()

        if tf not in VALID_TIMEFRAMES:
            await update.message.reply_text("❌ Invalid timeframe. Choose: `5m`, `15m`, `45m`, `1h`, `4h`, `1d`", parse_mode="Markdown")
            return

        tp_price = float(context.args[3]) if len(context.args) > 3 else None

        fetch_ticker = get_fetch_ticker(ticker, tf)
        period = VALID_TIMEFRAMES[tf]
        
        df = yf.Ticker(fetch_ticker).history(period=period, interval=tf)
        if df.empty:
            await update.message.reply_text(f"❌ Could not fetch live data for `{ticker}`.", parse_mode="Markdown")
            return

        current_price = float(df.iloc[-1]['Close'])
        direction = "ABOVE" if target_price >= current_price else "BELOW"
        currency = get_currency_symbol(ticker)

        alert_item = {
            "chat_id": chat_id,
            "ticker": ticker,
            "fetch_ticker": fetch_ticker,
            "target_price": target_price,
            "direction": direction,
            "timeframe": tf,
            "currency": currency,
            "tp_price": tp_price,
            "last_alerted_candle": None
        }
        ACTIVE_ALERTS.append(alert_item)

        reply_msg = (
            f"✅ *Alert Set!*\n"
            f"• Stock: `{ticker}`\n"
            f"• Trigger Level: `{currency}{target_price:.2f}` ({direction})\n"
            f"• Timeframe: `{tf}`"
        )
        if tp_price:
            reply_msg += f"\n• Target Price: `{currency}{tp_price:.2f}`"

        await update.message.reply_text(reply_msg, parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ *Invalid format.*\nUse: `/alert <TICKER> <TRIGGER_PRICE> <TIMEFRAME> [TARGET_PRICE]`",
            parse_mode="Markdown"
        )

async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_alerts = [a for a in ACTIVE_ALERTS if a["chat_id"] == chat_id]

    if not user_alerts:
        await update.message.reply_text("No active alerts.")
        return

    text = "📋 *Active Watchlist:*\n\n"
    for idx, a in enumerate(user_alerts, 1):
        curr = a.get("currency", "$")
        tp_str = f" | Target: `{curr}{a['tp_price']}`" if a.get("tp_price") else ""
        text += f"{idx}. `{a['ticker']}` | Trigger: {a['direction']} `{curr}{a['target_price']}` | TF: `{a['timeframe']}`{tp_str}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def clear_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    global ACTIVE_ALERTS
    ACTIVE_ALERTS = [a for a in ACTIVE_ALERTS if a["chat_id"] != chat_id]
    await update.message.reply_text("🧹 Cleared all alerts.")

async def check_breakouts_job(context: ContextTypes.DEFAULT_TYPE):
    if not ACTIVE_ALERTS:
        return

    triggered = []

    for alert in ACTIVE_ALERTS:
        ticker = alert["ticker"]
        fetch_ticker = alert.get("fetch_ticker", ticker)
        target = alert["target_price"]
        direction = alert["direction"]
        tf = alert["timeframe"]
        currency = alert.get("currency", "$")
        period = VALID_TIMEFRAMES[tf]

        try:
            df = yf.Ticker(fetch_ticker).history(period=period, interval=tf)
            if df.empty or len(df) < 2:
                continue

            closed_df = df.iloc[:-1]
            last_closed = closed_df.iloc[-1]
            
            close_price = float(last_closed['Close'])
            candle_volume = float(last_closed['Volume'])
            candle_time = last_closed.name.strftime('%Y-%m-%d %H:%M')

            rsi_val = calculate_rsi(closed_df['Close'], period=14)

            # Calculate 60-period Volume Moving Average
            vol_series = closed_df['Volume']
            vol_sma60 = float(vol_series.iloc[-60:].mean()) if len(vol_series) >= 60 else float(vol_series.mean())
            vol_ratio60 = (candle_volume / vol_sma60) if vol_sma60 > 0 else 1.0

            is_triggered = False
            if direction == "ABOVE" and close_price > target:
                is_triggered = True
            elif direction == "BELOW" and close_price < target:
                is_triggered = True

            if is_triggered:
                if alert["last_alerted_candle"] != candle_time:
                    formatted_vol = format_volume(candle_volume) if candle_volume > 0 else "N/A"
                    vol_output = f"{formatted_vol} ({vol_ratio60:.1f}x 60-SMA)" if candle_volume > 0 else "N/A"
                    tp_val = f"{currency}{alert['tp_price']:.2f}" if alert.get("tp_price") else "N/A"

                    # MINIMAL REQUIRED OUTPUT WITH 60-SMA MULTIPLE
                    msg = (
                        f"🚨 *ALERT TRIGGERED* 🚨\n\n"
                        f"• Stock: *{ticker}*\n"
                        f"• RSI: *{rsi_val}*\n"
                        f"• Volume: *{vol_output}*\n"
                        f"• Entry Price: *{currency}{close_price:.2f}*\n"
                        f"• Target Price: *{tp_val}*"
                    )
                    await context.bot.send_message(chat_id=alert["chat_id"], text=msg, parse_mode="Markdown")
                    alert["last_alerted_candle"] = candle_time
                    triggered.append(alert)

        except Exception as e:
            logging.error(f"Error scanning {ticker}: {e}")

    for alert in triggered:
        if alert in ACTIVE_ALERTS:
            ACTIVE_ALERTS.remove(alert)

if __name__ == "__main__":
    BOT_TOKEN = "8964779286:AAEJJfB49NFgVxR8zMOvImegNrlRaqEjcHA"

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("alert", add_alert))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("clear", clear_alerts))

    job_queue = app.job_queue
    job_queue.run_repeating(check_breakouts_job, interval=60, first=5)

    print("🚀 @stockdotbot is online with 60-SMA Volume Multiple Output!")
    app.run_polling()
