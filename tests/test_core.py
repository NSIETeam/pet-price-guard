from decimal import Decimal
from datetime import datetime, timezone

import httpx
import pytest

from app import core


def create_demo_monitor(database, price="80", floor="100"):
    with database() as db:
        monitor = core.Monitor(brand="PawCare", product="Food", floor_price=Decimal(floor))
        monitor.channels = [core.Channel(name="demo", url=f"demo://{price}")]
        db.add(monitor)
        db.commit()
        return monitor.id, monitor.channels[0].id


def test_schema_check_fails_closed_without_alembic_version(database):
    with pytest.raises(RuntimeError, match="not migrated"):
        core.verify_schema()


def test_parse_price_only_accepts_explicit_money():
    assert core.parse_price("销量 1234") is None
    assert core.parse_price("促销价 ￥1,299.90") == Decimal("1299.90")
    assert core.parse_price("88.5 元") == Decimal("88.50")
    assert core.parse_price("79.00") == Decimal("79.00")


def test_alert_opens_once_per_continuous_breach(database):
    monitor_id, _ = create_demo_monitor(database)
    assert core.run_monitor(monitor_id)["new_alerts"] == 1
    assert core.run_monitor(monitor_id)["new_alerts"] == 0
    with database() as db:
        assert db.query(core.Alert).count() == 1


def test_alert_reopens_after_recovery(database):
    monitor_id, channel_id = create_demo_monitor(database)
    assert core.run_monitor(monitor_id)["new_alerts"] == 1
    with database() as db:
        channel = db.get(core.Channel, channel_id)
        channel.url = "demo://120"
        db.commit()
    assert core.run_monitor(monitor_id)["new_alerts"] == 0
    with database() as db:
        channel = db.get(core.Channel, channel_id)
        channel.url = "demo://70"
        db.commit()
    assert core.run_monitor(monitor_id)["new_alerts"] == 1
    with database() as db:
        assert db.query(core.Alert).count() == 2


def test_collection_error_is_persisted(database):
    monitor_id, channel_id = create_demo_monitor(database)
    with database() as db:
        channel = db.get(core.Channel, channel_id)
        channel.url = "https://example.invalid/item"
        db.commit()
    result = core.run_monitor(monitor_id)
    assert result["errors"] == 1
    with database() as db:
        observation = db.query(core.Observation).one()
        assert observation.price is None
        assert observation.error.startswith("collector_error:")


def test_webhook_failure_is_persisted_for_retry(database, monkeypatch):
    with database() as db:
        delivery = core.WebhookDelivery(alert_id=1, payload='{"type":"violation.opened"}', next_attempt_at=datetime.now(timezone.utc))
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
    monkeypatch.setenv("PPG_WEBHOOK_URL", "https://hooks.example.com/events")
    monkeypatch.setattr(core, "validate_public_url", lambda url: None)

    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(core, "_post_webhook", fail)
    result = core.deliver_pending_webhooks()
    assert result == {"delivered": 0, "failed": 1}
    with database() as db:
        delivery = db.get(core.WebhookDelivery, delivery_id)
        assert delivery.status == "retry"
        assert delivery.attempts == 1
        assert "offline" in delivery.last_error


def test_webhook_success_is_recorded(database, monkeypatch):
    with database() as db:
        delivery = core.WebhookDelivery(alert_id=1, payload='{"type":"violation.opened"}', next_attempt_at=datetime.now(timezone.utc))
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
    monkeypatch.setenv("PPG_WEBHOOK_URL", "https://hooks.example.com/events")
    monkeypatch.setattr(core, "validate_public_url", lambda url: None)
    monkeypatch.setattr(core, "_post_webhook", lambda *args, **kwargs: None)
    result = core.deliver_pending_webhooks()
    assert result == {"delivered": 1, "failed": 0}
    with database() as db:
        delivery = db.get(core.WebhookDelivery, delivery_id)
        assert delivery.status == "delivered"
        assert delivery.delivered_at is not None


def test_processing_webhook_is_not_claimed_twice(database, monkeypatch):
    now = datetime.now(timezone.utc)
    with database() as db:
        db.add(core.WebhookDelivery(alert_id=1, payload='{"alert_id":1}', status="processing", claim_token="worker-one", claimed_at=now, next_attempt_at=now))
        db.commit()
    monkeypatch.setenv("PPG_WEBHOOK_URL", "https://hooks.example.com/events")
    monkeypatch.setattr(core, "validate_public_url", lambda url: None)
    called = []
    monkeypatch.setattr(core, "_post_webhook", lambda *args: called.append(args))
    assert core.deliver_pending_webhooks(now=now) == {"delivered": 0, "failed": 0}
    assert called == []
