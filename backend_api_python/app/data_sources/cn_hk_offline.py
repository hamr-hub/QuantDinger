"""
T+1 offline data adapter for A股.

Reads pre-staged CSVs from the repository-root ``data/`` directory:

    data/stock_basic/stock_basic.csv   ~5,860 A-share metadata rows
    data/stk_factor/<symbol>.csv       daily OHLCV + technicals (last ~230 rows)
    data/weekly/<symbol>.csv           weekly OHLCV
    data/monthly/<symbol>.csv          monthly OHLCV
    data/fina_indicator/<symbol>.csv   150+ financial ratios per report period

Wired into:

- ``CNStockDataSource.get_kline`` (Tier 0 — before Tencent; covers 1D/1W/1M
  timeframes for symbols that have an offline file, falling back to live
  sources for intraday 1m/5m/... which aren't in the offline dump)
- ``fetch_*_fundamentals`` in :mod:`app.data_sources.cn_hk_fundamentals` (Tier
  0 — Twelve Data / AkShare only get called for HK or when the offline file
  is missing)
- :func:`search_market_symbols` in :mod:`app.services.market.symbol_search`
  (Tier 0 — name/code search against ``stock_basic`` is instant and offline)

The path is configurable via the ``QD_OFFLINE_DATA_DIR`` environment
variable; the default is the ``data/`` folder two levels above the backend
package (i.e. ``<repo>/data``). For Docker deployments, point this at the
bind-mounted host directory.

HK stocks are NOT covered by the offline dataset (only BJ/SH/SZ entries are
present). For HK we continue to rely on the live Tencent / Twelve Data /
yfinance cascade in ``HKStockDataSource``.
"""

from __future__ import annotations

import csv
import os
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_offline_data_dir() -> Optional[str]:
    """Return the absolute path to the offline ``data/`` directory, or None.

    Resolution order:
    1. ``QD_OFFLINE_DATA_DIR`` env var (absolute or relative to CWD)
    2. ``<repo>/data`` — three parents above ``app/data_sources/``:
       backend_api_python/app/data_sources/cn_hk_offline.py →
       backend_api_python/app/data_sources → backend_api_python/app →
       backend_api_python → <repo>
    3. ``<cwd>/data`` — convenience for running scripts/tests from the repo root.
    """
    env_dir = (os.getenv("QD_OFFLINE_DATA_DIR") or "").strip()
    if env_dir:
        candidate = os.path.abspath(env_dir)
        if os.path.isdir(candidate):
            return candidate
        logger.debug("QD_OFFLINE_DATA_DIR is set but not a directory: %s", candidate)

    here = os.path.dirname(os.path.abspath(__file__))
    # app/data_sources/ → ../../../data
    default = os.path.normpath(os.path.join(here, "..", "..", "..", "data"))
    if os.path.isdir(default):
        return default

    # CWD fallback: useful when the module is loaded from a script that has
    # the repo root on sys.path but the package-relative path doesn't work
    # (e.g. when running tests from the repo root).
    cwd_default = os.path.join(os.getcwd(), "data")
    if os.path.isdir(cwd_default):
        return cwd_default
    return None


_OFFLINE_DIR: Optional[str] = resolve_offline_data_dir()


def is_available() -> bool:
    """True iff the offline data directory is reachable."""
    return _OFFLINE_DIR is not None


def offline_dir() -> Optional[str]:
    """Public accessor (mostly for diagnostics / tests)."""
    return _OFFLINE_DIR


# ---------------------------------------------------------------------------
# Stock basic (one-time load, in-memory)
# ---------------------------------------------------------------------------


_STOCK_BASIC_INDEX: Optional[Dict[str, Dict[str, str]]] = None
_STOCK_BASIC_LOCK = threading.Lock()


def _load_stock_basic() -> Dict[str, Dict[str, str]]:
    """Lazy-load ``stock_basic.csv`` into a {symbol: row-dict} index."""
    global _STOCK_BASIC_INDEX
    if _STOCK_BASIC_INDEX is not None:
        return _STOCK_BASIC_INDEX
    with _STOCK_BASIC_LOCK:
        if _STOCK_BASIC_INDEX is not None:
            return _STOCK_BASIC_INDEX
        index: Dict[str, Dict[str, str]] = {}
        if not _OFFLINE_DIR:
            _STOCK_BASIC_INDEX = index
            return index
        path = os.path.join(_OFFLINE_DIR, "stock_basic", "stock_basic.csv")
        if not os.path.isfile(path):
            logger.warning("stock_basic.csv not found at %s", path)
            _STOCK_BASIC_INDEX = index
            return index
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    symbol = (row.get("股票代码") or "").strip()
                    if symbol:
                        index[symbol.upper()] = {k: (v or "").strip() for k, v in row.items()}
            logger.info("Loaded %d rows from stock_basic.csv", len(index))
        except Exception as e:
            logger.warning("Failed to load stock_basic.csv: %s", e)
            index = {}
        _STOCK_BASIC_INDEX = index
        return index


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_a_share_symbol(symbol: str) -> bool:
    """True iff ``symbol`` looks like an A-share identifier with a market suffix."""
    if not symbol:
        return False
    s = symbol.strip().upper()
    return bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", s))


def a_share_to_path_symbol(symbol: str) -> str:
    """Return the on-disk filename stem for an A-share symbol.

    The CSV files in ``data/`` are named ``<symbol>.csv`` (e.g. ``600930.SH.csv``),
    so the path symbol is just the upper-cased identifier.
    """
    return symbol.strip().upper()


def get_company_profile(symbol: str) -> Dict[str, Any]:
    """Return a dict of company info from ``stock_basic`` for an A-share symbol.

    Returns an empty dict if the symbol is unknown. All values are strings
    (numbers left as text to preserve precision; the caller can coerce).
    """
    if not is_a_share_symbol(symbol):
        return {}
    index = _load_stock_basic()
    row = index.get(symbol.strip().upper())
    if not row:
        return {}

    # Friendly aliases matching the convention used by other data sources.
    name = row.get("股票名称") or ""
    full_name = row.get("公司全称") or ""
    en_name = row.get("英文名称") or ""
    industry = row.get("行业") or ""
    region = row.get("地区") or ""
    listing_date = (row.get("上市日期") or "").strip()
    controller = row.get("实控人名称") or ""
    nature = row.get("企业性质") or ""
    market_type = row.get("市场类型") or ""
    exchange = row.get("交易所代码") or ""
    currency = row.get("交易货币") or ""
    hk_connect = row.get("是否沪深港通标的") or ""
    pinyin = row.get("拼音缩写") or ""

    out: Dict[str, Any] = {
        "symbol": symbol.strip().upper(),
        "name": name,
        "full_name": full_name,
        "english_name": en_name,
        "industry": industry,
        "region": region,
        "market_type": market_type,
        "exchange": exchange,
        "currency": currency,
        "hk_connect": hk_connect,
        "pinyin": pinyin,
        "controller": controller,
        "enterprise_nature": nature,
        "listing_date": listing_date,
        "source": "offline_stock_basic",
    }
    return out


def search_a_share_symbols(keyword: str, limit: int = 20) -> List[Dict[str, str]]:
    """Search offline ``stock_basic`` for an A-share keyword (code or name)."""
    if not is_available():
        return []
    kw = (keyword or "").strip().upper()
    if not kw:
        return []
    index = _load_stock_basic()
    if not index:
        return []

    out: List[Dict[str, str]] = []
    # Match against symbol first (most precise), then name. Both Chinese
    # names and pinyin abbreviations get matched for the convenience of
    # typing partial pinyin ("PAYH" → 平安银行).
    for symbol, row in index.items():
        if kw in symbol or kw in (row.get("股票名称") or "").upper() or kw in (row.get("拼音缩写") or "").upper():
            out.append({
                "market": "CNStock",
                "symbol": symbol,
                "name": row.get("股票名称") or "",
            })
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# K-line reader (daily / weekly / monthly)
# ---------------------------------------------------------------------------


def _kline_path(timeframe: str, symbol: str) -> Optional[str]:
    """Resolve the on-disk path for a k-line timeframe."""
    if not _OFFLINE_DIR:
        return None
    tf_map = {
        "1D": "stk_factor",
        "1W": "weekly",
        "1M": "monthly",
    }
    sub = tf_map.get(timeframe)
    if not sub:
        return None
    sym = a_share_to_path_symbol(symbol)
    return os.path.join(_OFFLINE_DIR, sub, f"{sym}.csv")


def _parse_csv_date(value: str) -> Optional[int]:
    """Convert a YYYY-MM-DD / YYYY/MM/DD cell to Unix seconds (UTC midnight)."""
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(raw[:10], fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_offline_kline(
    symbol: str,
    timeframe: str,
    limit: int,
    before_time: Optional[int] = None,
    after_time: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read OHLCV rows from offline CSV for A-share symbols.

    Returns a list of normalized kline dicts (time/open/high/low/close/volume),
    same shape as ``BaseDataSource.get_kline`` and the live Tencent path.
    Empty list when the offline file is missing or unreadable.
    """
    if not is_available():
        return []
    if not is_a_share_symbol(symbol):
        return []
    path = _kline_path(timeframe, symbol)
    if not path or not os.path.isfile(path):
        return []

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    except Exception as e:
        logger.debug("Offline kline read failed %s tf=%s: %s", symbol, timeframe, e)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        # Field name varies by file:
        #   stk_factor: 交易日期 / 收盘价 / 开盘价 / 最高价 / 最低价 / 成交量(手)
        #   weekly:     交易日期 / 周收盘价 / 周开盘价 / 周最高价 / 周最低价 / 周成交量(手)
        #   monthly:    交易日期 / 月收盘价 / 月开盘价 / 月最高价 / 月最低价 / 月成交量(手)
        ts = _parse_csv_date(row.get("交易日期") or row.get("日期") or "")
        if ts is None:
            continue
        # Try the timeframe-prefixed column first, fall back to the bare name
        # so the same parser works for all three buckets.
        prefix = {"1D": "", "1W": "周", "1M": "月"}.get(timeframe, "")
        def _g(*keys: str) -> Optional[float]:
            for k in keys:
                v = _safe_float(row.get(k))
                if v is not None:
                    return v
            return None
        close = _g(f"{prefix}收盘价前复权", f"{prefix}收盘价", "收盘价前复权", "收盘价")
        open_ = _g(f"{prefix}开盘价前复权", f"{prefix}开盘价", "开盘价前复权", "开盘价")
        high = _g(f"{prefix}最高价前复权", f"{prefix}最高价", "最高价前复权", "最高价")
        low = _g(f"{prefix}最低价前复权", f"{prefix}最低价", "最低价前复权", "最低价")
        vol = _g(f"{prefix}成交量(手)", "成交量(手)", f"{prefix}成交量", "成交量")
        if close is None or open_ is None or high is None or low is None:
            continue
        out.append({
            "time": ts,
            "open": round(open_, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": round(vol or 0.0, 2),
        })

    out.sort(key=lambda x: x["time"])
    if before_time:
        out = [k for k in out if k["time"] < before_time]
    if after_time is not None:
        out = [k for k in out if k["time"] >= after_time]
    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:]
    return out


# ---------------------------------------------------------------------------
# Fundamentals reader
# ---------------------------------------------------------------------------


def read_offline_financial_indicators(symbol: str, limit: int = 8) -> Dict[str, Any]:
    """Read the latest rows from ``fina_indicator`` for an A-share symbol.

    Returns a dict shaped to merge cleanly with the Twelve Data / AkShare
    output from :mod:`app.data_sources.cn_hk_fundamentals`. The source
    key is set to ``offline_fina_indicator`` so callers can identify which
    layer answered.

    Mapping (kept in sync with ``fetch_cn_fundamental_*`` keys):
        source → "offline_fina_indicator"
        eps (basic) / eps_diluted (diluted)
        revenue / revenue_per_share / capital_reserve_per_share / ...
        ROE (净资产收益率), ROA, gross margin, net margin, etc.
        debt_to_equity, current_ratio, quick_ratio
        operating_cash_flow, free_cash_flow, capital_expenditures
        latest_date (报告期, most recent row)
    """
    if not is_available():
        return {}
    if not is_a_share_symbol(symbol):
        return {}
    path = os.path.join(_OFFLINE_DIR or "", "fina_indicator", f"{a_share_to_path_symbol(symbol)}.csv")
    if not os.path.isfile(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    except Exception as e:
        logger.debug("Offline fina_indicator read failed %s: %s", symbol, e)
        return {}

    if not rows:
        return {}

    out: Dict[str, Any] = {"source": "offline_fina_indicator"}
    curr = rows[0]
    out["latest_date"] = (curr.get("报告期") or "").strip() or None
    out["eps"] = _safe_float(curr.get("基本每股收益"))
    out["eps_diluted"] = _safe_float(curr.get("稀释每股收益"))
    out["revenue_per_share"] = _safe_float(curr.get("每股营业总收入"))
    out["sales_per_share"] = _safe_float(curr.get("每股营业收入"))
    out["net_assets_per_share"] = _safe_float(curr.get("每股净资产"))
    out["operating_cash_flow_per_share"] = _safe_float(curr.get("每股经营活动产生的现金流量净额"))
    out["eps_growth_yoy"] = _safe_float(curr.get("基本每股收益同比增长率"))
    out["revenue_growth_yoy"] = _safe_float(curr.get("营业总收入同比增长率"))
    out["net_profit_growth_yoy"] = _safe_float(curr.get("归属母公司股东的净利润同比增长率"))
    out["operating_profit_growth_yoy"] = _safe_float(curr.get("营业利润同比增长率"))
    # Profitability ratios (already in %).
    out["roe"] = _safe_float(curr.get("净资产收益率"))
    out["roe_weighted"] = _safe_float(curr.get("加权平均净资产收益率"))
    out["roa"] = _safe_float(curr.get("总资产净利率") or curr.get("总资产报酬率"))
    out["net_margin"] = _safe_float(curr.get("销售净利率"))
    out["gross_margin"] = _safe_float(curr.get("销售毛利率"))
    out["operating_margin"] = _safe_float(curr.get("营业利润/营业总收入"))
    # Leverage / liquidity.
    out["debt_to_equity"] = _safe_float(curr.get("资产负债率"))
    out["current_ratio"] = _safe_float(curr.get("流动比率"))
    out["quick_ratio"] = _safe_float(curr.get("速动比率"))
    out["asset_turnover"] = _safe_float(curr.get("总资产周转率"))
    out["inventory_turnover_days"] = _safe_float(curr.get("存货周转天数"))
    out["receivable_turnover_days"] = _safe_float(curr.get("应收账款周转天数"))
    # Cash flow.
    # We don't have raw cash flow line items here, but we can synthesize
    # operating_cash_flow_per_share and FCF proxies from per-share fields.
    ocf_per_share = _safe_float(curr.get("每股经营活动产生的现金流量净额"))
    if ocf_per_share is not None:
        out["operating_cash_flow_per_share"] = ocf_per_share

    # Historical snapshot (last `limit` rows) for the fast-analysis trend view.
    history: List[Dict[str, Any]] = []
    for row in rows[: max(limit, 1)]:
        history.append({
            "date": (row.get("报告期") or "").strip(),
            "eps": _safe_float(row.get("基本每股收益")),
            "roe": _safe_float(row.get("净资产收益率")),
            "revenue_growth": _safe_float(row.get("营业总收入同比增长率")),
            "net_profit_growth": _safe_float(row.get("归属母公司股东的净利润同比增长率")),
            "gross_margin": _safe_float(row.get("销售毛利率")),
            "debt_to_equity": _safe_float(row.get("资产负债率")),
        })
    out["history"] = history

    return out


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def offline_stats() -> Dict[str, Any]:
    """Cheap diagnostic snapshot for /admin endpoints or test assertions."""
    if not _OFFLINE_DIR:
        return {"available": False}
    sub = {}
    for name in ("stock_basic", "stk_factor", "weekly", "monthly", "fina_indicator"):
        p = os.path.join(_OFFLINE_DIR, name)
        if os.path.isdir(p):
            try:
                sub[name] = len([f for f in os.listdir(p) if f.endswith(".csv")])
            except Exception:
                sub[name] = None
        else:
            sub[name] = None
    return {
        "available": True,
        "path": _OFFLINE_DIR,
        "files": sub,
        "stock_basic_loaded": len(_load_stock_basic()) if _STOCK_BASIC_INDEX is None else len(_STOCK_BASIC_INDEX),
    }