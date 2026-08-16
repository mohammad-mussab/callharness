from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..analysis.worker import get_or_create_config
from ..auth import require_api_key
from ..db import get_session
from ..schemas import AnalysisConfigIn, AnalysisConfigOut

router = APIRouter(prefix="/api/v1/config", tags=["config"])


def _dedupe(categories: list[dict]) -> list[dict]:
    """Keep the first entry per key — two buckets with the same key would produce
    an ambiguous prompt and a duplicated slice in the breakdown charts."""
    seen: set[str] = set()
    out = []
    for c in categories:
        if c["key"] not in seen:
            seen.add(c["key"])
            out.append(c)
    return out


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
    config.output_language = (payload.output_language or "english").strip().lower()
    config.extraction_enabled = payload.extraction_enabled
    config.extraction_fields = [f.model_dump() for f in payload.extraction_fields]
    config.bucketing_enabled = payload.bucketing_enabled
    # Same "empty means reset to defaults" rule as the taxonomies below — see
    # buckets.buckets_or_default. To stop bucketing, turn bucketing_enabled off.
    config.buckets = _dedupe([c.model_dump() for c in payload.buckets])
    config.classification_enabled = payload.classification_enabled
    # Saving an empty taxonomy is treated as "reset to defaults" rather than "no
    # categories" — see taxonomy.categories_or_default. To stop classifying, turn
    # classification_enabled off.
    config.transfer_reasons = _dedupe([c.model_dump() for c in payload.transfer_reasons])
    config.non_completion_reasons = _dedupe(
        [c.model_dump() for c in payload.non_completion_reasons]
    )
    await session.commit()
    await session.refresh(config)
    return AnalysisConfigOut.model_validate(config)
