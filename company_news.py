"""Fetch company headlines from Google News RSS for the run log."""
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from xml.etree import ElementTree

import pandas as pd
import requests


def normalized(value):
    value = re.sub(r"\b(limited|ltd|private|pvt)\b", "", str(value).lower())
    return re.sub(r"[^a-z0-9]", "", value)


def news_days():
    try:
        days = int(os.environ.get("NEWS_DAYS", "14"))
    except ValueError as error:
        raise ValueError("NEWS_DAYS must be a positive integer") from error
    if not 1 <= days <= 365:
        raise ValueError("NEWS_DAYS must be between 1 and 365")
    return days


def fetch_news(name, days, count):
    # Use the mapped company name: exchange symbols can be ambiguous words.
    terms = [term.strip().replace(chr(34), " ").strip()
             for term in os.environ.get("NEWS_SEARCH_TERMS", "stock,shares,company").split(",")]
    terms = list(dict.fromkeys(term for term in terms if term))
    term_query = "(" + " OR ".join(f'"{term}"' for term in terms) + ")" if terms else ""
    query = f'"{name.replace(chr(34), " ").strip()}" {term_query} when:{days}d'
    url = "https://news.google.com/rss/search?" + urlencode(
        dict(q=query, hl="en"))
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Google News returned an invalid RSS feed")
    name_words = re.findall(r"[a-z0-9]+", name.casefold())
    anchor = next((word for word in name_words if len(word) >= 3 and word not in {"limited", "ltd", "private", "pvt"}), "")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    articles, seen_links, seen_titles = [], set(), set()
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        publisher = (item.findtext("source") or "").strip()
        headline = title.removesuffix(" - " + publisher) if publisher else title
        if anchor and not re.search(r"\b" + re.escape(anchor) + r"\w*", headline.casefold()):
            continue
        title_key = re.sub(r"\W+", " ", headline.casefold()).strip()
        if not title or not link.startswith(("https://", "http://")):
            continue
        try:
            date = parsedate_to_datetime(item.findtext("pubDate") or "")
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            date = date.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        if not cutoff <= date <= now or link in seen_links or title_key in seen_titles:
            continue
        articles.append(dict(Title=headline, URL=link, Published=date.isoformat(), Publisher=publisher))
        seen_links.add(link)
        seen_titles.add(title_key)
    articles.sort(key=lambda article: article["Published"], reverse=True)
    return articles[:count]


def collect_company_news(groups, mapping_path=Path(__file__).resolve().parent / "data" / "mapping.csv", days=None, count=10):
    """Groups contain (label, frame); portfolio frames must already be filtered."""
    days = news_days() if days is None else days
    mapping = pd.read_csv(mapping_path, dtype=str, keep_default_na=False)
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
            company = companies.setdefault(key, dict(name=name, symbol=symbol, groups=[]))
            if label not in company["groups"]:
                company["groups"].append(label)

    results = []
    for company in companies.values():
        name = company["name"]
        result = dict(Company=name, Groups=", ".join(company["groups"]),
                      Symbol=company["symbol"], Status="", Articles=[])
        results.append(result)
        try:
            result["Articles"] = fetch_news(name, days, count)
            result["Status"] = "OK" if result["Articles"] else f"No news returned from the last {days} days"
        except Exception:
            result["Status"] = "Google News RSS retrieval failed"
            logging.exception("Google News RSS failed for %s", name)
    return results



def log_company_news(results):
    for result in results:
        logging.info("Company news: %s | %s | %s", result["Company"],
                     result["Symbol"] or "no symbol", result["Groups"])
        if not result["Articles"]:
            logging.info("News status: %s", result["Status"])
        for article in result["Articles"]:
            logging.info("News: %s | %s | %s | %s", article["Published"],
                         article["Publisher"], article["Title"], article["URL"])
