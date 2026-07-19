#!/usr/bin/env python3
"""Build and finalize multi-market dates, phases and data policies."""

from __future__ import annotations

import copy
import argparse
import datetime as dt
import json
from typing import Any
from urllib.parse import urlsplit

from timezone_utils import localize_utc, timezone_label_for_date

UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8), "CST")

MARKET_ORDER = ("US", "CN", "HK", "JP", "KR", "TW", "AU", "EU")
MARKETS: dict[str, dict[str, Any]] = {
    "US": {
        "name": "美股", "timezone": "America/New_York", "open": "09:30", "close": "16:00",
        "probes": ["SPY", "QQQ", "DIA"],
        "official": [
            {"name": "NYSE", "host": "nyse.com", "url": "https://www.nyse.com/markets/hours-calendars"},
            {"name": "Nasdaq Trader", "host": "nasdaqtrader.com", "url": "https://www.nasdaqtrader.com/trader.aspx?id=Calendar"},
        ],
    },
    "CN": {
        "name": "A股", "timezone": "Asia/Shanghai", "open": "09:30", "close": "15:00",
        "probes": ["000001.SS", "399001.SZ"],
        "official": [
            {"name": "上交所", "host": "sse.com.cn", "query": "上交所 官方 休市安排 交易日历"},
            {"name": "深交所", "host": "szse.cn", "query": "深交所 官方 休市安排 交易日历"},
        ],
    },
    "HK": {
        "name": "港股", "timezone": "Asia/Hong_Kong", "open": "09:30", "close": "16:00",
        "probes": ["^HSI"],
        "official": [{"name": "HKEX", "host": "hkex.com.hk", "query": "HKEX official trading calendar holidays"}],
    },
    "JP": {
        "name": "日股", "timezone": "Asia/Tokyo", "open": "09:00", "close": "15:30",
        "probes": ["^N225"],
        "official": [{"name": "JPX", "host": "jpx.co.jp", "query": "JPX official trading calendar holidays"}],
    },
    "KR": {
        "name": "韩股", "timezone": "Asia/Seoul", "open": "09:00", "close": "15:30",
        "probes": ["^KS11"],
        "official": [{"name": "KRX", "host": "global.krx.co.kr", "query": "KRX official trading calendar holidays"}],
    },
    "TW": {
        "name": "台股", "timezone": "Asia/Taipei", "open": "09:00", "close": "13:30",
        "probes": ["^TWII"],
        "official": [{"name": "TWSE", "host": "twse.com.tw", "query": "TWSE official trading calendar holidays"}],
    },
    "AU": {
        "name": "澳股", "timezone": "Australia/Sydney", "open": "10:00", "close": "16:00",
        "probes": ["^AXJO"],
        "official": [{"name": "ASX", "host": "asx.com.au", "query": "ASX official trading calendar holidays"}],
    },
    "EU": {
        "name": "欧股", "timezone": "Europe/Berlin", "open": "09:00", "close": "17:30",
        "probes": ["^GDAXI", "^STOXX"],
        "official": [{"name": "Deutsche Boerse", "host": "deutsche-boerse.com", "query": "Deutsche Boerse Xetra official trading calendar"}],
    },
}

SYMBOL_MARKETS = {
    "000001.SS": "CN", "399001.SZ": "CN", "^HSI": "HK", "^N225": "JP",
    "^KS11": "KR", "^TWII": "TW", "^AXJO": "AU", "^GDAXI": "EU", "^STOXX": "EU",
}
CONTINUOUS_SYMBOLS = {"CL=F", "GC=F", "DX-Y.NYB", "BTC-USD", "CNH=X", "AUDUSD=X", "CAD=X"}


def _aware_utc(value: dt.datetime | None) -> dt.datetime:
    value = value or dt.datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clock(value: str) -> dt.time:
    hour, minute = (int(part) for part in value.split(":"))
    return dt.time(hour, minute)


def market_for_symbol(symbol: str) -> str:
    if symbol in CONTINUOUS_SYMBOLS:
        return "GLOBAL"
    if symbol in SYMBOL_MARKETS:
        return SYMBOL_MARKETS[symbol]
    if symbol.endswith((".SS", ".SZ")):
        return "CN"
    if symbol.endswith(".HK"):
        return "HK"
    if symbol.endswith(".KS"):
        return "KR"
    if symbol.endswith(".TW"):
        return "TW"
    if symbol.endswith(".F"):
        return "EU"
    return "US"


def _synthetic_utc(mode: str, anchor_date: dt.date) -> dt.datetime:
    if mode == "pre":
        beijing = dt.datetime.combine(anchor_date, dt.time(20, 30), tzinfo=BEIJING)
    else:
        beijing = dt.datetime.combine(anchor_date + dt.timedelta(days=1), dt.time(8, 30), tzinfo=BEIJING)
    return beijing.astimezone(UTC)


def report_anchor(mode: str, now_utc: dt.datetime | None = None,
                  override_date: dt.date | None = None) -> tuple[dt.date, dt.datetime]:
    if mode not in {"pre", "post"}:
        raise ValueError(f"unsupported mode: {mode}")
    if override_date:
        return override_date, _synthetic_utc(mode, override_date) if now_utc is None else _aware_utc(now_utc)
    current = _aware_utc(now_utc)
    beijing_date = localize_utc(current, "Asia/Shanghai").date()
    anchor = beijing_date if mode == "pre" else beijing_date - dt.timedelta(days=1)
    return anchor, current


def _phase(local_now: dt.datetime, market: dict[str, Any]) -> str:
    if local_now.weekday() >= 5:
        return "closed"
    current = local_now.timetz().replace(tzinfo=None)
    if current < _clock(market["open"]):
        return "preopen"
    if current < _clock(market["close"]):
        return "open"
    return "completed"


def _base_policy(mode: str, market_code: str, phase: str) -> str:
    if phase == "closed":
        return "latest_regular_no_attribution"
    if market_code == "US":
        return "pre_market_only" if mode == "pre" else "regular_plus_post"
    if mode == "pre":
        return "current_regular" if phase in {"open", "completed"} else "previous_regular"
    return "current_regular" if phase in {"open", "completed"} else "previous_regular"


def build_initial_context(mode: str, now_utc: dt.datetime | None = None,
                          override_date: dt.date | None = None) -> dict[str, Any]:
    anchor, current = report_anchor(mode, now_utc, override_date)
    beijing_now = localize_utc(current, "Asia/Shanghai")
    markets: dict[str, Any] = {}
    for code in MARKET_ORDER:
        definition = MARKETS[code]
        local_now = localize_utc(current, definition["timezone"])
        target = anchor if code == "US" else local_now.date()
        phase = _phase(local_now, definition)
        scheduled = "scheduled_closed" if target.weekday() >= 5 else "scheduled_open"
        markets[code] = {
            "market": code,
            "name": definition["name"],
            "timezone": definition["timezone"],
            "timezone_label": local_now.tzname() or definition["timezone"],
            "target_local_date": target.isoformat(),
            "local_time_at_run": local_now.isoformat(timespec="seconds"),
            "calendar_status": scheduled,
            "session_type": "regular",
            "session_phase": "closed" if scheduled == "scheduled_closed" else phase,
            "latest_data_date": None,
            "evidence": [{"type": "session_template", "result": scheduled, "observed_at": current.isoformat(timespec="seconds")}],
            "confidence": "inferred",
            "data_policy": _base_policy(mode, code, phase),
            "attribution_allowed": False,
            "official_required": False,
            "official_reason": None,
            "probe_symbols": list(definition["probes"]),
            "official_sources": copy.deepcopy(definition["official"]),
        }
    return {
        "schema_version": 1,
        "mode": mode,
        "created_at_utc": current.isoformat(timespec="seconds"),
        "beijing_task_date": beijing_now.date().isoformat(),
        "beijing_task_time": beijing_now.isoformat(timespec="seconds"),
        "report_anchor": {
            "market": "US",
            "date": anchor.isoformat(),
            "session": "upcoming" if mode == "pre" else "completed",
            "timezone": "America/New_York",
            "timezone_label": timezone_label_for_date("America/New_York", anchor),
        },
        "markets": markets,
        "official_required_markets": [],
    }


def _probe_rows(snapshot: dict[str, Any], market_code: str) -> list[dict[str, Any]]:
    if market_code == "US":
        return [row for row in (snapshot.get("status_probes") or {}).values() if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    records = snapshot.get("records") or {}
    for symbol in MARKETS[market_code]["probes"]:
        record = records.get(symbol) or {}
        regular = (record.get("regular") or {}).get("time") or {}
        pre = (record.get("pre_market") or {}).get("time") or {}
        post = (record.get("post_market") or {}).get("time") or {}
        rows.append({
            "symbol": symbol,
            "market_state_raw": record.get("market_state_raw") or "UNKNOWN",
            "regular_date": regular.get("market_date"),
            "pre_date": pre.get("market_date"),
            "post_date": post.get("market_date"),
            "error": record.get("error"),
        })
    return rows


def _latest_date(rows: list[dict[str, Any]]) -> str | None:
    dates = [str(row.get("regular_date")) for row in rows if row.get("regular_date")]
    return max(dates) if dates else None


def apply_yahoo_evidence(context: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(context)
    required: list[str] = []
    live_states = {"PRE", "PREPRE", "REGULAR", "POST", "POSTPOST"}
    for code in MARKET_ORDER:
        market = result["markets"][code]
        target = market["target_local_date"]
        rows = _probe_rows(snapshot, code)
        regular_fresh = [row for row in rows if row.get("regular_date") == target]
        any_fresh = [row for row in rows if target in {row.get("regular_date"), row.get("pre_date"), row.get("post_date")}]
        live = [row for row in rows if row.get("market_state_raw") in live_states]
        market["latest_data_date"] = _latest_date(rows)
        market["evidence"].append({
            "type": "yahoo_probe",
            "symbols": [row.get("symbol") for row in rows],
            "regular_fresh_count": len(regular_fresh),
            "any_fresh_count": len(any_fresh),
            "live_count": len(live),
            "states": {str(row.get("symbol")): row.get("market_state_raw") for row in rows},
        })
        scheduled_closed = market["calendar_status"] == "scheduled_closed"
        phase = market["session_phase"]
        if regular_fresh:
            if scheduled_closed or phase == "preopen":
                market["calendar_status"] = "unconfirmed"
                market["confidence"] = "conflicting"
                market["official_required"] = True
                market["official_reason"] = "目标日期行情与本地时段/周末规则冲突"
            else:
                market["calendar_status"] = "scheduled_open"
                market["confidence"] = "confirmed"
                market["attribution_allowed"] = True
        elif scheduled_closed:
            market["calendar_status"] = "scheduled_closed"
            market["session_phase"] = "closed"
            market["confidence"] = "inferred"
            market["data_policy"] = "latest_regular_no_attribution"
        elif phase in {"open", "completed"}:
            market["calendar_status"] = "unconfirmed"
            market["confidence"] = "unconfirmed"
            market["official_required"] = True
            market["official_reason"] = "预期已开盘但 Yahoo 探针没有目标日期常规行情"
            market["data_policy"] = "latest_regular_no_attribution"
        elif any_fresh and live:
            market["calendar_status"] = "scheduled_open"
            market["confidence"] = "confirmed"
            market["attribution_allowed"] = True
        elif phase == "preopen" and code == "EU" and result.get("mode") == "post":
            # The morning post report intentionally uses Europe's previous close;
            # it does not need to confirm that day's later European session.
            market["calendar_status"] = "scheduled_open"
            market["confidence"] = "inferred"
            market["data_policy"] = "previous_regular"
        elif phase == "preopen" and live:
            market["calendar_status"] = "scheduled_open"
            market["confidence"] = "inferred"
        else:
            market["calendar_status"] = "unconfirmed"
            market["confidence"] = "unconfirmed"
            market["official_required"] = True
            market["official_reason"] = "盘前阶段无法仅凭 Yahoo 区分正常待开盘与节假日休市"
            market["data_policy"] = "previous_regular"
        if market["official_required"]:
            required.append(code)
    result["official_required_markets"] = required
    result["updated_at_utc"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
    return result


def _host_matches(url: str, expected: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    expected = expected.lower().rstrip(".")
    return host == expected or host.endswith("." + expected)


def finalize_context(context: dict[str, Any], search_context: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(context)
    confirmations = search_context.get("market_status_confirmations")
    confirmations = confirmations if isinstance(confirmations, dict) else {}
    for code in result.get("official_required_markets", []):
        market = result["markets"][code]
        entry = confirmations.get(code)
        statuses_by_host: dict[str, str] = {}
        valid_sources: list[dict[str, Any]] = []
        expected_hosts = {item["host"].lower() for item in market["official_sources"]}
        if isinstance(entry, dict):
            for source in entry.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                status = str(source.get("status") or "unavailable")
                url = str(source.get("url") or "")
                if status not in {"open", "early_close", "closed"}:
                    continue
                matched_host = next((item["host"].lower() for item in market["official_sources"] if _host_matches(url, item["host"])), None)
                if not matched_host:
                    continue
                statuses_by_host[matched_host] = status
                valid_sources.append(source)
        unique = set(statuses_by_host.values())
        if set(statuses_by_host) == expected_hosts and len(unique) == 1 and valid_sources:
            status = next(iter(unique))
            market["evidence"].append({"type": "official_calendar", "status": status, "sources": valid_sources})
            market["confidence"] = "confirmed"
            market["official_required"] = False
            if status == "closed":
                market["calendar_status"] = "scheduled_closed"
                market["session_phase"] = "closed"
                market["data_policy"] = "latest_regular_no_attribution"
                market["attribution_allowed"] = False
            else:
                market["calendar_status"] = "scheduled_open"
                market["attribution_allowed"] = market["latest_data_date"] == market["target_local_date"]
                if status == "early_close":
                    market["session_type"] = "early_close"
                    market["evidence"].append({"type": "session_note", "result": "early_close"})
        else:
            market["calendar_status"] = "unconfirmed"
            market["confidence"] = "conflicting" if len(unique) > 1 else "unconfirmed"
            market["attribution_allowed"] = False
            market["data_policy"] = "latest_regular_no_attribution"
    result["finalized_at_utc"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
    return result


def status_label(market: dict[str, Any]) -> str:
    status = market.get("calendar_status")
    if status == "scheduled_closed":
        return "休市"
    if status == "unconfirmed":
        return "状态未确认"
    phase = market.get("session_phase")
    label = {"preopen": "待开盘", "open": "交易中", "completed": "已收盘", "closed": "休市"}.get(str(phase), "正常交易日")
    if phase in {"open", "completed"} and market.get("attribution_allowed") is False:
        label += "（行情未确认）"
    return f"半日市（{label}）" if market.get("session_type") == "early_close" else label


def market_summary(context: dict[str, Any]) -> str:
    return "；".join(
        f"{context['markets'][code]['name']} {context['markets'][code]['target_local_date']} {status_label(context['markets'][code])}"
        for code in MARKET_ORDER
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("pre", "post"), required=True)
    parser.add_argument("--date", help="Override US anchor date YYYY-MM-DD; for deployment checks/tests")
    args = parser.parse_args()
    context = build_initial_context(
        args.mode,
        override_date=dt.date.fromisoformat(args.date) if args.date else None,
    )
    print(json.dumps(context, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
