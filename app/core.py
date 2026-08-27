import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DATABASE_URL = os.getenv("PPG_DATABASE_URL", "sqlite:///./priceguard.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase): pass

class Monitor(Base):
    __tablename__ = "monitors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(200))
    product: Mapped[str] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    floor_price: Mapped[float] = mapped_column(Float)
    schedule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    channels: Mapped[list["Channel"]] = relationship(cascade="all, delete-orphan")

class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"))
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(Text)
    selector: Mapped[str | None] = mapped_column(String(200), nullable=True)

class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"))
    channel: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(Text)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observations.id"))
    channel: Mapped[str] = mapped_column(String(100))
    price: Mapped[float] = mapped_column(Float)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

def init_db():
    Base.metadata.create_all(engine)

def parse_price(text: str) -> float | None:
    vals = re.findall(r"(?:¥|￥|RMB|CNY|USD|\$)?\s*([0-9]+(?:[.,][0-9]{1,2})?)", text.replace(",", ""))
    try: return float(vals[0]) if vals else None
    except ValueError: return None

def collect(channel: Channel) -> tuple[float | None, str]:
    if os.getenv("PPG_COLLECTOR", "web") == "demo" or channel.url.startswith("demo://"):
        price = parse_price(channel.url) or 88.0
        return price, f"demo:{channel.name}:{channel.url}"
    response = httpx.get(channel.url, timeout=20, follow_redirects=True, headers={"User-Agent": "PetPriceGuard/0.1"})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    if channel.selector:
        node = soup.select_one(channel.selector); text = node.get_text(" ", strip=True) if node else ""
    else:
        node = soup.select_one('meta[property="product:price:amount"], meta[property="og:price:amount"]')
        text = node.get("content", "") if node else ""
        if not text:
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    data = json.loads(script.string or "")
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        offer = item.get("offers", {}) if isinstance(item, dict) else {}
                        if isinstance(offer, list): offer = offer[0] if offer else {}
                        if offer.get("price") is not None: text = str(offer["price"]); break
                except (ValueError, TypeError): pass
                if text: break
        if not text: text = soup.get_text(" ", strip=True)
    return parse_price(text), text[:1000]

def run_monitor(monitor_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        monitor = db.get(Monitor, monitor_id)
        if not monitor: raise ValueError("monitor not found")
        found = alerts = 0
        for channel in monitor.channels:
            try: price, raw = collect(channel)
            except Exception as exc: price, raw = None, f"collector_error: {exc}"
            obs = Observation(monitor_id=monitor.id, channel=channel.name, url=channel.url, price=price, raw=raw)
            db.add(obs); db.flush(); found += 1
            if price is not None and price < monitor.floor_price:
                fp = hashlib.sha256(f"{monitor.id}|{channel.name}|{price}".encode()).hexdigest()
                evidence = hashlib.sha256(f"{channel.url}|{price}|{raw}".encode()).hexdigest()
                if not db.scalar(select(Alert).where(Alert.fingerprint == fp)):
                    alert = Alert(monitor_id=monitor.id, observation_id=obs.id, channel=channel.name, price=price, fingerprint=fp, evidence_hash=evidence)
                    db.add(alert); db.flush(); alerts += 1
                    webhook_url = os.getenv("PPG_WEBHOOK_URL")
                    if webhook_url:
                        try:
                            httpx.post(webhook_url, json={"type": "violation.opened", "version": "1", "alert_id": alert.id, "monitor_id": monitor.id, "brand": monitor.brand, "product": monitor.product, "sku": monitor.sku, "channel": channel.name, "url": channel.url, "observed_price": price, "threshold_price": monitor.floor_price, "evidence_hash": evidence, "occurred_at": obs.captured_at.isoformat()}, timeout=5, headers={"User-Agent": "PetPriceGuard/0.1"}).raise_for_status()
                        except httpx.HTTPError:
                            pass
        db.commit()
        return {"monitor_id": monitor_id, "observations": found, "new_alerts": alerts}
