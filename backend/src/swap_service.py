from datetime import datetime,timedelta,timezone
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import CrewCall,Performance,Production
from .staff_models import StaffMember,CrewAssignment
from .debrief_models import ShowDebrief
from .zone_models import ZonePost
from .incident_models import HouseIncident
from .swap_models import SwapBatch,SwapItem,SwapHandover
from .staff_service import locked,occupied,get
from .services import pack,audit

OPEN_ITEM_STATES=('waiting','arrived')
# 未结事件：接班者重新签到生效后接管
OPEN_INCIDENT_STATES=('open','assigned','resolved')

def now_iso():return datetime.now(timezone.utc).isoformat()

# ---------- 视图 ----------
def call_brief(s,call_id):
    c=s.get(CrewCall,call_id);p=s.get(Performance,c.performance_id);prod=s.get(Production,p.production_id)
    return {'crew_call_id':c.id,'performance_id':p.id,'production_id':prod.id,'production_code':prod.code,
            'date':p.date.isoformat(),'curtain':p.curtain,'call_time':c.call_time,'ushers':c.ushers}

def item_view(s,x):
    d=pack(x)
    d.update(call_brief(s,x.crew_call_id))
    d['from_view']={'staff_id':x.from_staff_id,'code':x.from_staff_code,'name':x.from_staff_name,
                    'checked_in':x.from_checked_in,'observed_at':x.from_observed_at,'recorded_by':x.from_recorded_by}
    d['to_view']={'staff_id':x.to_staff_id,'code':x.to_staff_code,'name':x.to_staff_name}
    d['handovers']=[pack(h) for h in s.scalars(select(SwapHandover).where(SwapHandover.swap_item_id==x.id).order_by(SwapHandover.id))]
    return d

def batch_view(s,b):
    items=s.scalars(select(SwapItem).where(SwapItem.batch_id==b.id).order_by(SwapItem.id)).all()
    d=pack(b)
    d['items']=[item_view(s,x) for x in items]
    d['total']=len(items);d['arrived']=sum(x.state in ('arrived','swapped') for x in items)
    return d

# ---------- 校验辅助 ----------
def ensure_open_show(s,call):
    """交接草稿或归档场次拒绝换班。"""
    p=get(s,Performance,call.performance_id)
    deb=s.scalar(select(ShowDebrief).where(ShowDebrief.performance_id==p.id))
    if deb:raise HTTPException(409,f'场次 {p.date.isoformat()} 已有散场交接（{deb.state}），班表冻结，拒绝换班')
    if call.status!='confirmed':raise HTTPException(409,'仅经理已确认的点名单可以临时换班')
    return p

def staff_conflicts(s,staff,target_call,batch_pairs,exclude_assignment_ids=()):
    """核对接班人员进入目标点名的时段重叠与每日上限。

    batch_pairs: 本批次内 (to_staff_id, CrewCall) 的全部拟变动，
    使一次跨场互换在批内也能互相核对，不依赖尚未落库的班表。
    exclude_assignment_ids: 本批次将被该人员换出的班表，其占用在换班后释放，
    不应再算作冲突（两人跨场互换的关键）。
    """
    start,end=occupied(s,target_call)
    rows=s.scalars(select(CrewAssignment).where(CrewAssignment.staff_id==staff.id)).all()
    intervals=[];daily=0
    for a in rows:
        if a.id in exclude_assignment_ids:continue
        c=get(s,CrewCall,a.crew_call_id)
        a0,b0=occupied(s,c)
        if a0.date()==start.date():daily+=1
        intervals.append((a0,b0))
    for sid,c in batch_pairs:
        if sid!=staff.id or c.id==target_call.id:continue
        a0,b0=occupied(s,c)
        if a0.date()==start.date():daily+=1
        intervals.append((a0,b0))
    for a0,b0 in intervals:
        if start<b0 and a0<end:raise HTTPException(409,f'{staff.code} {staff.name} 存在值班时段重叠（含散场30分钟），不能排入本场')
    if daily>=staff.max_daily_calls:raise HTTPException(409,f'{staff.code} {staff.name} 当日已达 {staff.max_daily_calls} 场上限')

def load_lines(s,d):
    """统一加载并锁定本批次涉及的班表与人员，固定加锁顺序避免死锁。"""
    if len({x.assignment_id for x in d.items})!=len(d.items):raise HTTPException(409,'同一实名班表在批次内重复出现')
    assignments=[locked(s,CrewAssignment,x.assignment_id) for x in d.items]
    # 显式预检：班表已在待接班换班中直接拒绝（数据库唯一索引为并发兜底）
    for a in assignments:
        if s.scalar(select(SwapItem.id).where(SwapItem.assignment_id==a.id,SwapItem.state.in_(OPEN_ITEM_STATES))):
            raise HTTPException(409,f'班表 {a.staff_code} 已在待接班换班中，完成或取消后才能再次换班')
    staff_ids=sorted({a.staff_id for a in assignments}|{x.to_staff_id for x in d.items})
    staffs={sid:locked(s,StaffMember,sid) for sid in staff_ids}
    calls={}
    for cid in sorted({a.crew_call_id for a in assignments}):
        c=locked(s,CrewCall,cid);ensure_open_show(s,c);calls[cid]=c
    return assignments,staffs,calls

# ---------- 批次登记 ----------
def create_batch(s,d,u):
    # 幂等优先：相同批次号的待接班批次重试直接返回（不重复换班），不触发班表预检
    old=s.scalar(select(SwapBatch).where(SwapBatch.batch_ref==d.batch_ref,SwapBatch.state=='pending'))
    if old:
        return batch_view(s,old)
    assignments,staffs,calls=load_lines(s,d)
    pair_seen=set();batch_pairs=[]
    for line,a in zip(d.items,assignments):
        to_staff=staffs[line.to_staff_id];c=calls[a.crew_call_id]
        key=(to_staff.id,c.id)
        if key in pair_seen:raise HTTPException(409,f'{to_staff.code} 在同一点名单中被重复安排接班')
        pair_seen.add(key);batch_pairs.append((to_staff.id,c))
        if not to_staff.active:raise HTTPException(409,f'接班人员 {to_staff.code} 已停用，不能换班')
        if a.staff_id==to_staff.id:raise HTTPException(409,'接班人员与原班为同一人，无需换班')
        # 该接班者已在待接班批次中接管同一点名单 → 双重占用拒绝
        busy=s.scalar(select(SwapItem.id).join(SwapBatch,SwapBatch.id==SwapItem.batch_id).where(
            SwapItem.to_staff_id==to_staff.id,SwapItem.crew_call_id==c.id,
            SwapBatch.state=='pending',SwapItem.state.in_(OPEN_ITEM_STATES)))
        if busy:raise HTTPException(409,f'{to_staff.code} 已在待接班批次中接管本场，不能重复安排')
        if s.scalar(select(CrewAssignment.id).where(CrewAssignment.crew_call_id==c.id,CrewAssignment.staff_id==to_staff.id)):
            raise HTTPException(409,f'{to_staff.code} 已在本场班表，不能重复占用')
    # 每个接班者在本批次中将要换出的班表（互换时其原占用在生效后释放，不计冲突）
    releases={}
    for line,a in zip(d.items,assignments):
        for other in assignments:
            if other.staff_id==line.to_staff_id and other.id!=a.id:
                releases.setdefault(line.to_staff_id,set()).add(other.id)
    # 全量核对：跨场互换的时段重叠、每日上限
    for line,a in zip(d.items,assignments):
        staff_conflicts(s,staffs[line.to_staff_id],calls[a.crew_call_id],batch_pairs,releases.get(line.to_staff_id,()))
    b=SwapBatch(batch_ref=d.batch_ref,reason=d.reason.strip(),state='pending',
                created_by=u.email,created_at=now_iso())
    s.add(b);s.flush()
    for line,a in zip(d.items,assignments):
        to_staff=staffs[line.to_staff_id]
        s.add(SwapItem(batch_id=b.id,assignment_id=a.id,crew_call_id=a.crew_call_id,
                       from_staff_id=a.staff_id,from_staff_code=a.staff_code,from_staff_name=a.staff_name,
                       from_checked_in=a.checked_in,from_observed_at=a.observed_at,from_recorded_by=a.recorded_by,
                       to_staff_id=to_staff.id,to_staff_code=to_staff.code,to_staff_name=to_staff.name,
                       state='waiting'))
    try:s.flush()
    except IntegrityError:raise HTTPException(409,'批次内存在已被其他换班占用的班表，请刷新后重试')
    audit(s,u,f'登记临时换班批次 {d.batch_ref}（{len(d.items)}条），接班未齐前原班继续值守')
    return batch_view(s,b)

# ---------- 接班签到 ----------
def checkin(s,item_id,d,u):
    preview=s.get(SwapItem,item_id)
    if not preview:raise HTTPException(404,'换班条目不存在')
    b=locked(s,SwapBatch,preview.batch_id);x=locked(s,SwapItem,item_id)
    if b.state=='effective':# 已生效后重复签到直接返回，不重复换班、不产生第二条证据
        return item_view(s,x)
    if b.state!='pending':raise HTTPException(409,'批次已取消，不能补签到')
    if x.state not in OPEN_ITEM_STATES:raise HTTPException(409,'该条换班已结束')
    if x.state=='arrived':# 幂等：多人员未齐时重复签到不产生第二条证据
        return item_view(s,x)
    staff=locked(s,StaffMember,x.to_staff_id)
    if not staff.active:raise HTTPException(409,'接班人员已停用，请经理核对换班名单')
    call=locked(s,CrewCall,x.crew_call_id);ensure_open_show(s,call)
    observed=datetime.fromisoformat(d.observed_at)
    call_time=datetime.fromisoformat(call.call_time)
    if not call_time-timedelta(minutes=15)<=observed<=call_time+timedelta(minutes=15):
        raise HTTPException(409,'接班到岗时间须在点名时刻前后15分钟内')
    x.state='arrived';x.observed_at=observed.isoformat();x.recorded_at=now_iso();x.recorded_by=u.email
    audit(s,u,f'接班签到 {x.to_staff_code}→点名#{call.id}（批次 {b.batch_ref}）')
    pending=s.scalars(select(SwapItem).where(SwapItem.batch_id==b.id,SwapItem.state=='waiting')).all()
    if not pending:_effect(s,b,u)  # 全部到岗才一次性切换；未齐时原班继续值守
    return item_view(s,x)

# ---------- 一次性生效 ----------
def _effect(s,b,u):
    items=s.scalars(select(SwapItem).where(SwapItem.batch_id==b.id)).all()
    staff_ids=sorted({i.to_staff_id for i in items}|{i.from_staff_id for i in items})
    for sid in staff_ids:locked(s,StaffMember,sid)
    call_ids=sorted({i.crew_call_id for i in items})
    for cid in call_ids:
        c=locked(s,CrewCall,cid);ensure_open_show(s,c)
    pairs=[(i.to_staff_id,s.get(CrewCall,i.crew_call_id)) for i in items]
    releases={}
    for x in items:
        for other in items:
            if other.from_staff_id==x.to_staff_id and other.id!=x.id:
                releases.setdefault(x.to_staff_id,set()).add(other.assignment_id)
    # 生效前最终复核
    for x in items:
        a=locked(s,CrewAssignment,x.assignment_id)
        if a.staff_id!=x.from_staff_id:raise HTTPException(409,'原班表已被其他操作改动，本批次保留原班，请重新登记')
        to_staff=locked(s,StaffMember,x.to_staff_id)
        if not to_staff.active:raise HTTPException(409,f'接班人员 {to_staff.code} 已停用，批次保留原班')
        staff_conflicts(s,to_staff,s.get(CrewCall,x.crew_call_id),pairs,releases.get(x.to_staff_id,()))
        if s.scalar(select(CrewAssignment.id).where(CrewAssignment.crew_call_id==x.crew_call_id,CrewAssignment.staff_id==to_staff.id,CrewAssignment.id!=a.id)):
            raise HTTPException(409,f'{to_staff.code} 已被其他操作排入本场，批次保留原班')
    # 全部通过后一次性切换班表并留痕
    notes=[]
    for x in items:
        a=locked(s,CrewAssignment,x.assignment_id)
        post=s.scalar(select(ZonePost).where(ZonePost.assignment_id==a.id))
        incidents=s.scalars(select(HouseIncident).where(HouseIncident.assignee_id==a.id)).all()
        # 原签到证据已在登记时快照到 SwapItem；班表身份整体换成接班者并视为已到岗
        a.staff_id=x.to_staff_id;a.staff_code=x.to_staff_code;a.staff_name=x.to_staff_name
        a.checked_in=True;a.observed_at=x.observed_at;a.recorded_at=x.recorded_at;a.recorded_by=x.recorded_by
        moved=[]
        if post:
            s.add(SwapHandover(swap_item_id=x.id,batch_id=b.id,kind='zone_post',ref_id=post.id,ref_state='active',
                               from_staff_code=x.from_staff_code,from_staff_name=x.from_staff_name,
                               to_staff_code=x.to_staff_code,to_staff_name=x.to_staff_name))
            moved.append('入口岗位')
        for inc in incidents:
            # 已结事件责任人保留为换班前人员快照；未结事件随班表由接班者接管
            s.add(SwapHandover(swap_item_id=x.id,batch_id=b.id,kind='incident',ref_id=inc.id,ref_state=inc.state,
                               from_staff_code=x.from_staff_code,from_staff_name=x.from_staff_name,
                               to_staff_code=x.to_staff_code,to_staff_name=x.to_staff_name))
            if inc.state in OPEN_INCIDENT_STATES:moved.append(f'未结事件#{inc.id}')
        x.state='swapped'
        x.handover_note=('接班者接管：'+'、'.join(moved)) if moved else '该岗无入口岗位或未结事件，直接接岗；已结事件责任人保留'
        notes.append(f'{x.from_staff_code}→{x.to_staff_code}')
    b.state='effective';b.effective_at=now_iso();b.effective_by=u.email
    audit(s,u,f'换班批次生效 {b.batch_ref}：'+'，'.join(notes))

def cancel(s,batch_id,u):
    preview=s.get(SwapBatch,batch_id)
    if not preview:raise HTTPException(404,'换班批次不存在')
    b=locked(s,SwapBatch,batch_id)
    if b.state!='pending':raise HTTPException(409,'仅待接班批次可以取消，已生效不可撤回')
    items=s.scalars(select(SwapItem).where(SwapItem.batch_id==b.id,SwapItem.state.in_(OPEN_ITEM_STATES))).all()
    for x in items:x.state='cancelled'
    b.state='cancelled';b.cancelled_at=now_iso();b.cancelled_by=u.email
    audit(s,u,f'取消换班批次 {b.batch_ref}，原班继续值守')
    return batch_view(s,b)

def list_batches(s):
    return [batch_view(s,b) for b in s.scalars(select(SwapBatch).order_by(SwapBatch.id.desc())).all()]
