# %%
import os
import re
import logging
import shutil
import tempfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urljoin, urlsplit
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime as d, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.cookiejar import MozillaCookieJar
import smtplib

from bs4 import BeautifulSoup  # pip install beautifulsoup4
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from company_news import collect_company_news, log_company_news

# set the working directory to the executable's location
os.chdir(os.path.dirname(os.path.abspath(__file__)))

LOG_FILE = "trade_recommendations.log"
RSI_FILE = "rsi_today.csv"
RSI_CACHE_FILE = "rsi_data.csv"
RSI_OVERBOUGHT_THRESHOLD = 70
RSI_OVERSOLD_THRESHOLD = 30
DIVIDEND_SCREEN_URL = "https://www.screener.in/screens/2043645/dividend-yield-companies/"
VALUE_SCREEN_URL = "https://www.screener.in/screens/3625642/value-companies-showing-up-trend/"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def attach_file(msg, filepath, mime_type, filename=None):
    if not os.path.exists(filepath):
        logging.warning("Attachment not found: %s", filepath)
        return

    maintype, subtype = mime_type.split("/", 1)
    with open(filepath, "rb") as f:
        part = MIMEBase(maintype, subtype)
        part.set_payload(f.read())
        encoders.encode_base64(part)

    part.add_header(
        "Content-Disposition",
        "attachment",
        filename=filename or os.path.basename(filepath),
    )
    msg.attach(part)


def send_error_email(sender_email, app_password, receiver_email, subject, body):
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    attach_file(msg, LOG_FILE, "text/plain")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, app_password)
        server.send_message(msg)

def add_symbol_from_mapping(df, mapping, name_column="Name"):
    """Resolve Screener's company identifier to the stable portfolio Symbol.

    Screener currently returns ticker symbols in the Company column, while
    mapping.csv contains both the older display Name and the stable Symbol.
    This supports both formats so a future Screener display-name change does
    not break the merge.
    """
    result = df.copy()
    raw_name = result[name_column].astype(str).str.strip()

    by_symbol = (
        mapping[["Symbol"]]
        .dropna()
        .drop_duplicates("Symbol")
        .set_index("Symbol")
        .index
    )
    name_to_symbol = (
        mapping[["Name", "Symbol"]]
        .dropna(subset=["Name", "Symbol"])
        .drop_duplicates("Name")
        .set_index("Name")["Symbol"]
    )

    # Current Screener format: Company is already the ticker symbol.
    result["Symbol"] = raw_name.where(raw_name.isin(by_symbol))

    # Some broker symbols in mapping.csv have suffixes such as -RR or -X,
    # while Screener may expose the base ticker. Build aliases only when the
    # stripped ticker maps uniquely to one configured Symbol.
    alias_candidates = {}
    for symbol in mapping["Symbol"].dropna().astype(str).str.strip().unique():
        base = re.sub(r"[-_](?:RR|X)$", "", symbol, flags=re.IGNORECASE)
        alias_candidates.setdefault(base, set()).add(symbol)
    unique_aliases = {
        base: next(iter(symbols))
        for base, symbols in alias_candidates.items()
        if len(symbols) == 1
    }

    missing = result["Symbol"].isna()
    result.loc[missing, "Symbol"] = raw_name[missing].map(unique_aliases)

    # Backward compatibility: older Screener format used the display Name.
    missing = result["Symbol"].isna()
    result.loc[missing, "Symbol"] = raw_name[missing].map(name_to_symbol)

    unresolved = result["Symbol"].isna()
    if unresolved.any():
        logging.warning(
            "Could not resolve %d Screener rows to a Symbol. Examples: %s",
            unresolved.sum(),
            raw_name[unresolved].head(10).tolist(),
        )

    return result


def fetch_screen(session, screen_url, include_symbols=False):
    pages = [screen_url]
    visited = set()
    frames = []
    screen = urlsplit(screen_url)
    while pages:
        url = pages.pop(0)
        if url in visited:
            continue
        visited.add(url)
        response = session.get(url, timeout=30, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        table = soup.select_one("table.data-table")
        if table is None:
            raise ValueError(f"Company table not found on screen page: {url}")
        frame = pd.read_html(StringIO(str(table)))[0]
        if "Company" not in frame.columns:
            raise ValueError(f"Company column not found on screen page: {url}")
        # Screener repeats column headings between groups of companies.
        company = frame["Company"].astype(str).str.strip()
        if include_symbols:
            symbols = {}
            for link in table.select('a[href^="/company/"]'):
                parts = urlsplit(link["href"]).path.strip("/").split("/")
                symbols[link.get_text(strip=True)] = parts[1]
            frame["Symbol"] = company.map(symbols)
        frames.append(frame[frame["Company"].notna() & company.ne("Company")])
        for link in soup.select("a[href]"):
            next_url = urljoin(response.url, link["href"])
            target = urlsplit(next_url)
            if (
                target.netloc == screen.netloc
                and target.path == screen.path
                and "page=" in target.query
                and next_url not in visited
                and next_url not in pages
            ):
                pages.append(next_url)
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def update_tradebook(tradebook_path="tradebook.csv", new_trades_path="new_trades.csv"):
    """Apply new trades to remaining buy lots; archive only after committing."""
    tradebook_path = Path(tradebook_path)
    new_trades_path = Path(new_trades_path)
    if not new_trades_path.exists():
        return

    book = pd.read_csv(tradebook_path, dtype=str, keep_default_na=False)
    incoming = pd.read_csv(new_trades_path, dtype=str, keep_default_na=False)
    normalize = lambda column: re.sub(r"[^a-z0-9]", "", column.lower())
    headers = {normalize(column): column for column in book.columns}
    incoming = incoming.rename(columns={
        column: headers.get(normalize(column), column) for column in incoming.columns
    })
    required = {"Symbol", "TradeDate", "Trade Type", "Quantity", "Price"}
    for label, frame in [("tradebook", book), ("new trades", incoming)]:
        if not frame.columns.is_unique or not required.issubset(frame.columns):
            raise ValueError(f"Invalid {label} columns; required: {sorted(required)}")
    unknown = set(incoming.columns) - set(book.columns)
    if unknown:
        raise ValueError(f"Unrecognized new trade columns: {sorted(unknown)}")

    def parse_row(row, label):
        row = dict(row)
        symbol = row["Symbol"].strip()
        kind = row["Trade Type"].strip().lower()
        try:
            quantity = Decimal(row["Quantity"])
            price = Decimal(row["Price"])
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError("quantity must be positive and finite")
            if not price.is_finite() or price < 0:
                raise ValueError("price must be nonnegative and finite")
            date = pd.Timestamp(row["TradeDate"])
            execution = row.get("Order Execution Time", "").strip()
            timestamp = pd.Timestamp(execution) if execution else date
            if pd.isna(date) or pd.isna(timestamp):
                raise ValueError("missing date")
            if date.tzinfo is not None or timestamp.tzinfo is not None:
                raise ValueError("dates must use local exchange time without timezone offsets")
            if not symbol or kind not in {"buy", "sell"}:
                raise ValueError("symbol is required and trade type must be buy or sell")
        except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
            raise ValueError(f"Invalid {label} for {symbol!r}: {exc}") from exc
        row.update(Symbol=symbol, TradeDate=date.strftime("%Y-%m-%d"))
        row["Trade Type"] = kind
        row["Quantity"] = str(quantity)
        return {"row": row, "qty": quantity, "date": date.normalize(), "time": timestamp}

    lots = [parse_row(row, f"tradebook row {i + 2}")
            for i, row in enumerate(book.to_dict("records"))]
    if any(lot["row"]["Trade Type"] != "buy" for lot in lots):
        raise ValueError("tradebook.csv must contain remaining buy lots only")
    trades = [parse_row(row, f"new trade row {i + 2}")
              for i, row in enumerate(incoming.to_dict("records"))]
    for trade in sorted(trades, key=lambda item: item["time"]):
        if trade["row"]["Trade Type"] == "buy":
            lots.append(trade)
            continue
        remaining = trade["qty"]
        eligible = sorted(
            (lot for lot in lots if lot["row"]["Symbol"] == trade["row"]["Symbol"]
             and lot["date"] <= trade["date"] and lot["time"] <= trade["time"]),
            key=lambda item: (item["date"], item["time"]),
        )
        for lot in eligible:
            consumed = min(lot["qty"], remaining)
            lot["qty"] -= consumed
            remaining -= consumed
            if remaining == 0:
                break
        if remaining:
            raise ValueError(
                f"Insufficient holdings for {trade['row']['Symbol']} sell on "
                f"{trade['row']['TradeDate']}: short by {remaining} shares; files unchanged"
            )

    rows = []
    for lot in lots:
        if lot["qty"] > 0:
            lot["row"]["Quantity"] = str(lot["qty"])
            rows.append(lot["row"])
    updated = pd.DataFrame(rows, columns=book.columns)
    run_date = d.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    archive = new_trades_path.with_name(f"new_trades_added_{run_date}.csv")
    suffix = 2
    while archive.exists():
        archive = new_trades_path.with_name(f"new_trades_added_{run_date}_{suffix}.csv")
        suffix += 1
    backup = tradebook_path.with_name("tradebook.before_update.csv")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", dir=tradebook_path.parent,
                                         encoding="utf-8", newline="", delete=False) as stream:
            temp_path = Path(stream.name)
            updated.to_csv(stream, index=False)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copy2(tradebook_path, backup)
        os.replace(temp_path, tradebook_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    try:
        # Exclusive creation prevents replacing an archive, even after a collision.
        with archive.open("xb") as destination, new_trades_path.open("rb") as source:
            shutil.copyfileobj(source, destination)
        new_trades_path.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Tradebook WAS updated, but archiving {new_trades_path} failed. "
            "Before rerunning, manually rename the remaining import to an unused archive "
            "filename to prevent applying it twice. Recommendations stopped."
        ) from exc
    logging.info("Updated tradebook: %d buys, %d sells, %d remaining lots; archive: %s",
                 sum(t["row"]["Trade Type"] == "buy" for t in trades),
                 sum(t["row"]["Trade Type"] == "sell" for t in trades), len(rows), archive)


def main(news_only=False):
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=True)
    setup_logging()
    logging.info("Starting trade recommendations run.")

    # parameters
    today_date = d.today()
    n = 12  # compounding periods per year
    admin_fee_pct = 2.5  # 2.5% admin / transaction fee
    st_tax_rate_pct = 20  # 20% tax on short term gains
    admin_and_tax = admin_fee_pct + st_tax_rate_pct
    profit_target_pct = 20  # 20% profit target before tax and admin
    short_term_target_multiplier = 1 + (profit_target_pct / (100 - admin_and_tax))
    lower_threshold = (profit_target_pct + admin_fee_pct) / 100
    lower_threshold_multiplier = (lower_threshold / n) + 1
    upper_threshold = lower_threshold + 0.15
    upper_threshold_multiplier = (upper_threshold / n) + 1

    sender_email = os.environ.get("SMTP_SENDER_EMAIL", "")
    app_password = os.environ.get("SMTP_APP_PASSWORD", "")
    receiver_email = os.environ.get("SMTP_RECEIVER_EMAIL", sender_email)
    if not news_only and not all((sender_email, app_password, receiver_email)):
        raise ValueError("Set SMTP_SENDER_EMAIL and SMTP_APP_PASSWORD in .env (SMTP_RECEIVER_EMAIL is optional).")

    if not os.path.exists("tradebook.csv"):
        raise FileNotFoundError("tradebook.csv not found.")

    if not news_only:
        update_tradebook()
    my_tb = pd.read_csv("tradebook.csv")
    logging.info("Loaded tradebook.csv with %d rows.", len(my_tb))

    tb = my_tb[["Symbol", "TradeDate", "Quantity", "Price"]].copy()
    tb["Amt"] = tb["Quantity"] * tb["Price"]

    tb["TradeDate"] = pd.to_datetime(tb["TradeDate"], errors="coerce")

    tb["t"] = (today_date - tb["TradeDate"]).dt.days / 365
    tb["nt"] = tb["t"] * n

    tb["target"] = np.where(
        tb["t"] > 1,
        tb["Amt"] * (lower_threshold_multiplier ** tb["nt"]),
        tb["Amt"] * short_term_target_multiplier,
    )
    tb["upper_threshold"] = tb["Amt"] * (upper_threshold_multiplier ** tb["nt"])

    # Read the RSI file stored yesterday. Screener now uses "Company"
    # instead of the older "Name" label, so keep "Company" consistently
    # throughout the RSI-processing logic.
    if not os.path.exists(RSI_CACHE_FILE):
        raise FileNotFoundError(f"{RSI_CACHE_FILE} not found.")

    prev_rsi_df = pd.read_csv(RSI_CACHE_FILE)

    # Current cache format contains:
    # Company,CMP,RSI,High_52W,Symbol
    # Also support the legacy four-column cache format.
    if "Company" in prev_rsi_df.columns:
        prev_rsi_df = prev_rsi_df.rename(columns={
            "CMP": "Prev_CMP",
            "RSI": "Prev_RSI",
        })
    elif "Name" in prev_rsi_df.columns:
        prev_rsi_df = prev_rsi_df.rename(columns={"Name": "Company"})
        prev_rsi_df = prev_rsi_df.rename(columns={
            "CMP": "Prev_CMP",
            "RSI": "Prev_RSI",
        })
    else:
        raise ValueError(
            f"Unexpected {RSI_CACHE_FILE} columns: "
            f"{prev_rsi_df.columns.tolist()}"
        )

    required_prev_columns = ["Company", "Prev_CMP", "Prev_RSI", "High_52W"]
    missing_prev_columns = [
        c for c in required_prev_columns if c not in prev_rsi_df.columns
    ]
    if missing_prev_columns:
        raise ValueError(
            f"Unexpected {RSI_CACHE_FILE} columns. Missing: "
            + ", ".join(missing_prev_columns)
            + f". Found: {prev_rsi_df.columns.tolist()}"
        )

    prev_rsi_df = prev_rsi_df[required_prev_columns]
    logging.info("Loaded %s with %d rows.", RSI_CACHE_FILE, len(prev_rsi_df))

    cookies_path = "cookies.txt"
    if not os.path.exists(cookies_path):
        raise FileNotFoundError("cookies.txt not found.")

    cj = MozillaCookieJar(cookies_path)
    cj.load(ignore_discard=True, ignore_expires=True)

    session = requests.Session()
    session.cookies = cj

    watchlist_url = "https://www.screener.in/watchlist/4986224/"
    try:
        r = session.get(watchlist_url, timeout=30, verify=False)
        r.raise_for_status()
    except requests.RequestException as exc:
        logging.exception("GET request failed for %s", watchlist_url)
        error_subject = "Trade Recommendations ERROR - {}".format(today_date.strftime("%Y-%m-%d"))
        error_body = (
            "Error while making GET request.\n"
            f"URL: {watchlist_url}\n"
            f"Error: {exc}\n"
        )
        if not news_only:
            try:
                send_error_email(sender_email, app_password, receiver_email, error_subject, error_body)
                logging.info("Error notification email sent.")
            except Exception:
                logging.exception("Failed to send error notification email.")
        raise

    logging.info("Fetched watchlist page, status %s.", r.status_code)

    my_soup = BeautifulSoup(r.content, "html.parser")
    #logging.info(my_soup)
    table = my_soup.select_one("table.data-table")
    if table is None:
        raise ValueError("RSI table not found on watchlist page.")

    if not os.path.exists("mapping.csv"):
        raise FileNotFoundError("mapping.csv not found.")

    mapping = pd.read_csv("mapping.csv")
    for rsi_column in ["BuyRSI", "SellRSI"]:
        if rsi_column not in mapping.columns:
            mapping[rsi_column] = np.nan
        mapping[rsi_column] = pd.to_numeric(mapping[rsi_column], errors="coerce")
    logging.info("Loaded mapping.csv with %d rows.", len(mapping))

    # Screener changed the Company column from the display name to the
    # ticker/Symbol (for example ITC, NTPC and UPL). Parse the table first,
    # then resolve the identifier to the stable Symbol used by tradebook.csv.
    raw_table = pd.read_html(StringIO(str(table)))[0]
    required_columns = ["Company", "CMP  Rs.", "RSI", "52w High  Rs."]
    missing_columns = [c for c in required_columns if c not in raw_table.columns]
    if missing_columns:
        raise ValueError(
            "Unexpected Screener table columns. Missing: "
            + ", ".join(missing_columns)
            + f". Found: {raw_table.columns.tolist()}"
        )

    df_rsi = raw_table[required_columns].copy()
    df_rsi = df_rsi[df_rsi["Company"].astype(str).str.strip() != "Company"]
    df_rsi.columns = ["Company", "CMP", "RSI", "High_52W"]

    df_rsi["Company"] = df_rsi["Company"].astype(str).str.strip()
    df_rsi["CMP"] = pd.to_numeric(df_rsi["CMP"], errors="coerce")
    df_rsi["RSI"] = pd.to_numeric(df_rsi["RSI"], errors="coerce")
    df_rsi["High_52W"] = pd.to_numeric(df_rsi["High_52W"], errors="coerce")
    df_rsi = add_symbol_from_mapping(df_rsi, mapping, name_column="Company")
    df_rsi = df_rsi.dropna(subset=["Symbol"])
    logging.info(
        "Parsed RSI table with %d rows; resolved %d rows to Symbols.",
        len(raw_table),
        len(df_rsi),
    )

    # Resolve the cached previous-day rows the same way. This also supports
    # rsi_data.csv files created before Screener changed its Company labels.
    prev_rsi_df["Company"] = prev_rsi_df["Company"].astype(str).str.strip()
    prev_rsi_df = add_symbol_from_mapping(prev_rsi_df, mapping, name_column="Company")
    prev_rsi_df = prev_rsi_df.dropna(subset=["Symbol"])

    # Merge on stable Symbol, not the Screener display label.
    rsi = (
        prev_rsi_df[["Symbol", "Prev_CMP", "Prev_RSI"]]
        .merge(
            df_rsi[["Symbol", "Company", "CMP", "RSI", "High_52W"]],
            on="Symbol",
            how="inner",
        )
        .merge(
            mapping[["Symbol", "BuyRSI", "SellRSI"]].drop_duplicates("Symbol"),
            on="Symbol",
            how="inner",
        )
    )
    logging.info("Merged RSI data with mapping: %d rows.", len(rsi))

    portfolio = build_portfolio(tb, rsi)
    portfolio_long_term = build_portfolio(tb[tb["t"] > 1], rsi)
    logging.info("Built portfolio summaries.")

    rsi.to_csv(RSI_FILE, index=False)
    logging.info("Wrote %s for email attachment.", RSI_FILE)

    subject = "Trade Recommendations - {}".format(today_date.strftime("%Y-%m-%d"))
    body_1 = portfolio.to_html(index=False)
    body_2 = portfolio_long_term.to_html(index=False)
    dividend_companies = pd.DataFrame()
    value_companies = pd.DataFrame()
    try:
        dividend_companies = fetch_screen(session, DIVIDEND_SCREEN_URL, include_symbols=True)
        dividend_body = dividend_companies.drop(columns="Symbol").to_html(index=False, na_rep="")
        logging.info("Fetched dividend screen with %d companies.", len(dividend_companies))
    except (requests.RequestException, ValueError):
        logging.exception("Failed to fetch dividend screen.")
        dividend_body = "<p>Dividend company data could not be retrieved for this run.</p>"
    try:
        value_companies = fetch_screen(session, VALUE_SCREEN_URL, include_symbols=True)
        value_body = value_companies.drop(columns="Symbol").to_html(index=False, na_rep="")
        logging.info("Fetched value screen with %d companies.", len(value_companies))
    except (requests.RequestException, ValueError):
        logging.exception("Failed to fetch value screen.")
        value_body = "<p>Value company data could not be retrieved for this run.</p>"

    news_groups = [
        ("Overall portfolio BUY / BUY-MAY-BE", portfolio[
            portfolio["Recommendation"].isin(["BUY", "BUY-MAY-BE"])]),
        ("Long-term portfolio BUY / BUY-MAY-BE", portfolio_long_term[
            portfolio_long_term["Recommendation"].isin(["BUY", "BUY-MAY-BE"])]),
        ("Dividend Yield Companies", dividend_companies),
        ("Value Companies Showing Up Trend", value_companies),
    ]
    company_news = collect_company_news(news_groups)
    log_company_news(company_news)
    if news_only:
        logging.info("News-only run complete: logged news for %d companies.",
                     len(company_news))
        return

    # Create message
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = subject
    msg.attach(MIMEText("Please find below the trade recommendations for overall portfolio.\n\n", "plain"))
    msg.attach(MIMEText(body_1, "html"))
    msg.attach(MIMEText("\n\n", "plain"))
    msg.attach(MIMEText("Please find below the trade recommendations for long term portfolio. \n\n", "plain"))
    msg.attach(MIMEText(body_2, "html"))
    msg.attach(MIMEText(
        '<h2>Dividend Yield Companies</h2>'
        f'<p>Source: <a href="{DIVIDEND_SCREEN_URL}">Screener.in dividend screen</a></p>'
        + dividend_body,
        "html",
    ))
    msg.attach(MIMEText(
        '<h2>Value Companies Showing Up Trend</h2>'
        f'<p>Source: <a href="{VALUE_SCREEN_URL}">Screener.in value screen</a></p>'
        + value_body,
        "html",
    ))

    attach_file(msg, LOG_FILE, "text/plain")
    attach_file(msg, RSI_FILE, "text/csv")

    # Send email
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, app_password)
        server.send_message(msg)

    logging.info("Email sent successfully.")

    # save latest rsi for use after recommendation email is successfully sent
    df_rsi.to_csv(RSI_CACHE_FILE, index=False)
    logging.info("Updated %s.", RSI_CACHE_FILE)

# %%
#rsi = pd.merge(prev_rsi_df, df_rsi, on='Name', how='inner')
#rsi

# %%
def recommendation(row):
    x = ''
    buy_rsi = pd.to_numeric(row.get('BuyRSI'), errors='coerce')
    sell_rsi = pd.to_numeric(row.get('SellRSI'), errors='coerce')
    oversold_threshold = buy_rsi if pd.notna(buy_rsi) else RSI_OVERSOLD_THRESHOLD
    overbought_threshold = sell_rsi if pd.notna(sell_rsi) else RSI_OVERBOUGHT_THRESHOLD

    if row['CMP'] >= row['Target'] and row['Target'] >= row['Upper_Threshold'] and row['RSI'] > overbought_threshold:
        #achieved short-term target
        x = 'SELL-SHORT-TERM'
    elif (row['Prev_RSI'] >= overbought_threshold or row['RSI'] >= overbought_threshold) and row['Target'] <= row['CMP'] <= row['Upper_Threshold']:
        # Price is within the target band and either RSI reading is overbought.
        x = 'SELL-LONG-TERM-IF-COOLS-OFF'
    elif row['CMP'] < row['Upper_Threshold'] and row['Prev_CMP'] >= row['Upper_Threshold'] and row['CMP'] >= row['Target']:
        #not a mult--bagger anymore since price has fallen below upper threshold
        x = 'SELL-LONG-TERM-BELOW-UPPER-THRESHOLD'
    elif row['RSI'] <= oversold_threshold and row['Prev_RSI'] <= oversold_threshold and row['Target'] > row['High_52W']:
        #undervalued and need to be bought to bring average cost down so that target goes below 52 week high
        x = 'BUY'
    elif row['RSI'] <= oversold_threshold and row['Prev_RSI'] <= oversold_threshold:
        x = 'BUY-MAY-BE'

    return x

# %%
def build_portfolio(tb_data, rsi_data):
    portfolio = tb_data.groupby('Symbol').agg(
        Total_Qty=('Quantity', 'sum'),
        Total_Amt=('Amt', 'sum'),
        Target_Amt=('target', 'sum'),
        Upper_Threshold_Amt=('upper_threshold', 'sum'),
    )

    portfolio['Avg_Cost'] = portfolio['Total_Amt'] / portfolio['Total_Qty']
    portfolio['Target'] = portfolio['Target_Amt'] / portfolio['Total_Qty']
    portfolio['Upper_Threshold'] = portfolio['Upper_Threshold_Amt'] / portfolio['Total_Qty']

    portfolio = pd.merge(portfolio, rsi_data, left_index=True, right_on='Symbol', how='inner')
    portfolio['Chg'] = ((portfolio['CMP'] - portfolio['Avg_Cost']) / portfolio['Avg_Cost']) * 100

    portfolio['Recommendation'] = portfolio.apply(recommendation, axis=1)
    portfolio = portfolio.round(2)
    return portfolio[[
        'Symbol',
        'Company',
        'Avg_Cost',
        'Target',
        'CMP',
        'High_52W',
        'Upper_Threshold',
        'Recommendation',
        'Chg',
        'Total_Qty',
        'Total_Amt',
        'Target_Amt',
        'Upper_Threshold_Amt',
        'Prev_RSI',
        'RSI'
    ]]



if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--news-only", action="store_true",
                        help="Log news without sending email, importing trades, or advancing the previous RSI cache")
    args = parser.parse_args()
    try:
        main(news_only=args.news_only)
    except Exception:
        logging.exception("Run failed.")
        raise
