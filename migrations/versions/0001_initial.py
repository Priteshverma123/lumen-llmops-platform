"""Initial schema + immutability guard for prompt versions.

Baseline created from the reviewed model metadata; later changes are explicit migrations.
A trigger makes prompt_versions append-only at the database level: even a buggy admin script
cannot rewrite a version that production traffic was served with (auditability).

Revision ID: 0001
Revises:
Create Date: 2026-11-15
"""

from alembic import op

from lumen.storage.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_prompt_version_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'prompt_versions are immutable; create a new version instead';
        END; $$ LANGUAGE plpgsql;""")
    op.execute("""
        CREATE TRIGGER prompt_versions_immutable BEFORE UPDATE ON prompt_versions
        FOR EACH ROW EXECUTE FUNCTION forbid_prompt_version_update();""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prompt_versions_immutable ON prompt_versions")
    op.execute("DROP FUNCTION IF EXISTS forbid_prompt_version_update()")
    Base.metadata.drop_all(bind=op.get_bind())
