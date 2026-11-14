"""Download the newest available NSE cash-market UDiFF final bhavcopy."""

import argparse
import csv
import io
import logging
import os
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
HOSTS = ("https://nsearchives.nseindia.com", "https://archives.nseindia.com")
LOG = logging.getLogger(__name__)


class BhavcopyError(RuntimeError):
    """A download could not be verified."""


def validate_zip(content, day):
    """Check CRC, schema and every row's trading date before saving anything."""
    expected = f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv"
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.namelist() != [expected]:
                raise BhavcopyError("Unexpected archive contents")
            if archive.getinfo(expected).file_size > 100_000_000:
                raise BhavcopyError("CSV exceeds size limit")
            raw = archive.read(expected)  # Also verifies CRC.
        rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        required = {"TradDt", "TckrSymb", "ClsPric"}
        if not required.issubset(rows.fieldnames or []):
            raise BhavcopyError("Missing UDiFF columns")
        count = 0
        for row in rows:
            if row["TradDt"] != day.isoformat():
                raise BhavcopyError("CSV trading date does not match requested date")
            count += 1
        if not count:
            raise BhavcopyError("Empty bhavcopy")
        return expected, raw
    except (zipfile.BadZipFile, UnicodeError, csv.Error, KeyError) as exc:
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
    filename = f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    errors = []
    for host in HOSTS:
        url = f"{host}/content/cm/{filename}"
        for attempt in range(attempts):
            try:
                response = session.get(url, timeout=(10, 30))
                if response.status_code in (404, 410):
                    break
                response.raise_for_status()
                validate_zip(response.content, day)
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
            "Accept": "application/zip,application/octet-stream,*/*",
            "Referer": "https://www.nseindia.com/all-reports",
        })
        for offset in range(lookback):
            day = as_of - timedelta(days=offset)
            LOG.info("Checking %s", day)
            # Always check NSE first so a local cache cannot hide a newer file.
            content = fetch_day(session, day, attempts)
            if content is None:
                continue
            filename, raw = validate_zip(content, day)
            atomic_write(output_dir / (filename + ".zip"), content)
            path = output_dir / filename
            atomic_write(path, raw)
            return day, path
    raise BhavcopyError(f"No published bhavcopy found in {lookback} calendar days ending {as_of}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "bhavcopy")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Search backwards from YYYY-MM-DD (default: today in IST)")
    parser.add_argument("--lookback", type=int, default=30)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        day, path = download_latest(args.output_dir, args.as_of, args.lookback, args.attempts)
    except (BhavcopyError, OSError, ValueError) as exc:
        LOG.error("Download failed: %s", exc)
        return 1
    LOG.info("Latest available trading date: %s; CSV: %s", day, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
