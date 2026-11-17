"""临时换班：批次登记、接班签到、整批生效与责任交接。"""
from datetime import date, datetime, timedelta
import pytest
from sqlalchemy import select

from src.models import CrewCall, Performance, Production
from src.staff_models import StaffMember, CrewAssignment
from src.swap_models import SwapBatch, SwapItem, SwapHandover
from src.zone_models import EntranceZone, ZonePost
from src.incident_models import HouseIncident
from src.debrief_models import ShowDebrief
from tests.factory import two_shows, make_show, make_staff, make_users, assign


@pytest.fixture()
def world(client, session_factory):
    with session_factory() as sf:
        data = two_shows(sf)
    return client, data, session_factory


def hdr(client, role='admin'):
    r = client.post('/auth/login', json={'email': f'{role}@nanan.demo', 'password': 'Curtain2026!'})
    assert r.status_code == 200
    return {'Authorization': 'Bearer ' + r.json()['token']}


def test_full_batch_switch_is_all_or_nothing(world):
    c, data, sf_factory = world
    h = hdr(c)
    a1, a2, b1, b2 = data['assignment_ids']
    call1, call2 = data['calls']
    s1, s3, s5 = data['staff_ids'][0], data['staff_ids'][2], data['staff_ids'][4]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-001', 'reason': '临时换班演练',
        'items': [{'assignment_id': a1, 'to_staff_id': s3},
                  {'assignment_id': b1, 'to_staff_id': s1}]})
    assert r.status_code == 200, r.text
    batch = r.json()
    assert batch['state'] == 'pending' and batch['total'] == 2 and batch['arrived'] == 0
    items = batch['items']
    with sf_factory() as s:
        assert s.get(CrewAssignment, a1).staff_id == s1
    first = next(x for x in items if x['from_view']['code'] == 'ST-01')
    ok_time = (datetime.fromisoformat(call1['call_time']) + timedelta(minutes=5)).isoformat()
    r = c.post(f"/swap-items/{first['id']}/check-in", headers=hdr(c, 'steward'),
               json={'observed_at': ok_time})
    assert r.status_code == 200
    got = c.get('/swap-batches', headers=h).json()[0]
    assert got['state'] == 'pending' and got['arrived'] == 1
    with sf_factory() as s:
        assert s.get(CrewAssignment, a1).staff_id == s1
    second = next(x for x in items if x['from_view']['code'] == 'ST-03')
    ok_time2 = (datetime.fromisoformat(call2['call_time']) - timedelta(minutes=10)).isoformat()
    r = c.post(f"/swap-items/{second['id']}/check-in", headers=h, json={'observed_at': ok_time2})
    assert r.status_code == 200
    got = c.get('/swap-batches', headers=h).json()[0]
    assert got['state'] == 'effective' and got['arrived'] == 2
    with sf_factory() as s:
        assert s.get(CrewAssignment, a1).staff_id == s3
        assert s.get(CrewAssignment, a1).checked_in is True
        assert s.get(CrewAssignment, b1).staff_id == s1
        assert s.get(CrewAssignment, b1).checked_in is True
        it = s.get(SwapItem, first['id'])
        assert it.from_checked_in is True and it.from_observed_at == call1['call_time']
        assert it.observed_at == ok_time


def test_checkin_window_and_duplicate_idempotent(world):
    c, data, _ = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-002',
        'items': [{'assignment_id': a1, 'to_staff_id': data['staff_ids'][4]}]})
    item = r.json()['items'][0]
    call = data['calls'][0]
    bad = (datetime.fromisoformat(call['call_time']) + timedelta(minutes=16)).isoformat()
    r = c.post(f"/swap-items/{item['id']}/check-in", headers=h, json={'observed_at': bad})
    assert r.status_code == 409 and '15分钟' in r.json()['detail']
    good = (datetime.fromisoformat(call['call_time']) - timedelta(minutes=15)).isoformat()
    r = c.post(f"/swap-items/{item['id']}/check-in", headers=h, json={'observed_at': good})
    assert r.status_code == 200
    r2 = c.post(f"/swap-items/{item['id']}/check-in", headers=h, json={'observed_at': good})
    assert r2.status_code == 200 and r2.json()['observed_at'] == good


def test_overlap_and_daily_cap_rejected_at_registration(world):
    c, data, sf_factory = world
    h = hdr(c)
    with sf_factory() as s:
        st5 = s.get(StaffMember, data['staff_ids'][4])
        st6 = s.get(StaffMember, data['staff_ids'][5])
        # 下午场 14:00 开场（占用至 16:05），与 18:00 开场的晚场（17:15 点名）相接不重叠，
        # 因此再构造一场 16:00 开场的下午场，与晚场真正重叠
        _, _, call_afternoon = make_show(s, 'P-PM2', date(2026, 11, 20), curtain='16:00')
        _, _, call_overlap = make_show(s, 'P-OVL', date(2026, 11, 20), curtain='18:00')
        assign(s, call_afternoon, st5)
        st5.max_daily_calls = 1
        ev = assign(s, call_overlap, st6)
        s.commit()
        st5_id, ev_id, a1_id = st5.id, ev.id, data['assignment_ids'][0]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-CAP',
        'items': [{'assignment_id': a1_id, 'to_staff_id': st5_id}]})
    assert r.status_code == 409 and '上限' in r.json()['detail']
    with sf_factory() as s:
        s.get(StaffMember, st5_id).max_daily_calls = 3
        s.commit()
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-OVL',
        'items': [{'assignment_id': ev_id, 'to_staff_id': st5_id}]})
    assert r.status_code == 409 and '重叠' in r.json()['detail']


def test_mutual_swap_releases_original_occupancy(world):
    c, data, _ = world
    h = hdr(c)
    a1, a2, b1, _ = data['assignment_ids']
    s1, s3 = data['staff_ids'][0], data['staff_ids'][2]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-MUTUAL',
        'items': [{'assignment_id': a1, 'to_staff_id': s3},
                  {'assignment_id': b1, 'to_staff_id': s1}]})
    assert r.status_code == 200
    r2 = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-DUP',
        'items': [{'assignment_id': a2, 'to_staff_id': s3}]})
    assert r2.status_code == 409


def test_cross_production_mutual_swap(world):
    """两名引导员在两个不同剧目的场次间整批互换。"""
    c, data, sf_factory = world
    h = hdr(c)
    with sf_factory() as s:
        # 另一剧目、另一天的确认点名，安排 data 中的 ST-05 在岗
        _, _, other_call = make_show(s, 'P-OTHER', date(2026, 11, 23), curtain='15:00')
        st5 = s.get(StaffMember, data['staff_ids'][4])
        other_a = assign(s, other_call, st5, True, other_call.call_time)
        s.commit(); other_id, st5_id = other_a.id, st5.id
    a1 = data['assignment_ids'][0]   # ST-01 在 P-DAY1
    s1 = data['staff_ids'][0]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-XPROD',
        'items': [{'assignment_id': a1, 'to_staff_id': st5_id},
                  {'assignment_id': other_id, 'to_staff_id': s1}]})
    assert r.status_code == 200, r.text
    b = r.json()
    times = {i['from_view']['code']: i['call_time'] for i in b['items']}
    for i in b['items']:
        rr = c.post(f"/swap-items/{i['id']}/check-in", headers=h,
                    json={'observed_at': i['call_time']})
        assert rr.status_code == 200, rr.text
    got = c.get(f"/swap-batches/{b['id']}", headers=h).json()
    assert got['state'] == 'effective' and got['arrived'] == 2
    with sf_factory() as s:
        assert s.get(CrewAssignment, a1).staff_id == st5_id
        assert s.get(CrewAssignment, other_id).staff_id == s1
    assert times  # 两条来自不同点名单


def test_debrief_draft_rejects_swap(client, session_factory):
    with session_factory() as sf:
        make_users(sf); staff = make_staff(sf, 2)
        _, perf, call = make_show(sf, 'P-FROZEN', date(2026, 11, 25))
        a = assign(sf, call, staff[0], True, call.call_time)
        sf.add(ShowDebrief(performance_id=perf.id, actual_attendance=80, exit_minutes=10,
                           forecast_snapshot=80, required_staff=1, checked_in_snapshot=1,
                           variance_pct=0, state='draft'))
        sf.commit(); a_id, to_id = a.id, staff[1].id
    h = hdr(client)
    r = client.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-FROZEN',
        'items': [{'assignment_id': a_id, 'to_staff_id': to_id}]})
    assert r.status_code == 409 and '交接' in r.json()['detail']


def test_handover_posts_and_incidents_with_evidence(world):
    c, data, sf_factory = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    call1 = data['calls'][0]
    with sf_factory() as s:
        cw = s.get(CrewCall, call1['id'])
        p = s.get(Performance, cw.performance_id); prod = s.get(Production, p.production_id)
        zone = EntranceZone(production_id=prod.id, code='EAST', capacity=80, required_staff=1)
        s.add(zone); s.flush()
        s.add(ZonePost(assignment_id=a1, zone_id=zone.id))
        inc_open = HouseIncident(performance_id=p.id, zone_id=zone.id, severity='normal',
                                 description='入口咨询事件', state='assigned', assignee_id=a1)
        inc_closed = HouseIncident(performance_id=p.id, zone_id=zone.id, severity='high',
                                   description='已处置拥堵', state='closed', assignee_id=a1,
                                   closed_by='admin@nanan.demo', resolution='已完成疏导')
        s.add_all([inc_open, inc_closed]); s.commit()
        open_id, closed_id, s3 = inc_open.id, inc_closed.id, data['staff_ids'][2]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-HAND',
        'items': [{'assignment_id': a1, 'to_staff_id': s3}]})
    item = r.json()['items'][0]
    r = c.post(f"/swap-items/{item['id']}/check-in", headers=h,
               json={'observed_at': call1['call_time']})
    assert r.status_code == 200
    with sf_factory() as s:
        ho = s.scalars(select(SwapHandover).where(SwapHandover.swap_item_id == item['id'])).all()
        kinds = {(x.kind, x.ref_state) for x in ho}
        assert ('zone_post', 'active') in kinds
        assert ('incident', 'assigned') in kinds and ('incident', 'closed') in kinds
        oi = s.get(HouseIncident, open_id)
        assert oi.assignee_id == a1 and s.get(CrewAssignment, a1).staff_code == 'ST-03'
        ci = s.get(HouseIncident, closed_id)
        assert ci.state == 'closed' and ci.closed_by == 'admin@nanan.demo'
    rows = {x['id']: x for x in c.get('/house-incidents', headers=h).json()}
    assert rows[closed_id]['assignee']['code'] == 'ST-01'
    assert rows[closed_id]['assignee_before_swap']['code'] == 'ST-01'
    assert rows[open_id]['assignee']['code'] == 'ST-03'


def test_cancel_keeps_original_crew(world):
    c, data, sf_factory = world
    h = hdr(c)
    a1 = data['assignment_ids'][0]
    s5 = data['staff_ids'][4]
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-CANCEL', 'items': [{'assignment_id': a1, 'to_staff_id': s5}]})
    bid, item_id = r.json()['id'], r.json()['items'][0]['id']
    r = c.post(f'/swap-batches/{bid}/cancel', headers=h)
    assert r.status_code == 200 and r.json()['state'] == 'cancelled'
    with sf_factory() as s:
        assert s.get(CrewAssignment, a1).staff_id == data['staff_ids'][0]
        assert s.get(SwapItem, item_id).state == 'cancelled'
    r2 = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-CANCEL', 'items': [{'assignment_id': a1, 'to_staff_id': s5}]})
    assert r2.status_code == 200


def test_retry_same_ref_returns_same_batch(world):
    c, data, _ = world
    h = hdr(c)
    payload = {'batch_ref': 'SW-RETRY',
               'items': [{'assignment_id': data['assignment_ids'][0],
                          'to_staff_id': data['staff_ids'][4]}]}
    first = c.post('/swap-batches', headers=h, json=payload).json()
    again = c.post('/swap-batches', headers=h, json=payload).json()
    assert first['id'] == again['id'] and again['state'] == 'pending'


def test_permissions_viewer_readonly(world):
    c, data, _ = world
    h = hdr(c, 'viewer')
    assert c.get('/swap-batches', headers=h).status_code == 200
    r = c.post('/swap-batches', headers=h, json={
        'batch_ref': 'SW-DENY',
        'items': [{'assignment_id': data['assignment_ids'][0],
                   'to_staff_id': data['staff_ids'][4]}]})
    assert r.status_code == 403
    hs = hdr(c, 'steward')
    r = c.post('/swap-batches', headers=hs, json={
        'batch_ref': 'SW-STEWARD-DENY',
        'items': [{'assignment_id': data['assignment_ids'][0],
                   'to_staff_id': data['staff_ids'][4]}]})
    assert r.status_code == 403
