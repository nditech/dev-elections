# -*- coding: utf-8 -*-
from html import escape
from itertools import chain
from operator import itemgetter

from dateutil.parser import parse
from dateutil.tz import UTC, gettz
from flask_babel import gettext as _
from markupsafe import Markup
from sqlalchemy import Integer, and_, false, or_, true
from sqlalchemy.dialects.postgresql import array
from wtforms import Form, fields, widgets
from wtforms.widgets import html_params
from wtforms_alchemy.fields import QuerySelectField

from apollo import models
from apollo.core import BooleanFilter, CharFilter, ChoiceFilter, FilterSet
from apollo.dal import utils
from apollo.helpers import _make_choices
from apollo.settings import TIMEZONE
from apollo.submissions.models import FLAG_CHOICES
from apollo.submissions.qa.query_builder import build_expression, generate_qa_query

APP_TZ = gettz(TIMEZONE)


class TagLookupFilter(ChoiceFilter):
    def __init__(self, *args, **kwargs):
        self.contains = kwargs.pop('contains', None)
        super().__init__(*args, **kwargs)

    def filter(self, query, value, **kwargs):
        if value:
            if value == 'NULL':
                condition = (models.Submission.data[self.name] == None)   # noqa
            elif value == 'NOT_NULL':
                condition = (models.Submission.data[self.name] != None)   # noqa
            else:
                condition = (models.Submission.data[self.name] == value)

            return (condition, None)
        elif self.contains:
            condition = (models.Submission.data[self.name] != None) # noqa
            return (condition, None)

        return (None, None)


def make_submission_sample_filter(
        participant_set_id, filter_on_locations=False):
    class SubmissionSampleFilter(ChoiceFilter):
        def __init__(self, *args, **kwargs):
            sample_choices = models.Sample.query.filter_by(
                participant_set_id=participant_set_id
            ).order_by(
                models.Sample.name
            ).with_entities(models.Sample.id, models.Sample.name).all()
            self.participant_set_id = participant_set_id
            self.filter_on_locations = filter_on_locations

            kwargs["choices"] = _make_choices(sample_choices, _("Sample"))
            super().__init__(*args, **kwargs)

        def filter_by_locations(self, query, value):
            # Join with Location model if not already present
            if utils.has_model(query, models.Location):
                query1 = query
            else:
                query1 = query.join(models.Submission.location)

            locations_subquery = (
                models.Sample.query.filter(
                    models.Sample.id == value, models.Sample.participant_set_id == participant_set_id
                )
                .join(models.Participant.samples)
                .with_entities(models.Participant.location_id.label("loc_id"))
                .subquery()
            )
            query2 = query1.join(locations_subquery, models.Submission.location_id == locations_subquery.c.loc_id)

            return query2

        def filter_by_participants(self, query, value):
            participants_subquery = (
                models.Sample.query.filter(
                    models.Sample.id == value, models.Sample.participant_set_id == participant_set_id
                )
                .join(models.Participant.samples)
                .with_entities(models.Participant.id.label("part_id"))
                .subquery
            )
            query2 = query.join(
                participants_subquery, models.Submission.participant_id == participants_subquery.c.part_id
            )

            return query2

        def queryset_(self, query, value, **kwargs):
            if value:
                if self.filter_on_locations:
                    return self.filter_by_locations(query, value)
                else:
                    return self.filter_by_participants(query, value)

            return query

    return SubmissionSampleFilter


def make_participant_role_filter(participant_set_id):
    class ParticipantRoleFilter(ChoiceFilter):
        def __init__(self, *args, **kwargs):
            role_choices = models.ParticipantRole.query.filter_by(
                participant_set_id=participant_set_id
            ).order_by(
                models.ParticipantRole.name
            ).with_entities(
                models.ParticipantRole.id, models.ParticipantRole.name).all()
            self.participant_set_id = participant_set_id

            kwargs['choices'] = _make_choices(
                role_choices, _('Participant Role'))

            super().__init__(*args, **kwargs)

        def queryset_(self, query, value, **kwargs):
            if value:
                return query.filter(models.Participant.role_id == value)

            return query

    return ParticipantRoleFilter


def make_submission_location_group_filter(location_set_id):
    class SubmissionLocationGroupFilter(ChoiceFilter):
        def __init__(self, *args, **kwargs):
            group_choices = models.LocationGroup.query.filter_by(
                location_set_id=location_set_id
            ).order_by(
                models.LocationGroup.name
            ).with_entities(
                models.LocationGroup.id, models.LocationGroup.name
            ).all()
            self.location_set_id = location_set_id

            kwargs['choices'] = _make_choices(group_choices, _('Group'))
            super().__init__(*args, **kwargs)

        def queryset_(self, query, value, **kwargs):
            if value:
                location_ids = models.Location.query.join(
                    models.Location.groups
                ).filter(
                    models.Location.location_set_id == self.location_set_id,
                    models.LocationGroup.id == value,
                ).with_entities(models.Location.id)

                return query.filter(
                    models.Submission.location_id.in_(location_ids))

            return query

    return SubmissionLocationGroupFilter


def make_base_submission_filter(event, filter_on_locations=False):
    class BaseSubmissionFilterSet(FilterSet):
        sample = make_submission_sample_filter(
            event.participant_set_id,
            filter_on_locations=filter_on_locations
        )()
        location_group = make_submission_location_group_filter(
            event.location_set_id
        )()

    return BaseSubmissionFilterSet


class IncidentStatusFilter(ChoiceFilter):
    def __init__(self, *args, **kwargs):
        kwargs['choices'] = (
            ('', _('All Incidents')),
            ('NULL', _('Unmarked Incidents')),
            ('confirmed', _('Confirmed Incidents')),
            ('rejected', _('Rejected Incidents')),
            ('citizen', _('Citizen Report Incidents')),
        )
        super().__init__(*args, **kwargs)

    def filter(self, query, value, **kwargs):
        if value:
            if value == 'NULL':
                return (models.Submission.incident_status == None, None)    # noqa

            return (models.Submission.incident_status == value, None)

        return (None, None)


def make_submission_analysis_filter(event, form, filter_on_locations=False):
    attributes = {}
    if form.form_type == 'INCIDENT':
        attributes['status'] = IncidentStatusFilter(default='confirmed')

    return type(
        'SubmissionAnalysisFilterSet',
        (make_base_submission_filter(
            event, filter_on_locations=filter_on_locations),),
        attributes
    )


def make_incident_location_filter(event, form, tag, filter_on_locations=False):
    base_filter_class = make_submission_analysis_filter(
        event, form, filter_on_locations=filter_on_locations)

    attributes = {
        tag: TagLookupFilter(
            choices=(('NOT_NULL', ''),),
            contains=True,
            default='NOT_NULL',
            widget=widgets.HiddenInput()
        )
    }

    return type(
        'CriticalIncidentLocationFilterSet',
        (base_filter_class,),
        attributes)


class FormGroupFilter(ChoiceFilter):
    def __init__(self, *args, **kwargs):
        self.formobj = kwargs.pop('form')
        self.group = kwargs.pop('group')
        super().__init__(*args, **kwargs)

    def filter(self, query, value, **kwargs):
        group_tags = self.formobj.get_group_tags(self.group['name'])

        if value == '1':
            # Partial
            if group_tags:
                constraint = and_(
                        ~models.Submission.data.has_all(array(group_tags)),
                        models.Submission.data.has_any(array(group_tags))
                    )
            else:
                constraint = false()
        elif value == '2':
            # Missing
            if group_tags:
                constraint = or_(
                        ~models.Submission.data.has_any(array(group_tags)),
                        models.Submission.data == None  # noqa
                    )
            else:
                constraint = true()
        elif value == '3':
            # Complete
            if group_tags:
                constraint = models.Submission.data.has_all(array(group_tags))
            else:
                constraint = false()
        elif value == '4':
            # Conflict
            if group_tags:
                query_params = [
                    models.Submission.conflicts.has_key(tag)    # noqa
                    for tag in group_tags
                ]
                constraint = or_(*query_params)
            else:
                constraint = false()
        else:
            constraint = None

        if constraint is None:
            return (None, None)
        else:
            form_ = kwargs['form']
            if form_.data and form_.data.get('conjunction') is True:
                # OR conjunction
                return (None, constraint)
            else:
                # AND conjunction
                return (constraint, None)


class FieldOptionFilter(ChoiceFilter):
    def filter(self, query, value, **kwargs):
        if value:
            return (
                models.Submission.data[self.name].astext.cast(
                    Integer) == int(value),
                None
            )

        return (None, None)


class FieldValueFilter(CharFilter):
    pass


class ParticipantIDFilter(CharFilter):
    def filter(self, query, value, **kwargs):
        if value:
            return (
                models.Participant.participant_id == value,
                None
            )

        return (None, None)


class SubmissionQuarantineStatusFilter(ChoiceFilter):
    def filter(self, query, value, **kwargs):
        if value and value == 'N':
            return (
                or_(
                    models.Submission.quarantine_status == None,    # noqa
                    models.Submission.quarantine_status == ''),
                None
            )
        elif value in ('A', 'R'):
            return (models.Submission.quarantine_status == value, None)
        elif value == 'AR':
            return (
                or_(
                    models.Submission.quarantine_status == 'A',
                    models.Submission.quarantine_status == 'R'
                ),
                None
            )
        else:
            return (None, None)


class SubmissionSenderVerificationFilter(ChoiceFilter):
    def filter(self, query, value, **kwargs):
        if value and value == '1':
            return (
                models.Submission.sender_verified == True,  # noqa
                None
            )
        elif value:
            return (
                models.Submission.sender_verified == False, # noqa
                None
            )

        return (None, None)


class OnlineStatusFilter(ChoiceFilter):
    def filter(self, query, value, **kwargs):
        if value and value == '1':
            return (
                models.Submission.unreachable == True,  # noqa
                None
            )
        elif value:
            return (
                models.Submission.unreachable == False, # noqa
                None
            )

        return (None, None)


class DateFilter(CharFilter):
    def filter(self, query, value, **kwargs):
        if value:
            try:
                dt = parse(value, dayfirst=True)
            except (OverflowError, ValueError):
                return (None, None)

            dt = dt.replace(tzinfo=APP_TZ)
            upper_bound = dt.replace(hour=23, minute=59, second=59).astimezone(
                UTC).replace(tzinfo=None)
            lower_bound = dt.replace(hour=0, minute=0, second=0).astimezone(
                UTC).replace(tzinfo=None)

            return (
                and_(
                    models.Submission.participant_updated >= lower_bound,
                    models.Submission.participant_updated <= upper_bound
                ),
                None
            )

        return (None, None)


class LocationSelectWidget(widgets.Select):
    @classmethod
    def render_option(cls, value, label, selected, **kwargs):
        options = dict(kwargs, value=value)
        if selected:
            options['selected'] = True
        if hasattr(label, 'location_type'):
            return Markup('<option %s>%s · %s</option>' % (
                html_params(**options),
                escape(str(label.name)),
                escape(str(label.location_type))))
        else:
            return Markup('<option %s>%s</option>' % (
                html_params(**options),
                escape(str(label))))


class ParticipantSelectWidget(widgets.Select):
    @classmethod
    def render_option(cls, value, label, selected, **kwargs):
        options = dict(kwargs, value=value)
        if selected:
            options['selected'] = True
        if hasattr(label, 'participant_id'):
            return Markup('<option %s>%s · %s</option>' % (
                html_params(**options),
                escape(str(label.participant_id)),
                escape(str(label.name))))
        else:
            return Markup('<option %s>%s</option>' % (
                html_params(**options),
                escape(str(label))))


class LocationQuerySelectField(QuerySelectField):
    widget = LocationSelectWidget()

    def process_formdata(self, valuelist):
        if valuelist and valuelist[0] and valuelist[0] != '__None':
            self.query = models.Location.query.filter(
                models.Location.id == valuelist[0])
        return super(LocationQuerySelectField, self).process_formdata(
            valuelist)


class ParticipantQuerySelectField(QuerySelectField):
    widget = ParticipantSelectWidget()

    def process_formdata(self, valuelist):
        if valuelist and valuelist[0] and valuelist[0] != '__None':
            self.query = models.Participant.query.filter(
                models.Participant.id == valuelist[0])
        return super(ParticipantQuerySelectField, self).process_formdata(
            valuelist)


class FormSerialNumberFilter(CharFilter):
    def filter(self, query, value, **kwargs):
        if value:
            return (
                models.Submission.serial_no == value,
                None
            )

        return (None, None)


class AJAXLocationFilter(ChoiceFilter):
    field_class = LocationQuerySelectField

    def __init__(self, *args, **kwargs):
        kwargs['query_factory'] = lambda: []
        kwargs['get_pk'] = lambda i: i.id

        return super(AJAXLocationFilter, self).__init__(*args, **kwargs)

    def queryset_(self, queryset, value, **kwargs):
        if value:
            location_query = models.Location.query.with_entities(
                models.Location.id
            ).join(
                models.LocationPath,
                models.Location.id == models.LocationPath.descendant_id
            ).filter(models.LocationPath.ancestor_id == value.id)

            return queryset.filter(
                models.Submission.location_id.in_(location_query))

        return queryset


class QualityAssuranceFilter(ChoiceFilter):
    field_class = fields.FormField

    def __init__(self, form, qa_form, *args, **kwargs):
        kwargs['form_class'] = form
        self.qa_form = qa_form
        super(QualityAssuranceFilter, self).__init__(*args, **kwargs)

    def queryset_(self, query, value):
        if (
            'criterion' in value and 'condition' in value and
            value['criterion'] and value['condition']
        ):
            if value['criterion'] == 'A':
                # find all records for which any match the
                # following condition
                condition = value['condition']

                qa_subqueries = []
                if self.qa_form.quality_checks:
                    for check in self.qa_form.quality_checks:
                        qa_expr = build_expression(check)
                        single_qa_query, tags = generate_qa_query(
                            qa_expr, self.qa_form)
                        uses_null = 'null' in qa_expr.lower()

                        if tags and uses_null is False:
                            null_query = or_(*[
                                models.Submission.data[tag] == None # noqa
                                for tag in tags])
                        else:
                            null_query = false()

                        filter_query = None

                        if condition == FLAG_CHOICES[3][0]:
                            # verified: checklists that fail QA,
                            # have all fields verified, and none are missing
                            term1 = (single_qa_query == True)   # noqa
                            if tags:
                                term2 = models.Submission.verified_fields.has_all(      # noqa
                                    array(tags))
                            else:
                                term2 = false()
                            filter_query = and_(term1, term2, null_query == False)  # noqa
                        elif condition == FLAG_CHOICES[1][0]:
                            # missing: checklist has missing data
                            filter_query = or_(
                                null_query == True, single_qa_query == None)    # noqa
                        elif condition == FLAG_CHOICES[0][0]:
                            # flagged: checklist fails QA, not all fields are
                            # verified, and none of them are missing
                            term1 = (single_qa_query == True)   # noqa
                            if tags:
                                term2 = ~models.Submission.verified_fields.has_all(     # noqa
                                    array(tags))
                            else:
                                term2 = true()

                            filter_query = and_(term1, term2, null_query == False)  # noqa
                        elif condition == FLAG_CHOICES[2][0]:
                            # ok: checklist passes QA and none of the fields are    # noqa
                            # missing
                            filter_query = and_(
                                single_qa_query == False, null_query == False)  # noqa

                        if filter_query is None:
                            return query.filter(false())

                        qa_subqueries.append(filter_query)
                else:
                    return query.filter(false())

                return query.filter(or_(*qa_subqueries))

            try:
                index = int(value['criterion'])
                check = self.qa_form.quality_checks[index]
            except (IndexError, ValueError):
                return query

            qa_expr = build_expression(check)
            qa_subquery, tags = generate_qa_query(qa_expr, self.qa_form)
            question_codes = array(tags)
            uses_null = 'null' in qa_expr.lower()
            if tags:
                null_query = or_(*[
                    models.Submission.data[tag] == None # noqa
                    for tag in question_codes]) if not uses_null else false()
            else:
                null_query = false()

            condition = value['condition']
            if condition == FLAG_CHOICES[3][0]:
                # verified
                if tags:
                    return query.filter(
                        null_query == False,    # noqa
                        qa_subquery == True,    # noqa
                        models.Submission.verified_fields.has_all(
                            question_codes))
                else:
                    return query.filter(
                        qa_subquery == True,    # noqa
                        false())
            elif condition == FLAG_CHOICES[1][0]:
                # missing
                if tags:
                    term1 = null_query
                    term2 = (qa_subquery == None)   # noqa

                    return query.filter(or_(term1, term2))

                return query.filter(qa_subquery == None)    # noqa
            elif condition == FLAG_CHOICES[0][0]:
                # flagged
                term1 = (qa_subquery == True)   # noqa
                if tags:
                    term2 = ~models.Submission.verified_fields.has_all(
                        question_codes)
                else:
                    term2 = false()
                return query.filter(null_query == False, term1, term2)  # noqa
            elif condition == FLAG_CHOICES[2][0]:
                # OK
                return query.filter(
                    null_query == False, qa_subquery == False)  # noqa
        return query


class SubmissionDateFilter(CharFilter):
    def queryset_(self, queryset, value):
        if value:
            try:
                timestamp = parse(value, dayfirst=True)
            except Exception:
                return queryset.filter(False)

            upper = timestamp.replace(hour=23, minute=59, second=59)
            lower = timestamp.replace(hour=0, minute=0, second=0)

            return queryset.filter(
                models.Submission.participant_updated >= lower,
                models.Submission.participant_updated <= upper
            )

        return queryset


class SubmissionValuesFilter(CharFilter):
    def __init__(self, name=None, widget=None, **kwargs):
        self.form = kwargs.pop('questionnaire', None)
        super().__init__(name, widget, **kwargs)
    
    def queryset_(self, queryset, value, **kwargs):
        if value:
            if self.form is None:
                return queryset.filter(false())

            try:
                parsed = value.split(',')
                terms = [generate_qa_query(item, self.form)[0] for item in parsed]
            except Exception:
                return queryset.filter(false())
            
            return queryset.filter(*terms)
        return super().queryset_(queryset, value, **kwargs)


def make_submission_location_filter(location_set_id):
    class AJAXLocationFilter(ChoiceFilter):
        field_class = LocationQuerySelectField

        def __init__(self, *args, **kwargs):
            kwargs['query_factory'] = lambda: []
            kwargs['get_pk'] = lambda i: i.id

            super().__init__(*args, **kwargs)

        def queryset_(self, query, value, **kwargs):
            if value:
                location_query = models.Location.query.with_entities(
                    models.Location.id
                ).join(
                    models.LocationPath,
                    models.Location.id == models.LocationPath.descendant_id
                ).filter(models.LocationPath.ancestor_id == value.id)

                return query.filter(
                    models.Submission.location_id.in_(location_query))

            return query

    return AJAXLocationFilter


def make_dashboard_filter(event, filter_on_locations=False):
    attributes = {}
    attributes['location'] = make_submission_location_filter(
            event.location_set_id)()
    attributes['sample'] = make_submission_sample_filter(
        event.participant_set_id, filter_on_locations=filter_on_locations)()
    attributes['location_group'] = make_submission_location_group_filter(
        event.location_set_id)()

    return type(
        'SubmissionFilterSet',
        (make_base_submission_filter(
            event, filter_on_locations=filter_on_locations),),
        attributes)


def make_submission_list_filter(event, form, filter_on_locations=False):
    attributes = {}
    form._populate_field_cache()

    if form.data and form.data.get('groups'):
        if form.form_type == 'INCIDENT':
            option_fields = [
                f for f in form._field_cache.values()
                if f['type'] == 'select']
            if len(option_fields) >= 1:
                option_fields = option_fields[:1]
            for field in option_fields:
                choices = _make_choices(sorted(
                    ((v, '{} - {}'.format(field['tag'], k)) for k, v in
                        field['options'].items()),
                    key=itemgetter(0)
                ), field['tag'])
                attributes[field['tag']] = FieldOptionFilter(choices=choices)

        for group in form.data.get('groups'):
            field_name = '{}__{}'.format(form.id, group['slug'])
            choices = [
                ('', _('%(group)s Status', group=group['name'])),
                ('1', _('%(group)s Partial', group=group['name'])),
                ('2', _('%(group)s Missing', group=group['name'])),
                ('3', _('%(group)s Complete', group=group['name'])),
                ('4', _('%(group)s Conflict', group=group['name']))
            ]
            attributes[field_name] = FormGroupFilter(
                choices=choices, form=form, group=group)

    if form.form_type == 'INCIDENT':
        attributes['status'] = IncidentStatusFilter()
    elif form.form_type in ['CHECKLIST', 'SURVEY']:
        attributes['quarantine_status'] = SubmissionQuarantineStatusFilter(
            choices=(
                ('', _('Quarantine Status')),
                ('A', _('Quarantine All')),
                ('R', _('Quarantine Results')),
                ('AR', _('Quarantine All + Results')),
                ('N', _('Quarantine None')),
            ), default='')
    attributes['sender_verification'] = SubmissionSenderVerificationFilter(
        choices=(
            ('', _('Phone Confirmation')),
            ('1', _('Phone Confirmed')),
            ('2', _('Phone Unconfirmed'))
        ))

    attributes['participant_id'] = ParticipantIDFilter()
    attributes['location'] = make_submission_location_filter(
        event.location_set_id)()
    attributes['conjunction'] = BooleanFilter(
        label=_('Optional Inclusion')
    )
    attributes['online_status'] = OnlineStatusFilter(
        choices=(
            ('', _('Signal Status')),
            ('0', _('Signal')),
            ('1', _('No Signal'))
        )
    )
    attributes['date'] = DateFilter()
    attributes['fsn'] = FormSerialNumberFilter()
    attributes['participant_role'] = make_participant_role_filter(
        event.participant_set_id)()
    attributes['values'] = SubmissionValuesFilter(questionnaire=form)

    return type(
        'SubmissionFilterSet',
        (make_base_submission_filter(
            event, filter_on_locations=filter_on_locations),),
        attributes)


def generate_quality_assurance_filter(event, form):
    from apollo.submissions.filters import make_base_submission_filter

    quality_check_criteria = [
        ('', _('Quality Check Criterion')),
        ('A', _('Any Criterion'))
    ] + \
        (
            [
                (str(idx), qc['description'])
                for idx, qc in enumerate(form.quality_checks)
            ]
            if form.quality_checks else []
        )
    quality_check_conditions = [('', _('Quality Check Condition'))] + \
        list(FLAG_CHOICES)

    class QualityAssuranceConditionsForm(Form):
        criterion = fields.SelectField(choices=quality_check_criteria)
        condition = fields.SelectField(choices=quality_check_conditions)

    attributes = {}

    attributes['quality_check'] = QualityAssuranceFilter(
        QualityAssuranceConditionsForm, form)

    # quarantine status
    attributes['quarantine_status'] = SubmissionQuarantineStatusFilter(
        choices=(
            ('', _('Quarantine Status')),
            ('N', _('Quarantine None')),
            ('A', _('Quarantine All')),
            ('R', _('Quarantine Results'))
        ))

    attributes['online_status'] = OnlineStatusFilter(
        choices=(
            ('', _('Signal Status')),
            ('0', _('Signal')),
            ('1', _('No Signal'))
        )
    )

    # participant id and location
    attributes['participant_id'] = ParticipantIDFilter()
    attributes['location'] = AJAXLocationFilter()
    attributes['date'] = SubmissionDateFilter()
    attributes['fsn'] = FormSerialNumberFilter()
    attributes['participant_role'] = make_participant_role_filter(
        event.participant_set_id)()

    return type(
        'QualityAssuranceFilterSet',
        (make_base_submission_filter(event),),
        attributes
    )
