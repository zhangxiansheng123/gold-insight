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
        "name_en": "London Gold / XAUUSD Spot",
        "unit": "美元/盎司",
        "currency": "USD",
        "description": "伦敦现货黄金（XAU/USD spot），全球定价基准。",
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
    """伦敦金现货：优先 goldprice.dev，其次 gold-api.com，最后 Yahoo 期货兜底。"""
    errors: list[str] = []
    for fetcher in (_fetch_london_goldprice_dev, _fetch_london_gold_api, _fetch_london_yahoo_fallback):
        try:
            return await fetcher()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fetcher.__name__}: {exc}")
    raise RuntimeError("伦敦金报价不可用: " + " | ".join(errors))


async def _fetch_london_goldprice_dev() -> LiveQuote:
    headers = {"User-Agent": "Mozilla/5.0 GoldInsight/0.1", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(
            settings.london_spot_url,
            params={"symbol": settings.london_spot_symbol},
        )
        resp.raise_for_status()
        body = resp.json()

    rows = body.get("symbols") or []
    if not rows:
        # 兼容单对象返回
        row = body if body.get("price") is not None else None
    else:
        row = rows[0]
    if not row or row.get("price") is None:
        raise RuntimeError(f"empty goldprice.dev payload: {body}")

    price = float(row["price"])
    bid = float(row["bid"]) if row.get("bid") not in (None, "") else price
    ask = float(row["ask"]) if row.get("ask") not in (None, "") else price
    mid = round((bid + ask) / 2, 2) if bid and ask else round(price, 2)

    meta = PRODUCTS[SYMBOL_LONDON]
    return LiveQuote(
        symbol=SYMBOL_LONDON,
        name=meta["name"],
        price=mid,
        buy_price=round(bid, 2),
        sell_price=round(ask, 2),
        change_amt=None,
        change_pct=None,
        currency=meta["currency"],
        unit=meta["unit"],
        source="goldprice.dev",
        ts=datetime.now().astimezone(),
        raw=row,
    )


async def _fetch_london_gold_api() -> LiveQuote:
    headers = {"User-Agent": "Mozilla/5.0 GoldInsight/0.1", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(settings.london_spot_fallback_url)
        resp.raise_for_status()
        body = resp.json()
    price = float(body["price"])
    meta = PRODUCTS[SYMBOL_LONDON]
    return LiveQuote(
        symbol=SYMBOL_LONDON,
        name=meta["name"],
        price=round(price, 2),
        buy_price=round(price, 2),
        sell_price=round(price, 2),
        change_amt=None,
        change_pct=None,
        currency=meta["currency"],
        unit=meta["unit"],
        source="gold-api.com",
        ts=datetime.now().astimezone(),
        raw=body,
    )


async def _fetch_london_yahoo_fallback() -> LiveQuote:
    """仅作兜底：COMEX 期货，可能与伦敦现货有升贴水差异。"""
    import asyncio

    def _load() -> LiveQuote:
        import yfinance as yf

        ticker = yf.Ticker(settings.london_yahoo_symbol)
        hist = ticker.history(period="5d", interval="1d")
        if hist is None or hist.empty:
            raise RuntimeError("yahoo GC=F empty")
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
            source=f"yahoo:{settings.london_yahoo_symbol}",
            ts=datetime.now().astimezone(),
            raw={"yahoo_symbol": settings.london_yahoo_symbol, "note": "futures_fallback"},
        )

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

    # 用本地昨日快照补齐涨跌（现货源通常不给昨日价）
    _fill_change_from_cache(results)
    return results


def _fill_change_from_cache(quotes: list[LiveQuote]) -> None:
    try:
        from app.db import QuoteSnapshot, SessionLocal
        from sqlalchemy import select
    except Exception:  # noqa: BLE001
        return

    db = SessionLocal()
    try:
        for q in quotes:
            if q.change_pct is not None:
                continue
            prev = db.scalars(
                select(QuoteSnapshot)
                .where(QuoteSnapshot.symbol == q.symbol, QuoteSnapshot.price != q.price)
                .order_by(QuoteSnapshot.ts.desc())
                .limit(1)
            ).first()
            if not prev or not prev.price:
                # 退一步：取更早一条
                prev = db.scalars(
                    select(QuoteSnapshot)
                    .where(QuoteSnapshot.symbol == q.symbol)
                    .order_by(QuoteSnapshot.ts.desc())
                    .offset(1)
                    .limit(1)
                ).first()
            if not prev or not prev.price:
                continue
            q.change_amt = round(q.price - prev.price, 2)
            q.change_pct = round((q.price - prev.price) / prev.price * 100, 4)
    finally:
        db.close()


def ounces_to_cny_per_gram(usd_per_oz: float, usdcny: float) -> float:
    """美元/盎司 → 元/克（理论平价，不含银行点差）。"""
    return usd_per_oz * usdcny / settings.troy_oz_to_gram
