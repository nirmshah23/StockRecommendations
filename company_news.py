"""Resolve Indian Yahoo listings and fetch news without sending email."""
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import pandas as pd
import yfinance as yf


def normalized(value):
    value = re.sub(r"\b(limited|ltd|private|pvt)\b", "", str(value).lower())
    return re.sub(r"[^a-z0-9]", "", value)


def indian_listing(quote):
    symbol = str(quote.get("symbol", "")).upper()
    exchange = quote.get("exchange", "")
    return ((symbol.endswith(".NS") and exchange == "NSI") or
            (symbol.endswith(".BO") and exchange == "BSE"))


def listing_base(ticker):
    # Yahoo retains NSE's SME segment suffix, which Screener omits.
    return re.sub(r"-SM$", "", str(ticker).upper().rsplit(".", 1)[0])


def resolve_ticker(name, symbol=""):
    """Require a matching name or known exchange symbol in Yahoo search results."""
    quotes = yf.Search(name, max_results=20, news_count=0).quotes
    base = re.sub(r"[-_](?:RR|X)$", "", symbol, flags=re.I).upper()
    if base and not any(indian_listing(q) and
                        listing_base(q.get("symbol", "")) == base for q in quotes):
        # Screener often abbreviates names; search its exchange identifier too.
        quotes += yf.Search(base, max_results=20, news_count=0).quotes
    matches = []
    for quote in quotes:
        if not indian_listing(quote):
            continue
        ticker = quote["symbol"].upper()
        names = [quote.get("shortname", ""), quote.get("longname", "")]
        if ((base and listing_base(ticker) == base) or
                any(normalized(name) == normalized(candidate) for candidate in names if candidate)):
            matches.append(ticker)
    # The same issuer may have both exchange listings. Multiple different NSE
    # (or BSE-only) matches remain unresolved instead of taking search rank 1.
    preferred = sorted({ticker for ticker in matches if ticker.endswith(".NS")})
    preferred = preferred or sorted(set(matches))
    return preferred[0] if len(preferred) == 1 else None


def save_mapping(mapping, path):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, delete=False) as stream:
            temporary = stream.name
            mapping.to_csv(stream, index=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def collect_company_news(groups, mapping_path="mapping.csv", days=7, count=10):
    """Groups contain (label, frame); portfolio frames must already be filtered."""
    mapping = pd.read_csv(mapping_path, dtype=str, keep_default_na=False)
    if "YahooTicker" not in mapping:
        mapping["YahooTicker"] = ""
    companies = {}
    for label, frame in groups:
        for row in frame.to_dict("records"):
            identifier = str(row.get("Company", "")).strip()
            symbol = str(row.get("Symbol", "") or "").strip()
            if symbol.lower() == "nan":
                symbol = ""
            matched = mapping.index[(mapping["Symbol"].eq(symbol) & bool(symbol)) |
                                    mapping["Symbol"].eq(identifier) |
                                    mapping["Name"].eq(identifier)]
            index = matched[0] if len(matched) == 1 else None
            name = mapping.at[index, "Name"] if index is not None else identifier
            if not name:
                continue
            symbol = mapping.at[index, "Symbol"] if index is not None else symbol
            key = normalized(name)
            company = companies.setdefault(key, dict(name=name, symbol=symbol, index=index, groups=[]))
            if label not in company["groups"]:
                company["groups"].append(label)

    results = []
    fetched = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for company in companies.values():
        name, index = company["name"], company["index"]
        result = dict(Company=name, Groups=", ".join(company["groups"]), YahooTicker="",
                      Status="", Articles=[])
        results.append(result)
        try:
            ticker = mapping.at[index, "YahooTicker"] if index is not None else ""
            if not re.fullmatch(r"[A-Z0-9&^_.-]+\.(NS|BO)", ticker):
                ticker = resolve_ticker(name, company["symbol"])
                if not ticker:
                    result["Status"] = "No unambiguous Indian listing found"
                    logging.warning("Yahoo listing unresolved: %s", name)
                    continue
                if index is None:
                    index = len(mapping)
                    mapping.loc[index] = {column: "" for column in mapping.columns}
                    mapping.at[index, "Name"] = name
                    mapping.at[index, "Symbol"] = company["symbol"]
                mapping.at[index, "YahooTicker"] = ticker
                save_mapping(mapping, mapping_path)
            result["YahooTicker"] = ticker
            if ticker not in fetched:
                fetched[ticker] = yf.Search(ticker, max_results=0, news_count=count).news
            seen = set()
            for item in fetched[ticker]:
                content = item.get("content") or item
                title = content.get("title", "")
                link = (content.get("canonicalUrl") or {}).get("url") or content.get("link", "")
                published = content.get("pubDate") or content.get("providerPublishTime")
                if not title or not link.startswith(("https://", "http://")) or link in seen:
                    continue
                try:
                    date = (datetime.fromtimestamp(published, timezone.utc) if isinstance(published, (int, float))
                            else datetime.fromisoformat(str(published).replace("Z", "+00:00")))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError, OverflowError):
                    continue
                if cutoff <= date <= datetime.now(timezone.utc):
                    provider = content.get("provider") or {}
                    result["Articles"].append(dict(Title=title, URL=link, Published=date.isoformat(),
                                                   Publisher=provider.get("displayName", content.get("publisher", ""))))
                    seen.add(link)
            result["Articles"].sort(key=lambda article: article["Published"], reverse=True)
            result["Status"] = "OK" if result["Articles"] else f"No news returned from the last {days} days"
        except Exception:
            result["Status"] = "Yahoo Finance lookup or news retrieval failed"
            logging.exception("Yahoo Finance news failed for %s", name)
    return results


def log_company_news(results):
    for result in results:
        logging.info("Company news: %s | %s | %s", result["Company"],
                     result["YahooTicker"] or "unresolved ticker", result["Groups"])
        if not result["Articles"]:
            logging.info("News status: %s", result["Status"])
        for article in result["Articles"]:
            logging.info("News: %s | %s | %s | %s", article["Published"],
                         article["Publisher"], article["Title"], article["URL"])


def news_html(results):
    parts = ["<h2>Recent Company News</h2><p>Yahoo Finance news from the last 7 days.</p>"]
    for result in results:
        parts.append(f'<h3>{escape(result["Company"])} ({escape(result["YahooTicker"])})</h3>'
                     f'<p>{escape(result["Groups"])}</p>')
        if not result["Articles"]:
            parts.append(f'<p>{escape(result["Status"])}</p>')
        for article in result["Articles"]:
            parts.append(f'<p><a href="{escape(article["URL"], quote=True)}">{escape(article["Title"])}</a>'
                         f' — {escape(article["Publisher"])} — {escape(article["Published"])}</p>')
    return "".join(parts)
