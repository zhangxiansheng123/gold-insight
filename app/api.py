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
) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in PRODUCTS:
        raise HTTPException(status_code=404, detail="未知品种")
    df = load_history_df(symbol, days=days)
    if df.empty:
        raise HTTPException(status_code=404, detail="暂无历史，请先同步数据")
    try:
        result = predict_price(to_ohlc_frame(df.to_dict(orient="records")), symbol, horizon_days=horizon)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = result_to_dict(result)
    payload["name"] = PRODUCTS[symbol]["name"]
    payload["unit"] = PRODUCTS[symbol]["unit"]
    return payload


@router.get("/compare")
def compare(days: int = Query(90, ge=30, le=730)) -> dict[str, Any]:
    """伦敦金与浙商积存金走势对比（归一化）。"""
    frames = {}
    for symbol in (SYMBOL_LONDON, SYMBOL_ZHESHANG):
        df = load_history_df(symbol, days=days)
        if df.empty:
            continue
        df = to_ohlc_frame(df.to_dict(orient="records"))
        base = float(df["close"].iloc[0])
        frames[symbol] = [
            {
                "date": r.trade_date.strftime("%Y-%m-%d"),
                "close": float(r.close),
                "indexed": float(r.close / base * 100),
            }
            for r in df.itertuples()
        ]

    if len(frames) < 2:
        raise HTTPException(status_code=404, detail="对比数据不足，请先同步两个品种历史")

    # 简单相关
    import pandas as pd

    a = pd.DataFrame(frames[SYMBOL_LONDON]).set_index("date")["indexed"]
    b = pd.DataFrame(frames[SYMBOL_ZHESHANG]).set_index("date")["indexed"]
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    joined.columns = ["london", "zheshang"]
    corr = float(joined["london"].corr(joined["zheshang"])) if len(joined) > 5 else None

    return {
        "days": days,
        "correlation": None if corr is None or pd.isna(corr) else round(corr, 4),
        "series": {
            SYMBOL_LONDON: frames[SYMBOL_LONDON],
            SYMBOL_ZHESHANG: frames[SYMBOL_ZHESHANG],
        },
        "note": "indexed 以区间首日收盘=100，便于跨币种对比相对走势。",
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
