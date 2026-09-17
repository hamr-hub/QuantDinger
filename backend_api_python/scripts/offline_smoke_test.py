#!/usr/bin/env python3
"""
Offline smoke test for the CN/HK data adapter.

Run from the backend root:
    cd QuantDinger/backend_api_python
    python scripts/offline_smoke_test.py

No network access is required. The script verifies:

- The offline dataset at ``<repo>/data`` is reachable.
- ``cn_hk_offline.get_company_profile`` returns a populated company dict
  for a known A-share (600930.SH).
- ``cn_hk_offline.read_offline_kline`` returns ≥100 daily bars for the same
  symbol, with the latest bar's timestamp within a sensible window.
- ``cn_hk_offline.read_offline_kline`` returns ≥10 weekly and monthly bars.
- ``cn_hk_offline.read_offline_financial_indicators`` returns the expected
  ratio fields (ROE, gross margin, etc.) and a non-empty ``history`` list.
- ``cn_hk_offline.search_a_share_symbols`` finds a known name (e.g. 平安银行)
  and a known pinyin (PAYH).
- ``offline_stats()`` reports a non-zero file count for every CSV bucket.

Exit code 0 on success, non-zero on any failure.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

# Allow running this script from anywhere — point at the backend package root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.data_sources import cn_hk_offline  # noqa: E402


def _expect(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ FAIL: {msg}")
        sys.exit(1)


def main() -> int:
    print("=" * 60)
    print("QuantDinger CN/HK offline smoke test")
    print("=" * 60)

    # 1. Resolve the offline directory.
    print("\n[1] Resolve offline data dir")
    is_avail = cn_hk_offline.is_available()
    print(f"  is_available() = {is_avail}")
    _expect(is_avail, "offline data dir is reachable")

    stats = cn_hk_offline.offline_stats()
    print(f"  path = {stats.get('path')}")
    _expect(stats.get("path"), "offline_stats reports a path")
    files = stats.get("files") or {}
    for k in ("stock_basic", "stk_factor", "weekly", "monthly", "fina_indicator"):
        v = files.get(k) or 0
        print(f"    {k}: {v} files")
        _expect(v > 0, f"{k} has >0 files")

    # 2. Company profile.
    print("\n[2] get_company_profile(600930.SH)")
    profile = cn_hk_offline.get_company_profile("600930.SH")
    print(f"  name={profile.get('name')!r} industry={profile.get('industry')!r}")
    _expect(profile.get("name"), "name populated")
    _expect(profile.get("industry"), "industry populated")
    _expect(profile.get("listing_date"), "listing_date populated")
    _expect(profile.get("controller"), "controller populated")

    # 3. Daily K-line.
    print("\n[3] read_offline_kline(600930.SH, 1D, 250)")
    daily = cn_hk_offline.read_offline_kline("600930.SH", "1D", limit=250)
    print(f"  rows={len(daily)}; latest ts={daily[-1]['time'] if daily else None}")
    _expect(len(daily) >= 100, "≥100 daily bars returned")
    if daily:
        latest_ts = daily[-1]["time"]
        latest_utc = datetime.fromtimestamp(latest_ts, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - latest_utc).total_seconds() / 86400
        print(f"  latest bar UTC = {latest_utc.isoformat()} (age {age_days:.1f} days)")
        # T+1 dataset: most recent bar should be from yesterday at most.
        _expect(age_days <= 7, "latest daily bar is at most a week old")
        # Spot-check shape of the first row.
        r = daily[-1]
        for k in ("time", "open", "high", "low", "close", "volume"):
            _expect(k in r and r[k] is not None, f"row has {k}")

    # 4. Weekly / monthly.
    print("\n[4] read_offline_kline(600930.SH, 1W, 100)")
    weekly = cn_hk_offline.read_offline_kline("600930.SH", "1W", limit=100)
    print(f"  rows={len(weekly)}")
    _expect(len(weekly) >= 10, "≥10 weekly bars returned")

    print("\n[5] read_offline_kline(600930.SH, 1M, 100)")
    monthly = cn_hk_offline.read_offline_kline("600930.SH", "1M", limit=100)
    print(f"  rows={len(monthly)}")
    _expect(len(monthly) >= 10, "≥10 monthly bars returned")

    # 6. Fundamentals.
    print("\n[6] read_offline_financial_indicators(600930.SH)")
    fin = cn_hk_offline.read_offline_financial_indicators("600930.SH")
    print(f"  latest_date={fin.get('latest_date')} ROE={fin.get('roe')}")
    _expect(fin.get("latest_date"), "latest_date populated")
    _expect(fin.get("roe") is not None, "ROE populated")
    _expect(fin.get("gross_margin") is not None, "gross_margin populated")
    _expect(fin.get("debt_to_equity") is not None, "debt_to_equity populated")
    hist = fin.get("history") or []
    print(f"  history rows={len(hist)}")
    _expect(len(hist) >= 4, "≥4 historical report rows in history[]")

    # 7. Symbol search.
    print("\n[7] search_a_share_symbols")
    # Known pinyin: PAYH → 平安银行 (000001.SZ)
    payh = cn_hk_offline.search_a_share_symbols("PAYH", limit=3)
    print(f"  PAYH hits: {[r['symbol'] for r in payh]}")
    _expect(any(r["name"] == "平安银行" for r in payh), "PAYH resolves to 平安银行")

    # Chinese name search.
    maotai = cn_hk_offline.search_a_share_symbols("贵州茅台", limit=3)
    print(f"  贵州茅台 hits: {[r['symbol'] for r in maotai]}")
    _expect(any("贵州茅台" in r["name"] for r in maotai), "中文名命中 贵州茅台")

    # Code prefix search.
    code600 = cn_hk_offline.search_a_share_symbols("600519", limit=3)
    print(f"  600519 hits: {[r['symbol'] for r in code600]}")
    _expect(any(r["symbol"].startswith("600519") for r in code600), "code prefix 600519 hits")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())