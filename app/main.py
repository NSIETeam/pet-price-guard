from contextlib import asynccontextmanager
import csv
import io

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .core import Alert, Channel, Monitor, SessionLocal, init_db, run_monitor

scheduler = BackgroundScheduler()

class ChannelIn(BaseModel):
    name: str
    url: str
    selector: str | None = None

class MonitorIn(BaseModel):
    brand: str = Field(min_length=1)
    product: str = Field(min_length=1)
    sku: str | None = None
    floor_price: float = Field(gt=0)
    channels: list[ChannelIn] = Field(min_length=1)
    schedule: str | None = None

class MonitorBatchIn(BaseModel):
    monitors: list[MonitorIn] = Field(min_length=1, max_length=1000)

def as_monitor(m):
    return {"id": m.id, "brand": m.brand, "product": m.product, "sku": m.sku, "floor_price": m.floor_price, "schedule": m.schedule, "active": m.active, "channels": [{"name": c.name, "url": c.url, "selector": c.selector} for c in m.channels]}

def schedule_monitor(m):
    if m.schedule:
        try:
            scheduler.add_job(run_monitor, CronTrigger.from_crontab(m.schedule), args=[m.id], id=f"monitor-{m.id}", replace_existing=True)
        except ValueError:
            pass

def create_one(db, data: MonitorIn):
    monitor = Monitor(brand=data.brand, product=data.product, sku=data.sku, floor_price=data.floor_price, schedule=data.schedule)
    monitor.channels = [Channel(name=channel.name, url=channel.url, selector=channel.selector) for channel in data.channels]
    db.add(monitor)
    db.flush()
    return monitor

@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler.start()
    with SessionLocal() as db:
        for monitor in db.query(Monitor).filter(Monitor.active == True):
            schedule_monitor(monitor)
    yield
    scheduler.shutdown(wait=False)

app = FastAPI(title="Pet Price Guard", version="0.2.0", lifespan=lifespan)

@app.post("/monitors", status_code=201)
def create_monitor(data: MonitorIn):
    with SessionLocal() as db:
        monitor = create_one(db, data)
        db.commit()
        db.refresh(monitor)
        schedule_monitor(monitor)
        return as_monitor(monitor)

@app.post("/monitors/batch", status_code=201)
def create_monitor_batch(data: MonitorBatchIn):
    """Create up to 1,000 monitors from one Agent-friendly JSON request."""
    with SessionLocal() as db:
        monitors = [create_one(db, item) for item in data.monitors]
        db.commit()
        for monitor in monitors:
            schedule_monitor(monitor)
        return {"created": len(monitors), "monitors": [as_monitor(monitor) for monitor in monitors]}

@app.post("/monitors/import-csv", status_code=201)
def import_csv(csv_text: str):
    """Import UTF-8 CSV text: brand,product,sku,floor_price,channel,url,selector,schedule."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        raise HTTPException(422, "CSV must include a header and at least one row")
    try:
        monitors = [MonitorIn(brand=row["brand"], product=row["product"], sku=row.get("sku") or None, floor_price=float(row["floor_price"]), schedule=row.get("schedule") or None, channels=[ChannelIn(name=row["channel"], url=row["url"], selector=row.get("selector") or None)]) for row in rows]
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"Invalid CSV: {exc}")
    return create_monitor_batch(MonitorBatchIn(monitors=monitors))

@app.get("/monitors")
def list_monitors():
    with SessionLocal() as db:
        return [as_monitor(monitor) for monitor in db.query(Monitor).order_by(Monitor.id).all()]

@app.post("/monitors/{monitor_id}/run")
def run(monitor_id: int):
    try:
        return run_monitor(monitor_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))

@app.get("/alerts")
def alerts(status: str | None = None):
    with SessionLocal() as db:
        query = db.query(Alert).order_by(Alert.created_at.desc())
        query = query.filter(Alert.status == status) if status else query
        return [{"id": alert.id, "monitor_id": alert.monitor_id, "observation_id": alert.observation_id, "channel": alert.channel, "price": alert.price, "status": alert.status, "evidence_hash": alert.evidence_hash, "created_at": alert.created_at} for alert in query.all()]

@app.post("/alerts/{alert_id}/ack")
def ack(alert_id: int):
    with SessionLocal() as db:
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(404, "alert not found")
        alert.status = "acknowledged"
        db.commit()
        return {"id": alert.id, "status": alert.status}

@app.get("/reports/summary")
def summary():
    with SessionLocal() as db:
        return {"monitors": db.query(Monitor).count(), "alerts_open": db.query(Alert).filter(Alert.status == "open").count(), "alerts_total": db.query(Alert).count()}