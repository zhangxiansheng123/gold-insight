"""汇率 + 美联储日程宏观特征（按交易日对齐，供预测模型使用）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd

# FOMC 议息会议结束日（通常为决议/声明日，日程提前公开，不构成未来泄露）
# 来源：美联储公开会议日历（2023–2026）
FOMC_DECISION_DATES: tuple[date, ...] = (
    # 2023
    date(2023, 2, 1),
    date(2023, 3, 22),
    date(2023, 5, 3),
    date(2023, 6, 14),
    date(2023, 7, 26),
    date(2023, 9, 20),
    date(2023, 11, 1),
    date(2023, 12, 13),
    # 2024
    date(2024, 1, 31),
    date(2024, 3, 20),
    date(2024, 5, 1),
    date(2024, 6, 12),
    date(2024, 7, 31),
    date(2024, 9, 18),
    date(2024, 11, 7),
    date(2024, 12, 18),
    # 2025
    date(2025, 1, 29),
    date(2025, 3, 19),
    date(2025, 5, 7),
    date(2025, 6, 18),
    date(2025, 7, 30),
    date(2025, 9, 17),
    date(2025, 10, 29),
    date(2025, 12, 10),
    # 2026
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
)

MACRO_FEATURE_COLS = (
    "fx_ret1",
    "fx_ret5",
    "fx_vol10",
    "is_fomc",
    "days_to_fomc",
)


@lru_cache(maxsize=4)
def _load_usdcny_history(period: str = "2y") -> pd.DataFrame:
    """拉取美元兑人民币日线；失败返回空表。"""
    try:
        import yfinance as yf

        hist = yf.Ticker("USDCNY=X").history(period=period, auto_adjust=True)
        if hist is None or hist.empty:
            return pd.DataFrame(columns=["trade_date", "fx_close"])
        frame = hist.reset_index()
        dcol = "Date" if "Date" in frame.columns else frame.columns[0]
        out = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(frame[dcol]).dt.tz_localize(None).dt.normalize().astype("datetime64[ns]"),
                "fx_close": frame["Close"].astype(float),
            }
        )
        out = out.dropna().drop_duplicates("trade_date").sort_values("trade_date")
        out["fx_ret1"] = out["fx_close"].pct_change()
        out["fx_ret5"] = out["fx_close"].pct_change(5)
        out["fx_vol10"] = out["fx_ret1"].rolling(10).std()
        return out.reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["trade_date", "fx_close", "fx_ret1", "fx_ret5", "fx_vol10"])


def _days_to_next_fomc(day: date) -> int:
    for d in FOMC_DECISION_DATES:
        if d >= day:
            return (d - day).days
    return 60


def fomc_features_for_dates(dates: pd.Series) -> pd.DataFrame:
    """给定日期序列，生成 is_fomc / days_to_fomc。"""
    fomc_set = set(FOMC_DECISION_DATES)
    rows = []
    for ts in pd.to_datetime(dates):
        d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
        rows.append(
            {
                "is_fomc": 1.0 if d in fomc_set else 0.0,
                "days_to_fomc": float(min(_days_to_next_fomc(d), 60)),
            }
        )
    return pd.DataFrame(rows)


def attach_macro_features(ohlc: pd.DataFrame, *, fx_period: str = "2y") -> pd.DataFrame:
    """
    把 USDCNY 与 FOMC 特征并入 OHLC（按 trade_date 向后对齐汇率）。
    汇率缺失时填 0；FOMC 始终可算。
    """
    if ohlc is None or ohlc.empty:
        return ohlc

    out = ohlc.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.tz_localize(None)
    out["trade_date"] = out["trade_date"].dt.normalize().astype("datetime64[ns]")

    fx = _load_usdcny_history(fx_period)
    if not fx.empty:
        fx = fx.copy()
        fx["trade_date"] = pd.to_datetime(fx["trade_date"]).dt.tz_localize(None)
        fx["trade_date"] = fx["trade_date"].dt.normalize().astype("datetime64[ns]")
        merged = pd.merge_asof(
            out.sort_values("trade_date"),
            fx[["trade_date", "fx_ret1", "fx_ret5", "fx_vol10"]].sort_values("trade_date"),
            on="trade_date",
            direction="backward",
        )
    else:
        merged = out.copy()
        merged["fx_ret1"] = 0.0
        merged["fx_ret5"] = 0.0
        merged["fx_vol10"] = 0.0

    fomc = fomc_features_for_dates(merged["trade_date"])
    merged["is_fomc"] = fomc["is_fomc"].values
    merged["days_to_fomc"] = fomc["days_to_fomc"].values

    for col in ("fx_ret1", "fx_ret5", "fx_vol10"):
        merged[col] = merged[col].fillna(0.0)

    return merged.reset_index(drop=True)


def macro_features_on_date(day: datetime | date | str, last_fx: dict[str, float] | None = None) -> dict[str, float]:
    """滚动预测时：未来日沿用末日汇率特征，FOMC 按日历更新。"""
    if isinstance(day, str):
        d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    elif isinstance(day, datetime):
        d = day.date()
    else:
        d = day

    fx = last_fx or {"fx_ret1": 0.0, "fx_ret5": 0.0, "fx_vol10": 0.0}
    return {
        "fx_ret1": float(fx.get("fx_ret1", 0.0)),
        "fx_ret5": float(fx.get("fx_ret5", 0.0)),
        "fx_vol10": float(fx.get("fx_vol10", 0.0)),
        "is_fomc": 1.0 if d in set(FOMC_DECISION_DATES) else 0.0,
        "days_to_fomc": float(min(_days_to_next_fomc(d), 60)),
    }


def clear_macro_cache() -> None:
    _load_usdcny_history.cache_clear()
