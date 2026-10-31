"""SQLAlchemy metadata: same-run foreign keys make citation isolation a DB invariant."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData(schema="research")
T = sa.Text
J = JSONB
U = UUID(as_uuid=False)
TS = sa.DateTime(timezone=True)


def col(name, typ=T, nullable=False, **kw):
    return sa.Column(name, typ, nullable=nullable, **kw)


def ident():
    return col("id", U, primary_key=True)


def run_fk():
    return col("run_id", U)


def timestamp(name="created_at", nullable=False):
    return col(name, TS, nullable, server_default=sa.func.now() if not nullable else None)


def run_table(name, *columns, constraints=()):
    return sa.Table(
        name,
        metadata,
        ident(),
        run_fk(),
        *columns,
        sa.UniqueConstraint("run_id", "id"),
        sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], ondelete="RESTRICT"),
        *constraints,
    )


def same_run(column, target):
    return sa.ForeignKeyConstraint(
        ["run_id", column], [f"research.{target}.run_id", f"research.{target}.id"], ondelete="RESTRICT"
    )


def check_enum(column, values):
    return sa.CheckConstraint(f"{column} IN ({','.join(repr(v) for v in values.split('|'))})")


RUN_STATUS = "queued|running|recovering|completed|partial|rejected|failed|cancelled"
OP_STATUS = "pending|running|succeeded|failed|unknown"
runs = sa.Table(
    "runs",
    metadata,
    ident(),
    col("owner_id"),
    col("request", J),
    col("config", J),
    col("request_hash"),
    col("idempotency_key"),
    col("status"),
    timestamp(),
    timestamp("started_at", True),
    timestamp("deadline_at", True),
    timestamp("finished_at", True),
    col("stop_reason", nullable=True),
    col("error", J, True),
    col("report_id", U, True),
    col("lease_owner", nullable=True),
    timestamp("lease_until", True),
    col("fence", sa.BigInteger, server_default="0"),
    col("cancel_requested", sa.Boolean, server_default="false"),
    col("phase", server_default="queued"),
    col("warnings", J, server_default=sa.text("'[]'::jsonb")),
    col("coverage", sa.Float, server_default="0"),
    col("memory_status", server_default="pending"),
    sa.UniqueConstraint("owner_id", "idempotency_key"),
    check_enum("status", RUN_STATUS),
)
plans = run_table(
    "plans",
    col("batch_no", sa.Integer),
    col("payload", J),
    timestamp(),
    constraints=[sa.UniqueConstraint("run_id", "batch_no"), sa.CheckConstraint("batch_no IN (0,1)")],
)
tasks = run_table(
    "tasks",
    col("plan_id", U),
    col("specification", J),
    col("snapshot", J),
    col("status"),
    timestamp("updated_at"),
    constraints=[same_run("plan_id", "plans")],
)
sources = run_table(
    "sources",
    col("requested_url"),
    col("canonical_url"),
    col("title"),
    col("publisher_key"),
    col("source_type"),
    col("media_type"),
    col("content_text"),
    col("content_sha256"),
    timestamp("published_at", True),
    timestamp("retrieved_at"),
    col("metadata", J),
    constraints=[
        sa.UniqueConstraint("run_id", "canonical_url", "content_sha256"),
        check_enum("source_type", "official|paper|repository|independent|discussion|unknown"),
    ],
)
claims = run_table(
    "claims",
    col("canonical_text"),
    col("normalized_text"),
    col("temporal_scope", nullable=True),
    col("temporal_key"),
    col("status"),
    col("confidence"),
    col("merged_into", U, True),
    timestamp(),
    constraints=[
        sa.UniqueConstraint("run_id", "normalized_text", "temporal_key"),
        same_run("merged_into", "claims"),
        check_enum("status", "supported|contested|unverified|superseded"),
        check_enum("confidence", "high|medium|low"),
    ],
)
evidence = run_table(
    "evidence",
    col("source_id", U),
    col("quote"),
    col("start_char", sa.Integer),
    col("end_char", sa.Integer),
    col("directness"),
    col("verified", sa.Boolean),
    timestamp(),
    constraints=[
        same_run("source_id", "sources"),
        sa.UniqueConstraint("run_id", "source_id", "start_char", "end_char"),
        sa.CheckConstraint("start_char >= 0 AND end_char > start_char"),
        check_enum("directness", "direct|indirect"),
    ],
)
claim_evidence = sa.Table(
    "claim_evidence",
    metadata,
    run_fk(),
    col("claim_id", U),
    col("evidence_id", U),
    col("relation"),
    col("verification", J),
    sa.PrimaryKeyConstraint("claim_id", "evidence_id", "relation"),
    same_run("claim_id", "claims"),
    same_run("evidence_id", "evidence"),
    check_enum("relation", "supports|contradicts"),
)
task_claims = sa.Table(
    "task_claims",
    metadata,
    run_fk(),
    col("task_id", U),
    col("criterion_id"),
    col("claim_id", U),
    sa.PrimaryKeyConstraint("task_id", "criterion_id", "claim_id"),
    same_run("task_id", "tasks"),
    same_run("claim_id", "claims"),
)
criterion_assessments = sa.Table(
    "criterion_assessments",
    metadata,
    run_fk(),
    col("criterion_id"),
    col("batch_no", sa.Integer),
    col("assessment", J),
    timestamp("updated_at"),
    sa.PrimaryKeyConstraint("run_id", "criterion_id", "batch_no"),
    sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], ondelete="RESTRICT"),
    sa.CheckConstraint("batch_no IN (0,1)"),
)
claim_conflicts = run_table(
    "claim_conflicts",
    col("claim_a_id", U),
    col("claim_b_id", U),
    col("reason"),
    col("status"),
    col("resolution", J, True),
    timestamp(),
    constraints=[
        same_run("claim_a_id", "claims"),
        same_run("claim_b_id", "claims"),
        check_enum("status", "open|resolved"),
    ],
)
reports = run_table(
    "reports",
    col("version", sa.Integer),
    col("status"),
    col("markdown"),
    col("structured", J),
    timestamp(),
    constraints=[sa.UniqueConstraint("run_id"), check_enum("status", "completed|partial")],
)
runs.append_constraint(
    sa.ForeignKeyConstraint(
        ["id", "report_id"],
        ["research.reports.run_id", "research.reports.id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
        name="fk_run_report",
    )
)
citations = run_table(
    "citations",
    col("report_id", U),
    col("statement_id"),
    col("claim_id", U),
    col("evidence_id", U),
    col("ordinal", sa.Integer),
    col("paragraph_id", nullable=True),
    col("start_char", sa.Integer, True),
    col("end_char", sa.Integer, True),
    constraints=[
        same_run("report_id", "reports"),
        same_run("claim_id", "claims"),
        same_run("evidence_id", "evidence"),
        sa.UniqueConstraint("report_id", "statement_id", "claim_id", "evidence_id"),
        sa.CheckConstraint("ordinal > 0"),
    ],
)
operations = run_table(
    "operations",
    col("operation_key"),
    col("task_id", U, True),
    col("kind"),
    col("input_hash"),
    col("status"),
    col("result", J, True),
    col("error", J, True),
    col("attempts", sa.Integer, server_default="0"),
    timestamp("updated_at"),
    constraints=[
        sa.UniqueConstraint("run_id", "operation_key"),
        same_run("task_id", "tasks"),
        check_enum("status", OP_STATUS),
        sa.CheckConstraint("attempts >= 0"),
    ],
)
operation_attempts = run_table(
    "operation_attempts",
    col("operation_id", U),
    col("attempt_no", sa.Integer),
    col("status"),
    col("reserved_tokens", sa.Integer),
    col("charged_tokens", sa.Integer),
    timestamp("started_at"),
    timestamp("ended_at", True),
    col("provider_request_id", nullable=True),
    col("error", J, True),
    col("reservation", J),
    col("charged", J),
    constraints=[
        same_run("operation_id", "operations"),
        sa.UniqueConstraint("operation_id", "attempt_no"),
        check_enum("status", "running|succeeded|failed|unknown"),
    ],
)
budgets = sa.Table(
    "budgets",
    metadata,
    col("run_id", U, primary_key=True),
    col("limits", J),
    col("reserved", J),
    col("used", J),
    timestamp("updated_at"),
    sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], ondelete="RESTRICT"),
)
events = sa.Table(
    "events",
    metadata,
    run_fk(),
    col("seq", sa.BigInteger),
    col("event_type"),
    col("payload", J),
    timestamp(),
    sa.PrimaryKeyConstraint("run_id", "seq"),
    sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], ondelete="RESTRICT"),
)
memory_outbox = run_table(
    "memory_outbox",
    col("agent_scope"),
    col("kind"),
    col("payload", J),
    col("status"),
    col("attempts", sa.Integer),
    timestamp("next_attempt_at"),
    col("remote_ids", J),
    col("error", J, True),
    timestamp(),
    constraints=[
        sa.UniqueConstraint("run_id", "kind"),
        check_enum("status", "pending|processing|applied|needs_reconciliation|dead_letter"),
    ],
)
memory_bindings = sa.Table(
    "memory_bindings",
    metadata,
    col("owner_id"),
    col("scope"),
    col("letta_agent_id"),
    col("block_ids", J),
    col("archive_id", nullable=True),
    col("revision", sa.BigInteger),
    timestamp("updated_at"),
    sa.PrimaryKeyConstraint("owner_id", "scope"),
)
sa.Index("ix_source_publisher", sources.c.run_id, sources.c.publisher_key)
sa.Index("ix_task_status", tasks.c.run_id, tasks.c.status)
sa.Index("ix_outbox_due", memory_outbox.c.status, memory_outbox.c.next_attempt_at)
sa.Index("ix_runs_queue", runs.c.status, runs.c.created_at)
