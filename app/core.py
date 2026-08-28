import hashlib
import ipaddress
import json
import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from bs4 import BeautifulSoup
from httpcore._backends.sync import SyncBackend
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, create_engine, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


DATABASE_URL = os.getenv("PPG_DATABASE_URL", "sqlite:///./priceguard.db")
SCHEMA_REVISION = "20260828_01"
MAX_RESPONSE_BYTES = int(os.getenv("PPG_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024)))
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Monitor(Base):
    __tablename__ = "monitors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str] = mapped_column(String(200))
    product: Mapped[str] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    floor_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
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
    in_breach: Mapped[bool] = mapped_column(Boolean, default=False)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    channel: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    raw: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"))
    observation_id: Mapped[int] = mapped_column(ForeignKey("observations.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    channel: Mapped[str] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    threshold_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_delivery_due", "status", "next_attempt_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def verify_schema() -> None:
    """Fail closed when the database has not been migrated to this release."""
    try:
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as exc:
        raise RuntimeError("database is not migrated; run `alembic upgrade head`") from exc
    if revision != SCHEMA_REVISION:
        raise RuntimeError(f"database revision {revision!r} does not match required revision {SCHEMA_REVISION!r}")


def parse_price(text: str) -> Decimal | None:
    """Parse an explicit monetary amount, rejecting arbitrary page numbers."""
    normalized = text.replace("，", ",").strip()
    patterns = (
        r"(?:¥|￥|RMB|CNY|USD|\$)\s*([0-9]{1,9}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"([0-9]{1,9}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?:元|人民币|CNY|USD)",
        r"^\s*([0-9]{1,9}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            try:
                return Decimal(match.group(1).replace(",", "")).quantize(Decimal("0.01"))
            except InvalidOperation:
                return None
    return None


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("channel URL must be an unauthenticated http(s) URL")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("channel URL uses a disallowed port")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("channel hostname cannot be resolved") from exc
    if not addresses:
        raise ValueError("channel hostname has no addresses")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("channel URL resolves to a private or reserved address")


class PublicNetworkBackend(SyncBackend):
    """Resolve once, validate, then connect to that exact public IP."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpcore.ConnectError("hostname cannot be resolved") from exc
        public_addresses = []
        for record in records:
            address = record[4][0]
            try:
                if ipaddress.ip_address(address).is_global:
                    public_addresses.append(address)
            except ValueError:
                continue
        if not public_addresses:
            raise httpcore.ConnectError("hostname resolves only to private or reserved addresses")
        # Passing a numeric address to the parent prevents a second DNS lookup.
        return super().connect_tcp(public_addresses[0], port, timeout, local_address, socket_options)


def _public_transport() -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(trust_env=False, retries=0)
    transport._pool = httpcore.ConnectionPool(network_backend=PublicNetworkBackend())
    return transport


def _safe_get(url: str) -> str:
    current = url
    with httpx.Client(transport=_public_transport(), timeout=20, follow_redirects=False, headers={"User-Agent": "PetPriceGuard/1.0"}) as client:
        for _ in range(6):
            validate_public_url(current)
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        response.raise_for_status()
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                    raise ValueError("response body exceeds configured size limit")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ValueError("response body exceeds configured size limit")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace")
    raise ValueError("too many redirects")


def collect(channel: Channel) -> tuple[Decimal | None, str]:
    if os.getenv("PPG_COLLECTOR", "web") == "demo":
        if not channel.url.startswith("demo://"):
            raise ValueError("demo collector only accepts demo:// URLs")
        return parse_price(channel.url.removeprefix("demo://")), f"demo:{channel.name}:{channel.url}"
    if channel.url.startswith("demo://"):
        raise ValueError("demo URL is disabled")
    body = _safe_get(channel.url)
    soup = BeautifulSoup(body, "html.parser")
    candidates: list[str] = []
    if channel.selector:
        node = soup.select_one(channel.selector)
        if not node:
            raise ValueError("price selector did not match")
        candidates.append(node.get("content", "") or node.get_text(" ", strip=True))
    else:
        for node in soup.select('meta[property="product:price:amount"], meta[property="og:price:amount"]'):
            candidates.append(node.get("content", ""))
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            for item in data if isinstance(data, list) else [data]:
                offer = item.get("offers", {}) if isinstance(item, dict) else {}
                for entry in offer if isinstance(offer, list) else [offer]:
                    if isinstance(entry, dict) and entry.get("price") is not None:
                        candidates.append(str(entry["price"]))
    for candidate in candidates:
        price = parse_price(candidate)
        if price is not None:
            return price, candidate[:1000]
    raise ValueError("no structured price found; configure a CSS selector")


def _event_payload(alert: Alert, monitor: Monitor, channel: Channel, observation: Observation) -> dict[str, Any]:
    return {
        "type": "violation.opened",
        "version": "1",
        "alert_id": alert.id,
        "monitor_id": monitor.id,
        "brand": monitor.brand,
        "product": monitor.product,
        "sku": monitor.sku,
        "channel": channel.name,
        "url": channel.url,
        "observed_price": str(alert.price),
        "threshold_price": str(alert.threshold_price),
        "evidence_hash": alert.evidence_hash,
        "occurred_at": observation.captured_at.isoformat(),
    }


def _post_webhook(url: str, payload: str) -> None:
    event = json.loads(payload)
    idempotency_key = f"pet-price-guard-alert-{event['alert_id']}"
    with httpx.Client(transport=_public_transport(), timeout=5, follow_redirects=False, headers={"Content-Type": "application/json", "User-Agent": "PetPriceGuard/1.0", "Idempotency-Key": idempotency_key}) as client:
        response = client.post(url, content=payload)
        response.raise_for_status()


def deliver_pending_webhooks(now: datetime | None = None, limit: int = 100) -> dict[str, int]:
    webhook_url = os.getenv("PPG_WEBHOOK_URL")
    if not webhook_url:
        return {"delivered": 0, "failed": 0}
    validate_public_url(webhook_url)
    now = now or datetime.now(timezone.utc)
    delivered = failed = 0
    stale_before = now - timedelta(minutes=10)
    claimed: list[tuple[int, str, str]] = []
    with SessionLocal() as db:
        db.execute(update(WebhookDelivery).where(WebhookDelivery.status == "processing", WebhookDelivery.claimed_at < stale_before).values(status="retry", claim_token=None, claimed_at=None))
        candidate_ids = db.scalars(select(WebhookDelivery.id).where(WebhookDelivery.status.in_(["pending", "retry"]), WebhookDelivery.next_attempt_at <= now).order_by(WebhookDelivery.id).limit(limit)).all()
        for job_id in candidate_ids:
            token = str(uuid.uuid4())
            result = db.execute(update(WebhookDelivery).where(WebhookDelivery.id == job_id, WebhookDelivery.status.in_(["pending", "retry"]), WebhookDelivery.next_attempt_at <= now).values(status="processing", claim_token=token, claimed_at=now, attempts=WebhookDelivery.attempts + 1))
            if result.rowcount == 1:
                payload = db.scalar(select(WebhookDelivery.payload).where(WebhookDelivery.id == job_id))
                claimed.append((job_id, token, payload))
        db.commit()
    for job_id, token, payload in claimed:
        try:
            _post_webhook(webhook_url, payload)
            with SessionLocal() as db:
                job = db.scalar(select(WebhookDelivery).where(WebhookDelivery.id == job_id, WebhookDelivery.claim_token == token))
                if job:
                    job.status = "delivered"
                    job.delivered_at = now
                    job.last_error = None
                    job.claim_token = None
                    job.claimed_at = None
                    delivered += 1
                    db.commit()
        except httpx.HTTPError as exc:
            with SessionLocal() as db:
                job = db.scalar(select(WebhookDelivery).where(WebhookDelivery.id == job_id, WebhookDelivery.claim_token == token))
                if not job:
                    continue
                job.last_error = str(exc)[:1000]
                job.status = "failed" if job.attempts >= 5 else "retry"
                job.next_attempt_at = now + timedelta(minutes=2 ** min(job.attempts, 5))
                job.claim_token = None
                job.claimed_at = None
                failed += 1
                db.commit()
    return {"delivered": delivered, "failed": failed}


def run_monitor(monitor_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        monitor = db.get(Monitor, monitor_id)
        if not monitor:
            raise ValueError("monitor not found")
        if not monitor.active:
            raise ValueError("monitor is inactive")
        monitor_id_value = monitor.id
        channel_snapshots = [(channel.id, channel.name, channel.url, channel.selector) for channel in monitor.channels]

    collected = []
    errors = 0
    for channel_id, name, url, selector in channel_snapshots:
        detached_channel = Channel(id=channel_id, monitor_id=monitor_id_value, name=name, url=url, selector=selector)
        error = None
        try:
            price, raw = collect(detached_channel)
        except Exception as exc:
            price, raw, error = None, "", f"collector_error: {exc}"
            errors += 1
        collected.append((channel_id, price, raw, error))

    with SessionLocal() as db:
        monitor = db.get(Monitor, monitor_id)
        if not monitor:
            raise ValueError("monitor not found")
        if not monitor.active:
            raise ValueError("monitor is inactive")
        observations = alerts = 0
        for channel_id, price, raw, error in collected:
            channel = db.scalar(select(Channel).where(Channel.id == channel_id, Channel.monitor_id == monitor.id).with_for_update())
            if channel is None:
                continue
            observation = Observation(monitor_id=monitor.id, channel_id=channel.id, channel=channel.name, url=channel.url, price=price, raw=raw, error=error)
            db.add(observation)
            db.flush()
            observations += 1
            if price is None:
                continue
            channel.last_price = price
            if price >= monitor.floor_price:
                channel.in_breach = False
                continue
            if channel.in_breach:
                continue
            channel.in_breach = True
            evidence = hashlib.sha256(f"{channel.url}|{price}|{raw}".encode()).hexdigest()
            fingerprint = hashlib.sha256(f"{monitor.id}|{channel.id}|{observation.id}".encode()).hexdigest()
            alert = Alert(monitor_id=monitor.id, observation_id=observation.id, channel_id=channel.id, channel=channel.name, price=price, threshold_price=monitor.floor_price, fingerprint=fingerprint, evidence_hash=evidence)
            db.add(alert)
            db.flush()
            alerts += 1
            if os.getenv("PPG_WEBHOOK_URL"):
                db.add(WebhookDelivery(alert_id=alert.id, payload=json.dumps(_event_payload(alert, monitor, channel, observation), ensure_ascii=False)))
        db.commit()
    if os.getenv("PPG_WEBHOOK_URL"):
        deliver_pending_webhooks()
    return {"monitor_id": monitor_id, "observations": observations, "new_alerts": alerts, "errors": errors}
