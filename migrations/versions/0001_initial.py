"""Spec 1.1 relational model."""

from alembic import op
from research_agent.storage.models import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Evidence retention requires an explicit maintenance procedure; automatic downgrade disabled"
    )
