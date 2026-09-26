from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, require_device
from ..db import get_session
from ..raids import build_raid
from ..schemas import RaidResponse

router = APIRouter(prefix="/v1", tags=["raids"])


@router.get("/raid", response_model=RaidResponse)
async def get_raid(
    caller: Caller = Depends(require_device),
    db: AsyncSession = Depends(get_session),
) -> RaidResponse:
    return await build_raid(db, viewer_id=caller.user.id)
