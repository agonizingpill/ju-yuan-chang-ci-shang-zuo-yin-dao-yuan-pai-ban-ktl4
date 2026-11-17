from pydantic import BaseModel,Field,field_validator
class SwapLine(BaseModel):
    assignment_id:int
    to_staff_id:int
class SwapInput(BaseModel):
    batch_ref:str=Field(min_length=3,max_length=60)
    reason:str=Field(default='',max_length=2000)
    items:list[SwapLine]=Field(min_length=1)
    @field_validator('batch_ref')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('换班批次号不能为空')
        return v.strip()
class SwapCheckinInput(BaseModel):
    observed_at:str=Field(min_length=16,max_length=30)
    @field_validator('observed_at')
    @classmethod
    def valid_local(cls,v):
        from datetime import datetime
        try:datetime.fromisoformat(v)
        except ValueError:raise ValueError('到岗时刻格式应为本地时间 YYYY-MM-DDTHH:MM')
        return v
