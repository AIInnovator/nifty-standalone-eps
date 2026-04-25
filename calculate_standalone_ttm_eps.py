#!/usr/bin/env python3
"""Calculate standalone TTM EPS for Nifty 50 constituents from NSE filings.

The workflow fetches current index constituents from NSE, reads NSE Integrated
Filing - Financials rows, keeps standalone filings, extracts reported quarterly
EPS from XBRL/iXBRL, and writes traceable JSON/CSV/HTML outputs.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


NSE_API = "https://www.nseindia.com/api/integrated-filing-results"
INDEX_API = "https://www.nseindia.com/api/equity-stockIndices"
NSE_HOME = "https://www.nseindia.com/"
FILING_TYPE = "Integrated Filing- Financials"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-integrated-filing",
}

EPS_BASIC_TAG_GROUPS = [
    ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"],
    ["BasicEarningsPerShareAfterExtraordinaryItems"],
    ["BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"],
    ["BasicEarningsLossPerShareFromContinuingOperations"],
    ["BasicEarningsPerShareBeforeExtraordinaryItems"],
    ["BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"],
]

EPS_DILUTED_TAG_GROUPS = [
    ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"],
    ["DilutedEarningsPerShareAfterExtraordinaryItems"],
    ["BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"],
    ["DilutedEarningsLossPerShareFromContinuingOperations"],
    ["DilutedEarningsPerShareBeforeExtraordinaryItems"],
    ["BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"],
]

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output" / "latest"


@dataclass(frozen=True)
class Company:
    name: str
    industry: str
    symbol: str
    series: str
    isin: str


@dataclass(frozen=True)
class IndexSnapshot:
    name: str
    timestamp: str | None
    market_status: dict[str, Any]
    metadata: dict[str, Any]
    index_row: dict[str, Any]
    constituents: list[dict[str, Any]]


@dataclass(frozen=True)
class Filing:
    symbol: str
    company_name: str
    seq_id: str
    qe_date: date
    type_sub: str | None
    audited: str | None
    consolidated: str | None
    broadcast_date: str | None
    creation_date: str | None
    xbrl_url: str | None
    ixbrl_url: str | None
    xbrl_file_size: str | None
    ixbrl_file_size: str | None
    raw: dict[str, Any]


@dataclass
class EpsFact:
    eps_basic: float | None = None
    eps_diluted: float | None = None
    period_start: str | None = None
    period_end: str | None = None
    context_ref: str | None = None
    context_duration_days: int | None = None
    source_format: str | None = None
    source_url: str | None = None
    eps_basic_tag: str | None = None
    eps_diluted_tag: str | None = None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate standalone TTM EPS for Nifty 50 constituents from NSE XBRL filings."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where raw files, JSON, and CSV sheets will be written.",
    )
    parser.add_argument(
        "--index-name",
        default="NIFTY 50",
        help="NSE index name used for live constituent and FFMC data.",
    )
    parser.add_argument(
        "--symbols",
        help="Optional comma-separated symbol filter for validation runs, for example ADANIENT,HDFCBANK.",
    )
    parser.add_argument("--size", type=int, default=20, help="NSE API page size.")
    parser.add_argument("--max-pages", type=int, default=4, help="Maximum NSE API pages per symbol.")
    parser.add_argument(
        "--filings-to-try",
        type=int,
        default=10,
        help="Standalone filings to try per symbol until four valid quarters are found.",
    )
    parser.add_argument("--sleep", type=float, default=0.25, help="Delay between NSE requests.")
    parser.add_argument("--timeout", type=float, default=35.0, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retries per request.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached raw API and filing files.")
    parser.add_argument(
        "--no-home-warmup",
        action="store_true",
        help="Skip the initial NSE homepage request used to establish cookies.",
    )
    return parser.parse_args()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def norm_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def parse_float(value: Any) -> float | None:
    text = clean_text(value)
    if not text or text.lower() in {"na", "nan", "nil", "-", "--"}:
        return None
    text = text.replace(",", "")
    text = re.sub(r"(?i)\brs\.?\b", "", text)
    text = text.replace("(", "-").replace(")", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def round_or_none(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def parse_qe_date(value: Any) -> date:
    text = clean_text(value).title()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unable to parse quarter-end date: {value!r}")


def parse_any_date(value: Any) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.replace("/", "-").title()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_nse_datetime(value: Any) -> datetime | None:
    text = clean_text(value).title()
    if not text:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%B-%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "file"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def company_from_index_row(row: dict[str, Any]) -> Company:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    return Company(
        name=clean_text(meta.get("companyName") or row.get("identifier") or row.get("symbol")),
        industry=clean_text(meta.get("industry")),
        symbol=clean_text(row.get("symbol")),
        series=clean_text(row.get("series") or "EQ"),
        isin=clean_text(meta.get("isin")),
    )


def is_index_constituent_row(row: dict[str, Any], index_name: str) -> bool:
    symbol = clean_text(row.get("symbol"))
    return bool(symbol and symbol.upper() != index_name.upper() and row.get("ffmc") is not None)


class NSEClient:
    def __init__(self, timeout: float, retries: int, sleep_seconds: float) -> None:
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.timeout = timeout
        self.retries = retries
        self.sleep_seconds = sleep_seconds

    def warm_up(self) -> None:
        try:
            self.session.get(NSE_HOME, timeout=self.timeout)
        except requests.RequestException:
            pass

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout, **kwargs)
                if response.status_code in {401, 403} and url != NSE_HOME:
                    self.warm_up()
                    response = self.session.get(url, timeout=self.timeout, **kwargs)
                if response.status_code >= 500 and attempt < self.retries:
                    raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
                time.sleep(self.sleep_seconds)
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.sleep_seconds * attempt)
        assert last_error is not None
        raise last_error

    def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.get(url, **kwargs)
        response.raise_for_status()
        return response.json()


def read_cached_json(path: Path, refresh: bool) -> dict[str, Any] | None:
    if refresh or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_index_snapshot(
    client: NSEClient,
    index_name: str,
    output_dir: Path,
    refresh: bool,
) -> IndexSnapshot:
    index_dir = output_dir / "raw" / "index"
    latest_path = index_dir / f"{safe_name(index_name)}_latest.json"

    payload: dict[str, Any] | None = None
    if not refresh:
        try:
            payload = client.get_json(INDEX_API, params={"index": index_name})
        except Exception:
            payload = read_cached_json(latest_path, refresh=False)
            if payload is None:
                raise
    if payload is None:
        payload = client.get_json(INDEX_API, params={"index": index_name})

    write_json(latest_path, payload)
    generated_suffix = safe_name(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    write_json(index_dir / f"{safe_name(index_name)}_{generated_suffix}.json", payload)

    data = payload.get("data") or []
    if not isinstance(data, list):
        raise ValueError("Expected NSE index payload to contain a data list")

    constituents = [row for row in data if isinstance(row, dict) and is_index_constituent_row(row, index_name)]
    index_row = next(
        (
            row
            for row in data
            if isinstance(row, dict) and clean_text(row.get("symbol")).upper() == index_name.upper()
        ),
        {},
    )
    if not constituents:
        raise ValueError(f"No constituents found in NSE index payload for {index_name}")

    return IndexSnapshot(
        name=clean_text(payload.get("name") or index_name),
        timestamp=clean_text(payload.get("timestamp")) or None,
        market_status=payload.get("marketStatus") if isinstance(payload.get("marketStatus"), dict) else {},
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        index_row=index_row,
        constituents=constituents,
    )


def companies_from_index_snapshot(snapshot: IndexSnapshot) -> list[Company]:
    companies = [company_from_index_row(row) for row in snapshot.constituents]
    if any(not company.symbol for company in companies):
        raise ValueError("One or more NSE index constituents did not include a symbol")
    return companies


def filter_index_snapshot(snapshot: IndexSnapshot, symbols: str | None) -> IndexSnapshot:
    if not symbols:
        return snapshot
    wanted = {item.strip().upper() for item in symbols.split(",") if item.strip()}
    filtered = [
        row
        for row in snapshot.constituents
        if clean_text(row.get("symbol")).upper() in wanted
    ]
    missing = sorted(wanted - {clean_text(row.get("symbol")).upper() for row in filtered})
    if missing:
        raise ValueError(f"Symbols not found in NSE index payload: {', '.join(missing)}")
    return IndexSnapshot(
        name=snapshot.name,
        timestamp=snapshot.timestamp,
        market_status=snapshot.market_status,
        metadata=snapshot.metadata,
        index_row=snapshot.index_row,
        constituents=filtered,
    )


def fetch_filings_page(
    client: NSEClient,
    symbol: str,
    page: int,
    size: int,
    cache_path: Path,
    refresh: bool,
) -> dict[str, Any]:
    cached = read_cached_json(cache_path, refresh)
    if cached is not None:
        return cached

    payload = client.get_json(
        NSE_API,
        params={"symbol": symbol, "type": FILING_TYPE, "page": page, "size": size},
    )
    write_json(cache_path, payload)
    return payload


def fetch_all_filings(
    client: NSEClient,
    symbol: str,
    api_dir: Path,
    size: int,
    max_pages: int,
    refresh: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_count: int | None = None

    for page in range(1, max_pages + 1):
        cache_path = api_dir / f"{safe_name(symbol)}_page_{page}.json"
        payload = fetch_filings_page(client, symbol, page, size, cache_path, refresh)
        page_rows = payload.get("data") or []
        if not isinstance(page_rows, list):
            break
        rows.extend(item for item in page_rows if isinstance(item, dict))
        if total_count is None:
            try:
                total_count = int(payload.get("totalCount"))
            except (TypeError, ValueError):
                total_count = None
        if total_count is not None and len(rows) >= total_count:
            break
        if len(page_rows) < size:
            break

    return rows


def filing_from_row(row: dict[str, Any]) -> Filing:
    return Filing(
        symbol=clean_text(row.get("symbol")),
        company_name=clean_text(row.get("smName") or row.get("cmName")),
        seq_id=clean_text(row.get("seq_Id")),
        qe_date=parse_qe_date(row.get("qe_Date")),
        type_sub=clean_text(row.get("type_Sub")) or None,
        audited=clean_text(row.get("audited")) or None,
        consolidated=clean_text(row.get("consolidated")) or None,
        broadcast_date=clean_text(row.get("broadcast_Date")) or None,
        creation_date=clean_text(row.get("creation_Date")) or None,
        xbrl_url=clean_text(row.get("xbrl")) or None,
        ixbrl_url=clean_text(row.get("ixbrl")) or None,
        xbrl_file_size=clean_text(row.get("xbrlFileSize")) or None,
        ixbrl_file_size=clean_text(row.get("ixbrlFileSize")) or None,
        raw=row,
    )


def is_standalone(row: dict[str, Any]) -> bool:
    return norm_text(row.get("consolidated")) == "standalone"


def latest_timestamp(filing: Filing) -> datetime:
    return (
        parse_nse_datetime(filing.creation_date)
        or parse_nse_datetime(filing.broadcast_date)
        or datetime.min
    )


def choose_latest_standalone(rows: list[dict[str, Any]]) -> list[Filing]:
    by_quarter: dict[date, Filing] = {}
    for row in rows:
        if not is_standalone(row):
            continue
        try:
            filing = filing_from_row(row)
        except ValueError:
            continue
        existing = by_quarter.get(filing.qe_date)
        if existing is None or latest_timestamp(filing) > latest_timestamp(existing):
            by_quarter[filing.qe_date] = filing

    return sorted(by_quarter.values(), key=lambda item: item.qe_date, reverse=True)


def fetch_cached_bytes(
    client: NSEClient,
    url: str,
    cache_path: Path,
    refresh: bool,
    require_xml: bool = False,
) -> tuple[bytes | None, int | None, str | None]:
    if not refresh and cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_bytes(), 200, str(cache_path)

    response = client.get(url)
    status = response.status_code
    content = response.content
    if status == 200 and content:
        if require_xml:
            prefix = content[:200].lstrip().lower()
            if not (prefix.startswith(b"<?xml") or prefix.startswith(b"<xbrli:xbrl")):
                return content, status, None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
    return content, status, str(cache_path) if status == 200 else None


def context_map(root: ET.Element) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for element in root.iter():
        if local_name(element.tag) != "context":
            continue
        context_id = element.attrib.get("id")
        if not context_id:
            continue

        start: date | None = None
        end: date | None = None
        instant: date | None = None
        for child in element.iter():
            name = local_name(child.tag)
            if name == "startDate":
                start = parse_any_date(child.text)
            elif name == "endDate":
                end = parse_any_date(child.text)
            elif name == "instant":
                instant = parse_any_date(child.text)

        duration_days = (end - start).days + 1 if start and end else None
        contexts[context_id] = {
            "start": start,
            "end": end or instant,
            "instant": instant,
            "duration_days": duration_days,
        }
    return contexts


def fact_score(context_ref: str | None, contexts: dict[str, dict[str, Any]], qe_date: date) -> int:
    score = 0
    if not context_ref:
        return score

    context = contexts.get(context_ref, {})
    end = context.get("end")
    duration = context.get("duration_days")
    lower_ref = context_ref.lower()

    if end == qe_date:
        score += 1000
    if duration is not None:
        if 75 <= duration <= 100:
            score += 500
        elif 60 <= duration <= 120:
            score += 250
        elif duration > 120:
            score -= min(duration, 400)
        score += max(0, 100 - abs(duration - 91))
    if context_ref == "OneD":
        score += 150
    elif lower_ref.startswith("one") and lower_ref.endswith("d"):
        score += 80
    elif context_ref == "FourD":
        score -= 100
    if "segment" in lower_ref or "expenses" in lower_ref:
        score -= 100
    return score


def choose_xbrl_fact(
    root: ET.Element,
    contexts: dict[str, dict[str, Any]],
    tag_groups: list[list[str]],
    qe_date: date,
) -> tuple[float | None, str | None, str | None]:
    for tag_group in tag_groups:
        candidates: list[tuple[int, float, str, str | None]] = []
        for element in root.iter():
            tag = local_name(element.tag)
            if tag not in tag_group:
                continue
            value = parse_float(element.text)
            if value is None:
                continue
            context_ref = element.attrib.get("contextRef")
            candidates.append((fact_score(context_ref, contexts, qe_date), value, tag, context_ref))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            _, value, tag, context_ref = candidates[0]
            return value, tag, context_ref
    return None, None, None


def parse_xbrl(content: bytes, source_url: str, qe_date: date) -> EpsFact:
    root = ET.fromstring(content)
    contexts = context_map(root)
    basic, basic_tag, basic_context_ref = choose_xbrl_fact(root, contexts, EPS_BASIC_TAG_GROUPS, qe_date)
    diluted, diluted_tag, diluted_context_ref = choose_xbrl_fact(
        root, contexts, EPS_DILUTED_TAG_GROUPS, qe_date
    )
    context_ref = basic_context_ref or diluted_context_ref
    selected_context = contexts.get(context_ref or "", {})
    if diluted is None and diluted_tag == basic_tag:
        diluted = basic

    return EpsFact(
        eps_basic=basic,
        eps_diluted=diluted,
        period_start=selected_context.get("start").isoformat()
        if selected_context.get("start")
        else None,
        period_end=selected_context.get("end").isoformat() if selected_context.get("end") else None,
        context_ref=context_ref,
        context_duration_days=selected_context.get("duration_days"),
        source_format="xbrl",
        source_url=source_url,
        eps_basic_tag=basic_tag,
        eps_diluted_tag=diluted_tag,
    )


def expanded_table_rows(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        row: list[str] = []
        for cell in tr.find_all(["th", "td"], recursive=False):
            text = clean_text(cell.get_text(" ", strip=True))
            try:
                colspan = int(cell.get("colspan", 1))
            except (TypeError, ValueError):
                colspan = 1
            row.extend([text] * max(colspan, 1))
        if any(cell for cell in row):
            rows.append(row)
    return rows


def find_label_index(row: list[str], phrase: str) -> int | None:
    phrase_norm = norm_text(phrase)
    for index, cell in enumerate(row):
        if phrase_norm in norm_text(cell):
            return index
    return None


def html_basic_patterns() -> list[tuple[str, Callable[[str], bool]]]:
    return [
        (
            "Basic earnings/loss per share from continuing and discontinued operations",
            lambda text: "basic" in text
            and "continuing" in text
            and "discontinued" in text
            and "diluted" not in text,
        ),
        (
            "Basic earnings per share after extraordinary items",
            lambda text: "basic" in text
            and "earnings per share" in text
            and "after extraordinary" in text
            and "diluted" not in text,
        ),
        (
            "Basic and diluted EPS after extraordinary items",
            lambda text: "basic and diluted" in text
            and "eps" in text
            and "after extraordinary" in text,
        ),
        (
            "Basic earnings/loss per share from continuing operations",
            lambda text: "basic" in text
            and "continuing operations" in text
            and "diluted" not in text,
        ),
        (
            "Basic earnings per share before extraordinary items",
            lambda text: "basic" in text
            and "earnings per share" in text
            and "before extraordinary" in text
            and "diluted" not in text,
        ),
        (
            "Generic basic EPS",
            lambda text: "basic" in text
            and ("earnings per share" in text or "eps" in text)
            and "diluted" not in text,
        ),
    ]


def html_diluted_patterns() -> list[tuple[str, Callable[[str], bool]]]:
    return [
        (
            "Diluted earnings/loss per share from continuing and discontinued operations",
            lambda text: "diluted" in text and "continuing" in text and "discontinued" in text,
        ),
        (
            "Diluted earnings per share after extraordinary items",
            lambda text: "diluted" in text
            and "earnings per share" in text
            and "after extraordinary" in text,
        ),
        (
            "Basic and diluted EPS after extraordinary items",
            lambda text: "basic and diluted" in text
            and "eps" in text
            and "after extraordinary" in text,
        ),
        (
            "Diluted earnings/loss per share from continuing operations",
            lambda text: "diluted" in text and "continuing operations" in text,
        ),
        (
            "Diluted earnings per share before extraordinary items",
            lambda text: "diluted" in text
            and "earnings per share" in text
            and "before extraordinary" in text,
        ),
        (
            "Generic diluted EPS",
            lambda text: "diluted" in text and ("earnings per share" in text or "eps" in text),
        ),
    ]


def choose_html_value(
    rows: list[list[str]],
    target_index: int,
    patterns: list[tuple[str, Callable[[str], bool]]],
) -> tuple[float | None, str | None]:
    for label, predicate in patterns:
        for row in rows:
            label_text = norm_text(" ".join(row[: min(target_index, len(row))]))
            if not predicate(label_text):
                continue
            value = parse_float(row[target_index] if target_index < len(row) else None)
            if value is not None:
                return value, label
    return None, None


def parse_ixbrl_html(content: bytes, source_url: str, qe_date: date) -> EpsFact:
    soup = BeautifulSoup(content, "html.parser")
    qe_datestr = qe_date.isoformat()

    for table in soup.find_all("table"):
        table_text = norm_text(table.get_text(" ", strip=True))
        if "date of end of reporting period" not in table_text:
            continue
        if "earnings per share" not in table_text and "eps" not in table_text:
            continue

        rows = expanded_table_rows(table)
        date_row = next(
            (row for row in rows if find_label_index(row, "Date of end of reporting period") is not None),
            None,
        )
        if not date_row:
            continue
        label_index = find_label_index(date_row, "Date of end of reporting period")
        assert label_index is not None

        date_candidates: list[tuple[int, date]] = []
        for index in range(label_index + 1, len(date_row)):
            parsed = parse_any_date(date_row[index])
            if parsed:
                date_candidates.append((index, parsed))
        if not date_candidates:
            continue

        target_index = next((index for index, parsed in date_candidates if parsed == qe_date), None)
        if target_index is None:
            target_index = date_candidates[0][0]

        start_row = next(
            (row for row in rows if find_label_index(row, "Date of start of reporting period") is not None),
            None,
        )
        period_start = None
        if start_row and target_index < len(start_row):
            parsed_start = parse_any_date(start_row[target_index])
            period_start = parsed_start.isoformat() if parsed_start else None

        basic, basic_label = choose_html_value(rows, target_index, html_basic_patterns())
        diluted, diluted_label = choose_html_value(rows, target_index, html_diluted_patterns())
        if basic is None:
            continue
        if diluted is None and basic_label and "basic and diluted" in norm_text(basic_label):
            diluted = basic
            diluted_label = basic_label

        return EpsFact(
            eps_basic=basic,
            eps_diluted=diluted,
            period_start=period_start,
            period_end=qe_datestr,
            context_ref=None,
            context_duration_days=None,
            source_format="ixbrl_html",
            source_url=source_url,
            eps_basic_tag=basic_label,
            eps_diluted_tag=diluted_label,
        )

    return EpsFact(source_format="ixbrl_html", source_url=source_url, error="EPS table not found")


def extract_eps(
    client: NSEClient,
    filing: Filing,
    filings_dir: Path,
    refresh: bool,
) -> EpsFact:
    errors: list[str] = []

    if filing.xbrl_url and filing.xbrl_url.lower() != "null":
        xbrl_path = filings_dir / f"{safe_name(filing.symbol)}_{safe_name(filing.seq_id)}.xml"
        try:
            content, status, _ = fetch_cached_bytes(
                client, filing.xbrl_url, xbrl_path, refresh, require_xml=True
            )
            if status == 200 and content:
                fact = parse_xbrl(content, filing.xbrl_url, filing.qe_date)
                if fact.eps_basic is not None:
                    return fact
                errors.append("XBRL parsed but basic EPS was not found")
            else:
                errors.append(f"XBRL HTTP status {status}")
        except Exception as exc:  # noqa: BLE001 - capture parser/network detail for output trace
            errors.append(f"XBRL failed: {exc}")

    if filing.ixbrl_url and filing.ixbrl_url.lower() != "null":
        ixbrl_path = filings_dir / f"{safe_name(filing.symbol)}_{safe_name(filing.seq_id)}.html"
        try:
            content, status, _ = fetch_cached_bytes(client, filing.ixbrl_url, ixbrl_path, refresh)
            if status == 200 and content:
                fact = parse_ixbrl_html(content, filing.ixbrl_url, filing.qe_date)
                if fact.eps_basic is not None:
                    return fact
                if fact.error:
                    errors.append(f"iXBRL failed: {fact.error}")
            else:
                errors.append(f"iXBRL HTTP status {status}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"iXBRL failed: {exc}")

    return EpsFact(error="; ".join(errors) or "No XBRL/iXBRL URL available")


def company_payload(
    company: Company,
    selected_filings: list[dict[str, Any]],
    used_quarters: list[dict[str, Any]],
    attempted_filings: list[dict[str, Any]],
) -> dict[str, Any]:
    used_sorted = sorted(used_quarters, key=lambda item: item["period_end"] or item["qe_date"], reverse=True)
    ttm_basic = (
        sum(item["eps_basic"] for item in used_sorted[:4] if item["eps_basic"] is not None)
        if len(used_sorted) >= 4
        else None
    )
    diluted_values = [item["eps_diluted"] for item in used_sorted[:4]]
    ttm_diluted = sum(diluted_values) if len(used_sorted) >= 4 and all(v is not None for v in diluted_values) else None
    status = "complete" if len(used_sorted) >= 4 else "incomplete"

    return {
        "company": asdict(company),
        "status": status,
        "standalone_ttm_basic_eps": round_or_none(ttm_basic),
        "standalone_ttm_diluted_eps": round_or_none(ttm_diluted),
        "quarters_used_count": min(len(used_sorted), 4),
        "quarters_used": used_sorted[:4],
        "standalone_filings_selected": selected_filings,
        "filings_attempted": attempted_filings,
        "note": "TTM EPS is the sum of the latest four reported standalone quarterly basic EPS facts.",
    }


def process_company(
    company: Company,
    client: NSEClient,
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    rows = fetch_all_filings(
        client,
        company.symbol,
        output_dir / "raw" / "api",
        args.size,
        args.max_pages,
        args.refresh,
    )
    standalone_filings = choose_latest_standalone(rows)
    selected_filings = [filing_summary(filing) for filing in standalone_filings]

    used_quarters: list[dict[str, Any]] = []
    attempted: list[dict[str, Any]] = []
    seen_periods: set[str] = set()

    for filing in standalone_filings[: args.filings_to_try]:
        fact = extract_eps(client, filing, output_dir / "raw" / "filings", args.refresh)
        attempt = filing_summary(filing)
        attempt["extraction"] = eps_fact_payload(fact)
        attempted.append(attempt)

        period_end = fact.period_end or filing.qe_date.isoformat()
        if fact.eps_basic is None or period_end in seen_periods:
            continue
        seen_periods.add(period_end)
        used_quarters.append(
            {
                "symbol": company.symbol,
                "company_name": company.name,
                "industry": company.industry,
                "seq_id": filing.seq_id,
                "qe_date": filing.qe_date.isoformat(),
                "period_start": fact.period_start,
                "period_end": period_end,
                "audited": filing.audited,
                "type_sub": filing.type_sub,
                "broadcast_date": filing.broadcast_date,
                "creation_date": filing.creation_date,
                "eps_basic": round_or_none(fact.eps_basic),
                "eps_diluted": round_or_none(fact.eps_diluted),
                "source_format": fact.source_format,
                "source_url": fact.source_url,
                "eps_basic_tag": fact.eps_basic_tag,
                "eps_diluted_tag": fact.eps_diluted_tag,
                "context_ref": fact.context_ref,
                "context_duration_days": fact.context_duration_days,
                "xbrl_url": filing.xbrl_url,
                "ixbrl_url": filing.ixbrl_url,
            }
        )
        if len(used_quarters) >= 4:
            break

    payload = company_payload(company, selected_filings, used_quarters, attempted)
    write_json(output_dir / "companies" / f"{safe_name(company.symbol)}.json", payload)
    return payload


def eps_fact_payload(fact: EpsFact) -> dict[str, Any]:
    payload = asdict(fact)
    payload["eps_basic"] = round_or_none(fact.eps_basic)
    payload["eps_diluted"] = round_or_none(fact.eps_diluted)
    return payload


def filing_summary(filing: Filing) -> dict[str, Any]:
    return {
        "symbol": filing.symbol,
        "company_name": filing.company_name,
        "seq_id": filing.seq_id,
        "qe_date": filing.qe_date.isoformat(),
        "type_sub": filing.type_sub,
        "audited": filing.audited,
        "consolidated": filing.consolidated,
        "broadcast_date": filing.broadcast_date,
        "creation_date": filing.creation_date,
        "xbrl_url": filing.xbrl_url,
        "ixbrl_url": filing.ixbrl_url,
        "xbrl_file_size": filing.xbrl_file_size,
        "ixbrl_file_size": filing.ixbrl_file_size,
    }


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    parsed = parse_float(value)
    if parsed is None:
        return "-"
    return f"{parsed:,.{digits}f}{suffix}"


def fmt_weight(value: Any) -> str:
    parsed = parse_float(value)
    if parsed is None:
        return "-"
    return f"{parsed * 100:.2f}%"


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def html_company_rows(rows: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for row in rows:
        rendered.append(
            "<tr>"
            f"<td><strong>{esc(row.get('symbol'))}</strong><span>{esc(row.get('company_name'))}</span></td>"
            f"<td>{esc(row.get('latest_period_end'))}</td>"
            f"<td class=\"num\">{fmt_number(row.get('standalone_ttm_basic_eps'))}</td>"
            f"<td class=\"num\">{fmt_number(row.get('last_price'))}</td>"
            f"<td class=\"num\">{fmt_weight(row.get('nifty_ffmc_weight'))}</td>"
            f"<td class=\"num\">{fmt_number(row.get('stock_standalone_ttm_basic_pe'))}</td>"
            f"<td class=\"num\">{fmt_number(row.get('free_float_ttm_basic_earnings'), 0)}</td>"
            "</tr>"
        )
    return "\n".join(rendered)


def html_contribution_rows(rows: list[dict[str, Any]], limit: int = 10) -> str:
    ordered = sorted(
        rows,
        key=lambda row: parse_float(row.get("free_float_ttm_basic_earnings")) or 0,
        reverse=True,
    )[:limit]
    rendered: list[str] = []
    for row in ordered:
        rendered.append(
            "<tr>"
            f"<td><strong>{esc(row.get('symbol'))}</strong><span>{esc(row.get('company_name'))}</span></td>"
            f"<td class=\"num\">{fmt_weight(row.get('nifty_ffmc_weight'))}</td>"
            f"<td class=\"num\">{fmt_weight(row.get('nifty_basic_earnings_weight'))}</td>"
            f"<td class=\"num\">{fmt_number(row.get('free_float_ttm_basic_earnings'), 0)}</td>"
            "</tr>"
        )
    return "\n".join(rendered)


def write_html_report(output_dir: Path, summary: dict[str, Any]) -> None:
    calc = summary["index_calculation"]
    snapshot = summary["index_snapshot"]
    company_rows = flatten_company_rows(summary["companies"], summary.get("index_constituents"))
    index_rows = summary.get("index_constituents", [])
    generated_at = summary.get("generated_at", "")
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nifty 50 Standalone TTM EPS Report</title>
  <style>
    :root {{
      --ink: #162033;
      --muted: #667085;
      --line: #d7dee8;
      --paper: #f3f5f8;
      --panel: #ffffff;
      --accent: #116b5f;
      --accent-soft: #e7f3ef;
      --gold: #a66321;
      --gold-soft: #fff5e8;
      --deep: #111827;
      --shadow: 0 18px 40px rgba(17, 24, 39, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(180deg, #eef2f6 0, var(--paper) 260px),
        var(--paper);
      font-family: "Aptos", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      line-height: 1.45;
    }}
    header {{
      background:
        linear-gradient(135deg, rgba(17, 24, 39, .98), rgba(17, 24, 39, .9)),
        var(--deep);
      color: white;
      border-bottom: 5px solid var(--accent);
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 30px 22px; }}
    .topline {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      flex-wrap: wrap;
    }}
    h1 {{
      margin: 0 0 10px;
      max-width: 760px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(30px, 4vw, 48px);
      line-height: 1.04;
      letter-spacing: 0;
    }}
    h2 {{ margin: 0 0 14px; font-size: 21px; letter-spacing: 0; }}
    h3 {{ margin: 0 0 10px; font-size: 13px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }}
    p {{ margin: 0; color: var(--muted); }}
    header p {{ color: #cbd5e1; max-width: 760px; }}
    .stamp {{
      min-width: 230px;
      padding: 12px 14px;
      text-align: right;
      font-size: 13px;
      color: #d6deea;
      border: 1px solid rgba(255, 255, 255, .18);
      border-radius: 8px;
      background: rgba(255, 255, 255, .06);
    }}
    main {{ padding: 26px 0 42px; }}
    .grid {{ display: grid; gap: 18px; }}
    .cards {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .card {{ padding: 18px; border-top: 3px solid var(--accent); }}
    .metric {{ font-size: 28px; line-height: 1.1; font-weight: 750; color: var(--ink); font-variant-numeric: tabular-nums; }}
    .metric.small {{ font-size: 22px; }}
    .label {{ margin-top: 7px; color: var(--muted); font-size: 13px; }}
    .two {{ grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); margin-top: 18px; }}
    .section {{ padding: 22px; }}
    .methodology {{
      background: linear-gradient(180deg, #ffffff, #fbfcfd);
      border-top: 4px solid var(--accent);
    }}
    .methodology p {{ max-width: 780px; }}
    .formula-stack {{ display: grid; gap: 10px; margin-top: 16px; }}
    .formula-step {{
      display: grid;
      grid-template-columns: 116px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      padding: 14px;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .formula-step strong {{
      color: var(--accent);
      font-size: 12px;
      letter-spacing: .07em;
      text-transform: uppercase;
    }}
    .formula {{
      margin: 0;
      color: #1e293b;
      font-family: "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
      font-size: 13px;
      line-height: 1.6;
      overflow-x: auto;
      white-space: nowrap;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #e9eef5;
      color: #354154;
      text-align: left;
      font-weight: 700;
      border-bottom: 1px solid var(--line);
      padding: 10px 9px;
      white-space: nowrap;
    }}
    td {{
      border-bottom: 1px solid #e6ebf2;
      padding: 10px 9px;
      vertical-align: top;
    }}
    tbody tr:hover {{ background: #f9fbfd; }}
    td span {{ display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .tablebox {{ max-height: 620px; overflow: auto; border-radius: 8px; border: 1px solid var(--line); }}
    .sources {{ border-top: 4px solid var(--gold); }}
    .sources p + p {{ margin-top: 9px; }}
    .sources a {{ color: var(--accent); text-decoration: none; font-weight: 650; }}
    .sources a:hover {{ text-decoration: underline; }}
    .note {{
      border-left: 4px solid var(--gold);
      padding: 12px 14px;
      background: var(--gold-soft);
      color: #713f12;
      border-radius: 0 6px 6px 0;
      margin-top: 14px;
    }}
    footer {{ color: var(--muted); font-size: 12px; padding: 18px 0 36px; }}
    @media (max-width: 900px) {{
      .cards, .two {{ grid-template-columns: 1fr; }}
      .stamp {{ text-align: left; }}
    }}
    @media (max-width: 560px) {{
      .wrap {{ padding-left: 16px; padding-right: 16px; }}
      .formula-step {{ grid-template-columns: 1fr; }}
      .metric, .metric.small {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topline">
      <div>
        <h1>Nifty 50 Standalone TTM EPS</h1>
        <p>Free-float market capitalization weighted EPS report generated from NSE index data and NSE Integrated Filing financial results.</p>
      </div>
      <div class="stamp">
        <div>Generated: {esc(generated_at)}</div>
        <div>NSE snapshot: {esc(snapshot.get('timestamp'))}</div>
        <div>Market status: {esc(snapshot.get('market_status'))}</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    <section class="grid cards">
      <div class="panel card"><div class="metric">{fmt_number(calc.get('nifty_standalone_ttm_basic_eps'))}</div><div class="label">Nifty standalone TTM basic EPS</div></div>
      <div class="panel card"><div class="metric">{fmt_number(calc.get('nifty_standalone_ttm_basic_pe'))}</div><div class="label">Nifty standalone TTM basic PE</div></div>
      <div class="panel card"><div class="metric">{fmt_number(calc.get('index_last_price'))}</div><div class="label">Nifty index value</div></div>
      <div class="panel card"><div class="metric small">{fmt_number(calc.get('total_constituent_ffmc_lakhs'), 2)}</div><div class="label">Total FFMC, Rs lakhs</div></div>
    </section>

    <section class="grid two">
      <div class="panel section methodology">
        <h2>Methodology</h2>
        <p>Constituents, prices, and FFMC are taken from the live NSE Nifty 50 index endpoint. Standalone EPS is extracted from each company's NSE Integrated Filing XBRL/iXBRL and summed across the latest four reported standalone quarters.</p>
        <div class="formula-stack" aria-label="Index EPS calculation formulas">
          <div class="formula-step">
            <strong>Step 1</strong>
            <pre class="formula">free_float_ttm_earnings_i = ffmc_i * standalone_ttm_eps_i / lastPrice_i</pre>
          </div>
          <div class="formula-step">
            <strong>Step 2</strong>
            <pre class="formula">index PE = sum(ffmc_i) / sum(free_float_ttm_earnings_i)</pre>
          </div>
          <div class="formula-step">
            <strong>Step 3</strong>
            <pre class="formula">index EPS = index last value / index PE</pre>
          </div>
        </div>
        <div class="note">This report does not add or equal-weight constituent EPS. It uses the same market-capitalization logic required for index-level earnings aggregation.</div>
      </div>
      <div class="panel section sources">
        <h2>Sources</h2>
        <p><a href="https://www.nseindia.com/api/equity-stockIndices?index=NIFTY+50">NSE Nifty 50 index API</a></p>
        <p><a href="https://www.nseindia.com/static/products-services/indices-price-earnings-ratio">NSE index P/E methodology</a></p>
        <p><a href="https://nsearchives.nseindia.com/content/indices/Method_Nifty_50.pdf">Nifty 50 methodology document</a></p>
        <p><a href="https://www.nseindia.com/products-services/indices-investible-weight-factors">NSE investible weight factors</a></p>
      </div>
    </section>

    <section class="panel section" style="margin-top:16px">
      <h2>Top Free-Float Earnings Contributors</h2>
      <div class="tablebox">
        <table>
          <thead><tr><th>Company</th><th class="num">FFMC Weight</th><th class="num">Earnings Weight</th><th class="num">Free-Float TTM Earnings</th></tr></thead>
          <tbody>
            {html_contribution_rows(index_rows)}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel section" style="margin-top:16px">
      <h2>Constituent Calculation Detail</h2>
      <div class="tablebox">
        <table>
          <thead><tr><th>Company</th><th>Latest EPS Period</th><th class="num">TTM EPS</th><th class="num">Last Price</th><th class="num">FFMC Weight</th><th class="num">Stock PE</th><th class="num">Free-Float TTM Earnings</th></tr></thead>
          <tbody>
            {html_company_rows(company_rows)}
          </tbody>
        </table>
      </div>
    </section>
  </main>
  <footer class="wrap">Generated by calculate_standalone_ttm_eps.py. Figures depend on the NSE index snapshot and available standalone filings at run time.</footer>
</body>
</html>
"""
    (output_dir / "nifty50_standalone_ttm_eps_report.html").write_text(html_text, encoding="utf-8")


def index_last_value(snapshot: IndexSnapshot) -> float | None:
    for candidate in (
        snapshot.metadata.get("last"),
        snapshot.index_row.get("lastPrice"),
        snapshot.market_status.get("last"),
    ):
        value = parse_float(candidate)
        if value is not None:
            return value
    return None


def index_snapshot_payload(snapshot: IndexSnapshot) -> dict[str, Any]:
    return {
        "name": snapshot.name,
        "timestamp": snapshot.timestamp,
        "last_price": round_or_none(index_last_value(snapshot)),
        "metadata_time": snapshot.metadata.get("timeVal"),
        "trade_date": snapshot.market_status.get("tradeDate"),
        "market_status": snapshot.market_status.get("marketStatus"),
        "api_ffmc_sum": snapshot.metadata.get("ffmc_sum"),
        "api_ffmc_sum_unit_note": (
            "NSE index payload reports metadata.ffmc_sum in the same display scale used on "
            "the website, while constituent ffmc rows are used directly for calculation."
        ),
        "constituent_count": len(snapshot.constituents),
    }


def build_index_calculation(
    companies: list[dict[str, Any]],
    snapshot: IndexSnapshot,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result_by_symbol = {item["company"]["symbol"]: item for item in companies}
    rows: list[dict[str, Any]] = []
    issues: list[str] = []

    for market_row in snapshot.constituents:
        symbol = clean_text(market_row.get("symbol"))
        result = result_by_symbol.get(symbol)
        last_price = parse_float(market_row.get("lastPrice"))
        ffmc = parse_float(market_row.get("ffmc"))
        basic_eps = result.get("standalone_ttm_basic_eps") if result else None
        diluted_eps = result.get("standalone_ttm_diluted_eps") if result else None

        if result is None:
            issues.append(f"{symbol}: missing EPS result")
        if last_price is None or last_price <= 0:
            issues.append(f"{symbol}: missing/invalid lastPrice")
        if ffmc is None or ffmc <= 0:
            issues.append(f"{symbol}: missing/invalid ffmc")
        if basic_eps is None:
            issues.append(f"{symbol}: missing standalone TTM basic EPS")

        free_float_shares = ffmc / last_price if ffmc and last_price and last_price > 0 else None
        basic_earnings = (
            float(basic_eps) * free_float_shares
            if basic_eps is not None and free_float_shares is not None
            else None
        )
        diluted_earnings = (
            float(diluted_eps) * free_float_shares
            if diluted_eps is not None and free_float_shares is not None
            else None
        )
        basic_pe = last_price / float(basic_eps) if basic_eps not in (None, 0) and last_price else None
        diluted_pe = last_price / float(diluted_eps) if diluted_eps not in (None, 0) and last_price else None

        rows.append(
            {
                "symbol": symbol,
                "company_name": clean_text((market_row.get("meta") or {}).get("companyName"))
                if isinstance(market_row.get("meta"), dict)
                else symbol,
                "industry": clean_text((market_row.get("meta") or {}).get("industry"))
                if isinstance(market_row.get("meta"), dict)
                else None,
                "isin": clean_text((market_row.get("meta") or {}).get("isin"))
                if isinstance(market_row.get("meta"), dict)
                else None,
                "last_price": round_or_none(last_price),
                "ffmc": round_or_none(ffmc, 2),
                "standalone_ttm_basic_eps": basic_eps,
                "standalone_ttm_diluted_eps": diluted_eps,
                "stock_standalone_ttm_basic_pe": round_or_none(basic_pe),
                "stock_standalone_ttm_diluted_pe": round_or_none(diluted_pe),
                "free_float_shares_proxy": round_or_none(free_float_shares, 4),
                "free_float_ttm_basic_earnings": round_or_none(basic_earnings, 2),
                "free_float_ttm_diluted_earnings": round_or_none(diluted_earnings, 2),
            }
        )

    total_ffmc = sum(float(row["ffmc"]) for row in rows if row.get("ffmc") is not None)
    gross_basic_earnings = sum(
        float(row["free_float_ttm_basic_earnings"])
        for row in rows
        if row.get("free_float_ttm_basic_earnings") is not None
    )
    gross_diluted_earnings = sum(
        float(row["free_float_ttm_diluted_earnings"])
        for row in rows
        if row.get("free_float_ttm_diluted_earnings") is not None
    )

    for row in rows:
        ffmc = float(row["ffmc"]) if row.get("ffmc") is not None else None
        basic_earnings = (
            float(row["free_float_ttm_basic_earnings"])
            if row.get("free_float_ttm_basic_earnings") is not None
            else None
        )
        row["nifty_ffmc_weight"] = round_or_none(ffmc / total_ffmc if ffmc and total_ffmc else None, 8)
        row["nifty_basic_earnings_weight"] = round_or_none(
            basic_earnings / gross_basic_earnings if basic_earnings is not None and gross_basic_earnings else None,
            8,
        )

    index_last = index_last_value(snapshot)
    divisor_proxy = total_ffmc / index_last if total_ffmc and index_last else None
    index_basic_pe = total_ffmc / gross_basic_earnings if gross_basic_earnings else None
    index_diluted_pe = total_ffmc / gross_diluted_earnings if gross_diluted_earnings else None
    index_basic_eps = index_last / index_basic_pe if index_last and index_basic_pe else None
    index_diluted_eps = index_last / index_diluted_pe if index_last and index_diluted_pe else None

    status = "complete" if not issues and len(rows) == len(snapshot.constituents) else "incomplete"
    calculation = {
        "status": status,
        "method": (
            "free_float_ttm_earnings_i = ffmc_i * standalone_ttm_eps_i / lastPrice_i; "
            "index PE = sum(ffmc_i) / sum(free_float_ttm_earnings_i); "
            "index EPS = index last value / index PE."
        ),
        "constituents_used": len(rows),
        "complete_companies": sum(1 for item in companies if item.get("status") == "complete"),
        "incomplete_companies": sum(1 for item in companies if item.get("status") != "complete"),
        "index_last_price": round_or_none(index_last),
        "total_constituent_ffmc": round_or_none(total_ffmc, 2),
        "total_constituent_ffmc_lakhs": round_or_none(total_ffmc / 100000 if total_ffmc else None, 2),
        "gross_free_float_ttm_basic_earnings": round_or_none(gross_basic_earnings, 2),
        "gross_free_float_ttm_diluted_earnings": round_or_none(gross_diluted_earnings, 2),
        "index_divisor_proxy": round_or_none(divisor_proxy, 4),
        "nifty_standalone_ttm_basic_pe": round_or_none(index_basic_pe, 4),
        "nifty_standalone_ttm_diluted_pe": round_or_none(index_diluted_pe, 4),
        "nifty_standalone_ttm_basic_eps": round_or_none(index_basic_eps, 4),
        "nifty_standalone_ttm_diluted_eps": round_or_none(index_diluted_eps, 4),
        "issues": issues,
    }
    return calculation, rows


def build_summary(
    companies: list[dict[str, Any]],
    generated_at: str,
    snapshot: IndexSnapshot,
) -> dict[str, Any]:
    complete = [item for item in companies if item.get("status") == "complete"]
    incomplete = [item for item in companies if item.get("status") != "complete"]
    index_calculation, index_constituents = build_index_calculation(companies, snapshot)

    return {
        "generated_at": generated_at,
        "constituents_source": "nse",
        "index_api": INDEX_API,
        "index_snapshot": index_snapshot_payload(snapshot),
        "nse_api": NSE_API,
        "filing_type": FILING_TYPE,
        "calculation_method": (
            "Symbols and FFMC are fetched from the live NSE equity-stockIndices endpoint. "
            "For each symbol, standalone quarterly EPS is extracted from NSE Integrated Filing "
            "- Financials and summed across the latest four quarters. Nifty standalone EPS is "
            "then calculated through aggregate free-float earnings, not by adding or averaging "
            "constituent EPS."
        ),
        "aggregate": {
            "complete_companies": len(complete),
            "incomplete_companies": len(incomplete),
            "incomplete_symbols": [item["company"]["symbol"] for item in incomplete],
        },
        "index_calculation": index_calculation,
        "index_constituents": index_constituents,
        "companies": companies,
    }


def flatten_company_rows(
    companies: list[dict[str, Any]],
    index_constituents: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    index_by_symbol = {row["symbol"]: row for row in index_constituents or []}
    rows: list[dict[str, Any]] = []
    for item in companies:
        company = item["company"]
        latest_quarter = item.get("quarters_used", [{}])[0] if item.get("quarters_used") else {}
        index_row = index_by_symbol.get(company["symbol"], {})
        rows.append(
            {
                "symbol": company["symbol"],
                "company_name": company["name"],
                "industry": company["industry"],
                "isin": company["isin"],
                "status": item["status"],
                "quarters_used_count": item["quarters_used_count"],
                "latest_period_end": latest_quarter.get("period_end"),
                "standalone_ttm_basic_eps": item.get("standalone_ttm_basic_eps"),
                "standalone_ttm_diluted_eps": item.get("standalone_ttm_diluted_eps"),
                "last_price": index_row.get("last_price"),
                "ffmc": index_row.get("ffmc"),
                "nifty_ffmc_weight": index_row.get("nifty_ffmc_weight"),
                "stock_standalone_ttm_basic_pe": index_row.get("stock_standalone_ttm_basic_pe"),
                "free_float_ttm_basic_earnings": index_row.get("free_float_ttm_basic_earnings"),
            }
        )
    return rows


def flatten_quarter_rows(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in companies:
        rows.extend(item.get("quarters_used", []))
    return sorted(rows, key=lambda row: (row["symbol"], row["period_end"]), reverse=False)


def flatten_attempt_rows(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in companies:
        symbol = item["company"]["symbol"]
        for attempted in item.get("filings_attempted", []):
            extraction = attempted.get("extraction", {})
            rows.append(
                {
                    "symbol": symbol,
                    "seq_id": attempted.get("seq_id"),
                    "qe_date": attempted.get("qe_date"),
                    "audited": attempted.get("audited"),
                    "consolidated": attempted.get("consolidated"),
                    "source_format": extraction.get("source_format"),
                    "eps_basic": extraction.get("eps_basic"),
                    "eps_diluted": extraction.get("eps_diluted"),
                    "period_start": extraction.get("period_start"),
                    "period_end": extraction.get("period_end"),
                    "eps_basic_tag": extraction.get("eps_basic_tag"),
                    "context_ref": extraction.get("context_ref"),
                    "error": extraction.get("error"),
                    "xbrl_url": attempted.get("xbrl_url"),
                    "ixbrl_url": attempted.get("ixbrl_url"),
                }
            )
    return rows


def write_outputs(output_dir: Path, summary: dict[str, Any]) -> None:
    write_json(output_dir / "nifty50_standalone_ttm_eps.json", summary)
    write_html_report(output_dir, summary)

    company_rows = flatten_company_rows(summary["companies"], summary.get("index_constituents"))
    write_csv(
        output_dir / "companies_ttm_eps.csv",
        company_rows,
        [
            "symbol",
            "company_name",
            "industry",
            "isin",
            "status",
            "quarters_used_count",
            "latest_period_end",
            "standalone_ttm_basic_eps",
            "standalone_ttm_diluted_eps",
            "last_price",
            "ffmc",
            "nifty_ffmc_weight",
            "stock_standalone_ttm_basic_pe",
            "free_float_ttm_basic_earnings",
        ],
    )

    write_csv(
        output_dir / "index_eps_calculation.csv",
        summary.get("index_constituents", []),
        [
            "symbol",
            "company_name",
            "industry",
            "isin",
            "last_price",
            "ffmc",
            "nifty_ffmc_weight",
            "standalone_ttm_basic_eps",
            "standalone_ttm_diluted_eps",
            "stock_standalone_ttm_basic_pe",
            "stock_standalone_ttm_diluted_pe",
            "free_float_shares_proxy",
            "free_float_ttm_basic_earnings",
            "free_float_ttm_diluted_earnings",
            "nifty_basic_earnings_weight",
        ],
    )

    quarter_rows = flatten_quarter_rows(summary["companies"])
    write_csv(
        output_dir / "quarterly_eps.csv",
        quarter_rows,
        [
            "symbol",
            "company_name",
            "industry",
            "seq_id",
            "qe_date",
            "period_start",
            "period_end",
            "audited",
            "type_sub",
            "broadcast_date",
            "creation_date",
            "eps_basic",
            "eps_diluted",
            "source_format",
            "source_url",
            "eps_basic_tag",
            "eps_diluted_tag",
            "context_ref",
            "context_duration_days",
            "xbrl_url",
            "ixbrl_url",
        ],
    )

    attempt_rows = flatten_attempt_rows(summary["companies"])
    write_csv(
        output_dir / "filing_extraction_audit.csv",
        attempt_rows,
        [
            "symbol",
            "seq_id",
            "qe_date",
            "audited",
            "consolidated",
            "source_format",
            "eps_basic",
            "eps_diluted",
            "period_start",
            "period_end",
            "eps_basic_tag",
            "context_ref",
            "error",
            "xbrl_url",
            "ixbrl_url",
        ],
    )


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    client = NSEClient(args.timeout, args.retries, args.sleep)
    if not args.no_home_warmup:
        client.warm_up()

    index_snapshot = fetch_index_snapshot(client, args.index_name, output_dir, args.refresh)
    index_snapshot = filter_index_snapshot(index_snapshot, args.symbols)
    companies = companies_from_index_snapshot(index_snapshot)

    results: list[dict[str, Any]] = []
    for index, company in enumerate(companies, start=1):
        print(f"[{index:02d}/{len(companies):02d}] {company.symbol}", flush=True)
        try:
            results.append(process_company(company, client, output_dir, args))
        except Exception as exc:  # noqa: BLE001
            error_payload = {
                "company": asdict(company),
                "status": "failed",
                "standalone_ttm_basic_eps": None,
                "standalone_ttm_diluted_eps": None,
                "quarters_used_count": 0,
                "quarters_used": [],
                "standalone_filings_selected": [],
                "filings_attempted": [],
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            write_json(output_dir / "companies" / f"{safe_name(company.symbol)}.json", error_payload)
            results.append(error_payload)

    summary = build_summary(
        results,
        generated_at,
        index_snapshot,
    )
    write_outputs(output_dir, summary)

    aggregate = summary["aggregate"]
    index_calculation = summary["index_calculation"]
    print(
        "Complete companies: "
        f"{aggregate['complete_companies']}; incomplete/failed: {aggregate['incomplete_companies']}",
        flush=True,
    )
    print(
        "Nifty standalone TTM basic EPS: "
        f"{index_calculation['nifty_standalone_ttm_basic_eps']}; "
        f"PE: {index_calculation['nifty_standalone_ttm_basic_pe']}",
        flush=True,
    )
    print(f"Output directory: {output_dir}", flush=True)
    return 0 if aggregate["complete_companies"] == len(companies) and not index_calculation["issues"] else 2


if __name__ == "__main__":
    sys.exit(main())
