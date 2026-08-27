import os
os.environ["PPG_COLLECTOR"] = "demo"
from app.core import init_db, Monitor, Channel, SessionLocal, run_monitor, Alert
def test_demo_monitor_and_dedup(tmp_path, monkeypatch):
    import app.core as core
    old=core.engine; core.engine=core.create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread":False}); core.SessionLocal=core.sessionmaker(bind=core.engine); init_db()
    with core.SessionLocal() as db:
        m=Monitor(brand="P",product="Food",floor_price=100); m.channels=[Channel(name="demo",url="demo://80")]; db.add(m); db.commit(); db.refresh(m); mid=m.id
    assert run_monitor(mid)["new_alerts"] == 1
    assert run_monitor(mid)["new_alerts"] == 0
    with core.SessionLocal() as db: assert db.query(Alert).count() == 1
    core.engine=old
