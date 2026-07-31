from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.db import DailyBar, ForecastRecord, SessionLocal
from app.services.indicators import to_ohlc_frame
from app.services.market import PRODUCTS, SYMBOL_LONDON, SYMBOL_ZHESHANG
from app.services.predictor import PredictionResult, predict_price, result_to_dict


def _as_day(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        return datetime.strptime(dt[:10], "%Y-%m-%d")
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def save_forecast(result: PredictionResult) -> int:
    """将一次预测的逐日高低点写入归档（同日同目标覆盖）。"""
    made_on = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    db = SessionLocal()
    saved = 0
    try:
        for point in result.points:
            target = _as_day(point.date)
            existing = db.scalar(
                select(ForecastRecord).where(
                    ForecastRecord.symbol == result.symbol,
                    ForecastRecord.made_on == made_on,
                    ForecastRecord.target_date == target,
                )
            )
            if existing:
                existing.predicted = point.predicted
                existing.high = point.upper
                existing.low = point.lower
                existing.base_price = result.current_price
                existing.horizon_days = result.horizon_days
                existing.confidence = result.confidence
                existing.model = result.model
                existing.created_at = datetime.now()
            else:
                db.add(
                    ForecastRecord(
                        symbol=result.symbol,
                        made_on=made_on,
                        target_date=target,
                        predicted=point.predicted,
                        high=point.upper,
                        low=point.lower,
                        base_price=result.current_price,
                        horizon_days=result.horizon_days,
                        confidence=result.confidence,
                        model=result.model,
                        created_at=datetime.now(),
                    )
                )
            saved += 1
        db.commit()
    finally:
        db.close()
    return saved


def query_forecasts(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    made_on: str | None = None,
) -> dict[str, Any]:
    """
    按目标日区间查询预测历史。
    同一目标日若有多次预测，取最新 made_on 的一条。
    """
    db = SessionLocal()
    try:
        q = db.query(ForecastRecord).filter(ForecastRecord.symbol == symbol)
        if made_on:
            q = q.filter(ForecastRecord.made_on == _as_day(made_on))
        if start:
            q = q.filter(ForecastRecord.target_date >= _as_day(start))
        if end:
            q = q.filter(ForecastRecord.target_date <= _as_day(end))
        if not start and not end and not made_on:
            # 默认最近 30 天目标日
            since = datetime.now() - timedelta(days=30)
            q = q.filter(ForecastRecord.target_date >= since.replace(hour=0, minute=0, second=0, microsecond=0))

        rows = q.order_by(ForecastRecord.target_date.asc(), ForecastRecord.made_on.desc()).all()

        # 每个 target_date 只保留最新一次预测
        latest: dict[str, ForecastRecord] = {}
        for row in rows:
            key = row.target_date.strftime("%Y-%m-%d")
            if key not in latest:
                latest[key] = row

        # 实际行情对照
        actual_map: dict[str, DailyBar] = {}
        if latest:
            dates = [r.target_date for r in latest.values()]
            bars = (
                db.query(DailyBar)
                .filter(
                    DailyBar.symbol == symbol,
                    DailyBar.trade_date >= min(dates),
                    DailyBar.trade_date <= max(dates),
                )
                .all()
            )
            for b in bars:
                actual_map[b.trade_date.strftime("%Y-%m-%d")] = b

        items = []
        for key in sorted(latest.keys()):
            r = latest[key]
            actual = actual_map.get(key)
            item = {
                "target_date": key,
                "made_on": r.made_on.strftime("%Y-%m-%d"),
                "predicted": round(r.predicted, 2),
                "high": round(r.high, 2),
                "low": round(r.low, 2),
                "base_price": round(r.base_price, 2),
                "horizon_days": r.horizon_days,
                "confidence": r.confidence,
                "model": r.model,
                "actual_close": round(actual.close, 2) if actual else None,
                "actual_high": round(actual.high, 2) if actual else None,
                "actual_low": round(actual.low, 2) if actual else None,
            }
            if actual:
                item["error"] = round(actual.close - r.predicted, 2)
                item["in_range"] = actual.low <= r.high and actual.high >= r.low
                # 更严：收盘是否落在预测高低之间
                item["close_in_band"] = r.low <= actual.close <= r.high
            else:
                item["error"] = None
                item["in_range"] = None
                item["close_in_band"] = None
            items.append(item)

        hit = [i for i in items if i["close_in_band"] is True]
        scored = [i for i in items if i["close_in_band"] is not None]
        return {
            "symbol": symbol,
            "name": PRODUCTS[symbol]["name"],
            "unit": PRODUCTS[symbol]["unit"],
            "count": len(items),
            "hit_rate": round(len(hit) / len(scored), 4) if scored else None,
            "items": items,
        }
    finally:
        db.close()


def run_and_save_forecast(symbol: str, horizon: int = 7, days: int = 365) -> dict[str, Any]:
    from app.services.data_service import load_history_df

    df = load_history_df(symbol, days=days)
    if df.empty:
        raise ValueError("暂无历史，请先同步数据")
    result = predict_price(to_ohlc_frame(df.to_dict(orient="records")), symbol, horizon_days=horizon)
    saved = save_forecast(result)
    payload = result_to_dict(result)
    payload["saved_points"] = saved
    payload["name"] = PRODUCTS[symbol]["name"]
    payload["unit"] = PRODUCTS[symbol]["unit"]
    return payload


def daily_forecast_job() -> dict[str, Any]:
    """定时：为双品种落库当日预测。"""
    out: dict[str, Any] = {}
    for symbol in (SYMBOL_LONDON, SYMBOL_ZHESHANG):
        try:
            out[symbol] = {
                "saved": run_and_save_forecast(symbol, horizon=7)["saved_points"],
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001
            out[symbol] = {"ok": False, "error": str(exc)}
    return out
