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


def save_forecast(result: PredictionResult, made_on: datetime | str | None = None) -> int:
    """将一次预测的逐日高低点写入归档（同日同目标覆盖）。"""
    made = _as_day(made_on) if made_on is not None else datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    db = SessionLocal()
    saved = 0
    try:
        for point in result.points:
            target = _as_day(point.date)
            existing = db.scalar(
                select(ForecastRecord).where(
                    ForecastRecord.symbol == result.symbol,
                    ForecastRecord.made_on == made,
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
                        made_on=made,
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


def backfill_forecasts(
    symbol: str,
    start: str,
    end: str,
    horizon: int = 7,
    lookback_days: int = 400,
    warmup_days: int = 14,
) -> dict[str, Any]:
    """
    按历史交易日回放预测并落库，用于回看准确率。
    每个 as_of 日只用当日及之前的日线，不掺实时价，避免偷看未来。
    warmup_days：从 start 再往前多跑几天，让月初目标日也有「前一日」预测可对照。
    """
    from app.services.data_service import load_history_df

    start_day = _as_day(start)
    end_day = _as_day(end)
    made_from = start_day - timedelta(days=warmup_days)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # 取区间内有日线的交易日作为生成日
    db = SessionLocal()
    try:
        bars = (
            db.query(DailyBar.trade_date)
            .filter(
                DailyBar.symbol == symbol,
                DailyBar.trade_date >= made_from,
                DailyBar.trade_date <= end_day,
                DailyBar.trade_date < today,  # 今天仍走实时落库逻辑，不覆盖
            )
            .order_by(DailyBar.trade_date.asc())
            .all()
        )
        as_of_list = [b.trade_date.replace(hour=0, minute=0, second=0, microsecond=0) for b in bars]
    finally:
        db.close()

    runs = 0
    saved_total = 0
    errors: list[str] = []
    for as_of in as_of_list:
        try:
            df = load_history_df(symbol, days=lookback_days, as_of=as_of)
            if df.empty or len(df) < 80:
                continue
            frame = to_ohlc_frame(df.to_dict(orient="records"))
            result = predict_price(frame, symbol, horizon_days=horizon)
            saved_total += save_forecast(result, made_on=as_of)
            runs += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{as_of.strftime('%Y-%m-%d')}: {exc}")

    # 回填后按目标日落在 [start, end] 给一份速览
    preview = query_forecasts(symbol, start=start, end=end)
    return {
        "symbol": symbol,
        "name": PRODUCTS[symbol]["name"],
        "made_on_from": made_from.strftime("%Y-%m-%d"),
        "made_on_to": end_day.strftime("%Y-%m-%d"),
        "target_start": start,
        "target_end": end,
        "runs": runs,
        "saved_points": saved_total,
        "errors": errors[:10],
        "preview_count": preview["count"],
        "hit_rate": preview["hit_rate"],
        "accuracy_rate": preview["accuracy_rate"],
        "hit_count": preview["hit_count"],
        "scored_count": preview["scored_count"],
        "preview_hits": preview["hit_count"],
        "preview_scored": preview["scored_count"],
    }


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
                # 准确：实际收盘是否落在预测高低之间
                accurate = r.low <= actual.close <= r.high
                item["accurate"] = accurate
                item["close_in_band"] = accurate  # 兼容旧字段
            else:
                item["error"] = None
                item["in_range"] = None
                item["accurate"] = None
                item["close_in_band"] = None
            items.append(item)

        hit = [i for i in items if i["accurate"] is True]
        scored = [i for i in items if i["accurate"] is not None]
        accuracy = round(len(hit) / len(scored), 4) if scored else None
        return {
            "symbol": symbol,
            "name": PRODUCTS[symbol]["name"],
            "unit": PRODUCTS[symbol]["unit"],
            "count": len(items),
            "hit_count": len(hit),
            "scored_count": len(scored),
            "accuracy_rate": accuracy,
            "hit_rate": accuracy,  # 兼容旧字段
            "items": items,
        }
    finally:
        db.close()


def run_and_save_forecast(symbol: str, horizon: int = 7, days: int = 365) -> dict[str, Any]:
    from app.services.data_service import load_history_df

    df = load_history_df(symbol, days=days)
    if df.empty:
        raise ValueError("暂无历史，请先同步数据")
    frame = to_ohlc_frame(df.to_dict(orient="records"))
    frame = _anchor_last_bar_to_live(frame, symbol)
    result = predict_price(frame, symbol, horizon_days=horizon)
    # 重算落库时并入今日已实现高低，避免「上车点还高于盘中已见最低」
    result = _blend_realized_extremes(result, symbol)
    saved = save_forecast(result)
    payload = result_to_dict(result)
    payload["saved_points"] = saved
    payload["name"] = PRODUCTS[symbol]["name"]
    payload["unit"] = PRODUCTS[symbol]["unit"]
    return payload


def _latest_live_price(symbol: str) -> float | None:
    from app.db import QuoteSnapshot

    db = SessionLocal()
    try:
        row = (
            db.query(QuoteSnapshot)
            .filter(QuoteSnapshot.symbol == symbol)
            .order_by(QuoteSnapshot.ts.desc())
            .first()
        )
        return float(row.price) if row else None
    finally:
        db.close()


def _today_session_range(symbol: str) -> tuple[float | None, float | None]:
    """今日已出现的行情最低/最高（来自快照）。"""
    from app.db import QuoteSnapshot

    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    db = SessionLocal()
    try:
        rows = (
            db.query(QuoteSnapshot)
            .filter(QuoteSnapshot.symbol == symbol, QuoteSnapshot.ts >= start)
            .all()
        )
        if not rows:
            return None, None
        prices = [float(r.price) for r in rows if r.price is not None]
        if not prices:
            return None, None
        return min(prices), max(prices)
    finally:
        db.close()


def _blend_realized_extremes(result: PredictionResult, symbol: str) -> PredictionResult:
    """
    仅在落库重算时调用：把今日已实现最低/最高、现价并入最近预测日的上下限。
    盘中不刷新；下次定时/手动重算才会再吸收新的极值。
    """
    if not result.points:
        return result

    live = _latest_live_price(symbol)
    session_low, session_high = _today_session_range(symbol)

    # 交易点取最近一天；同时把实现区间反映到该点，保证上车<=已见最低
    first = result.points[0]
    lows = [first.lower]
    highs = [first.upper]
    if session_low is not None:
        lows.append(session_low)
    if session_high is not None:
        highs.append(session_high)
    if live is not None:
        lows.append(live)
        highs.append(live)

    first.lower = round(min(lows), 2)
    first.upper = round(max(highs), 2)
    if first.lower > first.upper:
        first.lower, first.upper = first.upper, first.lower
    return result


def _anchor_last_bar_to_live(frame, symbol: str):
    """用实时价校准末日收盘，避免积存金日线滞后把预测整体抬高/打低。"""
    import pandas as pd

    live = _latest_live_price(symbol)
    if live is None or frame is None or frame.empty:
        return frame
    out = frame.copy()
    idx = out.index[-1]
    out.loc[idx, "close"] = live
    out.loc[idx, "high"] = max(float(out.loc[idx, "high"]), live)
    out.loc[idx, "low"] = min(float(out.loc[idx, "low"]), live)
    # 保证 trade_date 仍是 datetime
    if not pd.api.types.is_datetime64_any_dtype(out["trade_date"]):
        out["trade_date"] = pd.to_datetime(out["trade_date"])
    return out


def get_zheshang_entry_exit(horizon: int = 7) -> dict[str, Any]:
    """
    短线上车/下车点（仅浙商积存金）:
    - 上车点 = 已落库预测的模型下限
    - 下车点 = 已落库预测的模型上限
    只读数据库，不在此处重算；由定时任务或手动「生成预测」更新。
    """
    del horizon  # 保留参数兼容调用方
    live = _latest_live_price(SYMBOL_ZHESHANG)
    point = _nearest_forecast_point(SYMBOL_ZHESHANG)
    if point is None:
        raise ValueError("暂无预测数据，请先点击生成预测或等待定时重算")

    entry = round(float(point["lower"]), 2)
    exit_px = round(float(point["upper"]), 2)
    mid = round(float(point["predicted"]), 2)
    if entry > exit_px:
        entry, exit_px = exit_px, entry

    return {
        "symbol": SYMBOL_ZHESHANG,
        "name": PRODUCTS[SYMBOL_ZHESHANG]["name"],
        "unit": PRODUCTS[SYMBOL_ZHESHANG]["unit"],
        "target_date": point["date"],
        "entry": entry,
        "exit": exit_px,
        "mid": mid,
        "live": round(live, 2) if live is not None else None,
        "model_low": entry,
        "model_high": exit_px,
        "made_on": point.get("made_on"),
        "updated_at": point.get("updated_at"),
    }


def _nearest_forecast_point(symbol: str) -> dict[str, Any] | None:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    db = SessionLocal()
    try:
        row = (
            db.query(ForecastRecord)
            .filter(ForecastRecord.symbol == symbol, ForecastRecord.target_date >= today)
            .order_by(ForecastRecord.target_date.asc(), ForecastRecord.made_on.desc(), ForecastRecord.created_at.desc())
            .first()
        )
        if not row:
            row = (
                db.query(ForecastRecord)
                .filter(ForecastRecord.symbol == symbol)
                .order_by(ForecastRecord.created_at.desc())
                .first()
            )
        if not row:
            return None
        return {
            "date": row.target_date.strftime("%Y-%m-%d"),
            "predicted": float(row.predicted),
            "lower": float(row.low),
            "upper": float(row.high),
            "made_on": row.made_on.strftime("%Y-%m-%d"),
            "updated_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        }
    finally:
        db.close()


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
