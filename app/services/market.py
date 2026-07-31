from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings

SYMBOL_LONDON = "LONDON_GOLD"
SYMBOL_ZHESHANG = "ZHESHANG_GOLD"

PRODUCTS = {
    SYMBOL_LONDON: {
        "code": SYMBOL_LONDON,
        "name": "伦敦金",
        "name_en": "London Gold / XAU",
        "unit": "美元/盎司",
        "currency": "USD",
        "description": "国际现货黄金（COMEX/XAU 近似），全球定价基准。",
    },
    SYMBOL_ZHESHANG: {
        "code": SYMBOL_ZHESHANG,
        "name": "浙商积存金",
        "name_en": "CZBank Accumulated Gold",
        "unit": "元/克",
        "currency": "CNY",
        "description": "浙商银行积存金（京东金融渠道报价），面向个人投资者的人民币黄金积存产品。",
    },
}


@dataclass
class LiveQuote:
    symbol: str
    name: str
    price: float
    buy_price: float | None
    sell_price: float | None
    change_amt: float | None
    change_pct: float | None
    currency: str
    unit: str
    source: str
    ts: datetime
    raw: dict[str, Any] | None = None


def _parse_pct(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(str(text).replace("%", "").strip())
    except ValueError:
        return None


def _ts_from_ms(value: str | int | float | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).astimezone()
    try:
        ms = int(value)
        return datetime.fromtimestamp(ms / 1000.0).astimezone()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).astimezone()


async def fetch_zheshang_quote() -> LiveQuote:
    """京东金融 stdLatestPrice — 浙商积存金 SKU."""
    payload = {"reqData": {"productSku": settings.zheshang_sku}}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 GoldInsight/0.1",
        "Referer": "https://m.jd.com/",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(settings.jd_price_url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    if not body.get("success"):
        raise RuntimeError(f"JD API failed: {body.get('resultMsg')}")

    data = (body.get("resultData") or {}).get("datas") or {}
    price = float(data["price"])
    yesterday = float(data.get("yesterdayPrice") or price)
    change_amt = float(data.get("upAndDownAmt") or (price - yesterday))
    change_pct = _parse_pct(data.get("upAndDownRate"))
    if change_pct is None and yesterday:
        change_pct = (price - yesterday) / yesterday * 100

    meta = PRODUCTS[SYMBOL_ZHESHANG]
    return LiveQuote(
        symbol=SYMBOL_ZHESHANG,
        name=meta["name"],
        price=price,
        buy_price=price,
        sell_price=price,
        change_amt=change_amt,
        change_pct=change_pct,
        currency=meta["currency"],
        unit=meta["unit"],
        source="jd_finance",
        ts=_ts_from_ms(data.get("time")),
        raw=data,
    )


async def fetch_london_quote() -> LiveQuote:
    """优先 Yahoo GC=F，失败则用 XAUUSD=X。"""
    import asyncio

    def _load() -> LiveQuote:
        import yfinance as yf

        last_err: Exception | None = None
        for symbol in (settings.london_yahoo_symbol, settings.london_yahoo_spot):
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="5d", interval="1d")
                if hist is None or hist.empty:
                    info = ticker.fast_info
                    price = float(getattr(info, "last_price", None) or getattr(info, "lastPrice", 0) or 0)
                    if not price:
                        raise RuntimeError(f"empty quote for {symbol}")
                    prev = price
                else:
                    price = float(hist["Close"].iloc[-1])
                    prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price

                change_amt = price - prev
                change_pct = (change_amt / prev * 100) if prev else 0.0
                meta = PRODUCTS[SYMBOL_LONDON]
                return LiveQuote(
                    symbol=SYMBOL_LONDON,
                    name=meta["name"],
                    price=round(price, 2),
                    buy_price=round(price, 2),
                    sell_price=round(price, 2),
                    change_amt=round(change_amt, 2),
                    change_pct=round(change_pct, 4),
                    currency=meta["currency"],
                    unit=meta["unit"],
                    source=f"yahoo:{symbol}",
                    ts=datetime.now().astimezone(),
                    raw={"yahoo_symbol": symbol},
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        raise RuntimeError(f"London gold quote unavailable: {last_err}")

    return await asyncio.to_thread(_load)


async def fetch_all_quotes() -> list[LiveQuote]:
    results: list[LiveQuote] = []
    errors: list[str] = []
    for fetcher in (fetch_london_quote, fetch_zheshang_quote):
        try:
            results.append(await fetcher())
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    if not results and errors:
        raise RuntimeError("; ".join(errors))
    return results


def ounces_to_cny_per_gram(usd_per_oz: float, usdcny: float) -> float:
    """美元/盎司 → 元/克（理论平价，不含银行点差）。"""
    return usd_per_oz * usdcny / settings.troy_oz_to_gram
