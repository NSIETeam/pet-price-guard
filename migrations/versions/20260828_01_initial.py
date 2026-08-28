"""Initial production schema.

Revision ID: 20260828_01
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "20260828_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("monitors", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("brand", sa.String(200), nullable=False), sa.Column("product", sa.String(300), nullable=False), sa.Column("sku", sa.String(100)), sa.Column("floor_price", sa.Numeric(12, 2), nullable=False), sa.Column("schedule", sa.String(100)), sa.Column("active", sa.Boolean(), nullable=False))
    op.create_table("channels", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("monitor_id", sa.Integer(), sa.ForeignKey("monitors.id"), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("url", sa.Text(), nullable=False), sa.Column("selector", sa.String(200)), sa.Column("in_breach", sa.Boolean(), nullable=False), sa.Column("last_price", sa.Numeric(12, 2)))
    op.create_table("observations", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("monitor_id", sa.Integer(), sa.ForeignKey("monitors.id"), nullable=False), sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False), sa.Column("channel", sa.String(100), nullable=False), sa.Column("url", sa.Text(), nullable=False), sa.Column("price", sa.Numeric(12, 2)), sa.Column("raw", sa.Text(), nullable=False), sa.Column("error", sa.Text()), sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("alerts", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("monitor_id", sa.Integer(), sa.ForeignKey("monitors.id"), nullable=False), sa.Column("observation_id", sa.Integer(), sa.ForeignKey("observations.id"), nullable=False), sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False), sa.Column("channel", sa.String(100), nullable=False), sa.Column("price", sa.Numeric(12, 2), nullable=False), sa.Column("threshold_price", sa.Numeric(12, 2), nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False, unique=True), sa.Column("evidence_hash", sa.String(64), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("acknowledged_at", sa.DateTime(timezone=True)))
    op.create_table("webhook_deliveries", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id"), nullable=False), sa.Column("payload", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_error", sa.Text()), sa.Column("delivered_at", sa.DateTime(timezone=True)), sa.Column("claimed_at", sa.DateTime(timezone=True)), sa.Column("claim_token", sa.String(36)))
    op.create_index("ix_webhook_delivery_due", "webhook_deliveries", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_delivery_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("alerts")
    op.drop_table("observations")
    op.drop_table("channels")
    op.drop_table("monitors")
