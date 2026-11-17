"""测试数据构造：直接用 ORM 搭建已确认点名 + 实名班表的现场场景。"""
from datetime import date, datetime, timedelta
from src.models import Production, Performance, CrewCall, User
from src.staff_models import StaffMember, CrewAssignment
from src.auth import hash_password


def make_users(sf):
    for role in ('admin', 'steward', 'viewer'):
        sf.add(User(email=f'{role}@nanan.demo', role=role,
                    password_hash=hash_password('Curtain2026!')))


def make_staff(sf, n=6):
    names = ['林澈', '周芮', '许舟', '陈言', '方亭', '江沅']
    out = []
    for i in range(n):
        x = StaffMember(code=f'ST-{i+1:02d}', name=names[i], active=True, max_daily_calls=3)
        sf.add(x); out.append(x)
    sf.flush()
    return out


def make_show(sf, code, d, curtain='19:30', call_minutes_before=45, ushers=2):
    prod = Production(code=code, title='剧目' + code, seats=160,
                      duration_minutes=95, forecast_state='ready')
    sf.add(prod); sf.flush()
    perf = Performance(production_id=prod.id, date=d, curtain=curtain,
                       forecast=80, raw_forecast=80.0, status='crew_confirmed')
    sf.add(perf); sf.flush()
    call_time = (datetime.fromisoformat(d.isoformat() + 'T' + curtain)
                 - timedelta(minutes=call_minutes_before)).isoformat()
    call = CrewCall(performance_id=perf.id, ushers=ushers, call_time=call_time,
                    leader='领班', status='confirmed')
    sf.add(call); sf.flush()
    return prod, perf, call


def assign(sf, call, staff, checked_in=False, observed_at=None, recorded_by='steward@nanan.demo'):
    x = CrewAssignment(crew_call_id=call.id, staff_id=staff.id,
                       staff_code=staff.code, staff_name=staff.name,
                       checked_in=checked_in, observed_at=observed_at,
                       recorded_at=datetime.now().isoformat() if checked_in else None,
                       recorded_by=recorded_by if checked_in else None)
    sf.add(x); sf.flush()
    return x


def two_shows(sf):
    """两场不重叠的确认点名（不同日期），各配两名原班；返回纯 id/标量快照。"""
    make_users(sf)
    staff = make_staff(sf)
    _, _, call1 = make_show(sf, 'P-DAY1', date(2026, 11, 20))
    _, _, call2 = make_show(sf, 'P-DAY2', date(2026, 11, 21))
    a1 = assign(sf, call1, staff[0], True, call1.call_time)
    a2 = assign(sf, call1, staff[1], True, call1.call_time)
    b1 = assign(sf, call2, staff[2], True, call2.call_time)
    b2 = assign(sf, call2, staff[3], False)
    sf.commit()
    return {
        'staff_ids': [x.id for x in staff],
        'staff_codes': [x.code for x in staff],
        'calls': [{'id': call1.id, 'call_time': call1.call_time},
                  {'id': call2.id, 'call_time': call2.call_time}],
        'assignment_ids': [a1.id, a2.id, b1.id, b2.id],
    }
