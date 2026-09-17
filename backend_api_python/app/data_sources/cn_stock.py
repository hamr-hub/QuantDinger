"""
中国A股数据源 — 多层 fallback

数据源优先级（首个非空即返回）:

  Tier 0  本地 T+1 离线数据 (data/{stk_factor,weekly,monthly}/*.csv)
           —— 瞬时、不依赖网络、覆盖全部 A 股
  Tier 1  Twelve Data（付费，海外最稳）
  Tier 2  腾讯 fqkline（日/周线，免费，国内最稳）
  Tier 3  yfinance
  Tier 4  AkShare（境外偶有失败，最后兜底）

实时报价: 腾讯 qt.gtimg.cn（最快、免费）
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.tencent import normalize_cn_code, fetch_quote, parse_quote_to_ticker, fetch_kline, tencent_kline_rows_to_dicts
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.data_sources import cn_hk_offline
from app.utils.logger import get_logger

logger = get_logger(__name__)


class CNStockDataSource(BaseDataSource):
    """A股数据源（TwelveData + Tencent + yfinance + AkShare）"""

    name = "CNStock/multi-source"

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_cn_code(symbol)
        parts = fetch_quote(code)
        if not parts:
            return {"last": 0, "symbol": code}
        t = parse_quote_to_ticker(parts)
        return {
            "last": t.get("last", 0),
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 0: Local T+1 offline data (data/ directory).
        # Covers 1D/1W/1M only; for intraday 1m/5m/... we always fall through
        # to live sources. Free, instant, and works behind the GFW.
        if tf in ("1D", "1W", "1M") and cn_hk_offline.is_available():
            offline_rows = cn_hk_offline.read_offline_kline(
                symbol=code, timeframe=tf, limit=lim,
                before_time=before_time, after_time=after_time,
            )
            if offline_rows:
                logger.debug(
                    "CN offline kline hit: %s tf=%s -> %d rows",
                    code, tf, len(offline_rows),
                )
                return self.filter_and_limit(
                    offline_rows,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 1: Twelve Data (paid, most reliable)
        rows = fetch_twelvedata_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 2: Tencent for daily/weekly (fast, free)
        if tf in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(tf, "day")
            raw_rows = fetch_kline(code, period=period, count=lim, adj="qfq")
            out = tencent_kline_rows_to_dicts(raw_rows)
            if out:
                return self.filter_and_limit(
                    out,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 3: yfinance (works when Yahoo not rate-limited)
        rows = fetch_yfinance_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 4: AkShare (fragile overseas, last resort)
        if tf in ("1m", "5m", "15m", "30m", "1H", "4H"):
            rows = fetch_akshare_minute_klines(
                is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
            )
        elif tf == "1W":
            rows = fetch_akshare_weekly_klines(
                is_hk=False, tencent_code=code, limit=lim, before_time=before_time
            )
        else:
            rows = []

        return self.filter_and_limit(
            rows,
            limit=lim,
            before_time=before_time,
            after_time=after_time,
            truncate=(after_time is None),
        )
