"""Enforce run-to-report scope even for direct SQL writes."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("fk_run_report", "runs", schema="research", type_="foreignkey")
    op.create_foreign_key(
        "fk_run_report",
        "runs",
        "reports",
        ["id", "report_id"],
        ["run_id", "id"],
        source_schema="research",
        referent_schema="research",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade():
    raise RuntimeError("Unsafe schema downgrade disabled")
