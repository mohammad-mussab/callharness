from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..db import get_session
from ..models import AlertEvent, AlertRule
from ..schemas import AlertEventOut, AlertRuleIn, AlertRuleOut

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("/rules", response_model=list[AlertRuleOut])
async def list_rules(session: AsyncSession = Depends(get_session)):
    rules = (
        (await session.execute(select(AlertRule).order_by(AlertRule.id))).scalars().all()
    )
    return [AlertRuleOut.model_validate(r) for r in rules]


@router.post(
    "/rules",
    response_model=AlertRuleOut,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def create_rule(payload: AlertRuleIn, session: AsyncSession = Depends(get_session)):
    rule = AlertRule(**payload.model_dump())
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.put(
    "/rules/{rule_id}",
    response_model=AlertRuleOut,
    dependencies=[Depends(require_api_key)],
)
async def update_rule(
    rule_id: int, payload: AlertRuleIn, session: AsyncSession = Depends(get_session)
):
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    for key, value in payload.model_dump().items():
        setattr(rule, key, value)
    await session.commit()
    await session.refresh(rule)
    return AlertRuleOut.model_validate(rule)


@router.delete(
    "/rules/{rule_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def delete_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    rule = await session.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    await session.delete(rule)
    await session.commit()


@router.get("/events", response_model=list[AlertEventOut])
async def list_events(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, le=200),
):
    events = (
        (
            await session.execute(
                select(AlertEvent).order_by(AlertEvent.fired_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [AlertEventOut.model_validate(e) for e in events]
