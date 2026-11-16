"""Download the newest available NSE combined security/delivery bhavcopy."""

import argparse
import csv
import io
import logging
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
HOSTS = ("https://nsearchives.nseindia.com", "https://archives.nseindia.com")
LOG = logging.getLogger(__name__)


class BhavcopyError(RuntimeError):
    """A download could not be verified."""


def parse_csv(content, day):
    """Validate structure; return only rows matching the filename's date."""
    try:
        rows = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), skipinitialspace=True, strict=True)
        if rows.fieldnames:
            rows.fieldnames = [name.strip() for name in rows.fieldnames]
        required = {"SYMBOL", "SERIES", "DATE1", "CLOSE_PRICE", "PREV_CLOSE", "DELIV_PER", "DELIV_QTY"}
        if not required.issubset(rows.fieldnames or []):
            raise BhavcopyError("Missing SEC columns")
        count, skipped = 0, 0
        valid, seen = [], set()
        for row in rows:
            count += 1
            if None in row or any(value is None for value in row.values()):
                raise BhavcopyError("Malformed SEC row")
            row = {key: value.strip() for key, value in row.items()}
            try:
                row_day = datetime.strptime(row["DATE1"], "%d-%b-%Y").date()
            except ValueError:
                row_day = None
            if row_day != day:
                skipped += 1
                continue
            key = row["SYMBOL"], row["SERIES"]
            if not all(key) or key in seen:
                raise BhavcopyError(f"Empty or duplicate security: {key}")
            seen.add(key)
            valid.append(row)
        if not count:
            raise BhavcopyError("Empty bhavcopy")
        if skipped:
            LOG.warning("%s: skipped %d rows with mismatched/invalid dates", day, skipped)
        return valid
    except (UnicodeError, csv.Error, KeyError) as exc:
        raise BhavcopyError(f"Invalid bhavcopy: {exc}") from exc


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_day(session, day, attempts=3):
    """Return None only for confirmed missing files; outages remain errors."""
    filename = f"sec_bhavdata_full_{day:%d%m%Y}.csv"
    errors = []
    for host in HOSTS:
        url = f"{host}/products/content/{filename}"
        for attempt in range(attempts):
            try:
                response = session.get(url, timeout=(10, 30))
                if response.status_code in (404, 410):
                    break
                response.raise_for_status()
                parse_csv(response.content, day)
                return response.content
            except (requests.RequestException, BhavcopyError) as exc:
                if attempt + 1 == attempts:
                    errors.append(f"{host}: {exc}")
                else:
                    time.sleep(2 ** attempt)
    if errors:
        raise BhavcopyError(f"Cannot establish availability for {day}: {'; '.join(errors)}")
    return None


def download_latest(output_dir, as_of=None, lookback=30, attempts=3):
    """Search all calendar days, including special weekend trading sessions."""
    if lookback < 1 or attempts < 1:
        raise ValueError("lookback and attempts must be positive")
    as_of = as_of or datetime.now(IST).date()
    output_dir = Path(output_dir)
    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/csv,application/octet-stream,*/*",
            "Referer": "https://www.nseindia.com/all-reports",
        })
        for offset in range(lookback):
            day = as_of - timedelta(days=offset)
            LOG.info("Checking %s", day)
            # Always check NSE first so a local cache cannot hide a newer file.
            content = fetch_day(session, day, attempts)
            if content is None:
                continue
            parse_csv(content, day)
            path = output_dir / f"sec_bhavdata_full_{day:%d%m%Y}.csv"
            atomic_write(path, content)
            return day, path
    raise BhavcopyError(f"No published bhavcopy found in {lookback} calendar days ending {as_of}")


def main():
    from dotenv import load_dotenv
    from .analysis import configuration, generate_reports

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "bhavcopy")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Search backwards from YYYY-MM-DD (default: today in IST)")
    parser.add_argument("--lookback", type=int, default=30)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        readings, rebuild, price_threshold, delivery_threshold, quantity_threshold, days = configuration()
        day, path = download_latest(args.output_dir, args.as_of, args.lookback, args.attempts)
        generate_reports(args.output_dir, day, readings, rebuild, price_threshold, delivery_threshold, days, quantity_threshold=quantity_threshold)
    except (BhavcopyError, OSError, ValueError) as exc:
        LOG.error("bhavAnalysis failed: %s", exc)
        return 1
    LOG.info("Latest available trading date: %s; CSV: %s", day, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
