"""
Company info / fundamentals routes.

For A股 symbols we read straight from the local ``data/`` T+1 dataset
(``stock_basic`` for company profile + ``fina_indicator`` for financial
ratios) so the response is instant and never rate-limited.

HK symbols fall back to the live Tencent quote + Twelve Data / AkShare
fundamentals path wired in :mod:`app.data_sources.cn_hk_fundamentals`.

Endpoints:
  GET /api/company/profile?market=CNStock&symbol=600519.SH
    → returns company name (中/英), pinyin, industry, listing date, exchange,
      controller, HK-connect flag, etc.

  GET /api/company/fundamentals?market=CNStock&symbol=600519.SH
    → returns latest financial ratios (EPS, ROE, gross/net margin, debt,
      growth YoY) plus a small history block from ``fina_indicator``.

  GET /api/company/full?market=CNStock&symbol=600519.SH
    → combines profile + fundamentals + (when available) live quote snapshot.

  GET /api/market/offline-status
    → diagnostic snapshot of the offline dataset (file counts, path) so
      operators can confirm the bind-mount landed correctly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import jsonify, request

from app.data_sources import cn_hk_offline, tencent
from app.data_sources.cn_hk_fundamentals import (
    fetch_cn_fundamental_akshare,
    fetch_hk_fundamental_akshare,
    fetch_cn_company_extras,
    fetch_hk_company_extras,
)
from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.utils.logger import get_logger

logger = get_logger(__name__)

company_blp = Blueprint("company", __name__)


def _validate_a_symbol(market: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Echo an error response when the market/symbol pair is invalid, else None."""
    if not market or not symbol:
        return {"code": 0, "msg": "market and symbol are required", "data": None}, 400
    if market not in ("CNStock", "HKStock"):
        return {"code": 0, "msg": f"market {market!r} not supported on this endpoint", "data": None}, 400
    return None


def _resolve_tencent_code(market: str, symbol: str) -> str:
    if market == "CNStock":
        return tencent.normalize_cn_code(symbol)
    return tencent.normalize_hk_code(symbol)


def _empty_payload(market: str, symbol: str, **extra: Any) -> Dict[str, Any]:
    """Build a response envelope with code=1 and an empty data payload."""
    payload: Dict[str, Any] = {"market": market, "symbol": symbol}
    payload.update(extra)
    return payload


@company_blp.route("/profile", methods=["GET"])
def get_profile():
    """Company profile (industry, listing date, controller, etc.)."""
    market = (request.args.get("market") or "").strip()
    symbol = (request.args.get("symbol") or "").strip()
    err = _validate_a_symbol(market, symbol)
    if err is not None:
        return jsonify(err[0]), err[1]

    if market == "CNStock":
        # Try offline first.
        offline = cn_hk_offline.get_company_profile(symbol) if cn_hk_offline.is_available() else {}
        if offline:
            return jsonify({"code": 1, "msg": "success", "data": offline})

        tencent_code = _resolve_tencent_code(market, symbol)
        extras = fetch_cn_company_extras(tencent_code)
        if not extras:
            return jsonify({"code": 1, "msg": "success", "data": _empty_payload(market, symbol)})
        return jsonify({"code": 1, "msg": "success", "data": {"market": market, "symbol": symbol, **extras}})

    # HKStock: Tencent quote gives the name; AkShare Eastmoney fills in extras.
    tencent_code = _resolve_tencent_code(market, symbol)
    name = ""
    try:
        parts = tencent.fetch_quote(tencent_code)
        if parts and len(parts) > 1 and parts[1]:
            name = str(parts[1]).strip()
    except Exception as e:
        logger.debug("HK company profile: tencent quote failed %s: %s", symbol, e)
    extras = fetch_hk_company_extras(tencent_code)
    payload = {"market": market, "symbol": symbol}
    if name:
        payload["name"] = name
    payload.update(extras or {})
    return jsonify({"code": 1, "msg": "success", "data": payload})


@company_blp.route("/fundamentals", methods=["GET"])
def get_fundamentals():
    """Financial ratios (PE/PB/EPS/ROE/growth/margins/etc.)."""
    market = (request.args.get("market") or "").strip()
    symbol = (request.args.get("symbol") or "").strip()
    err = _validate_a_symbol(market, symbol)
    if err is not None:
        return jsonify(err[0]), err[1]

    tencent_code = _resolve_tencent_code(market, symbol)
    if market == "CNStock":
        data = fetch_cn_fundamental_akshare(tencent_code)
    else:
        data = fetch_hk_fundamental_akshare(tencent_code)

    if not data:
        return jsonify({"code": 1, "msg": "success", "data": _empty_payload(market, symbol)})
    return jsonify({"code": 1, "msg": "success", "data": {"market": market, "symbol": symbol, **data}})


@company_blp.route("/full", methods=["GET"])
def get_full():
    """Profile + fundamentals + best-effort live quote snapshot."""
    market = (request.args.get("market") or "").strip()
    symbol = (request.args.get("symbol") or "").strip()
    err = _validate_a_symbol(market, symbol)
    if err is not None:
        return jsonify(err[0]), err[1]

    tencent_code = _resolve_tencent_code(market, symbol)

    if market == "CNStock":
        profile = cn_hk_offline.get_company_profile(symbol) if cn_hk_offline.is_available() else {}
        if not profile:
            profile = fetch_cn_company_extras(tencent_code) or {}
        fundamentals = fetch_cn_fundamental_akshare(tencent_code)
    else:
        # HK: profile + fundamentals from AkShare Eastmoney + Tencent quote for name.
        fundamentals = fetch_hk_fundamental_akshare(tencent_code)
        profile = fetch_hk_company_extras(tencent_code) or {}
        try:
            parts = tencent.fetch_quote(tencent_code)
            if parts and len(parts) > 1 and parts[1]:
                profile.setdefault("name", str(parts[1]).strip())
        except Exception as e:
            logger.debug("HK full: tencent quote failed %s: %s", symbol, e)

    quote: Dict[str, Any] = {}
    try:
        parts = tencent.fetch_quote(tencent_code)
        if parts and len(parts) > 5:
            quote = {
                "last": float(parts[3]) if parts[3] else 0,
                "previous_close": float(parts[4]) if parts[4] else 0,
                "open": float(parts[5]) if parts[5] else 0,
                "high": float(parts[33]) if len(parts) > 33 and parts[33] else 0,
                "low": float(parts[34]) if len(parts) > 34 and parts[34] else 0,
                "source": "tencent",
            }
    except Exception as e:
        logger.debug("Tencent quote snapshot failed %s:%s: %s", market, symbol, e)

    data = {
        "market": market,
        "symbol": symbol,
        "profile": profile,
        "fundamentals": fundamentals,
        "quote": quote,
    }
    return jsonify({"code": 1, "msg": "success", "data": data})


@company_blp.route("/offline-status", methods=["GET"])
def offline_status():
    """Diagnostic snapshot of the offline ``data/`` dataset."""
    stats = cn_hk_offline.offline_stats()
    return jsonify({"code": 1, "msg": "success", "data": stats})


# openapi-compat alias used by some legacy imports.
company_bp = company_blp