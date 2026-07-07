from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..db import get_session
from ..models import EvaluationResult, Evaluator
from ..schemas import EvaluatorIn, EvaluatorOut, EvaluatorStatsOut

router = APIRouter(prefix="/api/v1/evaluators", tags=["evaluators"])


@router.get("", response_model=list[EvaluatorStatsOut])
async def list_evaluators(session: AsyncSession = Depends(get_session)):
    stats = (
        await session.execute(
            select(
                EvaluationResult.evaluator_id,
                func.count(EvaluationResult.id).label("total"),
                func.sum(
                    case((EvaluationResult.passed == True, 1), else_=0)  # noqa: E712
                ).label("passed"),
            )
            .where(EvaluationResult.passed.is_not(None))
            .group_by(EvaluationResult.evaluator_id)
        )
    ).all()
    by_id = {s.evaluator_id: s for s in stats}

    evaluators = (
        (await session.execute(select(Evaluator).order_by(Evaluator.id))).scalars().all()
    )
    out = []
    for e in evaluators:
        s = by_id.get(e.id)
        total = int(s.total) if s else 0
        passed = int(s.passed or 0) if s else 0
        out.append(
            EvaluatorStatsOut(
                id=e.id,
                name=e.name,
                enabled=e.enabled,
                total=total,
                passed=passed,
                pass_rate=round(passed / total, 3) if total else None,
            )
        )
    return out


@router.get("/{evaluator_id}", response_model=EvaluatorOut)
async def get_evaluator(evaluator_id: int, session: AsyncSession = Depends(get_session)):
    evaluator = await session.get(Evaluator, evaluator_id)
    if evaluator is None:
        raise HTTPException(status_code=404, detail="Evaluator not found")
    return EvaluatorOut.model_validate(evaluator)


@router.post(
    "", response_model=EvaluatorOut, status_code=201, dependencies=[Depends(require_api_key)]
)
async def create_evaluator(payload: EvaluatorIn, session: AsyncSession = Depends(get_session)):
    evaluator = Evaluator(**payload.model_dump())
    session.add(evaluator)
    await session.commit()
    await session.refresh(evaluator)
    return EvaluatorOut.model_validate(evaluator)


@router.put(
    "/{evaluator_id}", response_model=EvaluatorOut, dependencies=[Depends(require_api_key)]
)
async def update_evaluator(
    evaluator_id: int, payload: EvaluatorIn, session: AsyncSession = Depends(get_session)
):
    evaluator = await session.get(Evaluator, evaluator_id)
    if evaluator is None:
        raise HTTPException(status_code=404, detail="Evaluator not found")
    for key, value in payload.model_dump().items():
        setattr(evaluator, key, value)
    await session.commit()
    await session.refresh(evaluator)
    return EvaluatorOut.model_validate(evaluator)


@router.delete(
    "/{evaluator_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def delete_evaluator(evaluator_id: int, session: AsyncSession = Depends(get_session)):
    evaluator = await session.get(Evaluator, evaluator_id)
    if evaluator is None:
        raise HTTPException(status_code=404, detail="Evaluator not found")
    await session.delete(evaluator)
    await session.commit()
