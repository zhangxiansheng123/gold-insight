from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.data_service import (
    collect_live_quotes,
    latest_cached_quotes,
    load_history_df,
    sync_london_history,
    sync_zheshang_history,
)
from app.services.indicators import add_indicators, latest_signal_summary, to_ohlc_frame
from app.services.market import PRODUCTS, SYMBOL_LONDON, SYMBOL_ZHESHANG
from app.services.predictor import predict_price, result_to_dict

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "product": "Gold Insight"}


@router.get("/products")
def products() -> dict[str, Any]:
    return {"items": list(PRODUCTS.values())}


@router.get("/quotes")
async def quotes(refresh: bool = Query(True, description="是否强制刷新实时价")) -> dict[str, Any]:
    if refresh:
        try:
            items = await collect_live_quotes()
            return {"items": items, "live": True}
        except Exception as exc:  # noqa: BLE001
            cached = latest_cached_quotes()
            if cached:
                return {"items": cached, "live": False, "warning": str(exc)}
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": latest_cached_quotes(), "live": False}


@router.post("/sync/{symbol}")
async def sync_history(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    try:
        if symbol == SYMBOL_LONDON:
            return sync_london_history()
        if symbol == SYMBOL_ZHESHANG:
            return sync_zheshang_history()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail=f"未知品种: {symbol}")


@router.get("/history/{symbol}")
def history(
    symbol: str,
    days: int = Query(180, ge=30, le=2000),
    with_indicators: bool = True,
) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in PRODUCTS:
        raise HTTPException(status_code=404, detail="未知品种")
    df = load_history_df(symbol, days=days)
    if df.empty:
        raise HTTPException(status_code=404, detail="暂无历史数据，请先调用 /api/sync/{symbol}")
    if with_indicators:
        df = add_indicators(to_ohlc_frame(df.to_dict(orient="records")))
    records = []
    for _, row in df.iterrows():
        item = {
            "date": row["trade_date"].strftime("%Y-%m-%d")
            if hasattr(row["trade_date"], "strftime")
            else str(row["trade_date"])[:10],
            "open": _num(row.get("open")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "close": _num(row.get("close")),
            "volume": _num(row.get("volume")),
        }
        if with_indicators:
            for key in ("ma5", "ma20", "ma60", "rsi14", "macd", "macd_signal", "boll_upper", "boll_mid", "boll_lower"):
                item[key] = _num(row.get(key))
        records.append(item)
    signal = latest_signal_summary(df) if with_indicators else None
    return {
        "symbol": symbol,
        "name": PRODUCTS[symbol]["name"],
        "unit": PRODUCTS[symbol]["unit"],
        "count": len(records),
        "items": records,
        "signal": signal,
    }


@router.get("/predict/{symbol}")
def predict(
    symbol: str,
    horizon: int = Query(7, ge=1, le=30, description="预测天数 1-30"),
    days: int = Query(365, ge=90, le=2000),
    persist: bool = Query(False, description="是否落库预测高低点（定时任务/手动生成时为 true）"),
) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in PRODUCTS:
        raise HTTPException(status_code=404, detail="未知品种")
    try:
        from app.services.forecast_store import run_and_save_forecast

        if persist:
            return run_and_save_forecast(symbol, horizon=horizon, days=days)

        df = load_history_df(symbol, days=days)
        if df.empty:
            raise HTTPException(status_code=404, detail="暂无历史，请先同步数据")
        result = predict_price(to_ohlc_frame(df.to_dict(orient="records")), symbol, horizon_days=horizon)
        payload = result_to_dict(result)
        payload["name"] = PRODUCTS[symbol]["name"]
        payload["unit"] = PRODUCTS[symbol]["unit"]
        payload["saved_points"] = 0
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/forecasts/{symbol}")
def forecasts(
    symbol: str,
    start: str | None = Query(None, description="目标日起始 YYYY-MM-DD"),
    end: str | None = Query(None, description="目标日结束 YYYY-MM-DD"),
    made_on: str | None = Query(None, description="仅看某日生成的预测 YYYY-MM-DD"),
) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in PRODUCTS:
        raise HTTPException(status_code=404, detail="未知品种")
    from app.services.forecast_store import query_forecasts

    return query_forecasts(symbol, start=start, end=end, made_on=made_on)


@router.post("/forecasts/{symbol}/backfill")
def backfill_forecasts_api(
    symbol: str,
    start: str = Query("2026-07-01", description="目标日区间起始"),
    end: str = Query("2026-07-31", description="目标日区间结束"),
    horizon: int = Query(7, ge=1, le=14),
) -> dict[str, Any]:
    """按历史日线回放预测并落库，用于查看区间准确率。"""
    symbol = symbol.upper()
    if symbol not in PRODUCTS:
        raise HTTPException(status_code=404, detail="未知品种")
    from app.services.forecast_store import backfill_forecasts

    try:
        return backfill_forecasts(symbol, start=start, end=end, horizon=horizon)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/entry-exit")
def entry_exit() -> dict[str, Any]:
    """浙商积存金短线上车/下车点。"""
    from app.services.forecast_store import get_zheshang_entry_exit

    try:
        return get_zheshang_entry_exit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/compare")
def compare(days: int = Query(90, ge=30, le=730)) -> dict[str, Any]:
    """伦敦金与浙商积存金真实价格对比（双轴对齐日期）。"""
    frames = {}
    for symbol in (SYMBOL_LONDON, SYMBOL_ZHESHANG):
        df = load_history_df(symbol, days=days)
        if df.empty:
            continue
        df = to_ohlc_frame(df.to_dict(orient="records"))
        frames[symbol] = {
            r.trade_date.strftime("%Y-%m-%d"): float(r.close) for r in df.itertuples()
        }

    if len(frames) < 2:
        raise HTTPException(status_code=404, detail="对比数据不足，请先同步两个品种历史")

    dates = sorted(set(frames[SYMBOL_LONDON]) & set(frames[SYMBOL_ZHESHANG]))
    if len(dates) < 5:
        raise HTTPException(status_code=404, detail="两品种重叠交易日不足")

    london_series = [{"date": d, "close": frames[SYMBOL_LONDON][d]} for d in dates]
    zs_series = [{"date": d, "close": frames[SYMBOL_ZHESHANG][d]} for d in dates]

    import pandas as pd

    a = pd.Series([x["close"] for x in london_series], index=dates)
    b = pd.Series([x["close"] for x in zs_series], index=dates)
    # 用日收益率相关，避免绝对价位尺度干扰
    corr = float(a.pct_change().corr(b.pct_change())) if len(dates) > 5 else None

    return {
        "days": days,
        "correlation": None if corr is None or pd.isna(corr) else round(corr, 4),
        "units": {
            SYMBOL_LONDON: PRODUCTS[SYMBOL_LONDON]["unit"],
            SYMBOL_ZHESHANG: PRODUCTS[SYMBOL_ZHESHANG]["unit"],
        },
        "series": {
            SYMBOL_LONDON: london_series,
            SYMBOL_ZHESHANG: zs_series,
        },
        "note": "按交易日对齐后的真实收盘价；伦敦金与积存金分属不同单位，图中左右双轴展示。",
    }


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        import math

        f = float(v)
        if math.isnan(f):
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None
