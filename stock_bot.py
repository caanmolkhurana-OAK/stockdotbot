import os
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

ACTIVE_ALERTS = []

# Periods tailored to ensure 60+ historical candles for 60-SMA calculations
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

# Lightweight HTTP server to satisfy Render Free Web Service health check
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive with 60-SMA Volume Tracking!")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Welcome to @stockdotbot! (60-SMA Volume Edition)*\n\n"
        "Set an alert:\n"
        "`/alert <TICKER> <TARGET_PRICE> <TIMEFRAME>`\n\n"
        "• *Target > Current Price* ➔ Close **ABOVE** (Breakout)\n"
        "• *Target < Current Price* ➔ Close **BELOW** (Breakdown)\n\n"
        "Supported Timeframes:\n"
        "`5m`, `15m`, `45m`, `1h`, `4h`, `1d`\n\n"
        "Examples:\n"
        "• `/alert TATAMOTORS 980 15m` (NSE Stock in ₹)\n"
        "• `/alert XAUUSD=X 2650 15m` (Spot Gold in $)\n\n"
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

        fetch_ticker = get_fetch_ticker(ticker, tf)
        period = VALID_TIMEFRAMES[tf]
        
        df = yf.Ticker(fetch_ticker).history(period=period, interval=tf)
        if df.empty:
            await update.message.reply_text(f"❌ Could not fetch live data for `{ticker}`. Verify ticker or market hours.", parse_mode="Markdown")
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
            "last_alerted_candle": None
        }
        ACTIVE_ALERTS.append(alert_item)

        await update.message.reply_text(
            f"✅ *Alert Set (60-SMA Volume Enabled)!*\n"
            f"• Ticker: `{ticker}`\n"
            f"• Current Price: `{currency}{current_price:.2f}`\n"
            f"• Trigger: Close **{direction}** `{currency}{target_price:.2f}`\n"
            f"• Timeframe: `{tf}`",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ *Invalid format.*\nUse: `/alert <TICKER> <TARGET_PRICE> <TIMEFRAME>`",
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
        text += f"{idx}. `{a['ticker']}` | Target: {a['direction']} `{curr}{a['target_price']}` | TF: `{a['timeframe']}`\n"

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

            last_closed = df.iloc[-2]
            close_price = float(last_closed['Close'])
            candle_volume = float(last_closed['Volume'])
            candle_time = last_closed.name.strftime('%Y-%m-%d %H:%M')

            # Calculate 60-period Volume Moving Average
            vol_series = df['Volume'].iloc[:-1]  # Exclude current active/incomplete bar
            vol_sma60 = float(vol_series.iloc[-60:].mean()) if len(vol_series) >= 60 else float(vol_series.mean())
            vol_ratio60 = (candle_volume / vol_sma60) if vol_sma60 > 0 else 1.0

            is_triggered = False
            if direction == "ABOVE" and close_price > target:
                is_triggered = True
            elif direction == "BELOW" and close_price < target:
                is_triggered = True

            if is_triggered:
                if alert["last_alerted_candle"] != candle_time:
                    formatted_vol = format_volume(candle_volume)
                    if candle_volume > 0:
                        vol_text = f"{formatted_vol} ({vol_ratio60:.1f}x 60-SMA Avg)"
                    else:
                        vol_text = "N/A"

                    msg = (
                        f"🚨 *PRICE ALERT TRIGGERED!* 🚨\n\n"
                        f"• Ticker: *{ticker}*\n"
                        f"• Timeframe: *{tf}*\n"
                        f"• Condition: Close *{direction}* target\n"
                        f"• Candle Close: *{currency}{close_price:.2f}*\n"
                        f"• Target Level: *{currency}{target:.2f}*\n"
                        f"• Candle Volume: *{vol_text}* 📊\n"
                        f"• Candle Time: `{candle_time}`"
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

    print("🚀 @stockdotbot is online with 60-SMA Volume Analysis!")
    app.run_polling()
