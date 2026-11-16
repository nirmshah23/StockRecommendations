"""SEC price changes, delivery ratios, and incremental accumulation reports."""

import csv
import io
import logging
import math
import os
import re
from collections import defaultdict, deque
from datetime import datetime
from html import escape
from pathlib import Path

from .download_bhavcopy import BhavcopyError, atomic_write, parse_csv

LOG = logging.getLogger(__name__)
PREVIOUS_FIELDS = ["Date", "Symbol", "Series", "Close", "PreviousClose", "DeliveryPercentage", "PriceSMA",
          "DeliverySMA", "ReadingCount", "PriceChangePct", "DeliveryGrowth",
          "Interpretation", "SMAReadings"]
LEGACY_FIELDS = [field for field in PREVIOUS_FIELDS if field != "PreviousClose"]
LEGACY_FIELDS[LEGACY_FIELDS.index("PriceChangePct")] = "PriceGrowth"

QUANTITY_FIELDS = ["DeliveryQuantityValue", "DeliveryQuantitySMA", "DeliveryQuantityReadingCount", "DeliveryQuantity"]
FIELDS = PREVIOUS_FIELDS + QUANTITY_FIELDS
DAY_FIELDS = ("Date", "PriceChangePct", "DeliveryGrowth", "DeliveryQuantity", "Interpretation", "SMAReadings")

def report_fields(days):
    return ["ReportDate", "Symbol", "Series", "PriceChangeThreshold", "DeliveryGrowthThreshold", "DeliveryQuantityThreshold", "ConsecutiveDays"] + [
        f"{field}{i}" for i in range(1, days + 1)
        for field in DAY_FIELDS]


def configuration():
    try:
        readings = int(os.environ.get("BHAV_SMA_READINGS", "10"))
    except ValueError as exc:
        raise ValueError("BHAV_SMA_READINGS must be a positive integer") from exc
    if readings < 1:
        raise ValueError("BHAV_SMA_READINGS must be a positive integer")
    rebuild = os.environ.get("BHAV_RECALCULATE_HISTORY", "false").strip().lower()
    if rebuild not in {"true", "false"}:
        raise ValueError("BHAV_RECALCULATE_HISTORY must be true or false")
    thresholds = []
    for name in ("BHAV_PRICE_CHANGE_THRESHOLD", "BHAV_DELIVERY_GROWTH_THRESHOLD", "BHAV_DELIVERY_QUANTITY_THRESHOLD"):
        try:
            is_price = name == "BHAV_PRICE_CHANGE_THRESHOLD"
            threshold = float(os.environ.get(name, "0" if is_price else "1"))
            if not math.isfinite(threshold) or (not is_price and threshold <= 0):
                raise ValueError()
        except ValueError as exc:
            requirement = "finite number" if is_price else "finite positive number"
            raise ValueError(f"{name} must be a {requirement}") from exc
        thresholds.append(threshold)
    try:
        days = int(os.environ.get("BHAV_ACCUMULATION_DAYS", "3"))
        if days < 1:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("BHAV_ACCUMULATION_DAYS must be a positive integer") from exc
    return readings, rebuild == "true", *thresholds, days


def interpretation(price, delivery, quantity, price_threshold=0, delivery_threshold=1, quantity_threshold=1):
    above = (price > price_threshold, delivery > delivery_threshold, quantity > quantity_threshold)
    if all(above):
        return "Accumulation"
    if not any(above):
        return "Distribution"
    return "Speculative Trading"


def ratio(value, average):
    if value == "" or average == "" or average <= 0:
        return ""
    result = value / average
    return result if math.isfinite(result) else ""


def price_change_pct(close, raw_previous):
    """Return a validated previous close and percentage-point price change."""
    try:
        previous = float(raw_previous)
        if not math.isfinite(previous) or previous <= 0:
            return "", ""
        change = (close - previous) / previous * 100
        return (previous, change) if math.isfinite(change) else (previous, "")
    except (TypeError, ValueError, OverflowError):
        return "", ""


def csv_bytes(fields, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def generate_reports(output_dir, as_of, readings=10, rebuild=False,
                     price_threshold=0, delivery_threshold=1, days=3, quantity_threshold=1):
    """Use raw observations for windows; calculate only target dates."""
    root = Path(output_dir)
    sources = []
    for path in root.glob("sec_bhavdata_full_*.csv"):
        match = re.fullmatch(r"sec_bhavdata_full_(\d{8})\.csv", path.name)
        if match:
            try:
                day = datetime.strptime(match[1], "%d%m%Y").date()
            except ValueError:
                continue
            if day <= as_of:
                sources.append((day, path))
    sources.sort()
    if as_of not in {day for day, _ in sources}:
        raise BhavcopyError("Downloaded date has no source file")
    target_dates = {day for day, _ in sources[-readings:]} if rebuild else {as_of}
    target_labels = {day.isoformat() for day in target_dates}

    history_path = root / "analysis_history.csv"
    retained = []
    upgrade = False
    if history_path.exists():
        with history_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            upgrade = reader.fieldnames in (PREVIOUS_FIELDS, LEGACY_FIELDS)
            if reader.fieldnames != FIELDS and not upgrade:
                raise BhavcopyError("Invalid history schema; repair or remove analysis_history.csv")
            if upgrade:
                target_dates = {day for day, _ in sources[-readings:]}
                target_labels = {day.isoformat() for day in target_dates}
            for row in reader:
                if row["Date"] in target_labels:
                    continue
                if upgrade:
                    row.pop("PriceGrowth", None)
                    row = {field: row.get(field, "") for field in FIELDS}
                    row["Interpretation"] = ""
                retained.append(row)

    windows = defaultdict(lambda: deque(maxlen=readings))
    quantity_windows = defaultdict(lambda: deque(maxlen=readings))
    calculated, diagnostics = [], []
    if upgrade:
        message = "History upgraded: latest saved dates recalculated; older interpretations cleared"
        LOG.warning(message)
        diagnostics.append(message)
    for day, path in sources:
        try:
            rows = parse_csv(path.read_bytes(), day)
        except BhavcopyError as exc:
            if day == as_of:
                raise
            message = f"Excluded {path.name}: {exc}"
            LOG.warning(message)
            diagnostics.append(message)
            continue
        # Record date exclusions in the report as well as the parser log.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            raw_count = sum(1 for _ in csv.DictReader(handle, skipinitialspace=True))
        excluded = raw_count - len(rows)
        for row in rows:
            try:
                close, delivery = float(row["CLOSE_PRICE"]), float(row["DELIV_PER"])
                if not (math.isfinite(close) and close > 0 and
                        math.isfinite(delivery) and 0 <= delivery <= 100):
                    raise ValueError("Invalid observation")
            except ValueError:
                excluded += 1
                continue
            key = row["SYMBOL"], row["SERIES"]
            window = windows[key]
            quantity_window = quantity_windows[key]
            try:
                quantity = float(row["DELIV_QTY"])
                if not math.isfinite(quantity) or quantity < 0:
                    raise ValueError()
            except ValueError:
                quantity = ""
            # Read the prior windows before appending today's values, even for N=1.
            if day in target_dates:
                previous_close, price_change = price_change_pct(close, row["PREV_CLOSE"])
                price_sma = sum(value[0] / len(window) for value in window) if window else ""
                delivery_sma = sum(value[1] / len(window) for value in window) if window else ""
                quantity_sma = sum(value / len(quantity_window) for value in quantity_window) if quantity_window else ""
                delivery_growth = ratio(delivery, delivery_sma)
                quantity_ratio = ratio(quantity, quantity_sma)
                label = ""
                if all(value != "" for value in (price_change, delivery_growth, quantity_ratio)):
                    label = interpretation(price_change, delivery_growth, quantity_ratio,
                                           price_threshold, delivery_threshold, quantity_threshold)
                else:
                    message = f"{day} {key}: missing metric (invalid value, no prior observations, or zero SMA); interpretation unavailable"
                    LOG.warning(message)
                    diagnostics.append(message)
                calculated.append(dict(zip(FIELDS, [
                    day.isoformat(), *key, close, previous_close, delivery, price_sma, delivery_sma,
                    len(window), price_change, delivery_growth, label, readings,
                    quantity, quantity_sma, len(quantity_window), quantity_ratio,
                ])))
            window.append((close, delivery))
            if quantity != "":
                quantity_window.append(quantity)
        if excluded:
            message = f"{path.name}: excluded {excluded} date/value rows"
            LOG.warning(message)
            diagnostics.append(message)

    history = sorted(retained + calculated, key=lambda row: (row["Date"], row["Symbol"], row["Series"]))
    # Reclassify cached metrics without recomputing their prior-only SMAs.
    for row in history:
        row["Interpretation"] = ""
        if all(row[field] != "" for field in ("PriceChangePct", "DeliveryGrowth", "DeliveryQuantity")):
            row["Interpretation"] = interpretation(float(row["PriceChangePct"]), float(row["DeliveryGrowth"]),
                                                   float(row["DeliveryQuantity"]), price_threshold,
                                                   delivery_threshold, quantity_threshold)
    latest_dates = [day.isoformat() for day, _ in sources[-days:]]
    lookup = {(row["Date"], row["Symbol"], row["Series"]): row for row in history}
    report = []
    if len(latest_dates) == days:
        for current in calculated:
            if current["Date"] != as_of.isoformat():
                continue
            matches = [lookup.get((day, current["Symbol"], current["Series"])) for day in latest_dates]
            if not all(row and row["Interpretation"] == "Accumulation" for row in matches):
                continue
            item = dict(ReportDate=as_of.isoformat(), Symbol=current["Symbol"], Series=current["Series"],
                        PriceChangeThreshold=price_threshold, DeliveryGrowthThreshold=delivery_threshold, DeliveryQuantityThreshold=quantity_threshold,
                        ConsecutiveDays=days)
            for i, row in enumerate(matches, 1):
                for field in DAY_FIELDS:
                    item[f"{field}{i}"] = row[field]
            report.append(item)
    report.sort(key=lambda row: (row["Symbol"], row["Series"]))
    fields = report_fields(days)
    table = f"<p>No qualifying stocks. {days} consecutive calculated trading dates are required.</p>"
    if report:
        table = "<table><thead><tr>" + "".join(f"<th>{field}</th>" for field in fields)
        table += "</tr></thead><tbody>" + "".join(
            "<tr>" + "".join(f"<td>{escape(str(row[field]))}</td>" for field in fields) + "</tr>"
            for row in report) + "</tbody></table>"
    html = (f'<!doctype html><html><head><meta charset="utf-8"><title>Accumulation report</title></head>'
            f'<body><h1>Accumulation report — {as_of}</h1><p>SMA readings: {readings}; '
            f'price change threshold: {price_threshold}%; delivery threshold: {delivery_threshold}; quantity threshold: {quantity_threshold}; consecutive days: {days}; '
            f'mode: {"latest " + str(readings) + " files" if rebuild or upgrade else "incremental"}.</p>{table}<h2>Data diagnostics</h2>'
            + "".join(f"<p>{escape(message)}</p>" for message in diagnostics) + "</body></html>")
    # Calculate and serialize everything before publishing any outputs.
    outputs = [(history_path, csv_bytes(FIELDS, history)),
               (root / "accumulation_report.csv", csv_bytes(fields, report)),
               (root / "accumulation_report.html", html.encode("utf-8"))]
    for path, content in outputs:
        atomic_write(path, content)
    LOG.info("Calculated %d observations; %d accumulation matches", len(calculated), len(report))
    return history, report
