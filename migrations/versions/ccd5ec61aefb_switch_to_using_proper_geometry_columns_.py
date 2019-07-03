"""switch to using proper geometry columns for gis data

Revision ID: ccd5ec61aefb
Revises: d19820fc3341
Create Date: 2019-07-03 02:15:17.189283

"""
from alembic import op
import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'ccd5ec61aefb'
down_revision = 'd19820fc3341'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute('CREATE EXTENSION IF NOT EXISTS postgis')

    op.add_column('location', sa.Column('geom', geoalchemy2.types.Geometry(geometry_type='POINT', srid=4326), nullable=True))
    op.execute('UPDATE location SET geom = ST_SETSRID(ST_MAKEPOINT(location.lon, location.lat), 4326)')
    op.drop_column('location', 'lat')
    op.drop_column('location', 'lon')

    op.add_column('submission', sa.Column('geom', geoalchemy2.types.Geometry(geometry_type='POINT', srid=4326), nullable=True))
    op.execute("UPDATE submission SET geom = ST_SETSRID(ST_MAKEPOINT((submission.geopoint->>'lon')::numeric, (submission.geopoint->>'lat')::numeric), 4326) WHERE submission.geopoint->>'lon' IS NOT NULL AND submission.geopoint->>'lat' IS NOT NULL")
    op.drop_column('submission', 'geopoint')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('submission', sa.Column('geopoint', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True))
    op.execute("UPDATE submission SET geopoint = json_build_object('lon', ST_X(submission.geom), 'lat', ST_Y(submission.geom))")
    op.drop_column('submission', 'geom')

    op.add_column('location', sa.Column('lon', postgresql.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True))
    op.add_column('location', sa.Column('lat', postgresql.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True))
    op.execute('UPDATE location SET lon = ST_X(location.geom), lat = ST_Y(location.geom)')
    op.drop_column('location', 'geom')

    op.execute('DROP EXTENSION IF EXISTS postgis')
    # ### end Alembic commands ###
