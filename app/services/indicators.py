from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd


def to_ohlc_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["trade_date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    close = out["close"]

    out["ma5"] = close.rolling(5).mean()
    out["ma10"] = close.rolling(10).mean()
    out["ma20"] = close.rolling(20).mean()
    out["ma60"] = close.rolling(60).mean()

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))

    # Bollinger
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    out["boll_mid"] = mid
    out["boll_upper"] = mid + 2 * std
    out["boll_lower"] = mid - 2 * std

    # ATR
    high, low = out["high"], out["low"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()

    return out


def latest_signal_summary(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or len(df) < 30:
        return {"bias": "insufficient_data", "score": 0, "notes": ["历史数据不足，暂不给出倾向"]}

    row = df.iloc[-1]
    score = 0
    notes: list[str] = []

    if pd.notna(row.get("ma5")) and pd.notna(row.get("ma20")):
        if row["ma5"] > row["ma20"]:
            score += 1
            notes.append("短均线在中均线上方，偏多")
        else:
            score -= 1
            notes.append("短均线在中均线下方，偏空")

    rsi = row.get("rsi14")
    if pd.notna(rsi):
        if rsi >= 70:
            score -= 1
            notes.append(f"RSI={rsi:.1f} 超买区，注意回调")
        elif rsi <= 30:
            score += 1
            notes.append(f"RSI={rsi:.1f} 超卖区，存在反弹可能")
        else:
            notes.append(f"RSI={rsi:.1f} 中性区间")

    hist = row.get("macd_hist")
    if pd.notna(hist):
        if hist > 0:
            score += 1
            notes.append("MACD 柱为正，动能偏多")
        else:
            score -= 1
            notes.append("MACD 柱为负，动能偏空")

    if pd.notna(row.get("boll_upper")) and pd.notna(row.get("boll_lower")):
        if row["close"] >= row["boll_upper"]:
            score -= 1
            notes.append("价格触及布林上轨")
        elif row["close"] <= row["boll_lower"]:
            score += 1
            notes.append("价格触及布林下轨")

    if score >= 2:
        bias = "bullish"
    elif score <= -2:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "bias": bias,
        "score": score,
        "notes": notes,
        "as_of": _safe_date(row.get("trade_date")),
        "close": float(row["close"]),
        "rsi14": _safe_float(rsi),
        "macd_hist": _safe_float(hist),
        "ma5": _safe_float(row.get("ma5")),
        "ma20": _safe_float(row.get("ma20")),
    }


def _safe_float(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
        return None
    return float(v)


def _safe_date(v: Any) -> str | None:
    if v is None or pd.isna(v):
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]
