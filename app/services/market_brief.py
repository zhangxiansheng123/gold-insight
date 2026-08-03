"""规则版市场事件摘要与风险提示（不改动交易点数字）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.macro_features import (
    FOMC_DECISION_DATES,
    _days_to_next_fomc,
    _load_usdcny_history,
)
from app.services.market import SYMBOL_ZHESHANG


def _level(sev: str) -> str:
    return sev if sev in {"info", "watch", "warn", "high"} else "info"


def build_market_brief() -> dict[str, Any]:
    """
    基于 FOMC 日历 + USDCNY + 积存金交易点带宽，生成事件摘要与风险提示。
    纯规则，不调用 LLM，也不改写上车/下车。
    """
    today = datetime.now().date()
    events: list[dict[str, str]] = []
    risks: list[dict[str, str]] = []

    # —— FOMC ——
    days = _days_to_next_fomc(today)
    next_fomc = next((d for d in FOMC_DECISION_DATES if d >= today), None)
    if today in set(FOMC_DECISION_DATES):
        events.append(
            {
                "title": "美联储议息日",
                "detail": "今日为 FOMC 决议日，金价与汇率波动常放大。",
            }
        )
        risks.append(
            {
                "level": _level("high"),
                "text": "议息日模型区间参考意义下降，建议降低对上车/下车点的依赖。",
            }
        )
    elif days <= 1:
        events.append(
            {
                "title": "议息临近",
                "detail": f"距下次 FOMC（{next_fomc}）约 {days} 天，注意隔夜波动。",
            }
        )
        risks.append(
            {
                "level": _level("warn"),
                "text": "议息前后 1～2 个交易日，短线区间容易被打穿。",
            }
        )
    elif days <= 7:
        events.append(
            {
                "title": "本周关注美联储",
                "detail": f"下次 FOMC 决议日 {next_fomc}，还有 {days} 天。",
            }
        )
    else:
        events.append(
            {
                "title": "议息日历",
                "detail": f"下次 FOMC {next_fomc or '待更新'}，还有 {days} 天。",
            }
        )

    # —— 汇率 USDCNY ——
    fx = _load_usdcny_history("6mo")
    fx_meta: dict[str, Any] = {}
    if fx is not None and not fx.empty:
        last = fx.iloc[-1]
        ret1 = float(last.get("fx_ret1") or 0.0)
        ret5 = float(last.get("fx_ret5") or 0.0)
        vol10 = float(last.get("fx_vol10") or 0.0)
        fx_close = float(last.get("fx_close") or 0.0)
        fx_meta = {
            "usdcny": round(fx_close, 4),
            "fx_ret1": round(ret1 * 100, 3),
            "fx_ret5": round(ret5 * 100, 3),
            "fx_vol10": round(vol10 * 100, 3),
        }
        sign1 = "+" if ret1 >= 0 else ""
        events.append(
            {
                "title": "美元兑人民币",
                "detail": f"现汇约 {fx_close:.4f}，近1日 {sign1}{ret1 * 100:.2f}%，近5日 {ret5 * 100:+.2f}%。",
            }
        )

        # 波动分位：相对近 60 日 vol10
        recent = fx["fx_vol10"].dropna().tail(60)
        if len(recent) >= 20 and vol10 > 0:
            pct = float((recent <= vol10).mean())
            if pct >= 0.9 or abs(ret1) >= 0.004:
                risks.append(
                    {
                        "level": _level("warn"),
                        "text": "人民币汇率波动偏高，积存金与伦敦金短线可能短暂背离。",
                    }
                )
            elif abs(ret1) >= 0.0025:
                risks.append(
                    {
                        "level": _level("watch"),
                        "text": "汇率日内变动不小，换算积存金时注意点差与滞后。",
                    }
                )
    else:
        events.append(
            {
                "title": "美元兑人民币",
                "detail": "暂时未能拉取汇率，摘要仅含议息日历。",
            }
        )

    # —— 交易点带宽（若有）——
    band_meta: dict[str, Any] = {}
    try:
        from app.services.forecast_store import get_zheshang_entry_exit

        ee = get_zheshang_entry_exit()
        mid = float(ee["mid"])
        half = (float(ee["exit"]) - float(ee["entry"])) / 2.0
        if mid > 0:
            half_pct = half / mid * 100
            band_meta = {
                "target_date": ee.get("target_date"),
                "made_on": ee.get("made_on"),
                "half_width_pct": round(half_pct, 2),
            }
            events.append(
                {
                    "title": "模型区间宽度",
                    "detail": f"目标日 {ee.get('target_date')} 半宽约 ±{half_pct:.2f}%（仅供参考，非胜率）。",
                }
            )
            if half_pct >= 1.6:
                risks.append(
                    {
                        "level": _level("warn"),
                        "text": "当前预测带宽偏宽，说明模型认为不确定性较大，宜减小仓位参考权重。",
                    }
                )
            elif half_pct <= 0.7:
                risks.append(
                    {
                        "level": _level("watch"),
                        "text": "带宽偏窄时更怕跳空；若盘中已逼近上下沿，勿当作必达价。",
                    }
                )
    except Exception:  # noqa: BLE001
        pass

    # —— 积存金当日涨跌（缓存行情）——
    try:
        from app.services.data_service import latest_cached_quotes

        for q in latest_cached_quotes():
            if q.get("symbol") != SYMBOL_ZHESHANG:
                continue
            pct = q.get("change_pct")
            if pct is None:
                break
            if abs(float(pct)) >= 1.5:
                risks.append(
                    {
                        "level": _level("warn"),
                        "text": f"积存金今日已波动约 {float(pct):+.2f}%，盘中追价风险上升。",
                    }
                )
            break
    except Exception:  # noqa: BLE001
        pass

    if not risks:
        risks.append(
            {
                "level": _level("info"),
                "text": "暂无明显宏观警报；交易点仍为统计区间，不构成投资建议。",
            }
        )

    # 去重文案
    seen: set[str] = set()
    uniq_risks = []
    for r in risks:
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        uniq_risks.append(r)

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": today.isoformat(),
        "source": "rules:fomc+usdcny+band",
        "events": events,
        "risks": uniq_risks,
        "meta": {**fx_meta, **band_meta},
        "note": "事件摘要与风险提示不修改上车/下车点，仅作环境说明。",
    }
