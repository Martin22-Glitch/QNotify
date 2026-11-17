"""调用账本：每次模型调用记一笔，回答"钱花在哪"。

为什么要有它：一次外部成本评审指出，我们的 ¥0.8 账单分不清是检查还是日常使用，
也说不出一笔钱里输入/输出/推理各占多少。这个模块把每次调用落成一行，之后所有判断都有依据。

设计取舍：
- **落在业务库**（同库不同表），不搞 JSONL：便于 API 直接聚合，也随备份走
- **不抛异常**：记账失败绝不能影响判定/对话，一律吞掉并 WARNING
- **金额是估算**：按官方价目表与高峰时段算，用于看趋势与对账，不当作账单

价目（deepseek-flash，元/百万 token，2026-11 官方页）：
    输入缓存命中   空闲 0.02 / 高峰 0.04
    输入缓存未命中 空闲 1.00 / 高峰 2.00
    输出           空闲 4.00 / 高峰 8.00
高峰时段：北京时间 周一至周五 9:00-12:00、14:00-18:00（法定节假日除外，这里不判节假日）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("app.llm.ledger")

TZ = ZoneInfo("Asia/Shanghai")

# 元 / 百万 token
PRICES: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "idle": {"hit": 0.02, "miss": 1.0, "out": 4.0},
        "peak": {"hit": 0.04, "miss": 2.0, "out": 8.0},
    },
    "deepseek-v4-pro": {
        "idle": {"hit": 0.15, "miss": 4.5, "out": 13.5},
        "peak": {"hit": 0.30, "miss": 9.0, "out": 27.0},
    },
}
DEFAULT_PRICE = PRICES["deepseek-flash"]

DDL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    env TEXT NOT NULL DEFAULT 'prod',
    run_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    thinking INTEGER NOT NULL DEFAULT 1,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    finish_reason TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    block_id INTEGER,
    peak INTEGER NOT NULL DEFAULT 0,
    cost_cny REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage (ts);
CREATE INDEX IF NOT EXISTS idx_llm_usage_env ON llm_usage (env, ts);
"""


def _global(db):
    """通用资产（模型档案/系统设置/账本）只在主库，不跟账号走。"""
    from ..accounts import global_db

    return global_db(db)

def is_peak(when: datetime | None = None) -> bool:
    """北京时间工作日 9-12 / 14-18 为高峰。"""
    now = (when or datetime.now(TZ)).astimezone(TZ)
    if now.weekday() >= 5:  # 周六周日全天空闲
        return False
    return 9 <= now.hour < 12 or 14 <= now.hour < 18


def price_for(model: str, peak: bool) -> dict[str, float]:
    table = PRICES.get((model or "").strip(), DEFAULT_PRICE)
    return table["peak" if peak else "idle"]


def estimate_cost(model: str, usage: dict[str, Any], *, peak: bool | None = None) -> float:
    """按 usage 估算金额（元）。usage 缺字段时按 0 处理。"""
    peak = is_peak() if peak is None else peak
    p = price_for(model, peak)
    hit = _int(usage.get("cache_hit_tokens"))
    miss = _int(usage.get("cache_miss_tokens"))
    prompt = _int(usage.get("prompt_tokens"))
    if not miss and prompt:
        # 服务端没给缓存明细时，全部按未命中算（宁可高估）
        miss = max(0, prompt - hit)
    out = _int(usage.get("completion_tokens"))
    return round((hit * p["hit"] + miss * p["miss"] + out * p["out"]) / 1_000_000, 6)


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(data: dict[str, Any] | None) -> dict[str, Any]:
    """拍平一次调用的 usage。入参就是 usage 对象本身（不是整个响应体）。"""
    u = data or {}
    if isinstance(u.get("usage"), dict):  # 宽容：万一传进来的是整个响应体
        u = u["usage"]
    hit = u.get("prompt_cache_hit_tokens")
    miss = u.get("prompt_cache_miss_tokens")
    details = u.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens")
    if hit is None and u.get("cache_read_input_tokens") is not None:  # Anthropic 风格
        hit = u.get("cache_read_input_tokens")
    return {
        "prompt_tokens": _int(u.get("prompt_tokens") or u.get("input_tokens")),
        "cache_hit_tokens": _int(hit),
        "cache_miss_tokens": _int(miss),
        "completion_tokens": _int(u.get("completion_tokens") or u.get("output_tokens")),
        "reasoning_tokens": _int(reasoning),
    }


def record(db: Any, *, purpose: str, usage: dict[str, Any], provider: str = "", model: str = "",
           thinking: bool = True, attempt: int = 1, block_id: int | None = None,
           finish_reason: str = "", env: str | None = None) -> None:
    """记一笔。任何异常都不许冒泡到调用方。"""
    db = _global(db)
    try:
        now = datetime.now(TZ)
        peak = is_peak(now)
        u = normalize_usage(usage)
        cost = estimate_cost(model, u, peak=peak)
        env_name = env or os.environ.get("QN_USAGE_ENV", "prod") or "prod"
        run_id = os.environ.get("QN_USAGE_RUN_ID", "") or ""
        with db.write() as s:
            s.execute(
                db_text(),
                {
                    "ts": now.isoformat(timespec="seconds"),
                    "env": env_name, "run_id": run_id, "purpose": purpose,
                    "provider": provider, "model": model, "thinking": 1 if thinking else 0,
                    "prompt": u["prompt_tokens"], "hit": u["cache_hit_tokens"],
                    "miss": u["cache_miss_tokens"], "out": u["completion_tokens"],
                    "reason": u["reasoning_tokens"], "finish": finish_reason or "",
                    "attempt": int(attempt or 1), "block": block_id,
                    "peak": 1 if peak else 0, "cost": cost,
                },
            )
    except Exception as exc:  # 记账失败不能影响业务
        log.warning("记账失败（忽略）：%s", type(exc).__name__)


def db_text() -> Any:
    from sqlalchemy import text as sql

    return sql(
        "INSERT INTO llm_usage(ts,env,run_id,purpose,provider,model,thinking,prompt_tokens,"
        "cache_hit_tokens,cache_miss_tokens,completion_tokens,reasoning_tokens,finish_reason,"
        "attempt,block_id,peak,cost_cny) VALUES(:ts,:env,:run_id,:purpose,:provider,:model,:thinking,"
        ":prompt,:hit,:miss,:out,:reason,:finish,:attempt,:block,:peak,:cost)"
    )


def summary(db: Any, days: int = 30) -> dict[str, Any]:
    """按环境/用途/天聚合。days<=0 表示全部。"""
    db = _global(db)
    from app.m2 import _rows

    where = ""
    params: dict[str, Any] = {}
    if days > 0:
        where = "WHERE ts >= datetime('now', :ago)"
        params["ago"] = f"-{int(days)} days"
    rows = _rows(db, f"SELECT * FROM llm_usage {where} ORDER BY id", **params)
    total_cost = round(sum(float(r["cost_cny"] or 0) for r in rows), 6)
    by_purpose: dict[str, dict[str, float]] = {}
    by_env: dict[str, dict[str, float]] = {}
    by_day: dict[str, float] = {}
    hit_tokens = miss_tokens = out_tokens = reasoning_tokens = 0
    for r in rows:
        p = by_purpose.setdefault(r["purpose"], {"calls": 0, "cost": 0.0, "tokens": 0})
        p["calls"] += 1
        p["cost"] = round(p["cost"] + float(r["cost_cny"] or 0), 6)
        p["tokens"] += int(r["prompt_tokens"] or 0) + int(r["completion_tokens"] or 0)
        e = by_env.setdefault(r["env"], {"calls": 0, "cost": 0.0})
        e["calls"] += 1
        e["cost"] = round(e["cost"] + float(r["cost_cny"] or 0), 6)
        day = str(r["ts"])[:10]
        by_day[day] = round(by_day.get(day, 0.0) + float(r["cost_cny"] or 0), 6)
        hit_tokens += int(r["cache_hit_tokens"] or 0)
        miss_tokens += int(r["cache_miss_tokens"] or 0)
        out_tokens += int(r["completion_tokens"] or 0)
        reasoning_tokens += int(r["reasoning_tokens"] or 0)
    calls = len(rows)
    return {
        "days": days,
        "calls": calls,
        "cost_cny": total_cost,
        "cost_per_call": round(total_cost / calls, 6) if calls else 0.0,
        "by_purpose": by_purpose,
        "by_env": by_env,
        "by_day": by_day,
        "tokens": {
            "cache_hit": hit_tokens, "cache_miss": miss_tokens,
            "completion": out_tokens, "reasoning": reasoning_tokens,
            "cache_hit_rate": round(hit_tokens / (hit_tokens + miss_tokens), 4) if (hit_tokens + miss_tokens) else None,
        },
        "peak_calls": sum(1 for r in rows if r["peak"]),
    }


def recent(db: Any, limit: int = 50) -> list[dict[str, Any]]:
    db = _global(db)
    from app.m2 import _rows

    rows = _rows(db, "SELECT * FROM llm_usage ORDER BY id DESC LIMIT :l", l=max(1, min(int(limit), 500)))
    return [dict(r) for r in rows]


