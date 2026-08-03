"""本场涨跌预测：按京东猜涨跌场次（4小时），预测收盘相对开盘涨或跌。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.db import QuoteSnapshot, SessionLocal
from app.services.market import SYMBOL_ZHESHANG

BASE_SESSIONS: tuple[tuple[int, int], ...] = (
    (0, 4),
    (4, 8),
    (8, 12),
    (12, 16),
    (16, 20),
    (20, 24),
)


@dataclass
class SessionInfo:
    session_key: str
    session_no: int
    day: str
    start: datetime
    end: datetime
    guess_deadline: datetime
    phase: str
    seconds_left: int


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _sessions_for_day(day: datetime) -> list[tuple[int, datetime, datetime]]:
    day0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[tuple[int, datetime, datetime]] = []
    no = 1
    for start_h, end_h in BASE_SESSIONS:
        if day0.weekday() == 0 and start_h < 8:
            continue
        start = day0 + timedelta(hours=start_h)
        end = day0 + timedelta(hours=end_h) if end_h < 24 else day0 + timedelta(days=1)
        out.append((no, start, end))
        no += 1
    return out


def resolve_session(at: datetime | None = None) -> SessionInfo:
    at = at or _now()
    day0 = at.replace(hour=0, minute=0, second=0, microsecond=0)
    sessions = _sessions_for_day(day0)
    if not sessions or at < sessions[0][1]:
        yday = day0 - timedelta(days=1)
        for no, start, end in reversed(_sessions_for_day(yday)):
            if start <= at < end:
                return _pack_session(yday, no, start, end, at)
        if sessions:
            return _pack_session(day0, *sessions[0], at)

    for no, start, end in sessions:
        if start <= at < end:
            return _pack_session(day0, no, start, end, at)

    tomorrow = day0 + timedelta(days=1)
    t0 = _sessions_for_day(tomorrow)[0]
    return _pack_session(tomorrow, *t0, at)


def _pack_session(day0: datetime, no: int, start: datetime, end: datetime, at: datetime) -> SessionInfo:
    deadline = end - timedelta(hours=1)
    if at < start:
        phase, left = "upcoming", int((start - at).total_seconds())
    elif at < deadline:
        phase, left = "guessing", int((deadline - at).total_seconds())
    elif at < end:
        phase, left = "waiting", int((end - at).total_seconds())
    else:
        phase, left = "ended", 0
    return SessionInfo(
        session_key=f"{day0.strftime('%Y%m%d')}-{no}",
        session_no=no,
        day=day0.strftime("%Y-%m-%d"),
        start=start,
        end=end,
        guess_deadline=deadline,
        phase=phase,
        seconds_left=max(0, left),
    )


def _price_at_or_before(ts: datetime) -> float | None:
    db = SessionLocal()
    try:
        row = (
            db.query(QuoteSnapshot)
            .filter(QuoteSnapshot.symbol == SYMBOL_ZHESHANG, QuoteSnapshot.ts <= ts)
            .order_by(QuoteSnapshot.ts.desc())
            .first()
        )
        if row:
            return float(row.price)
        row = (
            db.query(QuoteSnapshot)
            .filter(QuoteSnapshot.symbol == SYMBOL_ZHESHANG)
            .order_by(QuoteSnapshot.ts.asc())
            .first()
        )
        return float(row.price) if row else None
    finally:
        db.close()


def _latest_price() -> float | None:
    db = SessionLocal()
    try:
        row = (
            db.query(QuoteSnapshot)
            .filter(QuoteSnapshot.symbol == SYMBOL_ZHESHANG)
            .order_by(QuoteSnapshot.ts.desc())
            .first()
        )
        return float(row.price) if row else None
    finally:
        db.close()


def _session_quotes(start: datetime, end: datetime) -> list[float]:
    db = SessionLocal()
    try:
        rows = (
            db.query(QuoteSnapshot)
            .filter(
                QuoteSnapshot.symbol == SYMBOL_ZHESHANG,
                QuoteSnapshot.ts >= start,
                QuoteSnapshot.ts <= end,
            )
            .order_by(QuoteSnapshot.ts.asc())
            .all()
        )
        return [float(r.price) for r in rows if r.price is not None]
    finally:
        db.close()


def _predict_end_price(start_price: float, live: float, sess: SessionInfo) -> tuple[float, dict[str, Any]]:
    """
    预测本场结束价：动量外推 + 短周期模型中枢，加权合成。
    判定规则与活动一致：结束价 vs 开盘价 → 涨/跌/平。
    """
    reasons: list[str] = []
    now = _now()
    elapsed = max((now - sess.start).total_seconds(), 1.0)
    total = max((sess.end - sess.start).total_seconds(), 1.0)
    remain_ratio = max((sess.end - now).total_seconds(), 0.0) / total
    progress = min(elapsed / total, 1.0)

    # 1) 盘中动量：按已实现涨跌外推剩余时段（衰减）
    move_so_far = live - start_price
    momentum_end = live + move_so_far * remain_ratio * 0.55
    reasons.append(f"盘中相对开盘 {move_so_far:+.2f}")

    # 2) 短线模型（明日预测中枢作偏置，贴近现价）
    model_end = live
    try:
        from app.services.data_service import load_history_df
        from app.services.forecast_store import _anchor_last_bar_to_live
        from app.services.indicators import to_ohlc_frame
        from app.services.predictor import predict_price

        df = load_history_df(SYMBOL_ZHESHANG, days=240)
        if not df.empty:
            frame = to_ohlc_frame(df.to_dict(orient="records"))
            frame = _anchor_last_bar_to_live(frame, SYMBOL_ZHESHANG)
            result = predict_price(frame, SYMBOL_ZHESHANG, horizon_days=1)
            # 把「下一交易日」方向映射到本场剩余：只取相对现价的偏移一部分
            model_bias = float(result.predicted_price) - live
            model_end = live + model_bias * (0.35 + 0.25 * remain_ratio)
            reasons.append(f"模型偏置 {model_bias:+.2f}")
    except Exception:  # noqa: BLE001
        reasons.append("模型暂不可用，主要看盘中动量")

    # 3) 微观：近若干快照斜率
    micro_end = live
    pts = _session_quotes(sess.start, now)
    if len(pts) >= 4:
        tail = pts[-min(12, len(pts)) :]
        slope = (tail[-1] - tail[0]) / max(len(tail) - 1, 1)
        steps_left = max(int(remain_ratio * 12), 1)
        micro_end = live + slope * steps_left * 0.8
        reasons.append(f"短线斜率 {slope:+.3f}/点")

    # 权重：越接近收盘，越信已实现路径
    w_mom = 0.35 + 0.35 * progress
    w_model = 0.40 - 0.20 * progress
    w_micro = 1.0 - w_mom - w_model
    predicted = w_mom * momentum_end + w_model * model_end + w_micro * micro_end

    meta = {
        "weights": {
            "momentum": round(w_mom, 3),
            "model": round(w_model, 3),
            "micro": round(w_micro, 3),
        },
        "components": {
            "momentum_end": round(momentum_end, 2),
            "model_end": round(model_end, 2),
            "micro_end": round(micro_end, 2),
        },
        "reasons": reasons,
        "progress": round(progress, 3),
    }
    return float(predicted), meta


def session_direction_forecast() -> dict[str, Any]:
    sess = resolve_session()
    live = _latest_price()
    start_price = _price_at_or_before(sess.start) or live
    if start_price is None or live is None:
        raise ValueError("暂无积存金行情，无法预测本场涨跌")

    predicted_end, meta = _predict_end_price(float(start_price), float(live), sess)
    delta = predicted_end - float(start_price)
    # 极小波动视为平（相对开盘 0.02%）
    flat_eps = abs(float(start_price)) * 0.0002
    if abs(delta) <= flat_eps:
        direction = "flat"
        label = "平"
    elif delta > 0:
        direction = "up"
        label = "涨"
    else:
        direction = "down"
        label = "跌"

    # 置信：偏离越大、进度越晚（路径更清晰）越高
    conf = 0.52 + min(abs(delta) / max(abs(float(start_price)) * 0.004, 1e-6), 1.0) * 0.28
    conf += meta["progress"] * 0.12
    conf = round(min(max(conf, 0.5), 0.88), 3)

    phase_label = {
        "upcoming": "即将开场",
        "guessing": "进行中",
        "waiting": "等待收盘判定",
        "ended": "已结束",
    }.get(sess.phase, sess.phase)

    realized = float(live) - float(start_price)
    return {
        "symbol": SYMBOL_ZHESHANG,
        "name": "浙商积存金",
        "unit": "元/克",
        "session": {
            "key": sess.session_key,
            "no": sess.session_no,
            "day": sess.day,
            "start": sess.start.strftime("%H:%M"),
            "end": sess.end.strftime("%H:%M"),
            "phase": sess.phase,
            "phase_label": phase_label,
            "seconds_left": sess.seconds_left,
            "rule": "本场结束价相对开盘价：高=涨，低=跌，相等=平",
        },
        "price": {
            "start": round(float(start_price), 2),
            "live": round(float(live), 2),
            "predicted_end": round(predicted_end, 2),
            "realized_change": round(realized, 2),
            "predicted_change": round(delta, 2),
        },
        "forecast": {
            "direction": direction,
            "label": label,
            "confidence": conf,
            "summary": f"预测本场收盘相对开盘偏「{label}」（预估结束价 {predicted_end:.2f}，较开盘 {delta:+.2f}）",
        },
        "meta": meta,
        "disclaimer": "仅供参考，不构成投注或投资建议。",
    }
