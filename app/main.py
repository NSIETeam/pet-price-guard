from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from .core import Alert, Channel, Monitor, SessionLocal, init_db, run_monitor

scheduler = BackgroundScheduler()
class ChannelIn(BaseModel):
    name: str
    url: str
    selector: str | None = None
class MonitorIn(BaseModel):
    brand: str = Field(min_length=1); product: str = Field(min_length=1)
    sku: str | None = None; floor_price: float = Field(gt=0)
    channels: list[ChannelIn] = Field(min_length=1); schedule: str | None = None
def as_monitor(m):
    return {"id":m.id,"brand":m.brand,"product":m.product,"sku":m.sku,"floor_price":m.floor_price,"schedule":m.schedule,"active":m.active,"channels":[{"name":c.name,"url":c.url,"selector":c.selector} for c in m.channels]}
def schedule_monitor(m):
    if m.schedule:
        try: scheduler.add_job(run_monitor, CronTrigger.from_crontab(m.schedule), args=[m.id], id=f"monitor-{m.id}", replace_existing=True)
        except ValueError: pass
@asynccontextmanager
async def lifespan(app):
    init_db(); scheduler.start()
    with SessionLocal() as db:
        for m in db.query(Monitor).filter(Monitor.active == True): schedule_monitor(m)
    yield
    scheduler.shutdown(wait=False)
app = FastAPI(title="Pet Price Guard", version="0.1.0", lifespan=lifespan)
@app.post("/monitors", status_code=201)
def create_monitor(data: MonitorIn):
    with SessionLocal() as db:
        m=Monitor(brand=data.brand,product=data.product,sku=data.sku,floor_price=data.floor_price,schedule=data.schedule)
        m.channels=[Channel(name=c.name,url=c.url,selector=c.selector) for c in data.channels]; db.add(m); db.commit(); db.refresh(m); schedule_monitor(m); return as_monitor(m)
@app.get("/monitors")
def list_monitors():
    with SessionLocal() as db: return [as_monitor(m) for m in db.query(Monitor).order_by(Monitor.id).all()]
@app.post("/monitors/{monitor_id}/run")
def run(monitor_id:int):
    try: return run_monitor(monitor_id)
    except ValueError as e: raise HTTPException(404,str(e))
@app.get("/alerts")
def alerts(status: str | None = None):
    with SessionLocal() as db:
        q=db.query(Alert).order_by(Alert.created_at.desc()); q=q.filter(Alert.status==status) if status else q
        return [{"id":a.id,"monitor_id":a.monitor_id,"observation_id":a.observation_id,"channel":a.channel,"price":a.price,"status":a.status,"evidence_hash":a.evidence_hash,"created_at":a.created_at} for a in q.all()]
@app.post("/alerts/{alert_id}/ack")
def ack(alert_id:int):
    with SessionLocal() as db:
        a=db.get(Alert,alert_id)
        if not a: raise HTTPException(404,"alert not found")
        a.status="acknowledged"; db.commit(); return {"id":a.id,"status":a.status}
@app.get("/reports/summary")
def summary():
    with SessionLocal() as db:
        return {"monitors":db.query(Monitor).count(),"alerts_open":db.query(Alert).filter(Alert.status=="open").count(),"alerts_total":db.query(Alert).count()}
