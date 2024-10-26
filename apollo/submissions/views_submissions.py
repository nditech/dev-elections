# -*- coding: utf-8 -*-
import codecs
from datetime import datetime
from functools import partial
from http import HTTPStatus
from uuid import uuid4

import geojson
import sqlalchemy as sa
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_babel import get_locale
from flask_babel import lazy_gettext as _
from flask_httpauth import HTTPBasicAuth
from flask_menu import register_menu
from flask_security import current_user, login_required
from flask_security.utils import verify_and_update_password
from geoalchemy2.shape import to_shape
from slugify import slugify
from sqlalchemy import BigInteger, case, desc, func, nullslast
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import joinedload
from sqlalchemy.sql import false
from tablib import Dataset
from werkzeug.datastructures import MultiDict

from apollo import models, services
from apollo import utils as autils
from apollo.core import db, docs
from apollo.dal import utils
from apollo.frontend import permissions, route
from apollo.frontend.helpers import (
    DictDiffer,
    displayable_location_types,
    get_event,
    get_form_list_menu,
    get_quality_assurance_form_dashboard_menu,
    get_quality_assurance_form_list_menu,
)
from apollo.frontend.template_filters import mkunixtimestamp
from apollo.messaging.tasks import send_messages
from apollo.submissions import filters, forms
from apollo.submissions.aggregation import _qa_counts, aggregate_dataset, aggregated_dataframe
from apollo.submissions.api import views as api_views
from apollo.submissions.incidents import incidents_csv
from apollo.submissions.models import QUALITY_STATUSES, Submission
from apollo.submissions.qa.query_builder import generate_qa_queries
from apollo.submissions.utils import make_submission_dataframe

auth = HTTPBasicAuth()
bp = Blueprint("submissions", __name__, template_folder="templates", static_folder="static")

bp.add_url_rule(
    "/api/submissions", view_func=api_views.SubmissionListResource.as_view("api_submission_list"), methods=["GET"]
)
bp.add_url_rule("/api/submissions", view_func=api_views.submission, methods=["POST", "PUT"])
bp.add_url_rule("/api/qastatus/<uuid>", view_func=api_views.checklist_qa_status, methods=["GET"])
bp.add_url_rule(
    "/api/submissions/<int:submission_id>",
    view_func=api_views.SubmissionItemResource.as_view("api_submission_item"),
    methods=["GET"],
)
bp.add_url_rule(
    "/api/submissions/image_manifest",
    view_func=api_views.get_image_manifest,
    methods=["GET", "POST"],
)

docs.register(api_views.SubmissionItemResource, "submissions.api_submission_item")
docs.register(api_views.SubmissionListResource, "submissions.api_submission_list")

SUB_VERIFIED = "4"


def get_valid_values(choices):
    """Returns values for the choices."""
    return [i[0] for i in choices]


@auth.verify_password
def verify_pw(username, password):
    """Verify the password matches and update if required."""
    deployment = g.deployment
    user = services.users.find(deployment=deployment, email=username).first()

    if not user:
        return False

    return verify_and_update_password(password, user)


@route(bp, "/submissions/<int:form_id>", methods=["GET", "POST"])
@register_menu(
    bp,
    "main.checklists",
    _("Checklists"),
    order=1,
    visible_when=lambda: len(get_form_list_menu(form_type="CHECKLIST")) > 0,
)
@register_menu(
    bp,
    "main.checklists.forms",
    _("Checklists"),
    dynamic_list_constructor=partial(get_form_list_menu, form_type="CHECKLIST"),
)
@register_menu(
    bp,
    "main.incidents",
    _("Critical Incidents"),
    order=2,
    visible_when=lambda: len(get_form_list_menu(form_type="INCIDENT")) > 0,
)
@register_menu(
    bp,
    "main.incidents.forms",
    _("Critical Incidents"),
    dynamic_list_constructor=partial(get_form_list_menu, form_type="INCIDENT"),
)
@register_menu(
    bp, "main.surveys", _("Surveys"), order=3, visible_when=lambda: len(get_form_list_menu(form_type="SURVEY")) > 0
)
@register_menu(
    bp, "main.surveys.forms", _("Surveys"), dynamic_list_constructor=partial(get_form_list_menu, form_type="SURVEY")
)
@login_required
def submission_list(form_id):
    """Returns the submissions for a particular form."""
    event = g.event
    form = (
        models.Form.query.filter_by(
            id=form_id,
            is_hidden=False,
        )
        .join(models.Form.events)
        .filter(models.Form.events.contains(event))
        .first_or_404()
    )
    permissions.can_access_resource(form)

    filter_class = filters.make_submission_list_filter(event, form)
    template_name = "frontend/submission_list.html"

    data = request.args.to_dict(flat=False)
    data["form_id"] = str(form.id)
    page = int(data.pop("page", [1])[0])
    loc_types = displayable_location_types(is_administrative=True, location_set_id=event.location_set_id)
    query = models.Submission.query.options(joinedload(models.Submission.form))
    _location_query = None

    user_locale = get_locale().language
    deployment_locale = g.deployment.primary_locale or "en"

    full_name_term = func.coalesce(
        models.Participant.full_name_translations.op("->>")(user_locale),
        models.Participant.full_name_translations.op("->>")(deployment_locale),
    ).label("full_name")
    first_name_term = func.coalesce(
        models.Participant.first_name_translations.op("->>")(user_locale),
        models.Participant.first_name_translations.op("->>")(deployment_locale),
    ).label("first_name")
    other_names_term = func.coalesce(
        models.Participant.other_names_translations.op("->>")(user_locale),
        models.Participant.other_names_translations.op("->>")(deployment_locale),
    ).label("other_names")
    last_name_term = func.coalesce(
        models.Participant.last_name_translations.op("->>")(user_locale),
        models.Participant.last_name_translations.op("->>")(deployment_locale),
    ).label("last_name")

    # if the user is a field-coordinator (i.e. there's a participant in the
    # session) then define the _location_query (which will be reused) and the
    # query to be used for the exports. Naturally, exports will not be
    # available to the field-coordinator but just in case the admin gives the
    # field-coordinator export access (by mistake)

    if "participant" in session:
        participant = models.Participant.query.get(session["participant"])

        if participant:
            _location_query = (
                models.Location.query.with_entities(models.Location.id)
                .join(models.LocationPath, models.Location.id == models.LocationPath.descendant_id)
                .filter(models.LocationPath.ancestor_id == participant.location_id)
            )

            query = query.filter(models.Submission.location_id.in_(_location_query))

    location = None
    if request.args.get("location"):
        location = services.locations.find(
            location_set_id=event.location_set_id, id=request.args.get("location")
        ).first()

    if request.args.get("export") and permissions.export_submissions.can():
        query = filter_class(query, request.args).qs
        include_group_timestamps = False

        mode = request.args.get("export")
        if mode in ["master", "aggregated"]:
            submission_type = "M"

            # only change the submission type if we are aggregating data
            # and are not tracking conflicts
            if mode == "aggregated" and form.untrack_data_conflicts == True:  # noqa
                submission_type = "O"

            queryset = query.filter(
                models.Submission.submission_type == submission_type,
                models.Submission.form == form,
                models.Submission.event == event,
            )
            if utils.has_model(queryset, models.Location):
                queryset = queryset.order_by(models.Location.code)
            else:
                queryset = queryset.join(models.Submission.location).order_by(models.Location.code)
        else:
            include_group_timestamps = True if mode == "observer-ts" else False
            queryset = query.filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event == event,
            )
            if not utils.has_model(queryset, models.Location):
                queryset = queryset.join(models.Submission.location)
            queryset = queryset.join(
                models.Participant,
                models.Submission.participant_id == models.Participant.id,
            ).order_by(models.Location.code, models.Participant.participant_id)

        # TODO: fix this. no exports yet. nor aggregation
        # query_filterset = filter_class(queryset, request.args)
        basename = slugify(
            "%s %s %s %s"
            % (g.event.name.lower(), form.name.lower(), datetime.utcnow().strftime("%Y %m %d %H%M%S"), mode)
        )
        content_disposition = "attachment; filename=%s.csv" % basename
        if mode == "aggregated":
            # TODO: you want to change the float format or even remove it
            # if you have columns that have float values
            dataset = aggregate_dataset(queryset.order_by(None), form, True)
        else:
            dataset = services.submissions.export_list(queryset, include_group_timestamps=include_group_timestamps)

        return Response(
            stream_with_context(dataset), headers={"Content-Disposition": content_disposition}, mimetype="text/csv"
        )

    # the following section defines the queryset for the submissions
    # to be retrieved. due to the fact that we do specialized sorting
    # the queryset will depend heavily on what is being sorted.
    if request.args.get("sort_by") == "location" and request.args.get("sort_value"):
        # when sorting based on location, we generally want to be able
        # to sort submissions based on a specific administrative division.
        # since we store the hierarchical structure in a separate table
        # we not only have to retrieve the table for the location data but
        # also join it based on results in the location hierarchy.

        # start out by getting all the locations (as a subquery) in a
        # particular division.
        division = (
            models.Location.query.with_entities(models.Location.id)
            .filter(models.Location.location_type_id == request.args.get("sort_value"))
            .subquery()
        )
        # next is we retrieve all the descendant locations for all the
        # locations in that particular administrative division making sure
        # to retrieve the name translations which would be used in sorting
        # the submissions when the time comes.
        descendants = (
            models.LocationPath.query.join(models.Location, models.Location.id == models.LocationPath.ancestor_id)
            .with_entities(models.Location.name_translations, models.LocationPath.descendant_id)
            .filter(models.LocationPath.ancestor_id.in_(division))
            .subquery()
        )

        # now we defined the actual queryset using the subqueries above
        # taking note to group by the translation name which essentially
        # is the division name.
        queryset = (
            models.Submission.query.select_from(
                models.Submission,
                models.Location,
                models.Participant,
            )
            .filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event_id == event.id,
            )
            .join(models.Location, models.Submission.location_id == models.Location.id)
            .join(models.Participant, models.Submission.participant_id == models.Participant.id)
            .outerjoin(descendants, descendants.c.descendant_id == models.Submission.location_id)
        )
    elif request.args.get("sort_by") == "phone":
        queryset = (
            models.Submission.query.filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event_id == event.id,
            )
            .join(models.Location, models.Submission.location_id == models.Location.id)
            .join(models.Participant, models.Submission.participant_id == models.Participant.id)
            .join(models.PhoneContact, models.PhoneContact.participant_id == models.Participant.id)
        )
    else:
        queryset = (
            models.Submission.query.select_from(models.Submission, models.Location, models.Participant)
            .filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event_id == event.id,
            )
            .join(models.Location, models.Submission.location_id == models.Location.id)
            .join(models.Participant, models.Submission.participant_id == models.Participant.id)
        )

    if _location_query:
        queryset = queryset.filter(models.Submission.location_id.in_(_location_query))

    if request.args.get("sort_by") == "pid":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(
                desc(models.Participant.participant_id.cast(BigInteger)),
                models.Submission.serial_no.cast(BigInteger),
                models.Location.code.cast(BigInteger),
            )
        else:
            queryset = queryset.order_by(
                models.Participant.participant_id.cast(BigInteger),
                models.Submission.serial_no.cast(BigInteger),
                models.Location.code.cast(BigInteger),
            )
    elif request.args.get("sort_by") == "fsn":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(models.Submission.serial_no.cast(BigInteger)))
        else:
            queryset = queryset.order_by(models.Submission.serial_no.cast(BigInteger))
    elif request.args.get("sort_by") == "id":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(models.Submission.id.cast(BigInteger)))
        else:
            queryset = queryset.order_by(models.Submission.id.cast(BigInteger))
    elif request.args.get("sort_by") == "location":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(
                desc(descendants.c.name_translations.op("->>")(user_locale)),
                desc(descendants.c.name_translations.op("->>")(deployment_locale)),
            )
        else:
            queryset = queryset.order_by(
                descendants.c.name_translations.op("->>")(user_locale),
                descendants.c.name_translations.op("->>")(deployment_locale),
            )
    elif request.args.get("sort_by") == "participant":
        # specify the conditions for the order term
        condition1 = full_name_term == None  # noqa
        condition2 = full_name_term != None  # noqa

        # concatenation for the full name
        full_name_concat = func.concat_ws(
            " ",
            first_name_term,
            other_names_term,
            last_name_term,
        )

        # if the full name is empty, order by the concatenated
        # name, else order by the full name
        order_term = case((condition1, full_name_concat), (condition2, full_name_term))
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(order_term))
        else:
            queryset = queryset.order_by(order_term)
    elif request.args.get("sort_by") == "phone":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(models.PhoneContact.number))
        else:
            queryset = queryset.order_by(models.PhoneContact.number)
    elif request.args.get("sort_by") == "moment":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(nullslast(desc(models.Submission.participant_updated)))
        else:
            queryset = queryset.order_by(models.Submission.participant_updated)
    else:
        queryset = queryset.order_by(
            models.Participant.participant_id.cast(BigInteger),
            models.Submission.serial_no.cast(BigInteger),
            models.Location.code.cast(BigInteger),
        )

    query_filterset = filter_class(queryset, request.args)
    filter_form = query_filterset.form

    if request.args.get("geojson"):
        features = []
        for s in query_filterset.qs.filter(models.Submission.geom != None):  # noqa
            features.append(
                geojson.Feature(
                    geometry=geojson.Point((to_shape(s.geom).x, to_shape(s.geom).y)),
                    properties={
                        "location": s.location.name,
                        "participant": s.participant.name,
                        "participant_id": s.participant.participant_id,
                        "phone": s.participant.primary_phone,
                        "last_updated": s.participant_updated.strftime("%b %d, %Y %l:%M %p")
                        if s.participant_updated
                        else None,  # noqa
                        "last_updated_timestamp": s.participant_updated.strftime("%s")
                        if s.participant_updated
                        else None,  # noqa
                    },
                )
            )
        feature_collection = geojson.FeatureCollection(features)

        return Response(geojson.dumps(feature_collection), mimetype="application/geo+json")

    # TODO: rewrite this. verify what select_related does
    if request.form.get("action") == "send_message":
        message = request.form.get("message", "")
        recipients = [
            x
            for x in [
                submission.participant.primary_phone
                if (submission.participant and submission.participant.primary_phone)
                else ""
                for submission in query_filterset.qs
            ]
            if x != ""
        ]
        recipients.extend(current_app.config.get("MESSAGING_CC"))

        if message and recipients and permissions.send_messages.can():
            send_messages.delay(event.id, message, recipients)
            return "OK"
        else:
            abort(400)

    if form.form_type == "CHECKLIST":
        form_fields = []
        breadcrumbs = [_("Checklists"), form.name]
    elif form.form_type == "SURVEY":
        form_fields = []
        breadcrumbs = [_("Surveys"), form.name]
    else:
        # show only the first question in the incident form
        tags = form.unsorted_tags
        form_fields = [form.get_field_by_tag(tags[0])] if tags else []
        breadcrumbs = [_("Critical Incidents"), form.name]

    return render_template(
        template_name,
        args=data,
        filter_form=filter_form,
        form=form,
        form_fields=form_fields,
        location_types=loc_types,
        location=location,
        breadcrumbs=breadcrumbs,
        pager=query_filterset.qs.paginate(page=page, per_page=current_app.config.get("PAGE_SIZE")),
        submissions=query_filterset.qs,
    )


@route(bp, "/submissions/<int:form_id>/new", methods=["GET", "POST"])
@login_required
@permissions.add_submission.require(403)
def submission_create(form_id):
    """Submission creation view."""
    event = g.event
    questionnaire_form = (
        models.Form.query.filter_by(id=form_id, form_type="INCIDENT")
        .join(models.Form.events)
        .filter(models.Form.events.contains(event))
        .first_or_404()
    )
    edit_form_class = forms.make_submission_edit_form_class(event, questionnaire_form)
    breadcrumbs = [_("Add Incident")]
    template_name = "frontend/incident_add.html"

    if request.method == "GET":
        submission_form = edit_form_class()
        return render_template(
            template_name, breadcrumbs=breadcrumbs, form=questionnaire_form, submission_form=submission_form
        )
    else:
        submission_form = edit_form_class(request.form)

        # a small hack since we're not using modelforms,
        # these fields are required for creating a new incident

        if not submission_form.validate():
            # really should redisplay the form again
            return render_template(
                template_name, breadcrumbs=breadcrumbs, form=questionnaire_form, submission_form=submission_form
            )

        data_fields = set(submission_form.data.keys()).intersection(questionnaire_form.tags)

        attachments = []
        data = {k: submission_form.data.get(k) for k in data_fields if submission_form.data.get(k) is not None}

        submission = models.Submission(
            event_id=event.id,
            deployment_id=event.deployment_id,
            form=questionnaire_form,
            submission_type="O",
            created=autils.current_timestamp(),
            updated=autils.current_timestamp(),
            participant_updated=autils.current_timestamp(),
            participant=submission_form.participant.data,
            location=submission_form.location.data or submission_form.participant.data.location,  # noqa
            incident_description=submission_form.description.data,
            incident_status=submission_form.status.data,
        )

        image_data = {}

        for form_field in data_fields:
            questionnaire_field = questionnaire_form.get_field_by_tag(form_field)
            if questionnaire_field["type"] == "image":
                web_form_field = getattr(submission_form, form_field)
                file_wrapper = request.files.get(web_form_field.name)

                if file_wrapper is None:
                    continue

                identifier = uuid4().hex
                if file_wrapper.mimetype.startswith("image/"):
                    if file_wrapper.filename != "":
                        image_data[form_field] = identifier
                        attachments.append(
                            models.SubmissionImageAttachment(
                                photo=file_wrapper,
                                submission=submission,
                                uuid=identifier,
                            )
                        )

        data.update(image_data)
        submission.update_group_timestamps(data)
        submission.data = data
        db.session.add_all(attachments)
        db.session.add(submission)

        db.session.commit()

        return redirect(url_for("submissions.submission_list", form_id=form_id))


@route(bp, "/submission/<int:submission_id>", methods=["GET", "POST"])
@login_required
@permissions.edit_submission.require(403)
def submission_edit(submission_id):
    """Submission editing view."""
    event = g.event
    submission = services.submissions.find(event_id=event.id, id=submission_id).first_or_404()
    questionnaire_form = submission.form
    edit_form_class = forms.make_submission_edit_form_class(event, submission.form)
    if questionnaire_form.form_type == "INCIDENT":
        breadcrumbs = [_("Edit Incident")]
    elif questionnaire_form.form_type == "SURVEY":
        breadcrumbs = [_("Edit Survey")]
    else:
        breadcrumbs = [_("Edit Checklist")]
    readonly = not g.deployment.allow_observer_submission_edit
    location_types = models.LocationType.query.filter(
        models.LocationType.location_set_id == event.location_set_id,  # noqa
        models.LocationType.is_administrative == True,  # noqa
    )  # noqa
    template_name = "frontend/submission_edit.html"
    comments = services.submission_comments.find(submission=submission)
    if questionnaire_form.form_type in ["CHECKLIST", "SURVEY"]:
        OutboundMsg = sa.orm.aliased(models.Message, name="outbound")

        messages_qs = models.Message.query.filter(models.Message.participant == submission.participant)
        split_messages = messages_qs.filter(
            sa.or_(
                models.Message.direction == "IN",
                sa.and_(
                    models.Message.originating_message_id == None,  # noqa
                    models.Message.direction == "OUT",
                ),
            )
        ).outerjoin(
            OutboundMsg,
            sa.and_(
                OutboundMsg.originating_message_id == models.Message.id,
                OutboundMsg.direction == "OUT",
            ),
        )

        messages = split_messages.with_entities(models.Message, OutboundMsg).order_by(models.Message.received.desc())

        incident_forms = models.Form.query.filter(
            models.Form.events.contains(submission.event), models.Form.form_type == "INCIDENT"
        )  # noqa
        incidents = models.Submission.query.filter(
            models.Submission.event == submission.event,  # noqa
            models.Submission.location == submission.location,  # noqa
            models.Submission.participant == submission.participant,  # noqa
            models.Submission.form_id.in_([f.id for f in incident_forms]),
        ).order_by(desc(models.Submission.created))
        changelog = models.SubmissionVersion.query.filter(
            models.SubmissionVersion.submission == submission, models.SubmissionVersion.channel == "WEB"
        ).order_by(desc(models.SubmissionVersion.timestamp))
        call_log = models.ContactHistory.query.filter(
            models.ContactHistory.participant == submission.participant
        ).order_by(models.ContactHistory.created.desc())
    else:
        messages = []
        incidents = []
        changelog = []
        call_log = []

    sibling_submissions = submission.siblings
    master_submission = submission.master

    if request.method == "GET":
        initial_data = submission.data.copy() if submission.data else {}
        initial_data.update(location=submission.location_id)
        initial_data.update(participant=submission.participant_id)
        initial_data.update(unreachable=submission.unreachable)
        if submission.quarantine_status:
            initial_data.update(quarantine_status=submission.quarantine_status.code)
        failed_checks = []
        failed_check_tags = set()

        if questionnaire_form.form_type == "INCIDENT":
            initial_data.update(description=submission.incident_description)
            if submission.incident_status:
                initial_data.update(status=submission.incident_status.code)
        else:
            if questionnaire_form.quality_checks_enabled and questionnaire_form.quality_checks:
                # use the QA query on this submission for the results
                # of the individual checks.
                # the joins are necessary to limit the number of results
                sub_query = (
                    models.Submission.query.filter_by(id=submission.id)
                    .join(models.Submission.location)
                    .join(models.Submission.participant)
                )
                qa_queries, tag_groups = generate_qa_queries(submission.form)
                result = sub_query.with_entities(*qa_queries).one()._asdict()

                # for checks that failed, add the description to the list
                # of failed check descriptions
                # for checks that failed or were verified, add the question
                # tags to the list of failed question tags
                for idx, check in enumerate(questionnaire_form.quality_checks):
                    if result[check["name"]] == "Flagged":
                        failed_checks.append(check["description"])
                    if result[check["name"]] in ("Flagged", "Verified"):
                        failed_check_tags.update(tag_groups[idx])

        submission_form = edit_form_class(data=initial_data, prefix=str(submission.id))
        sibling_forms = []
        for sibling in sibling_submissions:
            initial_data = sibling.data
            initial_data.update(unreachable=sibling.unreachable)
            if sibling.quarantine_status:
                initial_data.update(quarantine_status=sibling.quarantine_status.code)

            sibling_forms.append(edit_form_class(data=initial_data, prefix=str(sibling.id)))

        if master_submission:
            initial_data = master_submission.data
            if master_submission.quarantine_status:
                initial_data.update(quarantine_status=master_submission.quarantine_status.code)

            master_form = edit_form_class(data=initial_data, prefix=str(master_submission.id))
        else:
            master_form = None

        return render_template(
            template_name,
            breadcrumbs=breadcrumbs,
            submission=submission,
            submission_form=submission_form,
            sibling_forms=sibling_forms,
            master_form=master_form,
            readonly=readonly,
            location_types=location_types,
            comments=comments,
            failed_checks=failed_checks,
            failed_check_tags=failed_check_tags,
            messages=messages,
            incidents=incidents,
            changelog=changelog,
            call_log=call_log,
        )
    else:
        if questionnaire_form.form_type == "INCIDENT":
            # no master or sibling submission here
            submission_form = edit_form_class(request.form, prefix=str(submission.id))

            if submission_form.validate():
                form_fields = submission_form.data.keys()
                data_fields = set(form_fields).intersection(questionnaire_form.tags)
                data = submission.data.copy()
                update_params = {}
                changed = False
                attachments = []
                deleted_attachments = []

                for form_field in data_fields:
                    field_value = submission_form.data.get(form_field)

                    questionnaire_field = questionnaire_form.get_field_by_tag(form_field)

                    if questionnaire_field["type"] == "image":
                        web_form_field = getattr(submission_form, form_field)
                        file_wrapper = request.files.get(web_form_field.name)
                        identifier = uuid4()
                        original_field_data = submission.data.get(form_field)
                        if file_wrapper is None:
                            continue

                        if file_wrapper.mimetype.startswith("image/"):
                            changed = True
                            if original_field_data is not None:
                                original_attachment = models.SubmissionImageAttachment.query.filter_by(  # noqa
                                    uuid=original_field_data
                                ).first()

                                if original_attachment:
                                    deleted_attachments.append(original_attachment)

                            if file_wrapper.filename != "":
                                data[form_field] = identifier.hex
                                attachments.append(
                                    models.SubmissionImageAttachment(  # noqa
                                        photo=file_wrapper, submission=submission, uuid=identifier
                                    )
                                )
                    elif questionnaire_field["type"] in ("comment", "string"):
                        if field_value == "":
                            if data.get(form_field) is not None:
                                data.pop(form_field, None)
                                changed = True
                        else:
                            if data.get(form_field) != submission_form.data.get(form_field):
                                data[form_field] = field_value
                                changed = True
                    elif submission.data.get(form_field) != field_value:
                        if field_value is None and questionnaire_field["type"] != "image":
                            data.pop(form_field, None)
                        else:
                            data[form_field] = field_value
                        changed = True

                new_participant = submission_form.participant.data
                new_location = submission_form.location.data
                new_incident_description = submission_form.description.data
                new_incident_status = submission_form.status.data

                if new_incident_description != submission.incident_description:
                    changed = True
                    update_params["incident_description"] = new_incident_description
                if new_incident_status != submission.incident_status:
                    changed = True
                    update_params["incident_status"] = new_incident_status
                if new_location != submission.location:
                    changed = True
                    update_params["location_id"] = new_location.id
                if new_participant != submission.participant:
                    if new_participant:
                        changed = True
                        update_params["participant_id"] = new_participant.id

                submission.update_group_timestamps(data)
                update_params["data"] = data
                update_params["extra_data"] = submission.extra_data.copy()

                if changed:
                    db.session.add_all(attachments)

                    for attachment in deleted_attachments:
                        db.session.delete(attachment)

                    services.submissions.find(id=submission_id).update(update_params, synchronize_session=False)
                    db.session.commit()
                    update_submission_version(submission)

                if request.form.get("next"):
                    return redirect(
                        autils.safe_next_url(
                            request.form.get("next"),
                            url_for("submissions.submission_list", form_id=str(submission.form.id)),
                        )
                    )
                else:
                    return redirect(url_for("submissions.submission_list", form_id=str(submission.form.id)))
            else:
                return render_template(
                    template_name,
                    breadcrumbs=breadcrumbs,
                    submission=submission,
                    submission_form=submission_form,
                    location_types=location_types,
                )
        else:
            if master_submission:
                master_form = edit_form_class(request.form, prefix=str(master_submission.id))
            else:
                master_form = None

            submission_form = edit_form_class(request.form, obj=submission, prefix=str(submission.id))

            if sibling_submissions:
                sibling_forms = [
                    edit_form_class(obj=sibling, prefix=str(sibling.id)) for sibling in sibling_submissions
                ]
            else:
                sibling_forms = []

            no_error = True

            selection = request.form.get("submission_selection", None)
            if not selection and readonly:
                selection = "ps"
            elif not selection and not readonly:
                selection = "obs"

            # if the user is allowed to edit participant submissions,
            # everything has to be valid at one go. no partial update
            if master_form and selection == "ps":
                submission_form = edit_form_class(data=submission.data, prefix=str(submission.id))
                sibling_forms = [
                    edit_form_class(data=sibling.data, prefix=str(sibling.id)) for sibling in sibling_submissions
                ]

                if master_form.validate():
                    form_fields = master_form.data.keys()
                    data_fields = set(form_fields).intersection(questionnaire_form.tags)
                    changed = False
                    data = master_submission.data.copy()
                    update_params = {}
                    overridden_fields = (
                        master_submission.overridden_fields[:] if master_submission.overridden_fields else []
                    )
                    overridden_fields = set(overridden_fields)

                    new_verification_status = master_form.data.get("verification_status")
                    new_quarantine_status = master_form.data.get("quarantine_status")

                    if new_quarantine_status in get_valid_values(Submission.QUARANTINE_STATUSES):
                        if master_submission.quarantine_status != new_quarantine_status:
                            changed = True
                        update_params["quarantine_status"] = new_quarantine_status
                    if new_verification_status in get_valid_values(Submission.VERIFICATION_STATUSES):
                        if master_submission.verification_status != new_verification_status:
                            changed = True
                        update_params["verification_status"] = new_verification_status

                    for form_field in data_fields:
                        questionnaire_field = questionnaire_form.get_field_by_tag(form_field)
                        if (
                            master_form.data.get(form_field) == ""
                            and questionnaire_field["type"] in ("comment", "string")
                            and data.get(form_field) is not None
                        ):
                            data.pop(form_field, None)
                            changed = True
                            continue

                        if data.get(form_field, None) != master_form.data.get(form_field):
                            if master_form.data.get(form_field) is None:
                                data.pop(form_field, None)
                            else:
                                if master_form.data.get(form_field) is not None:
                                    data[form_field] = master_form.data.get(form_field)

                            if form_field not in ["quarantine_status", "verification_status"] and master_form.data.get(
                                form_field
                            ):
                                overridden_fields.add(form_field)
                            else:
                                # if the value was manually removed, then reset
                                # the overridden status for that field
                                try:
                                    overridden_fields.remove(form_field)
                                except KeyError:
                                    pass
                            changed = True
                    if changed:
                        update_params["data"] = data
                        if overridden_fields:
                            update_params["overridden_fields"] = array(overridden_fields)  # remove duplicates
                        else:
                            update_params["overridden_fields"] = []

                        extra_data = master_submission.extra_data or {}
                        now = autils.current_timestamp().isoformat()

                        # set the 'voting_timestamp' extra data attribute to
                        # the current timestamp only if a voting share was
                        # updated and it has not been previously set
                        if (
                            any(vs in data.keys() for vs in master_submission.form.vote_shares)  # noqa
                            and not (master_submission.extra_data or {}).get("voting_timestamp")  # noqa
                        ):
                            extra_data["voting_timestamp"] = now

                        # set the 'turnout_timestamps' attribute for any of the
                        # turnout fields if they have been reported for the
                        # first time
                        turnout_fields = set(data.keys()).intersection(set(master_submission.form.turnout_fields or []))  # noqa
                        if turnout_fields:
                            if not extra_data.get("turnout_timestamps"):
                                extra_data["turnout_timestamps"] = {}

                            for turnout_field in turnout_fields:
                                if not extra_data["turnout_timestamps"].get(turnout_field):  # noqa
                                    extra_data["turnout_timestamps"][turnout_field] = now  # noqa

                        if extra_data:
                            update_params["extra_data"] = extra_data

                        services.submissions.find(id=master_submission.id).update(
                            update_params, synchronize_session=False
                        )
                        db.session.commit()

                else:
                    no_error = False

            if selection == "obs":
                master_form = edit_form_class(data=master_submission.data, prefix=str(master_submission.id))
                sibling_forms = [
                    edit_form_class(data=sibling.data, prefix=str(sibling.id)) for sibling in sibling_submissions
                ]

                if submission_form.validate():
                    changed = False
                    data = submission.data.copy()
                    update_params = {}
                    form_fields = set(submission_form.data.keys()).intersection(questionnaire_form.tags)
                    attachments = []
                    deleted_attachments = []

                    new_verification_status = submission_form.data.get("verification_status")
                    new_quarantine_status = submission_form.data.get("quarantine_status")
                    new_offline_status = submission_form.unreachable.data

                    if permissions.edit_submission_verification_status.can():
                        new_verified_fields = submission_form.verified_fields.data
                        if new_verified_fields != submission.verified_fields:
                            changed = True
                            update_params["verified_fields"] = new_verified_fields

                    if permissions.edit_submission_quarantine_status.can():
                        if new_quarantine_status in get_valid_values(Submission.QUARANTINE_STATUSES):
                            if submission.quarantine_status != new_quarantine_status:
                                changed = True
                            update_params["quarantine_status"] = new_quarantine_status
                    if new_verification_status in get_valid_values(Submission.VERIFICATION_STATUSES):
                        if submission.verification_status != new_verification_status:
                            changed = True
                        update_params["verification_status"] = new_verification_status
                    if new_offline_status != submission.unreachable:
                        changed = True
                        update_params["unreachable"] = new_offline_status

                    changed_fields = []

                    for form_field in form_fields:
                        questionnaire_field = questionnaire_form.get_field_by_tag(form_field)

                        # process attachments
                        if questionnaire_field["type"] == "image":
                            web_form_field = getattr(submission_form, form_field)  # noqa
                            file_wrapper = request.files.get(web_form_field.name)  # noqa
                            identifier = uuid4()
                            original_field_data = submission.data.get(form_field)
                            if file_wrapper is None:
                                continue

                            # only process image uploads
                            if file_wrapper.mimetype.startswith("image/"):
                                changed = True
                                changed_fields.append(form_field)

                                # mark any existing data for deletion
                                if original_field_data is not None:
                                    original_attachment = models.SubmissionImageAttachment.query.filter_by(  # noqa
                                        uuid=original_field_data
                                    ).first()
                                    if original_attachment:
                                        deleted_attachments.append(original_attachment)

                                if file_wrapper.filename != "":
                                    data[form_field] = identifier.hex
                                    attachments.append(
                                        models.SubmissionImageAttachment(
                                            photo=file_wrapper, submission=submission, uuid=identifier
                                        )
                                    )

                        if data.get(form_field) != submission_form.data.get(form_field):
                            if (submission_form.data.get(form_field) is None) and questionnaire_field[
                                "type"
                            ] != "image":
                                data.pop(form_field, None)
                                changed_fields.append(form_field)
                            elif (submission_form.data.get(form_field) == "") and (
                                questionnaire_field["type"] in ("comment", "string")
                            ):
                                if data.get(form_field) is not None:
                                    data.pop(form_field)
                                    changed_fields.append(form_field)
                            else:
                                if submission_form.data.get(form_field) is not None:
                                    data[form_field] = submission_form.data.get(form_field)
                                    changed_fields.append(form_field)
                            changed = True
                    if changed:
                        update_params["data"] = data
                        submission.update_group_timestamps(data)
                        extra_data = submission.extra_data or {}
                        now = autils.current_timestamp().isoformat()

                        # set the 'voting_timestamp' extra data attribute to
                        # the current timestamp only if a voting share was
                        # updated and it has not been previously set
                        if (
                            any(vs in data.keys() for vs in submission.form.vote_shares)  # noqa
                            and not (submission.extra_data or {}).get("voting_timestamp")  # noqa
                        ):
                            extra_data["voting_timestamp"] = now

                        # set the 'turnout_timestamps' attribute for any of the
                        # turnout fields if they have been reported for the
                        # first time
                        turnout_fields = set(data.keys()).intersection(set(submission.form.turnout_fields or []))  # noqa
                        if turnout_fields:
                            if not extra_data.get("turnout_timestamps"):
                                extra_data["turnout_timestamps"] = {}

                            for turnout_field in turnout_fields:
                                if not extra_data["turnout_timestamps"].get(turnout_field):  # noqa
                                    extra_data["turnout_timestamps"][turnout_field] = now  # noqa

                        if extra_data:
                            update_params["extra_data"] = extra_data

                        services.submissions.find(id=submission.id).update(update_params)

                        submission.update_related(data)

                        submission.update_master_offline_status()

                        db.session.add_all(attachments)
                        for attachment in deleted_attachments:
                            db.session.delete(attachment)

                        db.session.commit()
                        update_submission_version(submission)
                else:
                    no_error = False

            if no_error:
                if request.form.get("next"):
                    return redirect(
                        autils.safe_next_url(
                            request.form.get("next"),
                            url_for("submissions.submission_list", form_id=str(questionnaire_form.id)),
                        )
                    )
                else:
                    return redirect(url_for("submissions.submission_list", form_id=str(questionnaire_form.id)))
            else:
                return render_template(
                    template_name,
                    breadcrumbs=breadcrumbs,
                    submission=submission,
                    submission_form=submission_form,
                    master_form=master_form,
                    sibling_forms=sibling_forms,
                    readonly=readonly,
                    location_types=location_types,
                    comments=comments,
                    messages=messages,
                    incidents=incidents,
                    changelog=changelog,
                    call_log=call_log,
                )


@route(bp, "/comments", methods=["POST"])
@login_required
@permissions.edit_submission.require(403)
def comment_create_view():
    """Comments creation view."""
    submission = services.submissions.fget_or_404(id=request.form.get("submission"))
    comment = request.form.get("comment")

    # TODO: fix this hack
    saved_comment = services.submission_comments.new(
        submission=submission,
        user=current_user._get_current_object(),
        comment=comment,
        submit_date=autils.current_timestamp(),
        deployment_id=submission.deployment_id,
    )

    saved_comment.save()

    return jsonify(
        comment=saved_comment.comment, date=mkunixtimestamp(saved_comment.submit_date), user=saved_comment.user.email
    )


def _incident_csv(form_id, location_type_id, location_id=None):
    """Generates a CSV file of critical incidents.

    Given an incident form id, a location type id, and optionally
    a location id, return a CSV file of the number of incidents of each
    type (form field tag) that has occurred, either for the entire
    deployment or under the given location for each location of the
    specified location type. Only submissions sent in by participants
    are used for generating the data.

    Sample output would be:

    LOC | A | B | ... | Z | TOT
    NY  | 2 | 0 | ... | 5 |  7

    `param form_id`: a `class`Form id
    `param location_type_id`: a `class`LocationType id
    `param location_id`: an optional `class`Location id. if given, only
    submissions under that location will be queried.

    `returns`: a string of bytes (str) containing the CSV data.
    """
    event = get_event()
    form = services.forms.fget_or_404(id=form_id, form_type="INCIDENT")
    location_type = services.location_types.objects.fget_or_404(id=location_type_id)

    submission_query = services.submissions.find(submission_type="O", form=form, event=event)

    if location_id:
        location_query = (
            models.Location.query.with_entities(models.Location.id)
            .join(models.LocationPath, models.Location.id == models.LocationPath.descendant_id)
            .filter(models.LocationPath.ancestor_id == location_id)
        )

        submission_query = submission_query.filter(models.Submission.location_id.in_(location_query))

    tags = form.tags
    submission_query = submission_query.filter(
        models.Submission.created <= event.end, models.Submission.created >= event.start
    )

    df = make_submission_dataframe(submission_query, form)
    ds = Dataset()
    ds.headers = ["LOC"] + tags + ["TOT"]

    for summary in incidents_csv(df, location_type.name, tags):
        ds.append([summary.get(heading) for heading in ds.headers])

    return codecs.BOM_UTF8.decode(encoding="utf-8") + ds.csv


@route(bp, "/incidents/<form_id>/<location_type_id>/incidents.csv")
@login_required
def incidents_csv_dl(form_id, location_type_id):
    """Download view for critical incidents."""
    response = make_response(_incident_csv(form_id, location_type_id))
    response.headers["Content-Type"] = "text/csv"

    return response


@route(bp, "/incidents/<form_id>/<location_type_id>" "/<location_id>/incidents.csv")
@login_required
def incidents_csv_with_location_dl(form_id, location_type_id, location_id):
    """Download view for critical incidents with a location."""
    response = make_response(_incident_csv(form_id, location_type_id, location_id))
    response.headers["Content-Type"] = "text/csv"

    return response


@route(bp, "/submissions/<submission_id>/version/<version_id>")
@login_required
def submission_version(submission_id, version_id):
    """Submission versioning view."""
    event = g.event
    submission = services.submissions.fget_or_404(id=submission_id, event_id=event.id)
    version = services.submission_versions.fget_or_404(id=version_id, submission_id=submission_id)
    form = submission.form
    form_data = MultiDict(version.data)
    page_title = _("View submission")
    template_name = "frontend/submission_history.html"

    diff = DictDiffer(submission.data, form_data)

    return render_template(
        template_name,
        page_title=page_title,
        diff=diff,
        form=form,
        submission=submission,
        submission_version=version,
        data=form_data,
    )


@route(bp, "/dashboard/qa/<form_id>")
@register_menu(
    bp,
    "main.dashboard.qa",
    _("Quality Assurance"),
    order=1,
    visible_when=lambda: len(get_quality_assurance_form_dashboard_menu(["CHECKLIST", "SURVEY"])) > 0
    and permissions.view_quality_assurance.can(),
    dynamic_list_constructor=partial(get_quality_assurance_form_dashboard_menu, form_types=["CHECKLIST", "SURVEY"]),
)
@login_required
@permissions.view_quality_assurance.require(403)
def quality_assurance_dashboard(form_id):
    """QA dashboard view."""
    form = services.forms.get_or_404(
        models.Form.id == form_id,
        models.Form.is_hidden == False,  # noqa
        models.Form.form_type.in_(["CHECKLIST", "SURVEY"]),
    )
    breadcrumbs = [_("Quality Assurance Dashboard"), form.name]
    filter_class = filters.generate_quality_assurance_filter(g.event, form)
    data = request.args.to_dict()
    data["form_id"] = str(form.id)
    loc_types = displayable_location_types(is_administrative=True, location_set_id=g.event.location_set_id)

    chart_type = data.pop("chart", None)
    if chart_type:
        session["dashboard_chart_type"] = chart_type if chart_type in ["pie", "bar"] else "pie"

    location = None
    if request.args.get("location"):
        location = services.locations.find(id=request.args.get("location")).first()

    submissions = models.Submission.query.filter_by(event=g.event, form=form, submission_type="O")
    query_filterset = filter_class(submissions, request.args)
    filter_form = query_filterset.form

    # get individual check data
    check_data = _qa_counts(query_filterset.qs, form)

    template_name = "frontend/quality_assurance_dashboard.html"

    context = {
        "filter_form": filter_form,
        "form": form,
        "args": data,
        "location_types": loc_types,
        "location": location,
        "breadcrumbs": breadcrumbs,
        "data": check_data,
    }

    return render_template(template_name, **context)


@route(bp, "/submissions/qa/<form_id>", methods=["GET", "POST"])
@register_menu(
    bp,
    "main.qa",
    _("Quality Assurance"),
    order=3,
    visible_when=lambda: len(get_quality_assurance_form_list_menu(["CHECKLIST", "SURVEY"])) > 0
    and permissions.view_quality_assurance.can(),
)
@register_menu(
    bp,
    "main.qa.checklists",
    _("Quality Assurance"),
    order=1,
    dynamic_list_constructor=partial(get_quality_assurance_form_list_menu, form_types=["CHECKLIST", "SURVEY"]),
)
@login_required
@permissions.view_quality_assurance.require(403)
def quality_assurance_list(form_id):
    """QA list view."""
    event = g.event
    form = services.forms.get_or_404(
        models.Form.id == form_id,
        models.Form.is_hidden == False,  # noqa
        models.Form.form_type.in_(["CHECKLIST", "SURVEY"]),
    )
    breadcrumbs = [_("Quality Assurance"), form.name]
    filter_class = filters.generate_quality_assurance_filter(event, form)

    data = request.args.to_dict(flat=False)
    data["form_id"] = str(form.id)
    page = int(data.pop("page", [1])[0])
    loc_types = displayable_location_types(is_administrative=True, location_set_id=g.event.location_set_id)

    user_locale = get_locale().language
    deployment_locale = g.deployment.primary_locale or "en"

    full_name_term = func.coalesce(
        models.Participant.full_name_translations.op("->>")(user_locale),
        models.Participant.full_name_translations.op("->>")(deployment_locale),
    ).label("full_name")
    first_name_term = func.coalesce(
        models.Participant.first_name_translations.op("->>")(user_locale),
        models.Participant.first_name_translations.op("->>")(deployment_locale),
    ).label("first_name")
    other_names_term = func.coalesce(
        models.Participant.other_names_translations.op("->>")(user_locale),
        models.Participant.other_names_translations.op("->>")(deployment_locale),
    ).label("other_names")
    last_name_term = func.coalesce(
        models.Participant.last_name_translations.op("->>")(user_locale),
        models.Participant.last_name_translations.op("->>")(deployment_locale),
    ).label("last_name")

    location = None
    if request.args.get("location"):
        location = services.locations.find(id=request.args.get("location")).first()

    if request.args.get("export") and permissions.export_submissions.can():
        mode = request.args.get("export")
        if form.quality_checks:
            queryset = (
                models.Submission.query.filter(
                    models.Submission.submission_type == "O",
                    models.Submission.form == form,
                    models.Submission.event == event,
                )
                .join(models.Submission.location)
                .join(models.Submission.participant)
                .order_by(models.Submission.location_id, models.Submission.participant_id)
            )
        else:
            queryset = models.Submission.query.filter(false())

        query_filterset = filter_class(queryset, request.args)
        dataset = services.submissions.export_list(query_filterset.qs, include_qa=True)
        basename = slugify(
            "%s %s %s %s"
            % (g.event.name.lower(), form.name.lower(), datetime.utcnow().strftime("%Y %m %d %H%M%S"), mode)
        )
        content_disposition = "attachment; filename=%s.csv" % basename

        return Response(
            stream_with_context(dataset), headers={"Content-Disposition": content_disposition}, mimetype="text/csv"
        )

    # the following section defines the queryset for the submissions
    # to be retrieved. due to the fact that we do specialized sorting
    # the queryset will depend heavily on what is being sorted.
    if request.args.get("sort_by") == "location" and request.args.get("sort_value"):
        # when sorting based on location, we generally want to be able
        # to sort submissions based on a specific administrative division.
        # since we store the hierarchical structure in a separate table
        # we not only have to retrieve the table for the location data but
        # also join it based on results in the location hierarchy.

        # start out by getting all the locations (as a subquery) in a
        # particular division.
        division = (
            models.Location.query.with_entities(models.Location.id)
            .filter(models.Location.location_type_id == request.args.get("sort_value"))
            .subquery()
        )
        # next is we retrieve all the descendant locations for all the
        # locations in that particular administrative division making sure
        # to retrieve the name translations which would be used in sorting
        # the submissions when the time comes.
        descendants = (
            models.LocationPath.query.join(models.Location, models.Location.id == models.LocationPath.ancestor_id)
            .with_entities(models.Location.name_translations, models.LocationPath.descendant_id)
            .filter(models.LocationPath.ancestor_id.in_(division))
            .subquery()
        )

        # now we defined the actual queryset using the subqueries above
        # taking note to group by the translation name which essentially
        # is the division name.
        queryset = (
            models.Submission.query.select_from(
                models.Submission,
                models.Location,
                models.Participant,
            )
            .filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event_id == event.id,
            )
            .join(models.Location, models.Submission.location_id == models.Location.id)
            .join(models.Participant, models.Submission.participant_id == models.Participant.id)
            .outerjoin(descendants, descendants.c.descendant_id == models.Submission.location_id)
        )
    elif request.args.get("sort_by") == "phone":
        participant_phones = (
            models.PhoneContact.query.filter(models.PhoneContact.verified == True)  # noqa
            .order_by(desc(models.PhoneContact.updated))
            .subquery()
        )
        queryset = (
            models.Submission.query.filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event_id == event.id,
            )
            .join(models.Location, models.Submission.location_id == models.Location.id)
            .join(models.Participant, models.Submission.participant_id == models.Participant.id)
            .outerjoin(participant_phones, participant_phones.c.participant_id == models.Participant.id)
        )
    else:
        queryset = (
            models.Submission.query.select_from(
                models.Submission,
                models.Location,
                models.Participant,
            )
            .filter(
                models.Submission.submission_type == "O",
                models.Submission.form == form,
                models.Submission.event_id == event.id,
            )
            .join(models.Location, models.Submission.location_id == models.Location.id)
            .join(models.Participant, models.Submission.participant_id == models.Participant.id)
        )

    if request.args.get("sort_by") == "pid":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(
                desc(models.Participant.participant_id.cast(BigInteger)),
                models.Submission.serial_no.cast(BigInteger),
                models.Location.code.cast(BigInteger),
            )
        else:
            queryset = queryset.order_by(
                models.Participant.participant_id.cast(BigInteger),
                models.Submission.serial_no.cast(BigInteger),
                models.Location.code.cast(BigInteger),
            )
    elif request.args.get("sort_by") == "fsn":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(models.Submission.serial_no.cast(BigInteger)))
        else:
            queryset = queryset.order_by(models.Submission.serial_no.cast(BigInteger))
    elif request.args.get("sort_by") == "location":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(
                desc(descendants.c.name_translations.op("->>")(user_locale)),
                desc(descendants.c.name_translations.op("->>")(deployment_locale)),
            )
        else:
            queryset = queryset.order_by(
                descendants.c.name_translations.op("->>")(user_locale),
                descendants.c.name_translations.op("->>")(deployment_locale),
            )
    elif request.args.get("sort_by") == "participant":
        # specify the conditions for the order term
        condition1 = full_name_term == None  # noqa
        condition2 = full_name_term != None  # noqa

        # concatenation for the full name
        full_name_concat = func.concat_ws(
            " ",
            first_name_term,
            other_names_term,
            last_name_term,
        )

        # if the full name is empty, order by the concatenated
        # name, else order by the full name
        order_term = case((condition1, full_name_concat), (condition2, full_name_term))
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(order_term))
        else:
            queryset = queryset.order_by(order_term)
    elif request.args.get("sort_by") == "phone":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(desc(participant_phones.c.number))
        else:
            queryset = queryset.order_by(participant_phones.c.number)
    elif request.args.get("sort_by") == "moment":
        if request.args.get("sort_direction") == "desc":
            queryset = queryset.order_by(nullslast(desc(models.Submission.participant_updated)))
        else:
            queryset = queryset.order_by(models.Submission.participant_updated)
    else:
        queryset = queryset.order_by(
            models.Participant.participant_id.cast(BigInteger),
            models.Submission.serial_no.cast(BigInteger),
            models.Location.code.cast(BigInteger),
        )

    if not form.quality_checks:
        queryset = models.Submission.query.filter(false())
    else:
        queryset = queryset.with_entities(models.Submission, *generate_qa_queries(form)[0])

    query_filterset = filter_class(queryset, request.args)
    filter_form = query_filterset.form
    VERIFICATION_OPTIONS = services.submissions.__model__.VERIFICATION_OPTIONS

    if request.form.get("action") == "send_message":
        message = request.form.get("message", "")
        recipients = [
            x
            for x in [
                res[0].participant.primary_phone if res[0].participant and res[0].participant.primary_phone else ""
                for res in query_filterset.qs
            ]
            if x != ""
        ]
        recipients.extend(current_app.config.get("MESSAGING_CC"))

        if message and recipients and permissions.send_messages.can():
            send_messages.delay(g.event.id, message, recipients)
            return "OK"
        else:
            abort(400)

    context = {
        "form": form,
        "args": data,
        "filter_form": filter_form,
        "breadcrumbs": breadcrumbs,
        "location_types": loc_types,
        "location": location,
        "pager": query_filterset.qs.paginate(page=page, per_page=current_app.config.get("PAGE_SIZE")),
        "submissions": queryset,
        "quality_statuses": QUALITY_STATUSES,
        "verification_statuses": VERIFICATION_OPTIONS,
    }

    template_name = "frontend/quality_assurance_list.html"

    return render_template(template_name, **context)


def update_submission_version(submission):
    """Submission version creation."""
    # reload the submission to get rid of the loading problem
    # with the incident_status attribute
    submission = models.Submission.query.get(submission.id)
    db.session.refresh(submission)

    # save actual version data
    data_fields = submission.form.tags
    version_data = {k: submission.data.get(k) for k in data_fields if k in submission.data}

    if submission.form.form_type == "INCIDENT":
        if submission.incident_status:
            version_data["status"] = submission.incident_status.code
        version_data["description"] = submission.incident_description

    # get previous version
    previous = (
        services.submission_versions.find(submission=submission)
        .order_by(models.SubmissionVersion.timestamp.desc())
        .first()
    )

    if previous:
        diff = DictDiffer(version_data, previous.data)

        # don't do anything if the data wasn't changed
        if not diff.added() and not diff.removed() and not diff.changed():
            return

    # save user email as identity
    channel = "WEB"
    user = current_user._get_current_object()
    identity = user.email if not user.is_anonymous else "unknown"

    services.submission_versions.create(
        submission_id=submission.id,
        data=version_data,
        timestamp=datetime.utcnow(),
        channel=channel,
        identity=identity,
        deployment_id=submission.deployment_id,
    )


@route(bp, "/api/v1/submissions/export/aggregated/<form_id>")
@auth.login_required
def submission_export(form_id):
    """Submissions export view."""
    form = services.forms.get_or_404(id=form_id)
    if form.untrack_data_conflicts == True:  # noqa
        submission_type = "O"
    else:
        submission_type = "M"

    queryset = services.submissions.find(form=form, submission_type=submission_type).order_by("location")
    dataset = aggregated_dataframe(queryset, form).to_csv(encoding="utf-8", index=False, float_format="%d")

    # TODO: any other way to control/stream the output?
    # currently it just takes the name of the form ID
    return Response(dataset, mimetype="text/csv")


@route(bp, "/submissions/<int:submission_id>/image/delete", methods=["POST"])
@permissions.modify_images.require(403)
def delete_image(submission_id: int):
    """Image deletion view."""
    submission = models.Submission.query.filter_by(id=submission_id).first()

    if submission is None:
        return jsonify(
            {
                "status": "error",
                "message": "Not found",
            }
        ), HTTPStatus.NOT_FOUND

    form_class = forms.generate_image_delete_form_class(submission)
    delete_form = form_class()
    if delete_form.validate_on_submit():
        tag = delete_form.data["tag"]
        flag = submission.delete_image_attachment(tag)
        if flag:
            return jsonify(
                {
                    "status": "ok",
                    "message": _("Image deleted"),
                }
            ), HTTPStatus.OK
        else:
            return jsonify(
                {
                    "status": "error",
                    "message": _("Image not found"),
                }
            ), HTTPStatus.NOT_FOUND
