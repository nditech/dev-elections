# -*- coding: utf-8 -*-
from http import HTTPStatus

from flask import g, jsonify, request
from flask_apispec import MethodResource, marshal_with, use_kwargs
from flask_babel import gettext
from flask_jwt_extended import (
    create_access_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
    set_access_cookies,
    unset_access_cookies,
)
from sqlalchemy import and_, bindparam, func, or_, text, true
from sqlalchemy.orm import aliased
from sqlalchemy.orm.exc import NoResultFound
from webargs import fields

from apollo import settings
from apollo.api.common import BaseListResource
from apollo.api.decorators import protect
from apollo.core import csrf, red
from apollo.deployments.models import Event
from apollo.formsframework.api.schema import FormSchema
from apollo.formsframework.models import Form
from apollo.locations.models import Location
from apollo.participants.api.schema import ParticipantSchema
from apollo.participants.models import (
    Participant,
    ParticipantFirstNameTranslations,
    ParticipantFullNameTranslations,
    ParticipantLastNameTranslations,
    ParticipantOtherNamesTranslations,
    ParticipantRole,
    ParticipantSet,
)
from apollo.submissions.models import Submission


@marshal_with(ParticipantSchema)
@use_kwargs({"event_id": fields.Int()}, location="query")
class ParticipantItemResource(MethodResource):
    @protect
    def get(self, participant_id, **kwargs):
        deployment = getattr(g, "deployment", None)
        event = getattr(g, "event", None)

        event_id = kwargs.get("event_id")
        if event_id:
            event = Event.query.filter_by(id=event_id).first_or_404()

        if deployment:
            deployment_id = deployment.id
        else:
            deployment_id = None

        if event and event.participant_set_id:
            participant_set_id = event.participant_set_id
        else:
            participant_set_id = None

        participant = (
            Participant.query.join(Participant.participant_set)
            .filter(
                ParticipantSet.id == participant_set_id,
                ParticipantSet.deployment_id == deployment_id,
                Participant.id == participant_id,
                Participant.participant_set_id == ParticipantSet.id,
            )
            .first_or_404()
        )

        return participant


@use_kwargs({"event_id": fields.Int(), "q": fields.String()}, location="query")
class ParticipantListResource(BaseListResource):
    schema = ParticipantSchema()

    def get_items(self, **kwargs):
        event_id = kwargs.get("event_id")
        lookup_item = kwargs.get("q")

        deployment = getattr(g, "deployment", None)
        if event_id is None:
            event = getattr(g, "event", None)
        else:
            event = Event.query.filter_by(id=event_id).one()

        if deployment:
            deployment_id = deployment.id
        else:
            deployment_id = None

        if event and event.participant_set_id:
            participant_set_id = event.participant_set_id
        else:
            participant_set_id = None

        full_name_lat_query = ParticipantFullNameTranslations.lateral("full_name")
        first_name_lat_query = ParticipantFirstNameTranslations.lateral("first_name")
        other_names_lat_query = ParticipantOtherNamesTranslations.lateral("other_names")
        last_name_lat_query = ParticipantLastNameTranslations.lateral("last_name")

        queryset = (
            Participant.query.select_from(Participant)
            .join(Participant.participant_set)
            .outerjoin(full_name_lat_query, true())
            .outerjoin(first_name_lat_query, true())
            .outerjoin(other_names_lat_query, true())
            .outerjoin(last_name_lat_query, true())
            .filter(
                Participant.participant_set_id == participant_set_id,
                ParticipantSet.deployment_id == deployment_id,
                ParticipantSet.id == participant_set_id,
            )
        )

        if lookup_item:
            queryset = queryset.filter(
                or_(
                    text("full_name.value ILIKE :name"),
                    func.btrim(
                        func.regexp_replace(
                            func.concat_ws(
                                " ",
                                text("first_name.value"),
                                text("other_names.value"),
                                text("last_name.value"),
                            ),
                            r"\s+",
                            " ",
                            "g",
                        )
                    ).ilike(f"%{lookup_item}%"),
                    Participant.participant_id.ilike(bindparam("pid")),
                )
            ).params(name=f"%{lookup_item}%", pid=f"{lookup_item}%")

        return queryset


@csrf.exempt
def login():
    """Participant login view."""
    request_data = request.json
    participant_id = request_data.get("participant_id")
    password = request_data.get("password")

    current_events = Event.overlapping_events(Event.default())
    participant = (
        current_events.join(Participant, Participant.participant_set_id == Event.participant_set_id)
        .with_entities(Participant)
        .filter(Participant.participant_id == participant_id, Participant.password == password)
        .first()
    )

    if participant is None:
        response_body = {"message": gettext("Login failed"), "status": "error"}
        response = jsonify(response_body)
        response.status_code = HTTPStatus.FORBIDDEN

        return response

    access_token = create_access_token(identity=str(participant.uuid), fresh=True)

    # only return tokens if client explicitly requests it
    # and cookies are disabled
    send_jwts_in_response = "cookies" not in settings.JWT_TOKEN_LOCATION or (
        request.headers.get("X-TOKEN-IN-BODY") is not None
    )

    response_body = {
        "data": {
            "participant": {
                "events": [ev.id for ev in participant.participant_set.events],
                "first_name": participant.first_name,
                "other_names": participant.other_names,
                "last_name": participant.last_name,
                "full_name": participant.full_name,
                "participant_id": participant_id,
                "location": participant.location.name if participant.location else "",
                "locale": participant.locale,
            },
        },
        "status": "ok",
        "message": gettext("Logged in successfully"),
    }

    if send_jwts_in_response:
        response_body["data"].update(access_token=access_token)

        return jsonify(response_body)

    resp = jsonify(response_body)
    set_access_cookies(resp, access_token)

    return resp


@csrf.exempt
@jwt_required()
def logout():
    """Participant logout view."""
    jti = get_jwt()["jti"]
    red.set(jti, "", int(settings.JWT_ACCESS_TOKEN_EXPIRES.total_seconds()))

    # unset cookies if they are used
    unset_cookies = "cookies" in settings.JWT_TOKEN_LOCATION

    response_body = {"status": "ok", "message": gettext("Logged out successfully")}
    response = jsonify(response_body)

    if unset_cookies:
        unset_access_cookies(response)

    return response


def _get_form_data(participant: Participant):
    EventAlias = aliased(Event, flat=True)
    FormAlias = aliased(Form, flat=True)

    # get incident forms
    incident_forms = (
        Form.query.join(EventAlias, Form.events)
        .filter(
            EventAlias.participant_set_id == participant.participant_set_id,
            Form.form_type == "INCIDENT",
            Form.is_hidden == False,  # noqa
        )
        .with_entities(Form)
        .order_by(Form.name, Form.id)
    )

    # get participant submissions
    participant_submissions = Submission.query.join(
        EventAlias,
        and_(EventAlias.participant_set_id == participant.participant_set_id, Submission.event_id == EventAlias.id),
    ).join(FormAlias, Submission.form_id == FormAlias.id)

    # get checklist and survey forms based on the available submissions
    non_incident_forms = participant_submissions.with_entities(FormAlias).distinct(FormAlias.id)
    checklist_forms = non_incident_forms.filter(FormAlias.form_type == "CHECKLIST", FormAlias.is_hidden == False)  # noqa
    survey_forms = non_incident_forms.filter(FormAlias.form_type == "SURVEY", FormAlias.is_hidden == False)  # noqa

    # get form serial numbers
    form_ids_with_serials = (
        participant_submissions.filter(FormAlias.form_type == "SURVEY")
        .with_entities(FormAlias.id, Submission.serial_no)
        .distinct(FormAlias.id, Submission.serial_no)
        .order_by(FormAlias.id, Submission.serial_no)
    )

    all_forms = checklist_forms.all() + incident_forms.all() + survey_forms.all()

    serials = [{"form": pair[0], "serial": pair[1]} for pair in form_ids_with_serials]

    return all_forms, serials


@csrf.exempt
@jwt_required()
def get_forms():
    """Retrieve a list of forms."""
    participant_uuid = get_jwt_identity()

    try:
        participant = Participant.query.filter_by(uuid=participant_uuid).one()
    except NoResultFound:
        response_body = {"message": gettext("Invalid participant"), "status": "error"}

        response = jsonify(response_body)
        response.status_code = HTTPStatus.BAD_REQUEST
        return response

    forms, serials = _get_form_data(participant)

    form_data = FormSchema(many=True).dump(forms)

    response_body = {
        "data": {
            "forms": form_data,
            "serials": serials,
        },
        "message": gettext("ok"),
        "status": "ok",
    }

    return jsonify(response_body)


@use_kwargs({"event_id": fields.Int(), "level_id": fields.Int(), "role_id": fields.Int()}, location="query")
@protect
def get_participant_count(**kwargs):
    """Returns the number of participants that a certain submission generation event will involve."""
    event_id = kwargs.get("event_id")
    level_id = kwargs.get("level_id")
    role_id = kwargs.get("role_id")

    event = Event.query.filter_by(id=event_id).first()
    if event is None:
        return {"participants": None}

    participants = Participant.query.filter_by(participant_set_id=event.participant_set_id)

    if level_id:
        participants = participants.join(Location, Participant.location_id == Location.id).filter(
            Location.location_type_id == level_id
        )

    if role_id:
        participants = participants.join(ParticipantRole, Participant.role_id == ParticipantRole.id).filter(
            ParticipantRole.id == role_id
        )

    num_participants = participants.with_entities(Participant).count()
    return {"participants": num_participants}
