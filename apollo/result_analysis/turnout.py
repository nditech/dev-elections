import math
from operator import attrgetter

import numpy as np
from flask import abort, current_app, g, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy import not_

from apollo import models
from apollo.formsframework.models import Form
from apollo.frontend.helpers import analysis_breadcrumb_data, analysis_navigation_data
from apollo.services import forms, location_types, locations, submissions
from apollo.submissions.filters import make_submission_analysis_filter
from apollo.submissions.models import FLAG_STATUSES
from apollo.submissions.utils import make_turnout_dataframe, valid_turnout_dataframe


def _point_estimate(dataframe, numerator, denominator):
    if not isinstance(numerator, list):
        numerator = [numerator]
    m = dataframe.loc[:, denominator].sum(axis=1)
    mbar = m.sum(axis=0) / m.size
    return dataframe.loc[:, numerator].sum(axis=0).sum(axis=0) / (mbar * m.size)  # noqa


def _variance(dataframe, numerator, denominator):
    p = _point_estimate(dataframe, numerator, denominator)
    psquared = p * p

    m = dataframe.loc[:, denominator].sum(axis=1)
    msquared = m * m

    if isinstance(numerator, list):
        a = dataframe.loc[:, numerator].sum(axis=1)
    else:
        a = dataframe.loc[:, [numerator]].sum(axis=1)
    asquared = a * a

    mbar = m.sum(axis=0) / m.size
    mbarsquared = mbar * mbar

    f = m.sum(axis=0) / current_app.config.get("BIG_N")
    k = m.size
    am = a * m
    sigma_asquared = asquared.sum(axis=0)
    sigma_msquared = msquared.sum(axis=0)
    sigma_am = am.sum(axis=0)

    return ((1 - f) / (k * mbarsquared)) * (
        (sigma_asquared - (2 * p * sigma_am) + (psquared * sigma_msquared)) / (k - 1)
    )


def _margin_of_error(dataframe, numerator, denominator, cv=1.96):
    """Computes the margin of error."""
    moe = round(math.sqrt(abs(_variance(dataframe, numerator, denominator))) * cv * 100, 3)
    if np.isnan(moe) or np.isinf(moe):
        moe = 0
    return moe


def turnout_convergence(form_id, location_id=None):
    """Turnout Convergence View."""
    event = g.event
    form = forms.filter(
        Form.turnout_fields != [], Form.id == form_id, Form.form_type == "CHECKLIST", Form.events.contains(event)
    ).first()

    if form is None:
        abort(404)

    if location_id is None:
        location = locations.root(event.location_set_id)
    else:
        location = locations.fget_or_404(id=location_id)

    location_ids = models.LocationPath.query.with_entities(models.LocationPath.descendant_id).filter_by(
        ancestor_id=location.id, location_set_id=event.location_set_id
    )

    template_name = "result_analysis/turnout.html"
    breadcrumbs = [_("Partial Turnout Data"), form.name]
    filter_on_locations = not form.untrack_data_conflicts
    filter_class = make_submission_analysis_filter(event, form, filter_on_locations)

    loc_types = [lt for lt in location_types.root(event.location_set_id).descendants() if lt.is_political is True]

    location_tree = {}
    for lt in loc_types:
        lt_locs = models.Location.query.filter_by(location_type_id=lt.id)
        location_tree[lt.name] = sorted(lt_locs, key=attrgetter("name"))

    # define the condition for which a submission should be included
    turnout_fields = [form.get_field_by_tag(tag) for tag in form.turnout_fields] if form.turnout_fields else []
    turnout_field_labels = form.turnout_fields
    turnout_field_descriptions = [field["description"] for field in turnout_fields]

    query_kwargs = {"event": event, "form": form}
    if not form.untrack_data_conflicts:
        query_kwargs["submission_type"] = "M"
    else:
        query_kwargs["submission_type"] = "O"

    queryset = submissions.find(**query_kwargs).filter(
        models.Submission.verification_status != FLAG_STATUSES["rejected"][0],
        not_(models.Submission.quarantine_status.in_(["A", "R"])),
        models.Submission.location_id.in_(location_ids),
    )
    filter_set = filter_class(queryset, request.args)
    dataset = make_turnout_dataframe(filter_set.qs, form)

    for turnout_field in turnout_fields:
        null_value_orig = turnout_field.get("null_value")
        if null_value_orig is None:
            continue
        try:
            null_value = int(null_value_orig)
        except (TypeError, ValueError):
            continue

        dataset[turnout_field["tag"]] = dataset[turnout_field["tag"]].replace(null_value, np.nan)

    summary = {}

    for turnout_field_label in turnout_field_labels:
        valid_dataset = valid_turnout_dataframe(dataset, form, turnout_field_label, "registered_voters").drop(
            ["updated"], axis=1
        )

        turnouts_count = valid_dataset.shape[0]
        turnouts_reported = valid_dataset.notna().sum()
        turnouts_missing = valid_dataset.isna().sum()
        turnouts_sum = valid_dataset.sum()

        summary[turnout_field_label] = {
            "total_registered": turnouts_sum["registered_voters"],
            "reported_cnt": turnouts_reported[turnout_field_label],
            "reported_pct": turnouts_reported[turnout_field_label] / turnouts_count,  # noqa
            "missing_cnt": turnouts_missing[turnout_field_label],
            "missing_pct": turnouts_missing[turnout_field_label] / turnouts_count,  # noqa
            "turnout_cnt": turnouts_sum[turnout_field_label],
            "turnout_moe_95": _margin_of_error(valid_dataset, [turnout_field_label], ["registered_voters"], 1.96),  # noqa
            "turnout_moe_99": _margin_of_error(valid_dataset, [turnout_field_label], ["registered_voters"], 2.58),  # noqa
        }
        try:
            summary[turnout_field_label]["turnout_pct"] = (
                turnouts_sum[turnout_field_label] / turnouts_sum["registered_voters"]
            )  # noqa
        except ZeroDivisionError:
            summary[turnout_field_label]["turnout_pct"] = 0

    breadcrumb_data = analysis_breadcrumb_data(form, location, analysis_type="results")
    breadcrumbs.extend(
        [
            {
                "text": location.name,
                "url": url_for(
                    "result_analysis.turnout_with_location", form_id=breadcrumb_data["form"].id, location_id=location.id
                )
                if idx < len(breadcrumb_data.get("locations", [])) - 1
                else "",
            }
            for idx, location in enumerate(breadcrumb_data.get("locations", []))
        ]
    )

    context = {
        "breadcrumbs": breadcrumbs,
        "filter_form": filter_set.form,
        "location": location,
        "dataframe": dataset,
        "form": form,
        "turnout_labels": turnout_field_labels,
        "turnout_fields": turnout_fields,
        "turnout_descriptions": turnout_field_descriptions,
        "location_types": loc_types,
        "location_tree": location_tree,
        "navigation_data": analysis_navigation_data(form, location),
        "summary": summary,
    }

    return render_template(template_name, **context)
