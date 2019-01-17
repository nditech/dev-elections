"""empty message

Revision ID: a2df58677e85
Revises: 174754b75952
Create Date: 2019-01-14 13:54:12.932740

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2df58677e85'
down_revision = '174754b75952'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('location_type_translation',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('locale', sa.String(length=10), nullable=False),
    sa.Column('name', sa.Unicode(length=255), nullable=True),
    sa.ForeignKeyConstraint(['id'], ['location_type.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', 'locale')
    )
    op.create_table('location_translation',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('locale', sa.String(length=10), nullable=False),
    sa.Column('name', sa.Unicode(length=255), nullable=True),
    sa.ForeignKeyConstraint(['id'], ['location.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', 'locale')
    )
    op.drop_column('location', 'name')
    op.drop_column('location_type', 'name')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('location_type', sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=False))
    op.add_column('location', sa.Column('name', sa.VARCHAR(), autoincrement=False, nullable=False))
    op.drop_table('location_translation')
    op.drop_table('location_type_translation')
    # ### end Alembic commands ###
