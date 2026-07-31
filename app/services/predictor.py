from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler

from app.services.indicators import add_indicators


@dataclass
class ForecastPoint:
    date: str
    predicted: float
    lower: float
    upper: float


@dataclass
class PredictionResult:
    symbol: str
    horizon_days: int
    model: str
    current_price: float
    predicted_price: float
    change_pct: float
    confidence: float
    points: list[ForecastPoint]
    feature_importance: dict[str, float]
    disclaimer: str


DISCLAIMER = (
    "预测仅基于历史价格与技术特征的统计模型，不构成投资建议。"
    "黄金受地缘、利率、汇率与政策等多因素影响，实盘请自行判断并控制风险。"
)


def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    data = add_indicators(df)
    data["ret1"] = data["close"].pct_change()
    data["ret5"] = data["close"].pct_change(5)
    data["ret10"] = data["close"].pct_change(10)
    data["vol10"] = data["ret1"].rolling(10).std()
    data["hl_range"] = (data["high"] - data["low"]) / data["close"]
    data["target"] = data["close"].shift(-1)
    return data


FEATURE_COLS = [
    "close",
    "ma5",
    "ma10",
    "ma20",
    "rsi14",
    "macd",
    "macd_hist",
    "boll_mid",
    "boll_upper",
    "boll_lower",
    "atr14",
    "ret1",
    "ret5",
    "ret10",
    "vol10",
    "hl_range",
]


def predict_price(df: pd.DataFrame, symbol: str, horizon_days: int = 7) -> PredictionResult:
    if df is None or len(df) < 80:
        raise ValueError("历史数据不足，至少需要约 80 个交易日才能预测")

    horizon_days = max(1, min(int(horizon_days), 30))
    feat = _feature_frame(df)
    train = feat.dropna(subset=FEATURE_COLS + ["target"]).copy()
    if len(train) < 60:
        raise ValueError("有效特征样本不足，请先同步更长历史")

    X = train[FEATURE_COLS].values
    y = train["target"].values
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    model = GradientBoostingRegressor(
        n_estimators=180,
        learning_rate=0.05,
        max_depth=3,
        subsample=0.9,
        random_state=42,
    )
    model.fit(Xs, y)

    # 残差用于置信区间
    y_hat = model.predict(Xs)
    resid = y - y_hat
    resid_std = float(np.std(resid)) if len(resid) else 0.0

    # 逐步滚动预测
    working = feat.copy()
    last_known = working.dropna(subset=["close"]).iloc[-1]
    current_price = float(last_known["close"])
    points: list[ForecastPoint] = []
    cursor_date = pd.to_datetime(last_known["trade_date"])

    last_row = working.dropna(subset=FEATURE_COLS).iloc[-1]
    current_features = last_row[FEATURE_COLS].astype(float).values.reshape(1, -1)

    predicted = current_price
    for step in range(1, horizon_days + 1):
        x_scaled = scaler.transform(current_features)
        predicted = float(model.predict(x_scaled)[0])
        # 轻微均值回归，避免长周期发散
        predicted = 0.85 * predicted + 0.15 * current_price
        band = resid_std * np.sqrt(step) * 1.64  # ~90% 粗区间
        cursor_date = cursor_date + timedelta(days=1)
        # 跳过周末（黄金周末休市近似）
        while cursor_date.weekday() >= 5:
            cursor_date += timedelta(days=1)

        points.append(
            ForecastPoint(
                date=cursor_date.strftime("%Y-%m-%d"),
                predicted=round(predicted, 2),
                lower=round(predicted - band, 2),
                upper=round(predicted + band, 2),
            )
        )

        # 用预测价更新下一期 close 相关特征的简化滚动
        next_feats = current_features.copy().reshape(-1)
        close_idx = FEATURE_COLS.index("close")
        next_feats[close_idx] = predicted
        # ma 近似滑动
        for name, w in (("ma5", 5), ("ma10", 10), ("ma20", 20)):
            idx = FEATURE_COLS.index(name)
            next_feats[idx] = (next_feats[idx] * (w - 1) + predicted) / w
        current_features = next_feats.reshape(1, -1)

    final_price = points[-1].predicted
    change_pct = (final_price - current_price) / current_price * 100

    # 置信：样本越多、残差越小越高
    sample_factor = min(1.0, len(train) / 200)
    noise_factor = max(0.2, 1.0 - min(resid_std / max(current_price, 1e-6) * 20, 0.7))
    confidence = round(float(0.35 + 0.4 * sample_factor + 0.25 * noise_factor), 3)

    importance = {
        name: round(float(w), 4)
        for name, w in sorted(
            zip(FEATURE_COLS, model.feature_importances_, strict=True),
            key=lambda x: x[1],
            reverse=True,
        )[:8]
    }

    return PredictionResult(
        symbol=symbol,
        horizon_days=horizon_days,
        model="GradientBoostingRegressor",
        current_price=round(current_price, 2),
        predicted_price=round(final_price, 2),
        change_pct=round(change_pct, 4),
        confidence=confidence,
        points=points,
        feature_importance=importance,
        disclaimer=DISCLAIMER,
    )


def result_to_dict(result: PredictionResult) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "horizon_days": result.horizon_days,
        "model": result.model,
        "current_price": result.current_price,
        "predicted_price": result.predicted_price,
        "change_pct": result.change_pct,
        "confidence": result.confidence,
        "points": [p.__dict__ for p in result.points],
        "feature_importance": result.feature_importance,
        "disclaimer": result.disclaimer,
    }
