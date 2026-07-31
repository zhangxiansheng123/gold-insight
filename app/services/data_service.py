from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import DailyBar, QuoteSnapshot, SessionLocal
from app.services.market import (
    SYMBOL_LONDON,
    SYMBOL_ZHESHANG,
    LiveQuote,
    fetch_all_quotes,
)


def persist_quote(db: Session, quote: LiveQuote) -> QuoteSnapshot:
    row = QuoteSnapshot(
        symbol=quote.symbol,
        price=quote.price,
        buy_price=quote.buy_price,
        sell_price=quote.sell_price,
        change_amt=quote.change_amt,
        change_pct=quote.change_pct,
        currency=quote.currency,
        unit=quote.unit,
        source=quote.source,
        ts=quote.ts.replace(tzinfo=None) if quote.ts.tzinfo else quote.ts,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


async def collect_live_quotes() -> list[dict[str, Any]]:
    quotes = await fetch_all_quotes()
    db = SessionLocal()
    out: list[dict[str, Any]] = []
    try:
        for q in quotes:
            persist_quote(db, q)
            out.append(_quote_dict(q))
    finally:
        db.close()
    return out


def _quote_dict(q: LiveQuote) -> dict[str, Any]:
    return {
        "symbol": q.symbol,
        "name": q.name,
        "price": q.price,
        "buy_price": q.buy_price,
        "sell_price": q.sell_price,
        "change_amt": q.change_amt,
        "change_pct": q.change_pct,
        "currency": q.currency,
        "unit": q.unit,
        "source": q.source,
        "ts": q.ts.isoformat(),
    }


def upsert_daily_bars(db: Session, symbol: str, frame: pd.DataFrame, source: str) -> int:
    count = 0
    for _, row in frame.iterrows():
        trade_date = pd.to_datetime(row["trade_date"]).to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)
        existing = db.scalar(
            select(DailyBar).where(DailyBar.symbol == symbol, DailyBar.trade_date == trade_date)
        )
        if existing:
            existing.open = float(row["open"])
            existing.high = float(row["high"])
            existing.low = float(row["low"])
            existing.close = float(row["close"])
            existing.volume = float(row["volume"]) if pd.notna(row.get("volume")) else None
            existing.source = source
        else:
            db.add(
                DailyBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]) if pd.notna(row.get("volume")) else None,
                    source=source,
                )
            )
            count += 1
    db.commit()
    return count


def sync_london_history(period: str = "2y") -> dict[str, Any]:
    import yfinance as yf

    last_err: Exception | None = None
    hist = None
    used = settings.london_yahoo_symbol
    for symbol in (settings.london_yahoo_symbol, settings.london_yahoo_spot):
        try:
            hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
            if hist is not None and not hist.empty:
                used = symbol
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if hist is None or hist.empty:
        raise RuntimeError(f"无法拉取伦敦金历史: {last_err}")

    frame = hist.reset_index()
    date_col = "Date" if "Date" in frame.columns else frame.columns[0]
    frame = frame.rename(
        columns={
            date_col: "trade_date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    frame = frame[["trade_date", "open", "high", "low", "close", "volume"]]
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.tz_localize(None)

    db = SessionLocal()
    try:
        inserted = upsert_daily_bars(db, SYMBOL_LONDON, frame, f"yahoo:{used}")
        total = db.query(DailyBar).filter(DailyBar.symbol == SYMBOL_LONDON).count()
    finally:
        db.close()
    return {"symbol": SYMBOL_LONDON, "source": f"yahoo:{used}", "inserted": inserted, "total": total}


def _estimate_zheshang_from_london(london_df: pd.DataFrame, usdcny: float = 7.2, markup: float = 1.012) -> pd.DataFrame:
    """用伦敦金 + 汇率粗略推算积存金历史（元/克），用于冷启动。"""
    oz = settings.troy_oz_to_gram
    out = london_df.copy()
    factor = usdcny / oz * markup
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * factor
    out["volume"] = None
    return out


def sync_zheshang_history(period: str = "2y") -> dict[str, Any]:
    """
    积存金缺少公开长历史时：
    1) 用伦敦金换算生成估算日线（source=estimated）
    2) 用已采集的 QuoteSnapshot 覆盖当日真实价
    """
    import yfinance as yf

    fx = yf.Ticker("USDCNY=X").history(period="5d")
    usdcny = float(fx["Close"].iloc[-1]) if fx is not None and not fx.empty else 7.2

    london = yf.Ticker(settings.london_yahoo_symbol).history(period=period, auto_adjust=True)
    if london is None or london.empty:
        london = yf.Ticker(settings.london_yahoo_spot).history(period=period, auto_adjust=True)
    if london is None or london.empty:
        raise RuntimeError("无法用伦敦金推算浙商积存金历史")

    frame = london.reset_index()
    date_col = "Date" if "Date" in frame.columns else frame.columns[0]
    frame = frame.rename(
        columns={
            date_col: "trade_date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    frame = frame[["trade_date", "open", "high", "low", "close", "volume"]]
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.tz_localize(None)
    estimated = _estimate_zheshang_from_london(frame, usdcny=usdcny)

    db = SessionLocal()
    try:
        inserted = upsert_daily_bars(db, SYMBOL_ZHESHANG, estimated, "estimated:london*usdcny")
        # 用真实快照修正最近交易日
        snaps = (
            db.query(QuoteSnapshot)
            .filter(QuoteSnapshot.symbol == SYMBOL_ZHESHANG)
            .order_by(QuoteSnapshot.ts.desc())
            .limit(500)
            .all()
        )
        if snaps:
            by_day: dict[str, list[float]] = {}
            for s in snaps:
                key = s.ts.strftime("%Y-%m-%d")
                by_day.setdefault(key, []).append(s.price)
            for day, prices in by_day.items():
                trade_date = datetime.strptime(day, "%Y-%m-%d")
                px = float(sum(prices) / len(prices))
                existing = db.scalar(
                    select(DailyBar).where(
                        DailyBar.symbol == SYMBOL_ZHESHANG,
                        DailyBar.trade_date == trade_date,
                    )
                )
                if existing:
                    existing.close = px
                    existing.high = max(existing.high, px)
                    existing.low = min(existing.low, px)
                    existing.source = "jd_snapshot+estimated"
                else:
                    db.add(
                        DailyBar(
                            symbol=SYMBOL_ZHESHANG,
                            trade_date=trade_date,
                            open=px,
                            high=px,
                            low=px,
                            close=px,
                            volume=None,
                            source="jd_snapshot",
                        )
                    )
            db.commit()

        # 若此刻有实时价，写入今日（同步 HTTP，避免事件循环嵌套）
        live_price = _fetch_zheshang_price_sync()
        if live_price is not None:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            existing = db.scalar(
                select(DailyBar).where(DailyBar.symbol == SYMBOL_ZHESHANG, DailyBar.trade_date == today)
            )
            if existing:
                existing.close = live_price
                existing.high = max(existing.high, live_price)
                existing.low = min(existing.low, live_price)
                existing.source = "jd_finance"
            else:
                db.add(
                    DailyBar(
                        symbol=SYMBOL_ZHESHANG,
                        trade_date=today,
                        open=live_price,
                        high=live_price,
                        low=live_price,
                        close=live_price,
                        volume=None,
                        source="jd_finance",
                    )
                )
            db.commit()
        total = db.query(DailyBar).filter(DailyBar.symbol == SYMBOL_ZHESHANG).count()
    finally:
        db.close()

    return {
        "symbol": SYMBOL_ZHESHANG,
        "source": "estimated+jd",
        "usdcny": usdcny,
        "inserted": inserted,
        "total": total,
        "note": "长历史由伦敦金×汇率估算，近期由京东报价校准；仅供模型训练参考。",
    }


def _fetch_zheshang_price_sync() -> float | None:
    import httpx

    try:
        payload = {"reqData": {"productSku": settings.zheshang_sku}}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 GoldInsight/0.1",
            "Referer": "https://m.jd.com/",
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(settings.jd_price_url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        data = ((body.get("resultData") or {}).get("datas")) or {}
        return float(data["price"])
    except Exception:  # noqa: BLE001
        return None


def load_history_df(symbol: str, days: int | None = None) -> pd.DataFrame:
    db = SessionLocal()
    try:
        q = db.query(DailyBar).filter(DailyBar.symbol == symbol).order_by(DailyBar.trade_date.asc())
        if days:
            since = datetime.now() - timedelta(days=days)
            q = q.filter(DailyBar.trade_date >= since)
        rows = q.all()
        data = [
            {
                "trade_date": r.trade_date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in rows
        ]
    finally:
        db.close()
    return pd.DataFrame(data)


def latest_cached_quotes() -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        out = []
        for symbol in (SYMBOL_LONDON, SYMBOL_ZHESHANG):
            row = (
                db.query(QuoteSnapshot)
                .filter(QuoteSnapshot.symbol == symbol)
                .order_by(QuoteSnapshot.ts.desc())
                .first()
            )
            if not row:
                continue
            name = "伦敦金" if symbol == SYMBOL_LONDON else "浙商积存金"
            out.append(
                {
                    "symbol": row.symbol,
                    "name": name,
                    "price": row.price,
                    "buy_price": row.buy_price,
                    "sell_price": row.sell_price,
                    "change_amt": row.change_amt,
                    "change_pct": row.change_pct,
                    "currency": row.currency,
                    "unit": row.unit,
                    "source": row.source,
                    "ts": row.ts.isoformat(),
                    "cached": True,
                }
            )
        return out
    finally:
        db.close()


async def ensure_bootstrap() -> dict[str, Any]:
    """启动时确保有行情与历史。"""
    result: dict[str, Any] = {"quotes": [], "history": {}}
    try:
        result["quotes"] = await collect_live_quotes()
    except Exception as exc:  # noqa: BLE001
        result["quote_error"] = str(exc)
        result["quotes"] = latest_cached_quotes()

    for syncer, key in ((sync_london_history, SYMBOL_LONDON), (sync_zheshang_history, SYMBOL_ZHESHANG)):
        try:
            result["history"][key] = await asyncio.to_thread(syncer)
        except Exception as exc:  # noqa: BLE001
            result["history"][key] = {"error": str(exc)}
    return result
