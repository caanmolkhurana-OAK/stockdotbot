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

# Updated valid timeframes including 45m and 4h
VALID_TIMEFRAMES = {
    "5m": "5d",
    "15m": "5d",
    "45m": "1mo",
    "1h": "1mo",
    "4h": "3mo",
    "1d": "3mo"
}

def get_currency_symbol(ticker: str) -> str:
    """Returns $ for international symbols/forex/commodities and ₹ for Indian stocks."""
    if ticker.endswith(".NS") or ticker.endswith(".BO") or ticker.startswith("^NSE") or ticker.startswith("^BSE"):
        return "₹"
    return "$"

# Lightweight HTTP server to satisfy Render Free Web Service health check
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Welcome to @stockdotbot! (24/7 Cloud)*\n\n"
        "Set an alert:\n"
        "`/alert <TICKER> <TARGET_PRICE> <TIMEFRAME>`\n\n"
        "• *Target > Current Price* ➔ Triggers on **Close ABOVE** (Breakout)\n"
        "• *Target < Current Price* ➔ Triggers on **Close BELOW** (Breakdown)\n\n"
        "Supported Timeframes:\n"
        "`5m`, `15m`, `45m`, `1h`, `4h`, `1d`\n\n"
        "Examples:\n"
        "• `/alert TATAMOTORS 980 45m` (NSE Stock in ₹)\n"
        "• `/alert GOLDBEES.NS 68.00 4h` (Gold ETF in ₹)\n"
        "• `/alert XAUUSD=X 2650 4h` (Spot Gold in $)\n\n"
        "Check alerts: `/list` | Clear all: `/clear`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        raw_ticker = context.args[0].upper()
        
        # SMART TICKER CLEANUP:
        # Do not append .NS to Forex, Commodities (=), Indices (^), Gold/FX (XAU/USD), or BSE (.BO)
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

        # Fetch current price to check validity & determine direction
        period = VALID_TIMEFRAMES[tf]
        df = yf.Ticker(ticker).history(period=period, interval=tf)
        if df.empty:
            await update.message.reply_text(f"❌ Could not fetch market data for `{ticker}`. Please verify ticker name.", parse_mode="Markdown")
            return

        current_price = float(df.iloc[-1]['Close'])
        direction = "ABOVE" if target_price >= current_price else "BELOW"
        currency = get_currency_symbol(ticker)

        alert_item = {
            "chat_id": chat_id,
            "ticker": ticker,
            "target_price": target_price,
            "direction": direction,
            "timeframe": tf,
            "currency": currency,
            "last_alerted_candle": None
        }
        ACTIVE_ALERTS.append(alert_item)

        await update.message.reply_text(
            f"✅ *Alert Set!*\n"
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
        target = alert["target_price"]
        direction = alert["direction"]
        tf = alert["timeframe"]
        currency = alert.get("currency", "$")
        period = VALID_TIMEFRAMES[tf]

        try:
            df = yf.Ticker(ticker).history(period=period, interval=tf)
            if df.empty or len(df) < 2:
                continue

            last_closed = df.iloc[-2]
            close_price = float(last_closed['Close'])
            candle_time = last_closed.name.strftime('%Y-%m-%d %H:%M')

            is_triggered = False
            if direction == "ABOVE" and close_price > target:
                is_triggered = True
            elif direction == "BELOW" and close_price < target:
                is_triggered = True

            if is_triggered:
                if alert["last_alerted_candle"] != candle_time:
                    msg = (
                        f"🚨 *PRICE ALERT TRIGGERED!* 🚨\n\n"
                        f"• Ticker: *{ticker}*\n"
                        f"• Timeframe: *{tf}*\n"
                        f"• Condition: Close *{direction}* target\n"
                        f"• Candle Close: *{currency}{close_price:.2f}*\n"
                        f"• Target Level: *{currency}{target:.2f}*\n"
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

    print("🚀 @stockdotbot is online in Render Free Web Service!")
    app.run_polling()
