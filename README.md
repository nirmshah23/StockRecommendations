# Trade recommendation logic

## Local configuration

Personal holdings, trade imports, Screener session cookies, mapping data, caches,
and generated reports are stored in `data/` and excluded from Git. Supply your own `tradebook.csv`,
`mapping.csv`, `rsi_data.csv`, and Netscape-format `cookies.txt` using the input
columns documented below, placing these files in `data/`. The RSI cache must contain data from a previous run.

Before sending email, copy `.env.example` to `.env` beside
`trade_recommendations.py` and fill in your settings once:

```dotenv
SMTP_SENDER_EMAIL=you@gmail.com
SMTP_APP_PASSWORD=your-google-app-password
SMTP_RECEIVER_EMAIL=recipient@example.com
```

Use a Google app password. The receiver defaults to the sender when omitted.
The application automatically loads this file on every run, including runs via
the batch file or Task Scheduler. Values in `.env` take precedence over existing
environment variables. The file contains credentials and is excluded from Git.
Email credentials are optional for `--news-only`.

`trade_recommendations.py` combines tradebook holdings with current Screener.in prices and RSI, calculates price targets, and emails recommendations. `trade_recommendations.bat` runs this script. The notebook contains older logic; this document describes the Python script.

## Inputs

| Source | Fields used | Purpose |
| --- | --- | --- |
| `tradebook.csv` | `Symbol`, `TradeDate`, `Quantity`, `Price` | Holding quantities, invested amounts, holding ages, and targets |
| `mapping.csv` | `Symbol`, `Name`, optional `BuyRSI`, `SellRSI` | Match Screener companies to tradebook symbols and set per-stock RSI thresholds |
| Screener watchlist | `Company`, `CMP  Rs.`, `RSI`, `52w High  Rs.` | Current price, RSI, and 52-week high |
| `rsi_data.csv` | `Company`, `CMP`, `RSI`, `High_52W` | Previous successful run's price and RSI; the cached 52-week high is required but not used in recommendations |
| `cookies.txt` | Screener session cookies | Authenticate requests to Screener |

The watchlist is https://www.screener.in/watchlist/4986224/.

Company identifiers are matched against configured symbols, unique aliases with `-RR`, `_RR`, `-X`, or `_X` suffixes removed, and then display names. Unresolved companies are logged and excluded. Inner joins mean a stock must be present in the current data, previous data, mapping, and relevant tradebook holdings to appear in the portfolio report.

`Prev_CMP` and `Prev_RSI` come from the previous successful run, which is not necessarily the previous trading day. The script requires an existing cache to start.

## 1. Calculate a target for each tradebook row

The configured constants are:

| Parameter | Value |
| --- | --- |
| Compounding periods per year (`n`) | 12 |
| Admin/transaction fee | 2.5% |
| Short-term tax parameter | 20% |
| Profit target parameter | 20% |

These are application parameters used in the formulas below, not a calculation of actual tax liability.

For each row:

```text
Amt = Quantity × Price
t = elapsed days since TradeDate / 365
nt = t × 12

short_term_target_multiplier = 1 + 20 / (100 - 2.5 - 20)
                             ≈ 1.2580645

lower_threshold_multiplier = 1 + ((20 + 2.5) / 100) / 12
                           = 1.01875

upper_threshold_multiplier = 1 + (0.225 + 0.15) / 12
                           = 1.03125
```

The target amount depends on age:

```text
If t > 1:
    target = Amt × 1.01875 ** nt
Otherwise:
    target = Amt × 1.2580645

upper_threshold = Amt × 1.03125 ** nt
```

Thus, rows held for one year or less use a fixed target uplift of approximately 25.81%. Older rows use monthly compounding at a nominal annual rate of 22.5%. The upper threshold uses monthly compounding at a nominal annual rate of 37.5% for every row, regardless of age.

For example, a row with 10 shares bought at Rs. 100 and held for six months has an invested amount of Rs. 1,000 and a target amount of approximately Rs. 1,258.06. Its per-share target is approximately Rs. 125.81.

## 2. Aggregate by stock

The script groups tradebook rows by `Symbol` and calculates:

```text
Total_Qty = sum(Quantity)
Total_Amt = sum(Amt)
Target_Amt = sum(target)
Upper_Threshold_Amt = sum(upper_threshold)

Avg_Cost = Total_Amt / Total_Qty
Target = Target_Amt / Total_Qty
Upper_Threshold = Upper_Threshold_Amt / Total_Qty
Chg = ((CMP - Avg_Cost) / Avg_Cost) × 100
```

`Chg` is the percentage gain or loss relative to average cost, not the daily price change. `Total_Amt` is the invested amount, not the current market value. The code aggregates the supplied rows directly; it does not reconstruct remaining acquisition lots after sales.

Two portfolios are calculated independently:

- **Overall portfolio:** all tradebook rows.
- **Long-term portfolio:** only rows where `t > 1`, filtered before aggregation. A stock with both newer and older purchases can therefore have different costs, quantities, targets, and recommendations in the two reports.

## 3. Choose RSI thresholds

For each stock:

- `B` (oversold threshold) is its numeric `BuyRSI` from `mapping.csv`, or **30** if missing or nonnumeric.
- `S` (overbought threshold) is its numeric `SellRSI` from `mapping.csv`, or **70** if missing or nonnumeric.

The script uses RSI supplied by Screener; it does not calculate RSI from price history.

## 4. Apply recommendation rules in order

**The first matching rule wins.** All conditions in a row must be true. Calculations and comparisons use unrounded values; report values are rounded afterward.

| Priority | Recommendation | Conditions |
| --- | --- | --- |
| 1 | `SELL-SHORT-TERM` | `CMP >= Target`, `Target >= Upper_Threshold`, and `RSI > S` |
| 2 | `SELL-LONG-TERM-IF-COOLS-OFF` | (`Prev_RSI >= S` OR `RSI >= S`) and `Target <= CMP <= Upper_Threshold` |
| 3 | `SELL-LONG-TERM-BELOW-UPPER-THRESHOLD` | `CMP < Upper_Threshold`, `Prev_CMP >= Upper_Threshold`, and `CMP >= Target` |
| 4 | `BUY` | `RSI <= B`, `Prev_RSI <= B`, and `Target > High_52W` |
| 5 | `BUY-MAY-BE` | `RSI <= B` and `Prev_RSI <= B` |
| No match | Blank | No rule matched; the code does not emit an explicit `HOLD` label |

The first sell rule uses the relationship between target and upper threshold; it does not explicitly test holding age. The second flags a price within the target band when either the previous or current RSI is at or above the overbought threshold; it does not require RSI to have fallen below that threshold. The third compares the previous price with the **current run's** upper threshold and does not require any RSI condition.

Both buy rules require the current and previous RSI to be at or below the oversold threshold. Rule 4 returns `BUY` when the target is also above the latest 52-week high. Otherwise, if no earlier rule matched, rule 5 returns `BUY-MAY-BE`. The former rules based on invested-amount and loss limits remain removed. The script returns a label only: it does not calculate an order quantity or place trades.

Boundary comparisons matter: an RSI equal to `S` satisfies the RSI condition in rule 2, but not `RSI > S` in rule 1. Rule 2 includes both ends of the price band. Rule 4 requires `Target` to be strictly greater than `High_52W`.

## Email and saved outputs

The email contains:

1. Overall portfolio recommendations.
2. Long-term portfolio recommendations.
3. A separate **Dividend Yield Companies** table from https://www.screener.in/screens/2043645/dividend-yield-companies/, including all its columns and paginated results. This table is informational and does not affect recommendation calculations. Repeated header rows are removed. If retrieval fails, this section shows an unavailable-data notice and the portfolio email can still be sent.
4. A separate **Value Companies Showing Up Trend** table from https://www.screener.in/screens/3625642/value-companies-showing-up-trend/, using the same all-column, pagination, and repeated-header handling. This table is also informational and does not affect recommendations. If its retrieval fails, its section shows an unavailable-data notice independently of the dividend section.

The portfolio tables include identifiers, average cost, targets, current price, 52-week high, recommendation, percentage gain/loss, quantities, invested and target amounts, and previous/current RSI.

`rsi_today.csv` contains the merged current/previous Screener data and configured RSI thresholds and is attached with `trade_recommendations.log`. After the email is successfully sent, the current watchlist data replaces `rsi_data.csv` for use by the next run. A failed email send does not advance this cache.

## Importing new trades

Before reading holdings for calculations, the script checks for `data/new_trades.csv`.
If absent, it continues normally. Lowercase broker export headers such as
`symbol`, `trade_date`, and `trade_type` are mapped to the existing tradebook
columns. Identifiers are read as strings. Imports must contain new trades only;
the script does not maintain a persistent duplicate-trade ledger.

Rows are validated and processed chronologically by execution time, falling back
to trade date. Buys append holding lots. Sells consume the oldest buys for the
same symbol across exchanges, removing exhausted lots and reducing partially
consumed quantities without changing purchase prices or other purchase details.
Buys later than a sell cannot cover that sell. The saved tradebook contains only
remaining buy lots, so recommendations use the remaining holdings and costs.

Invalid dates, quantities, prices, trade types, or insufficient holdings abort
the entire update before either input changes. Quantities must be positive and
prices nonnegative. Existing tradebook rows must represent remaining buy lots.

After validation, the original is backed up as `tradebook.before_update.csv`
(replaced on the next successful update), and the new tradebook is saved using
a temporary file and atomic replacement. The import is then archived as
`new_trades_added_YYYY-MM-DD.csv`, using the run date in Asia/Kolkata. Additional
imports on the same date use `_2`, `_3`, etc.; existing archives are preserved.

If archiving fails after saving, the script stops before recommendations and
reports that the tradebook **was updated**. Manually rename the remaining
`new_trades.csv` to an unused archive filename before rerunning; otherwise the
same trades could be applied twice. A subsequent email failure does not undo a
completed tradebook import.

Run isolated import checks with `python -m unittest test_tradebook_update.py`.
These tests use temporary fixtures and do not send email or apply pending trades.

## Running

Install dependencies with `python -m pip install -r requirements.txt`.

Recent company news is fetched using Google News RSS for every `BUY` or
`BUY-MAY-BE` company in either portfolio and every company in the Dividend Yield
and Value Companies Showing Up Trend screens. Duplicate companies are fetched
once, with all matching groups shown. Company names from `mapping.csv` are used
when available; new screen companies use their displayed identifier. News does
not modify the mapping file or require a Yahoo listing or API key.

Searches use quoted company names with configurable terms and English language
(`hl=en`), without India country or edition parameters. No country restriction
is added; Google may still apply its own regional ranking. Configure `.env`:

```dotenv
NEWS_SEARCH_TERMS=stock,shares,company
NEWS_DAYS=14
```

Terms are comma separated, combined with OR; multiword terms are quoted.
An empty terms value searches only by company name. `NEWS_DAYS` must be an
integer between 1 and 365 and defaults to 14. Both the feed query and local
publication-date filter use this value. The run log includes up to ten headlines per
company from the configured lookback period (14 days by default), newest first, with publisher, date and Google
News article link. Duplicate links and headlines are removed. Invalid dates,
future articles, older articles and headlines missing the first distinctive
company-name word are excluded. Empty feeds and retrieval
failures are logged separately; one failed company does not stop the others.
Search results may include unrelated stories or miss abbreviated company names.
RSS supplies headlines and links, not full article text. Each request has a
30-second timeout. News remains in the run log attached to the email.

To retrieve and review news without sending email, importing trades, or
advancing `rsi_data.csv`, run:

```powershell
python trade_recommendations.py --news-only
```

This still fetches current Screener data using `cookies.txt`, writes
`rsi_today.csv` and replaces the run log.
Run isolated checks with
`python -m unittest test_company_news.py test_tradebook_update.py`.

Run from an environment with `beautifulsoup4`, `numpy`, `pandas`, `requests`, and a pandas HTML parser such as `lxml` installed:

```powershell
python trade_recommendations.py
```

The script reads and writes all inputs, caches, trade archives, reports, and logs in `data/` relative to its own directory. It creates this directory when needed. Keep `.env` beside the script. All data filenames above refer to files inside `data/`. Running it sends the email using the configured SMTP settings and updates the cache after successful delivery. Check `trade_recommendations.log` for errors and unresolved company mappings.
