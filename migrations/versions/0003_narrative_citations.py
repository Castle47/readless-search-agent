"""Optional narrative locations preserve legacy citation rows."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # Initial bootstrap uses current metadata; upgrades and fresh databases agree.
    op.execute("ALTER TABLE research.citations ADD COLUMN IF NOT EXISTS paragraph_id TEXT")
    op.execute("ALTER TABLE research.citations ADD COLUMN IF NOT EXISTS start_char INTEGER")
    op.execute("ALTER TABLE research.citations ADD COLUMN IF NOT EXISTS end_char INTEGER")


def downgrade():
    raise RuntimeError("Unsafe schema downgrade disabled")
