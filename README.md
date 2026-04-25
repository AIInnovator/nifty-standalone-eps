# Nifty 50 Standalone TTM EPS

Calculate standalone trailing-twelve-month EPS for the current Nifty 50 constituents using NSE filings, then calculate Nifty 50 standalone EPS using free-float market capitalization.

The project fetches the live Nifty 50 constituent list, prices, and free-float market capitalization from NSE, extracts standalone quarterly EPS from NSE Integrated Filing XBRL/iXBRL files, and generates JSON, CSV, and HTML reports.

## What It Calculates

- Latest standalone quarterly EPS for each current Nifty 50 company.
- Standalone TTM EPS for each company by summing the latest four standalone quarterly EPS values.
- Free-float earnings contribution for each constituent.
- Nifty 50 standalone TTM EPS and PE using aggregate free-float market capitalization and earnings.

This is not the official NSE published index EPS. It is a reproducible standalone-financials estimate based on the methodology and source data described below.

## Methodology

### Constituent And Market Data

The script fetches the current Nifty 50 constituents, prices, and FFMC from:

```text
https://www.nseindia.com/api/equity-stockIndices?index=NIFTY+50
```

The NSE payload provides:

- `symbol`: current constituent symbol
- `lastPrice`: latest/closing price in the index snapshot
- `ffmc`: free-float market capitalization for the constituent
- index value and timestamp metadata

### Company EPS

For each current Nifty 50 symbol, the script fetches NSE Integrated Filing - Financials:

```text
https://www.nseindia.com/api/integrated-filing-results
```

It filters to `Standalone` filings, keeps the latest filing per quarter, and extracts reported quarterly EPS from XBRL first. If XML is unavailable or invalid, it falls back to iXBRL HTML.

Standalone TTM EPS is:

```text
standalone_ttm_eps_i = sum(latest four standalone quarterly EPS values for company i)
```

### Nifty EPS

Nifty EPS cannot be calculated by adding or equal-weight averaging constituent EPS. It must be aggregated through free-float market capitalization and earnings.

The script uses:

```text
free_float_shares_proxy_i = ffmc_i / lastPrice_i
free_float_ttm_earnings_i = standalone_ttm_eps_i * free_float_shares_proxy_i

index PE = sum(ffmc_i) / sum(free_float_ttm_earnings_i)
index EPS = index_last_value / index PE
```

Equivalent compact form:

```text
free_float_ttm_earnings_i = ffmc_i * standalone_ttm_eps_i / lastPrice_i
```

## Methodology Sources

- NSE index P/E methodology: https://www.nseindia.com/static/products-services/indices-price-earnings-ratio
- Nifty 50 methodology document: https://nsearchives.nseindia.com/content/indices/Method_Nifty_50.pdf
- NSE investible weight factors / free-float explanation: https://www.nseindia.com/products-services/indices-investible-weight-factors
- NSE Nifty 50 index API used for current symbols, prices, and FFMC: https://www.nseindia.com/api/equity-stockIndices?index=NIFTY+50
- NSE Integrated Filing API used for financial filings: https://www.nseindia.com/api/integrated-filing-results

## Repository Layout

```text
.
├── README.md
├── requirements.txt
├── calculate_standalone_ttm_eps.py
└── output/
    └── latest/
        ├── nifty50_standalone_ttm_eps_report.html
        ├── nifty50_standalone_ttm_eps.json
        ├── companies_ttm_eps.csv
        ├── index_eps_calculation.csv
        ├── quarterly_eps.csv
        └── filing_extraction_audit.csv
```

## Quick Start

Clone the repository:

```bash
git clone https://github.com/AIInnovator/nifty-standalone-eps.git
cd nifty-standalone-eps
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the calculation:

```bash
python3 calculate_standalone_ttm_eps.py
```

Open the generated HTML report:

```text
output/latest/nifty50_standalone_ttm_eps_report.html
```

## Outputs

Every run writes these files to `output/latest/`:

- `nifty50_standalone_ttm_eps_report.html`: professional self-contained HTML report.
- `nifty50_standalone_ttm_eps.json`: full structured output with methodology, index snapshot, company data, and calculation details.
- `companies_ttm_eps.csv`: company-level TTM EPS plus FFMC weight, price, PE, and earnings contribution.
- `index_eps_calculation.csv`: constituent-level free-float earnings calculation used for Nifty EPS.
- `quarterly_eps.csv`: the four EPS facts used for every company.
- `filing_extraction_audit.csv`: attempted filings, source URLs, tags/rows used, and extraction errors if any.
- `companies/*.json`: one detailed trace file per symbol.
- `raw/index`, `raw/api`, `raw/filings`: cached NSE source responses and filings for traceability.

## Useful Commands

Force a fresh NSE fetch instead of using cached files:

```bash
python3 calculate_standalone_ttm_eps.py --refresh
```

Run only a few symbols for validation:

```bash
python3 calculate_standalone_ttm_eps.py --symbols HDFCBANK,RELIANCE,INFY
```

Write to a different output directory:

```bash
python3 calculate_standalone_ttm_eps.py --output-dir runs/$(date +%Y%m%d)
```

Use a different NSE index name supported by the same NSE endpoint:

```bash
python3 calculate_standalone_ttm_eps.py --index-name "NIFTY NEXT 50"
```

## Optional CSV Fallback

The default run uses live NSE index constituents and does not require a CSV file. If you want to run from a local constituent file instead, pass:

```bash
python3 calculate_standalone_ttm_eps.py --constituents-source csv --input path/to/ind_nifty50list.csv
```

## Notes And Limitations

- The script calculates standalone EPS because this repository is focused on standalone financials. NSE's published P/E methodology generally uses consolidated earnings where available, with standalone as a fallback.
- NSE endpoints can throttle or block automated traffic. The script uses browser-like headers, retries, and local caching, but a rerun may be needed if NSE temporarily rejects a request.
- Current constituents come from NSE at run time. A CSV can be supplied only as an explicit fallback.
- This is an analytical tool, not investment advice.
