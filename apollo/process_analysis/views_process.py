# -*- coding: utf-8 -*-
from collections import OrderedDict
from functools import partial
from operator import itemgetter
from string import Template

from flask import Blueprint, g, render_template, request, url_for
from flask_babel import lazy_gettext as _
from flask_menu import register_menu
from flask_security import login_required
from sqlalchemy import and_, or_

from apollo import models
from apollo.formsframework.models import FIELD_SUMMARY_TYPES
from apollo.frontend import permissions, route
from apollo.frontend.helpers import analysis_breadcrumb_data, analysis_navigation_data
from apollo.process_analysis.common import generate_incidents_data, generate_process_data
from apollo.services import forms, location_types, locations, submissions
from apollo.submissions import filters
from apollo.submissions.utils import make_submission_dataframe


def filter_option_value(tag: str, op: str, value: str) -> str:
    """Template fragment generator for filter options."""
    s = Template("$tag$op$value")
    return s.substitute(tag=tag, op=op, value=value)


def get_analysis_menu():
    """Menu generator for process summary."""
    event = g.event
    subquery = or_(
        *[
            models.Form.data["groups"].op("@>")([{"fields": [{"analysis_type": s_type}]}])
            for s_type in FIELD_SUMMARY_TYPES[1:]
        ]
    )
    return [
        {"url": url_for("process_analysis.process_analysis", form_id=form.id), "text": form.name}
        for form in forms.filter(
            models.Form.is_hidden == False,  # noqa
            models.Form.events.contains(event),
            or_(
                models.Form.form_type == "INCIDENT", and_(models.Form.form_type.in_(["CHECKLIST", "SURVEY"]), subquery)
            ),
        ).order_by(models.Form.form_type, models.Form.name)
    ]


def get_process_analysis_menu(form_type="CHECKLIST"):
    """Menu generator for process summary."""
    event = g.event
    if form_type == "CHECKLIST":
        subquery = or_(
            *[
                models.Form.data["groups"].op("@>")([{"fields": [{"analysis_type": s_type}]}])
                for s_type in FIELD_SUMMARY_TYPES[1:]
            ]
        )
        formlist = forms.filter(
            models.Form.is_hidden == False,  # noqa
            models.Form.events.contains(event),
            models.Form.form_type.in_(["CHECKLIST", "SURVEY"]),
            subquery,
        ).order_by(models.Form.form_type, models.Form.name)
    else:
        formlist = forms.filter(
            models.Form.events.contains(event),
            models.Form.form_type == "INCIDENT",
        ).order_by(models.Form.form_type, models.Form.name)

    return [
        {"url": url_for("process_analysis.process_analysis", form_id=form.id), "text": form.name} for form in formlist
    ]


bp = Blueprint(
    "process_analysis", __name__, template_folder="templates", static_folder="static", static_url_path="/core/static"
)


def _process_analysis(event, form_id, location_id=None, tag=None):
    form = forms.fget_or_404(id=form_id, is_hidden=False)
    location = locations.fget_or_404(id=location_id) if location_id else locations.root(event.location_set_id)

    template_name = ""
    tags = []
    breadcrumbs = [_("Process Data"), form.name]
    grouped = False
    display_tag = None
    event = g.event
    filter_on_locations = False

    location_ids = models.LocationPath.query.with_entities(models.LocationPath.descendant_id).filter_by(
        ancestor_id=location.id, location_set_id=event.location_set_id
    )

    # set the correct template and fill out the required data
    if form.form_type in ["CHECKLIST", "SURVEY"]:
        if tag:
            template_name = "process_analysis/checklist_summary_breakdown.html"
            tags.append(tag)
            display_tag = tag
            grouped = True
        else:
            template_name = "process_analysis/checklist_summary.html"
            form._populate_field_cache()
            tags.extend([f["tag"] for f in form._field_cache.values() if f["analysis_type"] != "N/A"])
            grouped = False

        query_kwargs = {"event": event, "form": form}
        if not form.untrack_data_conflicts and form.form_type == "CHECKLIST":
            query_kwargs["submission_type"] = "M"
            filter_on_locations = True
        else:
            query_kwargs["submission_type"] = "O"
        queryset = submissions.find(**query_kwargs).filter(
            models.Submission.location_id.in_(location_ids), models.Submission.quarantine_status != "A"
        )
    else:
        grouped = True
        queryset = submissions.find(event=event, form=form).filter(models.Submission.location_id.in_(location_ids))
        template_name = "process_analysis/critical_incident_summary.html"

        if tag:
            # a slightly different filter, one prefiltering
            # on the specified tag
            display_tag = tag
            template_name = "process_analysis/critical_incidents_locations.html"  # noqa
            filter_class = filters.make_incident_location_filter(event, form, tag)

    # create data filter
    filter_class = filters.make_submission_analysis_filter(event, form, filter_on_locations)
    filter_set = filter_class(queryset, request.args)
    breadcrumb_data = analysis_breadcrumb_data(form, location, display_tag)

    if breadcrumb_data["tag"]:
        breadcrumbs.extend(
            [
                {
                    "text": location.name,
                    "url": url_for(
                        "process_analysis.process_analysis_with_location_and_tag",
                        form_id=breadcrumb_data["form"].id,
                        location_id=location.id,
                        tag=breadcrumb_data["tag"],
                    )
                    if idx < len(breadcrumb_data.get("locations", [])) - 1
                    else "",
                }
                for idx, location in enumerate(breadcrumb_data.get("locations", []))
            ]
        )
    else:
        breadcrumbs.extend(
            [
                {
                    "text": location.name,
                    "url": url_for(
                        "process_analysis.process_analysis_with_location",
                        form_id=breadcrumb_data["form"].id,
                        location_id=location.id,
                    )
                    if idx < len(breadcrumb_data.get("locations", [])) - 1
                    else "",
                }
                for idx, location in enumerate(breadcrumb_data.get("locations", []))
            ]
        )

    # set up template context
    context = {}
    submission_dataframe = make_submission_dataframe(filter_set.qs, form)
    context["dataframe"] = submission_dataframe
    context["breadcrumbs"] = breadcrumbs
    context["display_tag"] = display_tag
    context["filter_form"] = filter_set.form
    context["form"] = form
    context["location"] = location
    context["field_groups"] = OrderedDict()
    context["navigation_data"] = analysis_navigation_data(form, location, display_tag)
    context["filter_option_value"] = filter_option_value

    # processing for incident forms
    if form.form_type == "INCIDENT":
        if display_tag:
            context["form_field"] = form.get_field_by_tag(display_tag)
            context["location_types"] = location_types.find(
                is_political=True, location_set_id=event.location_set_id
            )
            context["incidents"] = filter_set.qs
        else:
            incidents_summary = generate_incidents_data(submission_dataframe, form, location, grouped, tags)
            context["incidents_summary"] = incidents_summary

        detail_visible = False
        for group in form.data["groups"]:
            process_fields = sorted(
                [field for field in group["fields"] if field["analysis_type"] != "N/A" and field["type"] != "boolean"],
                key=itemgetter("tag"),
            )
            context["field_groups"][group["name"]] = process_fields
            if process_fields:
                detail_visible = True

        context["detail_visible"] = detail_visible
    else:
        for group in form.data["groups"]:
            process_fields = sorted(
                [field for field in group["fields"] if field["analysis_type"] != "N/A"], key=itemgetter("tag")
            )
            context["field_groups"][group["name"]] = process_fields

        process_summary = generate_process_data(submission_dataframe, form, location, grouped=True, tags=tags)
        context["process_summary"] = process_summary

    return render_template(template_name, **context)


@route(bp, "/process_summary/<int:form_id>")
@register_menu(
    bp,
    "main.analyses",
    _("Data Summary"),
    order=4,
    visible_when=lambda: len(get_analysis_menu()) > 0 and permissions.view_process_analysis.can(),
)
@register_menu(
    bp,
    "main.analyses.incidents_analysis",
    _("Incidents Data"),
    dynamic_list_constructor=partial(get_process_analysis_menu, "INCIDENT"),
    visible_when=lambda: len(get_process_analysis_menu("INCIDENT")) > 0 and permissions.view_process_analysis.can(),
)
@register_menu(
    bp,
    "main.analyses.process_analysis",
    _("Process Data"),
    dynamic_list_constructor=partial(get_process_analysis_menu, "CHECKLIST"),
    visible_when=lambda: len(get_process_analysis_menu("CHECKLIST")) > 0 and permissions.view_process_analysis.can(),
)
@login_required
@permissions.view_process_analysis.require(403)
def process_analysis(form_id):
    """Process summary view."""
    event = g.event
    return _process_analysis(event, form_id)


@route(bp, "/process_summary/<int:form_id>/<int:location_id>")
@login_required
@permissions.view_process_analysis.require(403)
def process_analysis_with_location(form_id, location_id):
    """Process summary with location view."""
    event = g.event
    return _process_analysis(event, form_id, location_id)


@route(bp, "/process_summary/<int:form_id>/<int:location_id>/<tag>")
@login_required
@permissions.view_process_analysis.require(403)
def process_analysis_with_location_and_tag(form_id, location_id, tag):
    """Process summary with location and tag view."""
    event = g.event
    return _process_analysis(event, form_id, location_id, tag)
