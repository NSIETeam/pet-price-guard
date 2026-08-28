import csv
import hmac
import io
import os
from contextlib import asynccontextmanager
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core import Alert, Channel, Monitor, SessionLocal, WebhookDelivery, deliver_pending_webhooks, run_monitor, validate_public_url, verify_schema

scheduler = BackgroundScheduler(timezone="UTC")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("PPG_API_KEY")
    if not expected:
        raise HTTPException(503, "PPG_API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "invalid API key")


class ChannelIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    selector: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if os.getenv("PPG_COLLECTOR", "web") == "demo" and value.startswith("demo://"):
            return value
        validate_public_url(value)
        return value


class MonitorIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    brand: str = Field(min_length=1, max_length=200)
    product: str = Field(min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=100)
    floor_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    channels: list[ChannelIn] = Field(min_length=1, max_length=100)
    schedule: str | None = Field(default=None, max_length=100)

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str | None) -> str | None:
        if value:
            try:
                CronTrigger.from_crontab(value, timezone="UTC")
            except ValueError as exc:
                raise ValueError("invalid five-field cron expression") from exc
        return value


class MonitorBatchIn(BaseModel):
    monitors: list[MonitorIn] = Field(min_length=1, max_length=1000)


class CSVImportIn(BaseModel):
    csv_text: str = Field(min_length=1, max_length=5_000_000)


def as_monitor(monitor: Monitor) -> dict:
    return {"id": monitor.id, "brand": monitor.brand, "product": monitor.product, "sku": monitor.sku, "floor_price": str(monitor.floor_price), "schedule": monitor.schedule, "active": monitor.active, "channels": [{"id": c.id, "name": c.name, "url": c.url, "selector": c.selector, "in_breach": c.in_breach, "last_price": str(c.last_price) if c.last_price is not None else None} for c in monitor.channels]}


def schedule_monitor(monitor: Monitor) -> None:
    job_id = f"monitor-{monitor.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if monitor.active and monitor.schedule:
        scheduler.add_job(run_monitor, CronTrigger.from_crontab(monitor.schedule, timezone="UTC"), args=[monitor.id], id=job_id, replace_existing=True, max_instances=1, coalesce=True)


def create_one(db, data: MonitorIn) -> Monitor:
    monitor = Monitor(brand=data.brand, product=data.product, sku=data.sku, floor_price=data.floor_price, schedule=data.schedule)
    monitor.channels = [Channel(name=item.name, url=item.url, selector=item.selector) for item in data.channels]
    db.add(monitor)
    db.flush()
    return monitor


@asynccontextmanager
async def lifespan(app: FastAPI):
    verify_schema()
    webhook_url = os.getenv("PPG_WEBHOOK_URL")
    if webhook_url:
        validate_public_url(webhook_url)
    scheduler.start()
    scheduler.add_job(deliver_pending_webhooks, "interval", minutes=1, id="webhook-delivery", replace_existing=True, max_instances=1, coalesce=True)
    with SessionLocal() as db:
        for monitor in db.query(Monitor).filter(Monitor.active.is_(True)):
            schedule_monitor(monitor)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Pet Price Guard", version="1.0.0", lifespan=lifespan)
protected = [Depends(require_api_key)]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/monitors", status_code=201, dependencies=protected)
def create_monitor(data: MonitorIn):
    with SessionLocal() as db:
        monitor = create_one(db, data)
        db.commit()
        db.refresh(monitor)
        schedule_monitor(monitor)
        return as_monitor(monitor)


@app.post("/monitors/batch", status_code=201, dependencies=protected)
def create_monitor_batch(data: MonitorBatchIn):
    with SessionLocal() as db:
        monitors = [create_one(db, item) for item in data.monitors]
        db.commit()
        for monitor in monitors:
            schedule_monitor(monitor)
        return {"created": len(monitors), "monitors": [as_monitor(monitor) for monitor in monitors]}


@app.post("/monitors/import-csv", status_code=201, dependencies=protected)
def import_csv(data: CSVImportIn):
    rows = list(csv.DictReader(io.StringIO(data.csv_text)))
    if not rows:
        raise HTTPException(422, "CSV must include a header and at least one row")
    required = {"brand", "product", "floor_price", "channel", "url"}
    if not required.issubset(rows[0]):
        raise HTTPException(422, f"CSV is missing columns: {sorted(required - set(rows[0]))}")
    try:
        monitors = [MonitorIn(brand=row["brand"], product=row["product"], sku=row.get("sku") or None, floor_price=row["floor_price"], schedule=row.get("schedule") or None, channels=[ChannelIn(name=row["channel"], url=row["url"], selector=row.get("selector") or None)]) for row in rows]
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"Invalid CSV: {exc}") from exc
    return create_monitor_batch(MonitorBatchIn(monitors=monitors))


@app.get("/monitors", dependencies=protected)
def list_monitors():
    with SessionLocal() as db:
        return [as_monitor(monitor) for monitor in db.query(Monitor).order_by(Monitor.id).all()]


@app.post("/monitors/{monitor_id}/run", dependencies=protected)
def run(monitor_id: int):
    try:
        return run_monitor(monitor_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/alerts", dependencies=protected)
def alerts(status: str | None = None):
    if status not in {None, "open", "acknowledged"}:
        raise HTTPException(422, "invalid alert status")
    with SessionLocal() as db:
        query = db.query(Alert).order_by(Alert.created_at.desc())
        query = query.filter(Alert.status == status) if status else query
        return [{"id": item.id, "monitor_id": item.monitor_id, "observation_id": item.observation_id, "channel": item.channel, "price": str(item.price), "threshold_price": str(item.threshold_price), "status": item.status, "evidence_hash": item.evidence_hash, "created_at": item.created_at} for item in query.all()]


@app.post("/alerts/{alert_id}/ack", dependencies=protected)
def ack(alert_id: int):
    from datetime import datetime, timezone
    with SessionLocal() as db:
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(404, "alert not found")
        alert.status = "acknowledged"
        alert.acknowledged_at = datetime.now(timezone.utc)
        db.commit()
        return {"id": alert.id, "status": alert.status}


@app.post("/webhooks/deliver", dependencies=protected)
def deliver_webhooks():
    return deliver_pending_webhooks()


@app.get("/reports/summary", dependencies=protected)
def summary():
    with SessionLocal() as db:
        return {"monitors": db.query(Monitor).count(), "alerts_open": db.query(Alert).filter(Alert.status == "open").count(), "alerts_total": db.query(Alert).count(), "webhooks_pending": db.query(WebhookDelivery).filter(WebhookDelivery.status.in_(["pending", "retry"])).count(), "webhooks_failed": db.query(WebhookDelivery).filter(WebhookDelivery.status == "failed").count()}
