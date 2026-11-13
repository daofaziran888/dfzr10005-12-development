"""SLA风险窗口推演接口：窗口推演、处置队列、异常/恢复事件、升级额度占用与关闭结论。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import ActionResult, EntryPayload
from app.services.sla_risk import risk_engine

router = APIRouter(prefix="/api/sla/risk", tags=["SLA风险窗口推演"])


@router.get("/rules")
def risk_rules() -> dict[str, Any]:
    """推演口径：延迟阈值、允许范围、判定标准与不允许保存的风险窗口。"""
    return risk_engine.rules_meta()


@router.post("/derive", response_model=ActionResult)
def derive_windows(payload: EntryPayload) -> ActionResult:
    """按同一份时间快照推演风险窗口；基线与SLA共用该快照，重复到达自动去重。"""
    result = risk_engine.derive(
        snapshot_at=payload.values.get("时间快照"),
        rule_code=str(payload.values.get("规则编号") or "").strip() or None,
    )
    rejected = [item for item in result["拒绝"]]
    message = result["message"]
    if rejected:
        reasons = "；".join(f"{item.get('规则编号')}: {'、'.join(item.get('原因', []))}" for item in rejected)
        message = f"{message}。拒绝明细：{reasons}"
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/windows")
def list_windows(
    rule_code: str | None = Query(default=None, description="按规则编号过滤"),
    status: str | None = Query(default=None, description="预警中、已超限、已升级、已关闭、已更替"),
) -> dict[str, Any]:
    """风险窗口清单：含历史窗口，既有窗口按原口径保留。"""
    items = risk_engine.windows(rule_code=rule_code, status=status)
    return {"items": items, "total": len(items)}


@router.get("/queue")
def risk_queue(snapshot_at: str | None = Query(default=None, description="队列计算用时间快照")) -> dict[str, Any]:
    """处置队列：按风险优先级降序输出活动窗口。"""
    items = risk_engine.queue(snapshot_at=snapshot_at)
    return {"items": items, "total": len(items)}


@router.get("/escalations")
def list_escalations() -> dict[str, Any]:
    """升级额度占用记录：同一去重键在窗口内只保留一条有效记录。"""
    items = risk_engine.escalations()
    return {"items": items, "total": len(items)}


@router.post("/events", response_model=ActionResult)
def ingest_event(payload: EntryPayload) -> ActionResult:
    """上报异常/恢复事件：异常首次到达生成窗口，重复到达去重，恢复写入关闭结论。"""
    window, message = risk_engine.ingest_event(
        rule_code=str(payload.values.get("规则编号") or "").strip(),
        event_time=payload.values.get("事件时间"),
        event_type=str(payload.values.get("事件类型") or "").strip(),
        note=str(payload.values.get("说明") or "").strip(),
    )
    if window is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=window)


@router.post("/escalations", response_model=ActionResult)
def acquire_escalation(payload: EntryPayload) -> ActionResult:
    """手工占用升级额度：两个告警任务争抢相同去重键时只允许一个成功，后到不被接受。"""
    record, message = risk_engine.acquire_escalation(
        window_id=int(payload.values.get("窗口编号id") or 0),
        snapshot_at=payload.values.get("时间快照"),
    )
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.post("/windows/{window_id}/close", response_model=ActionResult)
def close_window(window_id: int, payload: EntryPayload) -> ActionResult:
    """关闭风险窗口：必须填写关闭依据，历史窗口保留原通知渠道与承诺时间。"""
    window, message = risk_engine.close_window(
        window_id,
        conclusion=str(payload.values.get("关闭依据") or ""),
        closed_at=payload.values.get("关闭时间"),
    )
    if window is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=window)


@router.post("/baselines/{baseline_code}/advance", response_model=ActionResult)
def advance_baseline(baseline_code: str, payload: EntryPayload) -> ActionResult:
    """基线最近产出向前推进：旧窗口保留原始承诺时间，新窗口重新核算首次预警时刻。"""
    result, message = risk_engine.advance_baseline(
        baseline_code,
        new_output=payload.values.get("最近产出"),
        snapshot_at=payload.values.get("时间快照"),
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)
