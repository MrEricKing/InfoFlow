"""Add homepage read model tables

Revision ID: 20260602_homepage_rm
Revises: 20260601_v3_0_0_baseline
Create Date: 2026-06-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260602_homepage_rm"
down_revision = "20260601_v3_0_0_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feed_item_homepage_read_models",
        sa.Column("feed_item_id", sa.Integer(), nullable=False),
        sa.Column("card_json", sa.JSON(), nullable=False),
        sa.Column("read_model_version", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_category_id", sa.String(length=512), nullable=False),
        sa.Column("source_category_label", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("saved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("not_interested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_window_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority_score", sa.Integer(), nullable=False),
        sa.Column("low_priority_bucket", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("flow_lane", sa.String(length=32), nullable=False),
        sa.Column("flow_lane_facet", sa.String(length=32), nullable=False),
        sa.Column("reading_depth", sa.String(length=32), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("is_stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stale_reason", sa.String(length=128), nullable=True),
        sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["feed_item_id"], ["feed_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("feed_item_id"),
    )
    op.create_index(
        "ix_homepage_rm_flow_lane",
        "feed_item_homepage_read_models",
        ["flow_lane_facet"],
        unique=False,
    )
    op.create_index(
        "ix_homepage_rm_newest",
        "feed_item_homepage_read_models",
        [sa.text("published_at DESC"), sa.text("feed_item_id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_homepage_rm_priority",
        "feed_item_homepage_read_models",
        ["low_priority_bucket", sa.text("priority_score DESC"), sa.text("published_at DESC"), sa.text("feed_item_id DESC")],
        unique=False,
    )
    op.create_index("ix_homepage_rm_reading_depth", "feed_item_homepage_read_models", ["reading_depth"], unique=False)
    op.create_index("ix_homepage_rm_source_category_id", "feed_item_homepage_read_models", ["source_category_id"], unique=False)
    op.create_index(
        "ix_homepage_rm_source_category_state",
        "feed_item_homepage_read_models",
        ["source_category_id", "state"],
        unique=False,
    )
    op.create_index("ix_homepage_rm_source_id", "feed_item_homepage_read_models", ["source_id"], unique=False)
    op.create_index(
        "ix_homepage_rm_source_sort",
        "feed_item_homepage_read_models",
        ["source_name", sa.text("published_at DESC"), sa.text("feed_item_id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_homepage_rm_source_state",
        "feed_item_homepage_read_models",
        ["source_id", "state"],
        unique=False,
    )
    op.create_index("ix_homepage_rm_stale", "feed_item_homepage_read_models", ["is_stale", "read_model_version"], unique=False)
    op.create_index("ix_homepage_rm_state", "feed_item_homepage_read_models", ["state"], unique=False)
    op.create_index("ix_homepage_rm_saved", "feed_item_homepage_read_models", ["saved"], unique=False)
    op.create_index("ix_homepage_rm_not_interested", "feed_item_homepage_read_models", ["not_interested"], unique=False)
    op.create_index("ix_homepage_rm_published_at", "feed_item_homepage_read_models", ["published_at"], unique=False)
    op.create_index("ix_homepage_rm_time_window_at", "feed_item_homepage_read_models", ["time_window_at"], unique=False)
    op.create_index(
        "ix_homepage_rm_read_model_version",
        "feed_item_homepage_read_models",
        ["read_model_version"],
        unique=False,
    )

    op.create_table(
        "feed_item_homepage_facets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_item_id", sa.Integer(), nullable=False),
        sa.Column("facet_kind", sa.String(length=32), nullable=False),
        sa.Column("facet_id", sa.String(length=512), nullable=False),
        sa.Column("facet_label", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(["feed_item_id"], ["feed_item_homepage_read_models.feed_item_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_item_id", "facet_kind", "facet_id", name="uq_homepage_facet_item_kind_id"),
    )
    op.create_index("ix_feed_item_homepage_facets_feed_item_id", "feed_item_homepage_facets", ["feed_item_id"], unique=False)
    op.create_index("ix_feed_item_homepage_facets_facet_kind", "feed_item_homepage_facets", ["facet_kind"], unique=False)
    op.create_index("ix_homepage_facets_item_kind", "feed_item_homepage_facets", ["feed_item_id", "facet_kind"], unique=False)
    op.create_index("ix_homepage_facets_kind_id", "feed_item_homepage_facets", ["facet_kind", "facet_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_homepage_facets_kind_id", table_name="feed_item_homepage_facets")
    op.drop_index("ix_homepage_facets_item_kind", table_name="feed_item_homepage_facets")
    op.drop_index("ix_feed_item_homepage_facets_facet_kind", table_name="feed_item_homepage_facets")
    op.drop_index("ix_feed_item_homepage_facets_feed_item_id", table_name="feed_item_homepage_facets")
    op.drop_table("feed_item_homepage_facets")

    op.drop_index("ix_homepage_rm_read_model_version", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_time_window_at", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_published_at", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_not_interested", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_saved", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_state", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_stale", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_source_state", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_source_sort", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_source_id", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_source_category_state", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_source_category_id", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_reading_depth", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_priority", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_newest", table_name="feed_item_homepage_read_models")
    op.drop_index("ix_homepage_rm_flow_lane", table_name="feed_item_homepage_read_models")
    op.drop_table("feed_item_homepage_read_models")
