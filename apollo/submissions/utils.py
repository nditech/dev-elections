# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
from sqlalchemy import TIMESTAMP, BigInteger, String, cast, func
from sqlalchemy.dialects.postgresql import array_agg
from sqlalchemy.orm import aliased

from apollo.core import db
from apollo.locations.models import Location, LocationPath, LocationType
from apollo.submissions.models import Submission


def make_submission_dataframe(query, form, selected_tags=None, excluded_tags=None):
    """Create a pandas data frame from the given query."""
    if not db.session.query(query.exists()).scalar():
        return pd.DataFrame()

    # excluded tags have higher priority than selected tags
    fields = set(form.tags)
    if selected_tags:
        fields = fields.intersection(selected_tags)
    if excluded_tags:
        fields = fields.difference(excluded_tags)

    integral_fields = [
        tag
        for tag in fields
        if form.get_field_by_tag(tag)["type"] == "integer" or form.get_field_by_tag(tag)["type"] == "select"
    ]

    columns = [cast(func.nullif(Submission.data[tag].astext, ""), BigInteger).label(tag) for tag in integral_fields]
    other_fields = fields.difference(integral_fields)

    # the 'updated' field is required for results analysis
    columns.extend(
        [Submission.data[tag].label(tag) for tag in other_fields]
        + [
            func.coalesce(
                # casting to TIMESTAMP so as to lose the time zone
                Submission.extra_data["voting_timestamp"].astext.cast(TIMESTAMP),  # noqa
                Submission.updated,
            ).label("updated")
        ]
    )

    # alias just in case the query is already joined to the tables below
    ancestor_loc = aliased(Location, name="ancestor")
    ancestor_loc_type = aliased(LocationType, name="ancestor_type")
    ancestor_loc_path = aliased(LocationPath, name="ancestor_path")
    own_loc = aliased(Location, name="own_location")

    sub_query = (
        Submission.query.join(own_loc, Submission.location_id == own_loc.id)
        .join(ancestor_loc_path, Submission.location_id == ancestor_loc_path.descendant_id)
        .join(ancestor_loc, ancestor_loc.id == ancestor_loc_path.ancestor_id)
        .join(ancestor_loc_type, ancestor_loc.location_type_id == ancestor_loc_type.id)
        .with_entities(
            cast(ancestor_loc.name, String).label("ancestor_name"),
            cast(ancestor_loc_type.name, String).label("ancestor_type"),
            Submission.id.label("submission_id"),
        )
        .subquery()
    )

    # add registered voters and path extraction to the columns
    columns.append(own_loc.registered_voters.label("registered_voters"))
    columns.append(
        func.jsonb_object(
            array_agg(sub_query.c.ancestor_type),
            array_agg(sub_query.c.ancestor_name),
        ).label("location_data")
    )

    # type coercion is necessary for numeric columns
    # if we allow Pandas to infer the column type for these,
    # there's a risk that it will guess wrong, then it might
    # raise exceptions when we're calculating the mean and
    # standard deviation on those columns
    type_coercions = {
        tag: np.float64
        for tag in fields
        if form.get_field_by_tag(tag)["type"] == "integer" or form.get_field_by_tag(tag)["type"] == "select"
    }

    dataframe_query = (
        query.filter(Submission.location_id == own_loc.id)
        .join(sub_query, Submission.id == sub_query.c.submission_id)
        .group_by(Submission.id, own_loc.registered_voters)
        .with_entities(*columns)
    )

    df = pd.read_sql(
        dataframe_query.selectable,
        dataframe_query.session.get_bind(),
    ).astype(type_coercions)

    loc_data_df = pd.json_normalize(df["location_data"]).replace('(^"|"$)', "", regex=True)
    loc_data_df.columns = loc_data_df.columns.str.strip('"')

    df_summary = pd.concat([df.drop("location_data", axis=1), loc_data_df], axis=1).reindex(df.index)

    return df_summary


def make_turnout_dataframe(query, form):  # noqa
    if not db.session.query(query.exists()).scalar():
        return pd.DataFrame()

    fields = set(form.turnout_fields)  # noqa

    integral_fields = [
        tag
        for tag in fields
        if form.get_field_by_tag(tag)["type"] == "integer" or form.get_field_by_tag(tag)["type"] == "select"
    ]

    columns = [cast(func.nullif(Submission.data[tag].astext, ""), BigInteger).label(tag) for tag in integral_fields]
    other_fields = fields.difference(integral_fields)

    # the 'updated' field is required for results analysis
    columns.extend(
        [Submission.data[tag].label(tag) for tag in other_fields]
        + [
            func.coalesce(
                # casting to TIMESTAMP so as to lose the time zone
                Submission.extra_data["voting_timestamp"].astext.cast(TIMESTAMP),  # noqa
                Submission.updated,
            ).label("updated")
        ]
    )

    # alias just in case the query is already joined to the tables below
    ancestor_loc = aliased(Location, name="ancestor")
    ancestor_loc_type = aliased(LocationType, name="ancestor_type")
    ancestor_loc_path = aliased(LocationPath, name="ancestor_path")
    own_loc = aliased(Location, name="own_location")

    sub_query = (
        Submission.query.join(own_loc, Submission.location_id == own_loc.id)
        .join(ancestor_loc_path, Submission.location_id == ancestor_loc_path.descendant_id)
        .join(ancestor_loc, ancestor_loc.id == ancestor_loc_path.ancestor_id)
        .join(ancestor_loc_type, ancestor_loc.location_type_id == ancestor_loc_type.id)
        .with_entities(
            cast(ancestor_loc.name, String).label("ancestor_name"),
            cast(ancestor_loc_type.name, String).label("ancestor_type"),
            Submission.id.label("submission_id"),
        )
        .subquery()
    )

    # add registered voters and path extraction to the columns
    columns.append(
        own_loc.registered_voters.label("registered_voters")
        if not form.turnout_registered_voters_tag
        else Submission.data[form.turnout_registered_voters_tag].label("registered_voters")
    )
    columns.append(
        func.jsonb_object(
            array_agg(sub_query.c.ancestor_type),
            array_agg(sub_query.c.ancestor_name),
        ).label("location_data")
    )

    # type coercion is necessary for numeric columns
    # if we allow Pandas to infer the column type for these,
    # there's a risk that it will guess wrong, then it might
    # raise exceptions when we're calculating the mean and
    # standard deviation on those columns
    type_coercions = {
        tag: np.float64
        for tag in form.turnout_fields
        if form.get_field_by_tag(tag)["type"] == "integer" or form.get_field_by_tag(tag)["type"] == "select"
    }

    dataframe_query = (
        query.filter(Submission.location_id == own_loc.id)
        .join(sub_query, Submission.id == sub_query.c.submission_id)
        .group_by(Submission.id, own_loc.registered_voters)
        .with_entities(*columns)
    )

    df = pd.read_sql(
        dataframe_query.selectable,
        dataframe_query.session.get_bind(),
    ).astype(type_coercions)

    loc_data_df = pd.json_normalize(df["location_data"]).replace('(^"|"$)', "", regex=True)
    loc_data_df.columns = loc_data_df.columns.str.strip('"')

    df_summary = pd.concat([df.drop("location_data", axis=1), loc_data_df], axis=1).reindex(df.index)

    return df_summary


def valid_turnout_dataframe(dataframe: pd.DataFrame, form: object, turnout_field_label: str, rv_field_label: str):  # noqa
    turnout_field = form.get_field_by_tag(turnout_field_label)
    turnout_rv_field = form.get_field_by_tag(form.turnout_registered_voters_tag)  # noqa
    if turnout_rv_field:
        return dataframe[
            (dataframe[rv_field_label] > 0)
            & (dataframe[rv_field_label] != turnout_rv_field.get("null_value", np.nan))  # noqa
            & dataframe[turnout_field["tag"]].notna()
            & (dataframe[turnout_field["tag"]] != turnout_field.get("null_value", np.nan))  # noqa
        ]
    else:
        return dataframe[
            (dataframe[rv_field_label] > 0)
            & dataframe[turnout_field["tag"]].notna()
            & (dataframe[turnout_field["tag"]] != turnout_field.get("null_value", np.nan))  # noqa
        ]
