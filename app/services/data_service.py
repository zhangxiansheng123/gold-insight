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
            _apply_daily_change(db, q)
            persist_quote(db, q)
            _calibrate_today_bar(db, q)
            out.append(_quote_dict(q))
    finally:
        db.close()
    return out


def _apply_daily_change(db: Session, quote: LiveQuote) -> None:
    """优先用昨日日线收盘计算涨跌幅。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    prev = (
        db.query(DailyBar)
        .filter(DailyBar.symbol == quote.symbol, DailyBar.trade_date < today)
        .order_by(DailyBar.trade_date.desc())
        .first()
    )
    if not prev or not prev.close:
        return
    quote.change_amt = round(quote.price - prev.close, 2)
    quote.change_pct = round((quote.price - prev.close) / prev.close * 100, 4)


def _calibrate_today_bar(db: Session, quote: LiveQuote) -> None:
    """用实时价校准当日 K 线收盘，避免期货升贴水污染当日点位。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    existing = db.scalar(
        select(DailyBar).where(DailyBar.symbol == quote.symbol, DailyBar.trade_date == today)
    )
    if existing:
        existing.close = quote.price
        existing.high = max(existing.high, quote.price)
        existing.low = min(existing.low, quote.price)
        existing.source = quote.source
    else:
        db.add(
            DailyBar(
                symbol=quote.symbol,
                trade_date=today,
                open=quote.price,
                high=quote.price,
                low=quote.price,
                close=quote.price,
                volume=None,
                source=quote.source,
            )
        )
    db.commit()


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


def upsert_daily_bars(
    db: Session,
    symbol: str,
    frame: pd.DataFrame,
    source: str,
    *,
    protect_sources: tuple[str, ...] = (),
) -> int:
    """写入日线。若当日已有 protect_sources 中的来源，则跳过覆盖。"""
    count = 0
    for _, row in frame.iterrows():
        trade_date = pd.to_datetime(row["trade_date"]).to_pydatetime().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        existing = db.scalar(
            select(DailyBar).where(DailyBar.symbol == symbol, DailyBar.trade_date == trade_date)
        )
        if existing and existing.source in protect_sources:
            continue
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


def _scale_frame_to_anchor(frame: pd.DataFrame, anchor: float) -> pd.DataFrame:
    """把整段序列按末日收盘锚定到目标价（通常是京东积存金现价）。"""
    out = frame.copy()
    last = float(out["close"].iloc[-1])
    if last <= 0 or anchor <= 0:
        return out
    factor = anchor / last
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * factor
    return out


def _estimate_zheshang_from_london(
    london_df: pd.DataFrame,
    fx_df: pd.DataFrame | None = None,
    usdcny: float = 7.2,
    markup: float = 1.008,
) -> pd.DataFrame:
    """伦敦金 → 元/克。优先逐日 USDCNY，缺日用最近汇率或默认值。"""
    oz = settings.troy_oz_to_gram
    out = london_df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.normalize()
    if fx_df is not None and not fx_df.empty:
        fx = fx_df.copy()
        fx["trade_date"] = pd.to_datetime(fx["trade_date"]).dt.normalize()
        fx = fx[["trade_date", "usdcny"]].drop_duplicates("trade_date").sort_values("trade_date")
        out = pd.merge_asof(
            out.sort_values("trade_date"),
            fx,
            on="trade_date",
            direction="backward",
        )
        out["usdcny"] = out["usdcny"].ffill().fillna(usdcny)
    else:
        out["usdcny"] = usdcny

    factor = out["usdcny"] / oz * markup
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * factor
    out["volume"] = None
    return out.drop(columns=["usdcny"], errors="ignore")


def fetch_au9999_history(limit: int = 800) -> pd.DataFrame | None:
    """
    东财 · 上金所黄金9999 日线（元/克），比伦敦×固定汇率更接近积存金。
    网络不稳定时返回 None，由调用方降级。
    """
    import httpx

    hosts = (
        "https://push2his.eastmoney.com",
        "https://push2delay.eastmoney.com",
        "https://82.push2his.eastmoney.com",
    )
    params = {
        "secid": settings.au9999_eastmoney_secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": str(limit),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
    }
    for host in hosts:
        for _ in range(2):
            try:
                with httpx.Client(timeout=25.0, headers=headers) as client:
                    resp = client.get(f"{host}/api/qt/stock/kline/get", params=params)
                    resp.raise_for_status()
                    payload = resp.json()
                klines = ((payload or {}).get("data") or {}).get("klines") or []
                if not klines:
                    continue
                rows = []
                for line in klines:
                    parts = str(line).split(",")
                    if len(parts) < 6:
                        continue
                    rows.append(
                        {
                            "trade_date": pd.to_datetime(parts[0]),
                            "open": float(parts[1]),
                            "close": float(parts[2]),
                            "high": float(parts[3]),
                            "low": float(parts[4]),
                            "volume": float(parts[5]) if parts[5] not in ("", "-") else None,
                        }
                    )
                if not rows:
                    continue
                frame = pd.DataFrame(rows).sort_values("trade_date")
                frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.tz_localize(None)
                return frame.reset_index(drop=True)
            except Exception:  # noqa: BLE001
                continue
    return None


def _snapshot_daily_ohlc(db: Session, symbol: str) -> pd.DataFrame:
    """把实时快照聚合成日 OHLC（开=首笔，收=末笔）。"""
    snaps = (
        db.query(QuoteSnapshot)
        .filter(QuoteSnapshot.symbol == symbol)
        .order_by(QuoteSnapshot.ts.asc())
        .all()
    )
    if not snaps:
        return pd.DataFrame()
    by_day: dict[str, list[float]] = {}
    for s in snaps:
        key = s.ts.strftime("%Y-%m-%d")
        by_day.setdefault(key, []).append(float(s.price))
    rows = []
    for day, prices in by_day.items():
        rows.append(
            {
                "trade_date": datetime.strptime(day, "%Y-%m-%d"),
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": None,
            }
        )
    return pd.DataFrame(rows).sort_values("trade_date")


def sync_zheshang_history(period: str = "2y") -> dict[str, Any]:
    """
    浙商积存金日线（优先级）：
    1) 东财黄金9999（人民币/克）骨架，按京东现价锚定缩放
    2) 失败则用伦敦金 × 逐日 USDCNY，再锚定到京东现价
    3) 用已采集 QuoteSnapshot 覆盖对应交易日（真实报价优先）
    """
    import yfinance as yf

    live_price = _fetch_zheshang_price_sync()
    backbone: pd.DataFrame | None = None
    source_tag = "estimated:london*usdcny"

    au = fetch_au9999_history(limit=900)
    if au is not None and not au.empty:
        backbone = au
        source_tag = "eastmoney:AU9999"

    if backbone is None:
        fx_hist = yf.Ticker("USDCNY=X").history(period=period, auto_adjust=True)
        usdcny = 7.2
        fx_df = None
        if fx_hist is not None and not fx_hist.empty:
            usdcny = float(fx_hist["Close"].iloc[-1])
            fx_df = fx_hist.reset_index()
            dcol = "Date" if "Date" in fx_df.columns else fx_df.columns[0]
            fx_df = fx_df.rename(columns={dcol: "trade_date", "Close": "usdcny"})
            fx_df["trade_date"] = pd.to_datetime(fx_df["trade_date"]).dt.tz_localize(None)
            fx_df = fx_df[["trade_date", "usdcny"]]

        london = yf.Ticker(settings.london_yahoo_symbol).history(period=period, auto_adjust=True)
        if london is None or london.empty:
            london = yf.Ticker(settings.london_yahoo_spot).history(period=period, auto_adjust=True)
        if london is None or london.empty:
            raise RuntimeError("无法拉取积存金历史骨架（AU9999 与伦敦金均失败）")
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
        backbone = _estimate_zheshang_from_london(frame, fx_df=fx_df, usdcny=usdcny)
        source_tag = "estimated:london*daily_usdcny"

    if live_price:
        backbone = _scale_frame_to_anchor(backbone, live_price)
        source_tag = f"{source_tag}+jd_anchor"

    db = SessionLocal()
    snap_frame = pd.DataFrame()
    snap_n = 0
    try:
        inserted = upsert_daily_bars(
            db,
            SYMBOL_ZHESHANG,
            backbone,
            source_tag,
            protect_sources=("jd_snapshot", "jd_finance", "jd_snapshot_ohlc"),
        )

        snap_frame = _snapshot_daily_ohlc(db, SYMBOL_ZHESHANG)
        if not snap_frame.empty:
            snap_n = upsert_daily_bars(db, SYMBOL_ZHESHANG, snap_frame, "jd_snapshot_ohlc")

        if live_price is not None:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            existing = db.scalar(
                select(DailyBar).where(
                    DailyBar.symbol == SYMBOL_ZHESHANG, DailyBar.trade_date == today
                )
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
        "source": source_tag,
        "anchor": live_price,
        "inserted": inserted,
        "snapshot_days": int(len(snap_frame)) if snap_frame is not None and not snap_frame.empty else 0,
        "snapshot_upserted": snap_n,
        "total": total,
        "note": "优先黄金9999+京东锚定；失败则伦敦×逐日汇率；快照日覆盖为真实报价。",
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


def load_history_df(
    symbol: str,
    days: int | None = None,
    as_of: datetime | str | None = None,
) -> pd.DataFrame:
    """加载日线。as_of 用于回测：只取该日（含）之前的数据，避免未来信息泄露。"""
    cutoff = None
    if as_of is not None:
        if isinstance(as_of, str):
            cutoff = datetime.strptime(as_of[:10], "%Y-%m-%d")
        else:
            cutoff = as_of.replace(hour=0, minute=0, second=0, microsecond=0)

    db = SessionLocal()
    try:
        q = db.query(DailyBar).filter(DailyBar.symbol == symbol).order_by(DailyBar.trade_date.asc())
        if cutoff is not None:
            q = q.filter(DailyBar.trade_date <= cutoff)
        if days:
            anchor = cutoff or datetime.now()
            since = anchor - timedelta(days=days)
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
