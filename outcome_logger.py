# outcome_logger.py
# Shared Supabase REST logger for all VMc1 Railway services
# Usage: from outcome_logger import OutcomeLogger

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
RAILWAY_SERVICE_NAME = os.environ.get("VMC1_SERVICE_NAME") or os.environ.get("RAILWAY_SERVICE_NAME", "unknown")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


class OutcomeLogger:
    def __init__(self, service_name: str = RAILWAY_SERVICE_NAME):
        self.service_name = service_name
        self._service_id: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS)
        return self._session

    async def _get_service_id(self) -> Optional[str]:
        if self._service_id:
            return self._service_id
        session = await self._get_session()
        url = f"{SUPABASE_URL}/rest/v1/services"
        params = {
            "railway_service_name": f"eq.{self.service_name}",
            "select": "id",
        }
        try:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if data:
                    self._service_id = data[0]["id"]
                    return self._service_id
                logger.error(f"[OutcomeLogger] Service not found: {self.service_name}")
        except Exception as e:
            logger.error(f"[OutcomeLogger] Failed to fetch service_id: {e}")
        return None

    async def _post(self, table: str, payload: dict) -> Optional[dict]:
        service_id = await self._get_service_id()
        if not service_id:
            return None
        payload["service_id"] = service_id
        session = await self._get_session()
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                text = await resp.text()
                logger.error(f"[OutcomeLogger] POST {table} failed {resp.status}: {text}")
        except Exception as e:
            logger.error(f"[OutcomeLogger] POST {table} exception: {e}")
        return None

    async def log_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        position_size: float,
        pnl_usd: float,
        pnl_pct: float,
        entry_at: datetime,
        exit_at: datetime,
        entry_signal: Optional[str] = None,
        exit_reason: Optional[str] = None,
        mae: Optional[float] = None,
        mfe: Optional[float] = None,
        hold_bars: Optional[int] = None,
        config_id: Optional[str] = None,
    ) -> Optional[dict]:
        payload = {
            "symbol": symbol.upper(),
            "direction": direction.lower(),
            "entry_price": str(entry_price),
            "exit_price": str(exit_price),
            "position_size": str(position_size),
            "pnl_usd": str(pnl_usd),
            "pnl_pct": str(pnl_pct),
            "entry_at": entry_at.astimezone(timezone.utc).isoformat(),
            "exit_at": exit_at.astimezone(timezone.utc).isoformat(),
        }
        if entry_signal:
            payload["entry_signal"] = entry_signal
        if exit_reason:
            payload["exit_reason"] = exit_reason
        if mae is not None:
            payload["mae"] = str(mae)
        if mfe is not None:
            payload["mfe"] = str(mfe)
        if hold_bars is not None:
            payload["hold_bars"] = hold_bars
        if config_id:
            payload["config_id"] = config_id
        result = await self._post("trade_outcomes", payload)
        if result:
            logger.info(f"[OutcomeLogger] Trade logged: {symbol} {direction} pnl={pnl_usd:.2f}")
        return result

    async def log_signal(
        self,
        symbol: str,
        signal_type: str,
        trade_taken: bool,
        score: Optional[float] = None,
        signal_detail: Optional[dict] = None,
        skip_reason: Optional[str] = None,
    ) -> Optional[dict]:
        payload = {
            "symbol": symbol.upper(),
            "signal_type": signal_type,
            "trade_taken": trade_taken,
            "signal_detail": signal_detail or {},
            "fired_at": datetime.now(timezone.utc).isoformat(),
        }
        if score is not None:
            payload["score"] = str(score)
        if skip_reason:
            payload["skip_reason"] = skip_reason
        return await self._post("signal_events", payload)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


_default_logger: Optional[OutcomeLogger] = None

def get_logger(service_name: str = RAILWAY_SERVICE_NAME) -> OutcomeLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = OutcomeLogger(service_name)
    return _default_logger
