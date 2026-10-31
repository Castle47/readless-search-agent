from alembic import context
from sqlalchemy import create_engine
from research_agent.config import Settings
from research_agent.storage.models import metadata

url = Settings().database_url.get_secret_value().replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url)
with engine.connect() as connection:
    connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS research")
    connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS checkpoints")
    connection.commit()
    context.configure(connection=connection, target_metadata=metadata, version_table_schema="research")
    with context.begin_transaction():
        context.run_migrations()
