"""empty message

Revision ID: 001635503b12
Revises: 14d29a283f2b
Create Date: 2018-03-19 12:45:57.636210

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001635503b12'
down_revision = '14d29a283f2b'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('participant_set', sa.Column('location_set_id', sa.Integer(), nullable=False))
    op.create_foreign_key(None, 'participant_set', 'location_set', ['location_set_id'], ['id'])
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, 'participant_set', type_='foreignkey')
    op.drop_column('participant_set', 'location_set_id')
    # ### end Alembic commands ###
