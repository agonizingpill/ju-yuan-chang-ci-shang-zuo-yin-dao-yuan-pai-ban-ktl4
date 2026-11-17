from sqlalchemy import String,Integer,Text,ForeignKey,CheckConstraint,Index
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base

class SwapBatch(Base):
    __tablename__='swap_batches'
    __table_args__=(
        CheckConstraint("state in ('pending','effective','cancelled')"),
        # 批次号唯一性由服务层对 pending 批次保证；取消后号码可重新登记
        Index('ix_swap_batch_ref','batch_ref'),
    )
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_ref:Mapped[str]=mapped_column(String(60))
    reason:Mapped[str]=mapped_column(Text,default='')
    state:Mapped[str]=mapped_column(String(20),default='pending')
    created_by:Mapped[str]=mapped_column(String(100))
    created_at:Mapped[str]=mapped_column(String(40))
    effective_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    effective_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
    cancelled_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    cancelled_by:Mapped[str|None]=mapped_column(String(100),nullable=True)

class SwapItem(Base):
    __tablename__='swap_items'
    __table_args__=(
        CheckConstraint("state in ('waiting','arrived','swapped','cancelled')"),
        # 并发互斥由服务层行锁 + 开放条目预检保证（load_lines 先锁班表行），
        # 索引加速“班表是否在待接班换班中”的查询；取消或生效后可对同一班表再次换班。
        Index('ix_swap_item_assignment','assignment_id'),
    )
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('swap_batches.id'))
    assignment_id:Mapped[int]=mapped_column(ForeignKey('crew_assignments.id'))
    crew_call_id:Mapped[int]=mapped_column(ForeignKey('crew_calls.id'))
    # 换出方（原班）
    from_staff_id:Mapped[int]=mapped_column(ForeignKey('staff_members.id'))
    from_staff_code:Mapped[str]=mapped_column(String(40))
    from_staff_name:Mapped[str]=mapped_column(String(80))
    from_checked_in:Mapped[bool]=mapped_column(default=False)
    # 原签到证据快照：换出者已到岗时原样保留，不随班表切换被覆盖
    from_observed_at:Mapped[str|None]=mapped_column(String(30),nullable=True)
    from_recorded_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
    # 接班方
    to_staff_id:Mapped[int]=mapped_column(ForeignKey('staff_members.id'))
    to_staff_code:Mapped[str]=mapped_column(String(40))
    to_staff_name:Mapped[str]=mapped_column(String(80))
    state:Mapped[str]=mapped_column(String(20),default='waiting')
    # 接班者重新签到的现场证据
    observed_at:Mapped[str|None]=mapped_column(String(30),nullable=True)
    recorded_at:Mapped[str|None]=mapped_column(String(40),nullable=True)
    recorded_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
    # 生效时的责任交接摘要
    handover_note:Mapped[str|None]=mapped_column(Text,nullable=True)

class SwapHandover(Base):
    """生效瞬间的责任快照：入口岗位与事件（含已结）的前后责任人。

    班表身份在原 CrewAssignment 行上整体切换，已结事件仍引用该行，
    本表保留换班前责任人证据；未结事件随班表由接班者接管，同样留痕。
    """
    __tablename__='swap_handovers'
    __table_args__=(CheckConstraint("kind in ('zone_post','incident')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    swap_item_id:Mapped[int]=mapped_column(ForeignKey('swap_items.id'))
    batch_id:Mapped[int]=mapped_column(ForeignKey('swap_batches.id'))
    kind:Mapped[str]=mapped_column(String(20))
    ref_id:Mapped[int]=mapped_column(Integer)
    ref_state:Mapped[str]=mapped_column(String(20),default='')
    from_staff_code:Mapped[str]=mapped_column(String(40))
    from_staff_name:Mapped[str]=mapped_column(String(80))
    to_staff_code:Mapped[str]=mapped_column(String(40))
    to_staff_name:Mapped[str]=mapped_column(String(80))
