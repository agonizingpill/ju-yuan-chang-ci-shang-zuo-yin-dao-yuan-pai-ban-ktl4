from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from .db import db_session
from .auth import current,writers,admin
from .swap_models import SwapBatch,SwapItem
from .swap_schemas import SwapInput,SwapCheckinInput
from . import swap_service as swap
router=APIRouter()

@router.get('/swap-batches')
def list_batches(s=Depends(db_session),u=Depends(current)):
    return swap.list_batches(s)

@router.post('/swap-batches')
def create_batch(d:SwapInput,s=Depends(db_session),u=Depends(admin)):
    x=swap.create_batch(s,d,u);s.commit();return x

@router.get('/swap-batches/{bid}')
def get_batch(bid:int,s=Depends(db_session),u=Depends(current)):
    b=s.get(SwapBatch,bid)
    if not b:raise HTTPException(404,'换班批次不存在')
    return swap.batch_view(s,b)

@router.post('/swap-items/{item_id}/check-in')
def item_checkin(item_id:int,d:SwapCheckinInput,s=Depends(db_session),u=Depends(writers)):
    x=swap.checkin(s,item_id,d,u);s.commit();return x

@router.post('/swap-batches/{bid}/cancel')
def cancel(bid:int,s=Depends(db_session),u=Depends(admin)):
    x=swap.cancel(s,bid,u);s.commit();return x
