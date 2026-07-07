from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..analysis.worker import get_or_create_config
from ..auth import require_api_key
from ..db import get_session
from ..schemas import AnalysisConfigIn, AnalysisConfigOut

router = APIRouter(prefix="/api/v1/config", tags=["config"])


@router.get("/analysis", response_model=AnalysisConfigOut)
async def get_analysis_config(session: AsyncSession = Depends(get_session)):
    config = await get_or_create_config(session)
    return AnalysisConfigOut.model_validate(config)


@router.put(
    "/analysis",
    response_model=AnalysisConfigOut,
    dependencies=[Depends(require_api_key)],
)
async def update_analysis_config(
    payload: AnalysisConfigIn, session: AsyncSession = Depends(get_session)
):
    config = await get_or_create_config(session)
    config.summary_enabled = payload.summary_enabled
    config.summary_prompt = payload.summary_prompt
    config.sentiment_enabled = payload.sentiment_enabled
    config.success_enabled = payload.success_enabled
    config.success_prompt = payload.success_prompt
    config.success_rubric = payload.success_rubric
    config.extraction_enabled = payload.extraction_enabled
    config.extraction_fields = [f.model_dump() for f in payload.extraction_fields]
    await session.commit()
    await session.refresh(config)
    return AnalysisConfigOut.model_validate(config)
