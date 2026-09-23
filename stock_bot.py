import os
import sqlite3
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

DB_FILE = "bot_data.db"

# Yahoo Supported Intervals Mapping
VALID_TIMEFRAMES = {
    "5m": ("5m", "5d"),
    "15m": ("15m", "5d"),
    "30m": ("30m", "1mo"),
    "45m": ("15m", "1mo"),  # Custom resampled from 15m
    "1h": ("1h", "1mo"),
    "4h": ("4h", "3mo"),
    "1d": ("1d", "6mo")
}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            ticker TEXT,
            fetch_ticker TEXT,
            target_price REAL,
            direction TEXT,
            timeframe TEXT,
            currency TEXT,
            tp_price REAL,
            last_alerted_candle TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            ticker TEXT,
            fetch_ticker TEXT,
            entry_price REAL,
            target_price REAL,
            currency TEXT,
            status TEXT DEFAULT 'ACTIVE',
            last_alerted_candle TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_currency_symbol(ticker: str) -> str:
    if ticker.endswith(".NS") or ticker.endswith(".BO") or ticker.startswith("^NSE") or ticker.startswith("^BSE"):
        return "₹"
    return "$"

def get_fetch_ticker(ticker: str, timeframe: str = "1d") -> str:
    if ticker in ["XAUUSD=X", "XAUUSD"] and timeframe in ["5m", "15m", "30m", "45m"]:
        return "GC=F"
    return ticker

def fetch_candle_data(ticker: str, tf: str) -> pd.DataFrame:
    """Fetches data from yfinance and applies 45m resampling when requested."""
    if tf not in VALID_TIMEFRAMES:
        return pd.DataFrame()

    yf_interval, period = VALID_TIMEFRAMES[tf]
    fetch_sym = get_fetch_ticker(ticker, tf)

    try:
        df = yf.Ticker(fetch_sym).history(period=period, interval=yf_interval)
        if df.empty:
            return pd.DataFrame()

        # Custom 45m candle resampling from 15m base data
        if tf == "45m":
            df_45m = df.resample('45min').agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            }).dropna()
            return df_45m

        return df
    except Exception as e:
        logging.error(f"Error fetching candle data for {ticker}: {e}")
        return pd.DataFrame()

def format_volume(volume: float) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"{volume / 1_000:.1f}K"
    return f"{int(volume)}"

def calculate_rsi(close_series: pd.Series, period: int = 14) -> float:
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

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Unified Stock Bot with 45m Resampling is running!")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Welcome to @stockdotbot!*\n\n"
        "1️⃣ *Set Price Breakout Alert:*\n"
        "`/alert <TICKER> <TRIGGER_PRICE> <TIMEFRAME> [TARGET_PRICE]`\n"
        "• Example: `/alert GREAVESCOT 209 45m 221`\n\n"
        "2️⃣ *Set Trade Target:*\n"
        "`/trade <TICKER> <ENTRY_PRICE> <TARGET_PRICE>`\n"
        "• Example: `/trade TEXRAIL 127.20 143`\n\n"
        "📋 *Management Commands:*\n"
        "• `/list` - View active alerts & trade targets with Live CMP\n"
        "• `/clear` - Wipe all active alerts & trade targets"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def add_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        if len(context.args) < 3:
            await update.message.reply_text("❌ Usage: `/alert <TICKER> <TRIGGER_PRICE> <TIMEFRAME> [TARGET_PRICE]`", parse_mode="Markdown")
            return

        raw_ticker = context.args[0].upper()
        is_global = ("=" in raw_ticker or "XAU" in raw_ticker or "USD" in raw_ticker or "^" in raw_ticker or raw_ticker.endswith(".BO"))
        ticker = raw_ticker if is_global else (raw_ticker if raw_ticker.endswith(".NS") else raw_ticker + ".NS")

        target_price = float(context.args[1])
        tf = context.args[2].lower()

        if tf not in VALID_TIMEFRAMES:
            await update.message.reply_text("❌ Invalid timeframe. Choose: `5m`, `15m`, `30m`, `45m`, `1h`, `4h`, `1d`", parse_mode="Markdown")
            return

        tp_price = float(context.args[3]) if len(context.args) > 3 else None
        
        df = fetch_candle_data(ticker, tf)
        if df.empty:
            await update.message.reply_text(f"❌ Could not fetch data for `{ticker}`. Check ticker symbol (e.g. `GREAVESCOT`).", parse_mode="Markdown")
            return

        current_price = float(df.iloc[-1]['Close'])
        direction = "ABOVE" if target_price >= current_price else "BELOW"
        currency = get_currency_symbol(ticker)
        fetch_ticker = get_fetch_ticker(ticker, tf)

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO alerts (chat_id, ticker, fetch_ticker, target_price, direction, timeframe, currency, tp_price, last_alerted_candle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ''', (chat_id, ticker, fetch_ticker, target_price, direction, tf, currency, tp_price))
        conn.commit()
        conn.close()

        reply_msg = (
            f"✅ *Alert Saved!*\n"
            f"• Stock: `{ticker}`\n"
            f"• CMP: `{currency}{current_price:.2f}`\n"
            f"• Trigger Level: `{currency}{target_price:.2f}` ({direction})\n"
            f"• Timeframe: `{tf}`"
        )
        if tp_price:
            reply_msg += f"\n• Target Price: `{currency}{tp_price:.2f}`"

        await update.message.reply_text(reply_msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in add_alert: {e}")
        await update.message.reply_text("❌ Format error. Syntax: `/alert <TICKER> <TRIGGER_PRICE> <TIMEFRAME> [TARGET_PRICE]`", parse_mode="Markdown")

async def add_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        if len(context.args) < 3:
            await update.message.reply_text("❌ Usage: `/trade <TICKER> <ENTRY_PRICE> <TARGET_PRICE>`", parse_mode="Markdown")
            return

        raw_ticker = context.args[0].upper()
        is_global = ("=" in raw_ticker or "XAU" in raw_ticker or "USD" in raw_ticker or "^" in raw_ticker or raw_ticker.endswith(".BO"))
        ticker = raw_ticker if is_global else (raw_ticker if raw_ticker.endswith(".NS") else raw_ticker + ".NS")

        entry_price = float(context.args[1])
        target_price = float(context.args[2])
        fetch_ticker = get_fetch_ticker(ticker, "1d")
        currency = get_currency_symbol(ticker)

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trades (chat_id, ticker, fetch_ticker, entry_price, target_price, currency, status, last_alerted_candle)
            VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', NULL)
        ''', (chat_id, ticker, fetch_ticker, entry_price, target_price, currency))
        conn.commit()
        conn.close()

        reply_msg = (
            f"🎯 *Trade Target Active!*\n"
            f"• Stock: `{ticker}`\n"
            f"• Entry Price: `{currency}{entry_price:.2f}`\n"
            f"• Target Price: `{currency}{target_price:.2f}` 🎯"
        )
        await update.message.reply_text(reply_msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in add_trade: {e}")
        await update.message.reply_text("❌ Format error. Syntax: `/trade <TICKER> <ENTRY_PRICE> <TARGET_PRICE>`", parse_mode="Markdown")

async def list_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT ticker, fetch_ticker, direction, currency, target_price, timeframe, tp_price FROM alerts WHERE chat_id = ?", (chat_id,))
        alerts = cursor.fetchall()

        cursor.execute("SELECT ticker, fetch_ticker, entry_price, target_price, currency FROM trades WHERE chat_id = ? AND status = 'ACTIVE'", (chat_id,))
        trades = cursor.fetchall()
        conn.close()

        if not alerts and not trades:
            await update.message.reply_text("No active alerts or trade targets.")
            return

        await update.message.reply_text("⏳ *Fetching live CMP for watchlist...*", parse_mode="Markdown")

        text = ""
        if alerts:
            text += "🚨 *Active Price Alerts:*\n"
            for idx, a in enumerate(alerts, 1):
                ticker, fetch_ticker, direction, curr, target_price, tf, tp_price = a
                try:
                    df = fetch_candle_data(ticker, tf)
                    cmp_val = float(df.iloc[-1]['Close']) if not df.empty else None
                    cmp_str = f"`{curr}{cmp_val:.2f}`" if cmp_val else "N/A"
                except Exception:
                    cmp_str = "N/A"

                tp_str = f" | Target: `{curr}{tp_price}`" if tp_price else ""
                text += f"{idx}. `{ticker}` | CMP: {cmp_str} | Trigger: {direction} `{curr}{target_price}` | TF: `{tf}`{tp_str}\n"
            text += "\n"

        if trades:
            text += "🎯 *Active Trade Targets:*\n"
            for idx, t in enumerate(trades, 1):
                ticker, fetch_ticker, entry_price, target, curr = t
                try:
                    df = fetch_candle_data(ticker, "1d")
                    cmp_val = float(df.iloc[-1]['Close']) if not df.empty else None
                    if cmp_val:
                        pnl_pct = ((cmp_val - entry_price) / entry_price) * 100
                        pnl_sign = "+" if pnl_pct >= 0 else ""
                        cmp_str = f"`{curr}{cmp_val:.2f}` ({pnl_sign}{pnl_pct:.2f}%)"
                    else:
                        cmp_str = "N/A"
                except Exception:
                    cmp_str = "N/A"

                text += f"{idx}. `{ticker}` | Entry: `{curr}{entry_price:.2f}` | CMP: {cmp_str} | Target: `{curr}{target:.2f}`\n"

        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in list_all: {e}")
        await update.message.reply_text("❌ Could not retrieve watchlist.", parse_mode="Markdown")

async def clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts WHERE chat_id = ?", (chat_id,))
    cursor.execute("DELETE FROM trades WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🧹 Cleared all active alerts and trade targets.")

async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, ticker, fetch_ticker, target_price, direction, timeframe, currency, tp_price, last_alerted_candle FROM alerts")
        alerts = cursor.fetchall()

        cursor.execute("SELECT id, chat_id, ticker, fetch_ticker, entry_price, target_price, currency, last_alerted_candle FROM trades WHERE status = 'ACTIVE'")
        trades = cursor.fetchall()
        conn.close()

        triggered_alerts = []
        updated_trades = []

        for alert in alerts:
            aid, chat_id, ticker, fetch_ticker, target, direction, tf, currency, tp_price, last_alerted_candle = alert
            try:
                df = fetch_candle_data(ticker, tf)
                if df.empty or len(df) < 2:
                    continue

                closed_df = df.iloc[:-1]
                last_closed = closed_df.iloc[-1]
                close_price = float(last_closed['Close'])
                candle_volume = float(last_closed['Volume'])
                candle_time = last_closed.name.strftime('%Y-%m-%d %H:%M')

                rsi_val = calculate_rsi(closed_df['Close'], period=14)
                vol_series = closed_df['Volume']
                vol_sma60 = float(vol_series.iloc[-60:].mean()) if len(vol_series) >= 60 else float(vol_series.mean())
                vol_ratio60 = (candle_volume / vol_sma60) if vol_sma60 > 0 else 1.0

                is_triggered = False
                if direction == "ABOVE" and close_price > target:
                    is_triggered = True
                elif direction == "BELOW" and close_price < target:
                    is_triggered = True

                if is_triggered and last_alerted_candle != candle_time:
                    formatted_vol = format_volume(candle_volume) if candle_volume > 0 else "N/A"
                    vol_output = f"{formatted_vol} ({vol_ratio60:.1f}x 60-SMA)" if candle_volume > 0 else "N/A"
                    tp_val = f"{currency}{tp_price:.2f}" if tp_price else "N/A"

                    msg = (
                        f"🚨 *ALERT TRIGGERED* 🚨\n\n"
                        f"• Stock: *{ticker}*\n"
                        f"• RSI: *{rsi_val}*\n"
                        f"• Volume: *{vol_output}*\n"
                        f"• Entry Price: *{currency}{close_price:.2f}*\n"
                        f"• Target Price: *{tp_val}*"
                    )
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    triggered_alerts.append(aid)
            except Exception as e:
                logging.error(f"Error scanning alert {ticker}: {e}")

        for trade in trades:
            tid, chat_id, ticker, fetch_ticker, entry_price, target, currency, last_alerted_candle = trade
            try:
                df = fetch_candle_data(ticker, "1d")
                if df.empty or len(df) < 2:
                    continue

                closed_df = df.iloc[:-1]
                last_closed = closed_df.iloc[-1]
                close_price = float(last_closed['Close'])
                candle_volume = float(last_closed['Volume'])
                candle_time = last_closed.name.strftime('%Y-%m-%d %H:%M')

                rsi_val = calculate_rsi(closed_df['Close'], period=14)
                vol_series = closed_df['Volume']
                vol_sma60 = float(vol_series.iloc[-60:].mean()) if len(vol_series) >= 60 else float(vol_series.mean())
                vol_ratio60 = (candle_volume / vol_sma60) if vol_sma60 > 0 else 1.0

                if close_price >= target and last_alerted_candle != candle_time:
                    pnl_pct = ((close_price - entry_price) / entry_price) * 100
                    formatted_vol = format_volume(candle_volume) if candle_volume > 0 else "N/A"
                    vol_output = f"{formatted_vol} ({vol_ratio60:.1f}x 60-SMA)" if candle_volume > 0 else "N/A"

                    msg = (
                        f"🎉 *TARGET ACHIEVED!* 🎯\n\n"
                        f"• Stock: *{ticker}*\n"
                        f"• Return: *+{pnl_pct:.2f}%* 📈\n"
                        f"• Target Price: *{currency}{target:.2f}*\n"
                        f"• Candle Close: *{currency}{close_price:.2f}*\n"
                        f"• RSI: *{rsi_val}*\n"
                        f"• Volume: *{vol_output}*"
                    )
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    updated_trades.append(("TARGET_ACHIEVED", candle_time, tid))
            except Exception as e:
                logging.error(f"Error scanning trade {ticker}: {e}")

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        if triggered_alerts:
            cursor.executemany("DELETE FROM alerts WHERE id = ?", [(aid,) for aid in triggered_alerts])
        if updated_trades:
            cursor.executemany("UPDATE trades SET status = ?, last_alerted_candle = ? WHERE id = ?", updated_trades)
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error in scanner_job: {e}")

if __name__ == "__main__":
    BOT_TOKEN = "8964779286:AAEJJfB49NFgVxR8zMOvImegNrlRaqEjcHA"

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("alert", add_alert))
    app.add_handler(CommandHandler("trade", add_trade))
    app.add_handler(CommandHandler("list", list_all))
    app.add_handler(CommandHandler("clear", clear_all))

    job_queue = app.job_queue
    job_queue.run_repeating(scanner_job, interval=60, first=5)

    print("🚀 Bot safely running with 45m Candle Resampling!")
    app.run_polling()
