"""本地测试夹具：用 SQLite + StaticPool 作为 PostgreSQL 的测试替身。

生产环境仍使用 PostgreSQL（docker-compose），本文件只服务于 pytest，
不改动任何应用代码；FOR UPDATE 在 SQLite 上被 SQLAlchemy 编译为无害空操作，
并发互斥另由部分唯一索引与线程级集成测试覆盖。
"""
import os
os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:///:memory:')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('JWT_SECRET', 'test-secret')

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db import Base
# 导入所有模型，保证 Base.metadata 收集到全部表
from src import models, staff_models, debrief_models, zone_models, incident_models, lost_models  # noqa: F401
from src import swap_models  # noqa: F401


@pytest.fixture()
def engine():
    eng = create_engine(
        'sqlite+pysqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def client(monkeypatch, engine, session_factory):
    """FastAPI TestClient，db_session 依赖替换为同一个内存库会话工厂。"""
    from fastapi.testclient import TestClient
    from src import db as db_pkg
    from src import main as main_module

    def fake_session():
        with session_factory() as s:
            yield s

    monkeypatch.setattr(db_pkg, 'Session', session_factory)
    monkeypatch.setattr(main_module, 'engine', engine)
    main_module.app.dependency_overrides[db_pkg.db_session] = fake_session
    with TestClient(main_module.app) as c:
        yield c
    main_module.app.dependency_overrides.clear()


@pytest.fixture()
def tokens(client):
    def login(role):
        r = client.post('/auth/login', json={'email': f'{role}@nanan.demo', 'password': 'Curtain2026!'})
        assert r.status_code == 200, r.text
        return r.json()['token']

    return {role: login(role) for role in ('admin', 'steward', 'viewer')}


@pytest.fixture()
def auth(tokens):
    def h(role):
        return {'Authorization': 'Bearer ' + tokens[role]}

    return h
