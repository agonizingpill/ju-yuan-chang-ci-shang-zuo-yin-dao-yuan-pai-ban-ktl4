"""临时换班：并发占用、待接班守卫与失败保留原班。"""
from datetime import date
import pytest
from sqlalchemy import select

from src.models import CrewCall, Performance
from src.staff_models import StaffMember, CrewAssignment
from src.swap_models import SwapBatch, SwapItem
from src.zone_models import EntranceZone
from src.incident_models import HouseIncident
from tests.factory import two_shows


@pytest.fixture()
def world(client, session_factory):
    with session_factory() as sf:
        data = two_shows(sf)
    return client, data, session_factory


def hdr(client, role='admin'):
    r = client.post('/auth/login', json={'email': f'{role}@nanan.demo', 'password': 'Curtain2026!'})
    return {'Authorization': 'Bearer ' + r.json()['token']}


def perf_id_of(sf_factory, call_id):
    with sf_factory() as s:
        return s.get(CrewCall, call_id).performance_id


def test_concurrent_swap_same_assignment_only_one_wins(world):
    """部分唯一索引：同一班表的两个待接班批次，数据库只放行一个。"""
    c, data, sf_factory = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    s5, s6 = data['staff_ids'][4], data['staff_ids'][5]
    r1 = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-C1', 'items': [{'assignment_id': a1, 'to_staff_id': s5}]})
    r2 = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-C2', 'items': [{'assignment_id': a1, 'to_staff_id': s6}]})
    assert r1.status_code == 200
    assert r2.status_code == 409 and '待接班换班' in r2.json()['detail']
    with sf_factory() as s:
        opens = s.scalars(select(SwapItem).where(
            SwapItem.assignment_id == a1, SwapItem.state.in_(('waiting', 'arrived')))).all()
        assert len(opens) == 1


def test_pending_swap_blocks_normal_assign_and_checkin(world):
    c, data, sf_factory = world
    h = hdr(c)
    a1, _, _, _ = data['assignment_ids']
    call1, call2 = data['calls']
    s5 = data['staff_ids'][4]
    assert c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-GUARD', 'items': [{'assignment_id': a1, 'to_staff_id': s5}]}).status_code == 200
    r = c.post('/crew-assignments', headers=h,
               json={'crew_call_id': call2['id'], 'staff_id': s5})
    assert r.status_code == 409 and '待接班' in r.json()['detail']
    r = c.post(f'/crew-assignments/{a1}/check-in', headers=h,
               json={'observed_at': call1['call_time']})
    assert r.status_code == 409 and '换班' in r.json()['detail']


def test_pending_swap_blocks_zone_post_and_dispatch(world):
    c, data, sf_factory = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    call1 = data['calls'][0]
    with sf_factory() as s:
        p = s.get(Performance, s.get(CrewCall, call1['id']).performance_id)
        zone = EntranceZone(production_id=p.production_id, code='NORTH', capacity=80, required_staff=1)
        s.add(zone); s.flush()
        inc = HouseIncident(performance_id=p.id, zone_id=zone.id, severity='normal',
                            description='测试事件至少八字内容', state='open')
        s.add(inc); s.commit(); zone_id, inc_id = zone.id, inc.id
    c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-GUARD2', 'items': [{'assignment_id': a1, 'to_staff_id': data['staff_ids'][4]}]})
    r = c.post('/zone-posts', headers=h, json={'assignment_id': a1, 'zone_id': zone_id})
    assert r.status_code == 409 and '换班' in r.json()['detail']
    r = c.post(f'/house-incidents/{inc_id}/dispatch', headers=h, json={'assignment_id': a1})
    assert r.status_code == 409 and '换班' in r.json()['detail']


def test_pending_swap_blocks_debrief_save(world):
    c, data, sf_factory = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    call1 = data['calls'][0]
    c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-GUARD3', 'items': [{'assignment_id': a1, 'to_staff_id': data['staff_ids'][4]}]})
    r = c.post('/show-debriefs', headers=h, json={
        'performance_id': perf_id_of(sf_factory, call1['id']),
        'actual_attendance': 80, 'exit_minutes': 10, 'incident_note': ''})
    assert r.status_code == 409 and '换班' in r.json()['detail']


def test_failed_effect_keeps_original_crew(world):
    """生效前复核失败（接班者被停用）→ 整批保留原班，无部分切换。"""
    c, data, sf_factory = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    call1 = data['calls'][0]
    s5 = data['staff_ids'][4]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-FAIL', 'items': [{'assignment_id': a1, 'to_staff_id': s5}]})
    item = r.json()['items'][0]
    with sf_factory() as s:
        s.get(StaffMember, s5).active = False
        s.commit()
    r = c.post(f"/swap-items/{item['id']}/check-in", headers=h,
               json={'observed_at': call1['call_time']})
    assert r.status_code == 409 and '停用' in r.json()['detail']
    with sf_factory() as s:
        assert s.get(CrewAssignment, a1).staff_id == data['staff_ids'][0]
        assert s.get(SwapBatch, item['batch_id']).state == 'pending'
        assert s.get(SwapItem, item['id']).state == 'waiting'


def test_inactive_replacement_rejected_at_registration(world):
    c, data, sf_factory = world
    h = hdr(c)
    s6 = data['staff_ids'][5]
    with sf_factory() as s:
        s.get(StaffMember, s6).active = False
        s.commit()
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-INACTIVE',
        'items': [{'assignment_id': data['assignment_ids'][0], 'to_staff_id': s6}]})
    assert r.status_code == 409 and '停用' in r.json()['detail']


def test_progress_view_shows_before_and_after(world):
    c, data, _ = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    call1 = data['calls'][0]
    s5 = data['staff_ids'][4]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-PROG', 'items': [{'assignment_id': a1, 'to_staff_id': s5}]})
    item = r.json()['items'][0]
    assert item['from_view']['code'] == 'ST-01'
    assert item['to_view']['code'] == 'ST-05'
    assert item['from_view']['observed_at'] == call1['call_time']
    c.post(f"/swap-items/{item['id']}/check-in", headers=h,
           json={'observed_at': call1['call_time']})
    got = c.get(f"/swap-batches/{r.json()['id']}", headers=h).json()
    assert got['state'] == 'effective' and got['arrived'] == 1
    assert got['items'][0]['state'] == 'swapped'
    assert got['effective_by'] == 'admin@nanan.demo'
