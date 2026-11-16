from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import pipeline

router = APIRouter(tags=["汇总发布"])


class PublishBody(BaseModel):
    domain_id: int = Field(..., description="主题域编号")


@router.get("/publish")
def list_publish(domain_id: int | None = None):
    return pipeline.list_publish(domain_id)


@router.get("/publish/{publish_id}")
def get_publish(publish_id: int):
    # 按发布记录 id 只读：发布记录 + 发布证明 + 该次发布的已发布快照
    detail = pipeline.publish_detail(publish_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    return detail


@router.post("/publish")
def publish(body: PublishBody):
    # 无论成功或拒绝都返回本次发布记录；拒绝时记录内含原因，且发布区不被触碰
    try:
        return pipeline.publish_summary(body.domain_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
