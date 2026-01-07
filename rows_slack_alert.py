#!/usr/bin/env python3
"""Fetch today's cost per lead from Rows and notify via Slack.

Environment variables:
  ROWS_API_KEY: Rows API key (required)
  ROWS_TABLE_ID: Rows table ID (required)
  ROWS_API_BASE_URL: Base URL for Rows API (optional, default: https://rows.com/api/v2)
  ROWS_DATE_COLUMN: Column name that contains the date (optional, default: Fecha)
  ROWS_COST_COLUMN: Column name that contains the cost per lead (optional,
    default: Costo por cliente potencial)
  SLACK_WEBHOOK_URL: Slack incoming webhook URL (required)
  TIMEZONE: IANA timezone for "today" (optional, default: America/Argentina/Buenos_Aires)

Example usage (cron hourly):
  0 * * * * ROWS_API_KEY=... ROWS_TABLE_ID=... SLACK_WEBHOOK_URL=... /usr/bin/python3 /path/to/rows_slack_alert.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - for older Python
    ZoneInfo = None  # type: ignore[assignment]


DEFAULT_API_BASE_URL = "https://rows.com/api/v2"
DEFAULT_DATE_COLUMN = "Fecha"
DEFAULT_COST_COLUMN = "Costo por cliente potencial"
DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _rows_request(api_key: str, url: str) -> Dict[str, Any]:
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Rows API error: {exc.code} {exc.reason} {detail}") from exc
    return json.loads(payload)


def _slack_post(webhook_url: str, text: str) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(webhook_url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Slack webhook error: {exc.code} {exc.reason} {detail}") from exc


def _iter_rows(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    rows = payload.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                yield row
        return

    data = payload.get("data")
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                yield row


def _column_lookup(columns: List[Dict[str, Any]]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for column in columns:
        column_id = str(column.get("id") or "").strip()
        name = str(column.get("name") or column.get("title") or "").strip()
        if column_id and name:
            lookup[column_id] = name
    return lookup


def _normalize_row(row: Dict[str, Any], column_lookup: Dict[str, str]) -> Dict[str, Any]:
    if "values" in row and isinstance(row["values"], dict):
        return row["values"]

    if "cells" in row and isinstance(row["cells"], list):
        normalized: Dict[str, Any] = {}
        for cell in row["cells"]:
            if not isinstance(cell, dict):
                continue
            column_id = str(cell.get("columnId") or "").strip()
            name = column_lookup.get(column_id) or str(cell.get("columnName") or "").strip()
            if name:
                normalized[name] = cell.get("value")
        if normalized:
            return normalized

    return row


def _parse_cost(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"sin datos", "n/a", "na"}:
        return None
    match = re.search(r"[0-9]+(?:[\.,][0-9]+)?", text)
    if not match:
        return None
    normalized = match.group(0).replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _today_date(timezone: str) -> dt.date:
    if ZoneInfo is None:
        return dt.datetime.utcnow().date()
    return dt.datetime.now(ZoneInfo(timezone)).date()


def _select_message(cost: float) -> str:
    if 1.0 <= cost < 2.5:
        return "La campaña viene bien"
    if 2.5 <= cost <= 3.5:
        return "Echarle un ojo"
    if cost > 3.5:
        return "Cuidado, el costo puede dispararse"
    return "Costo fuera de rango configurado"


def _find_today_cost(
    rows: Iterable[Dict[str, Any]],
    columns: List[Dict[str, Any]],
    date_column: str,
    cost_column: str,
    today: dt.date,
) -> Optional[float]:
    lookup = _column_lookup(columns)
    for row in rows:
        normalized = _normalize_row(row, lookup)
        date_value = normalized.get(date_column)
        if isinstance(date_value, (dt.date, dt.datetime)):
            row_date = date_value.date() if isinstance(date_value, dt.datetime) else date_value
        else:
            try:
                row_date = dt.date.fromisoformat(str(date_value))
            except (TypeError, ValueError):
                continue
        if row_date != today:
            continue
        return _parse_cost(normalized.get(cost_column))
    return None


def main() -> int:
    api_key = _require_env("ROWS_API_KEY")
    table_id = _require_env("ROWS_TABLE_ID")
    webhook_url = _require_env("SLACK_WEBHOOK_URL")

    api_base = os.getenv("ROWS_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
    date_column = os.getenv("ROWS_DATE_COLUMN", DEFAULT_DATE_COLUMN)
    cost_column = os.getenv("ROWS_COST_COLUMN", DEFAULT_COST_COLUMN)
    timezone = os.getenv("TIMEZONE", DEFAULT_TIMEZONE)

    url = f"{api_base}/tables/{table_id}/rows"
    payload = _rows_request(api_key, url)

    rows = list(_iter_rows(payload))
    columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []

    today = _today_date(timezone)
    cost = _find_today_cost(rows, columns, date_column, cost_column, today)
    if cost is None:
        _slack_post(
            webhook_url,
            f"No hay datos de costo para {today.isoformat()} en Rows.",
        )
        return 0

    message = _select_message(cost)
    _slack_post(
        webhook_url,
        f"Costo por cliente potencial ({today.isoformat()}): {cost:.2f}. {message}",
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover - runtime guard
        print(str(exc), file=sys.stderr)
        sys.exit(1)
