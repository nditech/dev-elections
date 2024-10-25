# -*- coding: utf-8 -*-
import re
from datetime import datetime
from io import BytesIO
from urllib.parse import urlencode
from zipfile import ZIP_DEFLATED, ZipFile

import magic
import pytz
from flask import flash, g, redirect, request, send_file, session
from flask_admin import BaseView, expose, form
from flask_admin.actions import action
from flask_admin.contrib.sqla import ModelView
from flask_admin.contrib.sqla.fields import InlineModelFormList
from flask_admin.contrib.sqla.form import AdminModelConverter, InlineModelConverter
from flask_admin.form import fields, rules
from flask_admin.model.form import InlineFormAdmin
from flask_admin.model.template import macro
from flask_babel import lazy_gettext as _
from flask_security import current_user, login_required, roles_required
from flask_security.utils import hash_password, url_for_security
from flask_wtf.file import FileField
from jinja2 import pass_context
from PIL import Image
from slugify import slugify
from sqlalchemy import func
from wtforms import BooleanField, PasswordField, SelectField, SelectMultipleField, validators

from apollo import models, services, settings
from apollo.constants import LANGUAGE_CHOICES
from apollo.core import admin, db, security
from apollo.deployments.serializers import EventArchiveSerializer
from apollo.formsframework.views_forms import (
    checklist_init,
    edit_form,
    export_form,
    form_builder,
    forms_list,
    import_form_schema,
    new_form,
    quality_control_delete,
    quality_control_edit,
    quality_controls,
    sort_quality_controls,
    survey_init,
)
from apollo.frontend import views_users as user_views
from apollo.locations.views_locations import (
    export_divisions,
    finalize_location_set,
    import_divisions,
    location_edit,
    locations_builder,
    locations_headers,
    locations_import,
    locations_list,
)
from apollo.participants.views_participants import (
    participant_edit,
    participant_headers,
    participant_list,
    participant_list_import,
)
from apollo.utils import resize_image

app_time_zone = pytz.timezone(settings.TIMEZONE)
utc_time_zone = pytz.utc

excluded_perm_actions = ["view_forms", "access_event"]

DATETIME_FORMAT_SPEC = "%Y-%m-%d %H:%M:%S %Z"


def resize_logo(pil_image: Image):
    """Resize a logo image to fit within the specified dimensions."""
    background_color = (255, 255, 255, 0)

    width, height = pil_image.size
    if width == height:
        return pil_image
    elif width > height:
        result = Image.new("RGBA", (width, width), background_color)
        result.paste(pil_image, (0, (width - height) // 2))
        return result
    else:
        result = Image.new("RBBA", (height, height), background_color)
        result.paste(pil_image, ((height - width) // 2, 0))
        return result


def _get_usable_forms():
    deployment = g.deployment
    return models.Form.query.filter(
        models.Form.deployment == deployment,
        models.Form.is_hidden == False,  # noqa
    )


def _get_usable_location_sets():
    deployment = g.deployment
    return models.LocationSet.query.filter(
        models.LocationSet.deployment == deployment,
        models.LocationSet.is_hidden == False,  # noqa
    )


def _get_usable_participant_sets():
    deployment = g.deployment
    return models.ParticipantSet.query.filter(
        models.ParticipantSet.deployment == deployment,
        models.ParticipantSet.is_hidden == False,  # noqa
    )


def _get_usable_resources():
    deployment = g.deployment
    event_resources = (
        models.Resource.query.filter(
            models.Resource.deployment_id == deployment.id, models.Resource.resource_type == "event"
        )
        .join(models.Event, models.Event.resource_id == models.Resource.resource_id)
        .filter(models.Event.is_hidden == False)  # noqa
        .with_entities(models.Resource)
    )
    form_resources = (
        models.Resource.query.filter(
            models.Resource.deployment_id == deployment.id, models.Resource.resource_type == "form"
        )
        .join(models.Form, models.Form.resource_id == models.Resource.resource_id)
        .filter(models.Form.is_hidden == False)  # noqa
        .with_entities(models.Resource)
    )

    return event_resources.union(form_resources)


class ParticipantSetFormModelConverter(AdminModelConverter):
    def _model_select_field(self, prop, multiple, remote_model, **kwargs):
        if remote_model == models.LocationSet:
            # TODO: there should be a better way of doing this
            def query_factory():
                return remote_model.query.filter_by(is_hidden=False)

            kwargs["query_factory"] = query_factory

        return super()._model_select_field(prop, multiple, remote_model, **kwargs)


class MultipleSelect2Field(fields.Select2Field):
    def iter_choices(self):
        if self.allow_blank:
            yield ("__None", self.blank_text, self.data is None)

        for value in self.data:
            yield (value, value, True)

    def process_data(self, value):
        """This is called when you create the form with existing data."""
        if value is None:
            self.data = []
        else:
            try:
                self.data = [self.coerce(value) for value in value]
            except (ValueError, TypeError):
                self.data = []

    def process_formdata(self, valuelist):
        """Process posted data."""
        if not valuelist:
            return

        if valuelist[0] == "__None":
            self.data = []
        else:
            try:
                self.data = [self.coerce(value) for value in valuelist]
            except ValueError:
                raise

    def pre_validate(self, form):
        pass


class HiddenObjectMixin(object):
    can_delete = False
    query_param_name = "show_all"

    @action("hide", _("Archive"), _("Are you sure you want to archive the selected items?"))
    def action_hide(self, ids):
        model_class = self.model
        model_class.query.filter(model_class.id.in_(ids)).update({"is_hidden": True}, synchronize_session=False)
        db.session.commit()

    @action("unhide", _("Unarchive"), _("Are you sure you want to unarchive the selected items?"))
    def action_unhide(self, ids):
        model_class = self.model
        model_class.query.filter(model_class.id.in_(ids)).update({"is_hidden": False}, synchronize_session=False)
        db.session.commit()

    def get_count_query(self):
        return self.get_query().with_entities(func.count(self.model.id))

    def get_one(self, id):
        return self.get_query().filter(self.model.id == id).one()

    def get_query(self):
        query = super().get_query()
        return self.filter_hidden(query)

    def filter_hidden(self, query):
        model_class = self.model
        query_params = request.args.to_dict(flat=False)
        show_hidden_direct = bool(query_params.get(self.query_param_name))
        show_hidden_param = any(
            re.search(rf'{self.query_param_name}=[^&]+', item)
            for item in query_params.get("url", [])
        )
        if show_hidden_direct or show_hidden_param:
            pass
        else:
            query = query.filter(model_class.is_hidden == False)  # noqa
        return query

    def render(self, template, **kwargs):
        query_params = request.args.to_dict(flat=False)
        show_hidden = bool(query_params.get(self.query_param_name))
        if show_hidden:
            # remove the 'show all' query parameter
            query_params.pop(self.query_param_name)
            kwargs["hidden_toggle_label"] = _("Hide Archived")
            kwargs["hidden_toggle_params"] = urlencode(query_params, doseq=True)
        else:
            # add the 'show all' query parameter
            query_params[self.query_param_name] = 1
            kwargs["hidden_toggle_label"] = _("Show All")
            kwargs["hidden_toggle_params"] = urlencode(query_params, doseq=True)
        kwargs["hide_toggle_on"] = True

        return super().render(template, **kwargs)


class BaseAdminView(ModelView):
    page_size = settings.PAGE_SIZE

    def is_accessible(self):
        """For checking if the admin view is accessible."""
        if current_user.is_anonymous:
            return False

        deployment = current_user.deployment
        role = models.Role.query.filter_by(deployment_id=deployment.id, name="admin").first()
        return current_user.has_role(role)

    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for_security("login", next=request.url))


class ExtraDataInlineFormAdmin(InlineFormAdmin):
    form_columns = ("id", "name", "label", "visible_in_lists")
    column_labels = {
        "name": _("Name"),
        "label": _("Label"),
        "visible_in_lists": _("Visible In Lists"),
    }
    column_descriptions = {
        "label": _("The display label to use for this field."),
        "name": _("Internal name to use for this attribute. Avoid using spaces in the name."),  # noqa
        "visible_in_lists": _("Specifies whether to display this field on data lists or not."),  # noqa
    }

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.deployment = g.deployment
            model.resource_type = model.__mapper_args__["polymorphic_identity"]
        model.name = slugify(model.name, separator="_")

    def on_model_delete(self, model):
        # if this model has a resource attached to it, delete it as well
        models.Resource.query.filter_by(uuid=model.uuid).delete()


class ExtraDataInlineFormList(InlineModelFormList):
    def __init__(self, frm, session, model, prop, inline_view, **kwargs):
        """Initializers."""
        self.form = frm
        self.session = session
        self.model = model
        self.prop = prop
        self.inline_view = inline_view
        self._pk = "id"

        form_opts = form.FormOpts(
            widget_args=getattr(inline_view, "form_widget_args", None), form_rules=inline_view._form_rules
        )
        form_field = self.form_field_type(frm, self._pk, form_opts=form_opts)
        super(InlineModelFormList, self).__init__(form_field, **kwargs)


class LocationExtraDataModelConverter(InlineModelConverter):
    inline_field_list_type = ExtraDataInlineFormList

    def _calculate_mapping_key_pair(self, model, info):
        return (models.LocationSet.extra_fields.key, models.LocationDataField.location_set.key)


class ParticipantExtraDataModelConverter(InlineModelConverter):
    inline_field_list_type = ExtraDataInlineFormList

    def _calculate_mapping_key_pair(self, model, info):
        return (models.ParticipantSet.extra_fields.key, models.ParticipantDataField.participant_set.key)


class DeploymentAdminView(BaseAdminView):
    can_create = False
    can_delete = False
    can_edit = True
    column_list = ("name", "hostnames")
    column_default_sort = [(models.Deployment.id, True)]
    column_labels = {
        "name": _("Name"),
        "hostnames": _("Hostnames"),
        "allow_observer_submission_edit": _("Allow Observer Submission Edit"),
        "dashboard_full_locations": _("Dashboard Full Locations"),
        "enable_partial_response_for_messages": _("Enable Partial Response For Messages"),  # noqa
    }
    column_descriptions = {
        "allow_observer_submission_edit": _("Specifies whether to allow the editing of observer data."),  # noqa
        "dashboard_full_locations": _(
            "Specifies whether to show all locations at an administrative division on the dashboard."
        ),  # noqa
        "enable_partial_response_for_messages": _(
            "Specifies whether to respond with an error when a partial response is received for a text message."
        ),  # noqa
        "hostnames": _(
            'An example of a hostname is "example.com". Used internally to determine what deployment to access.'
        ),  # noqa
    }
    form_edit_rules = [
        rules.FieldSet(
            (
                "name",
                "allow_observer_submission_edit",
                "dashboard_full_locations",
                "enable_partial_response_for_messages",
                "hostnames",
                "primary_locale",
                "other_locales",
                "brand_image",
                "remove_brand",
            ),
            _("Deployment"),
        )
    ]
    form_overrides = {"hostnames": MultipleSelect2Field}
    form_args = {"hostnames": {"render_kw": {"multiple": "multiple", "data-role": "select2-tags"}, "choices": []}}

    form_columns = [
        "name",
        "hostnames",
        "dashboard_full_locations",
        "enable_partial_response_for_messages",
        "allow_observer_submission_edit",
        "primary_locale",
        "other_locales",
    ]

    def _on_model_change(self, form, model, is_created):
        # strip out the empty strings
        if model.primary_locale == "":
            model.primary_locale = None
        model.other_locales = [loc for loc in model.other_locales if loc != ""]

        if hasattr(form, "remove_brand") and form.remove_brand.data:
            model.brand_image = None
            return

        if not form.brand_image.data:
            return

        logo_file = form.brand_image.data
        logo_bytes = logo_file.read()
        if len(logo_bytes) == 0:
            deployment = models.Deployment.query.filter(models.Deployment.id == model.id).first()
            if deployment is not None:
                model.brand_image = deployment.brand_image
        else:
            mimetype = magic.from_buffer(logo_bytes, mime=True)
            if not mimetype.startswith("image"):
                return

            logo_image = Image.open(BytesIO(logo_bytes))
            resized_logo = resize_image(logo_image, 300)
            with BytesIO() as buf:
                resized_logo.save(buf, format="PNG")
                contents = buf.getvalue()
                model.brand_image = contents

    def on_form_prefill(self, form, id):
        deployment = self.get_one(id)
        if deployment.brand_image is None:
            form.remove_brand.render_kw = {"disabled": True}
        return form

    def get_query(self):
        return models.Deployment.query.filter_by(id=current_user.deployment.id)

    def scaffold_form(self):
        form_class = super().scaffold_form()
        # not stripping the first item for the primary locale
        # because stripping it would mean the language is
        # set to the next item if nothing is selected and
        # the choice is saved. as at this moment, the next
        # item is the choice for Arabic
        form_class.primary_locale = SelectField(
            _("Primary Language"),
            choices=LANGUAGE_CHOICES,
            validators=[validators.optional()],
            description=_("Specifies the primary language that will be supported for data uploads."),
        )  # noqa
        # stripping the first item for the other locales
        # so that we avoid the "None is not a valid choice"
        # error when it is selected by mistake
        form_class.other_locales = SelectMultipleField(
            _("Other Languages"),
            choices=LANGUAGE_CHOICES,
            validators=[validators.optional()],
            description=_("Specifies other languages that will be supported for data uploads."),
        )  # noqa
        form_class.brand_image = FileField(_("Logo"), description=_("Will be resized as necessary. SVG not supported."))
        form_class.remove_brand = BooleanField(_("Remove logo"))

        return form_class


class EventAdminView(HiddenObjectMixin, BaseAdminView):
    column_filters = ("name", "start", "end")
    column_list = ("name", "start", "end", "location_set", "participant_set", "archive")
    column_labels = {
        "name": _("Name"),
        "start": _("Start"),
        "end": _("End"),
        "location_set": _("Location Set"),
        "participant_set": _("Participant Set"),
        "archive": _("Archive"),
    }
    column_descriptions = {
        "start": _("What time the event is to start in the local time."),
        "end": _("What time the event is to end in the local time."),
        "forms": _("What forms should be enabled for this event."),
    }
    column_default_sort = [(models.Event.start, True)]
    form_columns = ("name", "start", "end", "forms", "participant_set")
    form_rules = [rules.FieldSet(("name", "start", "end", "forms", "participant_set"), _("Event"))]
    column_formatters = {
        "archive": macro("event_archive"),
    }

    @action("hide", _("Archive"), _("Are you sure you want to archive the selected items?"))
    def action_hide(self, ids):
        model_class = self.model
        model_class.query.filter(model_class.resource_id.in_(ids)).update(
            {"is_hidden": True}, synchronize_session=False
        )
        db.session.commit()

    @action("unhide", _("Unarchive"), _("Are you sure you want to unarchive the selected items?"))
    def action_unhide(self, ids):
        model_class = self.model
        model_class.query.filter(model_class.resource_id.in_(ids)).update(
            {"is_hidden": False}, synchronize_session=False
        )
        db.session.commit()

    @expose("/download/<int:event_id>")
    def download(self, event_id):
        event = services.events.find(id=event_id).first_or_404()
        eas = EventArchiveSerializer()

        fp = BytesIO()
        with ZipFile(fp, "w", ZIP_DEFLATED) as zf:
            eas.serialize(event, zf)

        fp.seek(0)
        fname = slugify(f'event archive {event.name.lower()} {datetime.utcnow().strftime("%Y %m %d %H%M%S")}')  # noqa

        return send_file(fp, attachment_filename=f"{fname}.zip", as_attachment=True)

    def get_one(self, pk):
        model_class = self.model
        event = self.get_query().filter(model_class.resource_id == pk).one()

        # convert start and end dates to app time zone
        event.start = event.start.astimezone(app_time_zone)
        event.end = event.end.astimezone(app_time_zone)
        return event

    @pass_context
    def get_list_value(self, context, model, name):
        if name in ["start", "end"]:
            attribute = getattr(model, name, None)
            if attribute:
                return attribute.astimezone(app_time_zone).strftime(DATETIME_FORMAT_SPEC)

            return attribute

        return super(EventAdminView, self).get_list_value(context, model, name)

    def get_query(self):
        """Returns the queryset of the objects to list."""
        user = current_user._get_current_object()
        return self.filter_hidden(models.Event.query.filter_by(deployment_id=user.deployment.id))

    def on_model_change(self, form, model, is_created):
        # if we're creating a new event, make sure to set the
        # deployment, since it won't appear in the form
        if is_created:
            model.deployment = current_user.deployment

            # add role permissions for this event
            roles = models.Role.query.filter_by(deployment=model.deployment).all()
            model.roles = roles

        if form.participant_set.data:
            model.location_set = form.participant_set.data.location_set
        else:
            model.location_set = None

        # convert to the app time zone
        model.start = app_time_zone.localize(model.start).astimezone(utc_time_zone)
        model.end = app_time_zone.localize(model.end).astimezone(utc_time_zone)

    def delete_model(self, model):
        event_count = models.Event.query.filter_by(deployment=current_user.deployment).count()

        if event_count > 1:
            return super().delete_model(model)
        else:
            message = str(_("You cannot delete the last remaining event"))
            flash(message, "danger")
            return False

    def create_form(self, obj=None):
        form = super().create_form(obj)
        form.forms.query_factory = _get_usable_forms
        form.participant_set.query_factory = _get_usable_participant_sets

        return form

    def edit_form(self, obj=None):
        form = super().edit_form(obj)
        form.forms.query_factory = _get_usable_forms
        form.participant_set.query_factory = _get_usable_participant_sets

        return form


class UserAdminView(BaseAdminView):
    """Users admin view.

    Thanks to mrjoes and this Flask-Admin issue:
    https://github.com/mrjoes/flask-admin/issues/173
    """

    column_list = ("email", "roles", "active")
    column_labels = {"roles": _("Roles"), "active": _("Active")}
    column_searchable_list = ("email",)
    column_descriptions = {
        "roles": _("What roles are assigned to this user."),
        "permissions": _("Explicitly specifies which non-role permissions (in addition) to assign to the user."),  # noqa
    }
    column_default_sort = [(models.User.email, False)]
    form_columns = ("email", "username", "active", "roles", "permissions", "locale")
    form_excluded_columns = (
        "password",
        "confirmed_at",
        "login_count",
        "last_login_ip",
        "last_login_at",
        "current_login_at",
        "deployment",
        "current_login_ip",
        "submission_comments",
    )
    form_rules = [rules.FieldSet(("email", "username", "password2", "active", "roles", "permissions", "locale"))]
    list_template = "admin/user_list.html"

    def get_query(self):
        user = current_user._get_current_object()
        return models.User.query.filter_by(deployment=user.deployment)

    def build_new_instance(self):
        # TODO: change this implementation to completely
        # use the datastore to generate the user
        user = super().build_new_instance()
        security.datastore.set_uniquifier(user)
        return user

    def on_model_change(self, form, model, is_created):
        if form.password2.data:
            model.password = hash_password(form.password2.data)
        if is_created:
            model.deployment = current_user.deployment
        if not form.locale.data:
            model.locale = None

    def create_form(self, obj=None):
        form = super().create_form(obj)

        deployment = current_user.deployment

        # local function helper
        def _get_deployment_roles():
            return models.Role.query.filter_by(deployment_id=deployment.id)

        form.roles.query_factory = _get_deployment_roles

        return form

    def edit_form(self, obj=None):
        form = super().edit_form(obj)

        deployment = current_user.deployment

        # local function helper
        def _get_deployment_roles():
            return models.Role.query.filter_by(deployment_id=deployment.id)

        form.roles.query_factory = _get_deployment_roles

        return form

    def scaffold_form(self):
        form_class = super(UserAdminView, self).scaffold_form()
        form_class.password2 = PasswordField(_("New Password"))
        form_class.locale = SelectField(
            _("Language"), choices=LANGUAGE_CHOICES, description=_("Specifies the user's default language.")
        )
        return form_class

    @action("disable", _("Disable"), _("Are you sure you want to disable the selected users?"))
    def action_disable(self, ids):
        for role in models.User.query.filter(models.User.id.in_(ids)):
            role.active = False
            role.save()
        flash(str(_("User(s) successfully disabled.")), "success")

    @action("enable", _("Enable"), _("Are you sure you want to enable the selected users?"))
    def action_enable(self, ids):
        for role in models.User.query.filter(models.User.id.in_(ids)):
            role.active = True
            role.save()
        flash(str(_("User(s) successfully enabled.")), "success")

    @expose("/user-import", methods=["POST"])
    def import_users(self):
        return user_views.import_users()

    @expose("/user-import-headers/<int:upload_id>", methods=["GET", "POST"])
    def import_headers(self, upload_id: int):
        return user_views.import_headers(upload_id)


class RoleAdminView(BaseAdminView):
    can_delete = False
    column_list = ("name", "description")
    column_labels = {"name": _("Name"), "description": _("Description")}
    column_descriptions = {
        "description": _("Optional description for the role."),
        "permissions": _("Specifies which permissions to assign to users with this role."),  # noqa
        "resources": _("Specifies which resources (e.g. forms) to allow users with this role to access."),  # noqa
    }
    column_default_sort = [(models.Role.name, False)]
    form_columns = ("name", "description", "permissions", "resources")

    def create_form(self, obj=None):
        form = super().create_form(obj)
        form.resources.query_factory = _get_usable_resources

        return form

    def edit_form(self, obj=None):
        form = super().edit_form(obj)
        form.resources.query_factory = _get_usable_resources

        return form

    def get_one(self, pk):
        deployment = current_user.deployment
        return models.Role.query.filter_by(deployment_id=deployment.id, id=pk).first_or_404()

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.deployment = current_user.deployment
        super().on_model_change(form, model, is_created)


class SetViewMixin(HiddenObjectMixin):
    column_filters = ("name",)

    def on_model_change(self, form, model, is_created):
        if is_created:
            model.deployment_id = current_user.deployment.id
        super().on_model_change(form, model, is_created)


class LocationSetAdminView(SetViewMixin, BaseAdminView):
    column_list = ("name", "administrative_divisions", "locations")
    column_labels = {
        "name": _("Name"),
        "administrative_divisions": _("Administrative Divisions"),
        "locations": _("Location Data"),
    }
    column_formatters = {
        "administrative_divisions": macro("locations_builder"),
        "locations": macro("locations_list"),
    }
    column_default_sort = [(models.LocationSet.name, False)]
    form_columns = ("name",)
    inline_models = (ExtraDataInlineFormAdmin(models.LocationDataField),)
    inline_model_form_converter = LocationExtraDataModelConverter

    def on_model_delete(self, model):
        # delete dependent objects first
        models.LocationPath.query.filter_by(location_set=model).delete()
        models.Location.query.filter_by(location_set=model).delete()
        models.LocationTypePath.query.filter_by(location_set=model).delete()
        models.LocationType.query.filter_by(location_set=model).delete()
        return super().on_model_delete(model)

    @expose("/builder/<int:location_set_id>", methods=["GET", "POST"])
    def builder(self, location_set_id):
        return locations_builder(self, location_set_id)

    @expose("/builder/<int:location_set_id>/import", methods=["POST"])
    def import_divisions(self, location_set_id):
        return import_divisions(location_set_id)

    @expose("/builder/<int:location_set_id>/export")
    def export_divisions(self, location_set_id):
        return export_divisions(location_set_id)

    @expose("/locations/<int:location_set_id>")
    def locations_list(self, location_set_id):
        return locations_list(self, location_set_id)

    @expose("/locations/<int:location_set_id>/import", methods=["POST"])
    def locations_import(self, location_set_id):
        return locations_import(location_set_id)

    @expose("/locations/<int:location_set_id>/finalize", methods=["POST"])
    def locations_finalize(self, location_set_id):
        return finalize_location_set(location_set_id)

    @expose("/locations/<int:location_set_id>/headers/<int:upload_id>", methods=["GET", "POST"])
    def locations_headers(self, location_set_id, upload_id):
        return locations_headers(self, location_set_id, upload_id)

    @expose("/location/<int:id>", methods=["GET", "POST"])
    def location_edit(self, id):
        return location_edit(self, id)


class ParticipantSetAdminView(SetViewMixin, BaseAdminView):
    column_list = ("name", "location_set", "participants")
    column_labels = {
        "name": _("Name"),
        "location_set": _("Location Set"),
        "participants": _("Participants"),
        "gender_hidden": _("Hide Gender"),
        "role_hidden": _("Hide Role"),
        "partner_hidden": _("Hide Organization"),
    }
    column_descriptions = {
        "gender_hidden": _("If enabled, will hide the gender in the participant list."),  # noqa
        "role_hidden": _("If enabled, will hide the role in the participant list."),  # noqa
        "partner_hidden": _("If enabled, will hide the organization in the participant list."),  # noqa
    }
    column_default_sort = [(models.ParticipantSet.name, False)]
    form_columns = ("name", "location_set", "gender_hidden", "role_hidden", "partner_hidden")
    column_formatters = {"participants": macro("participants_list")}
    inline_models = (ExtraDataInlineFormAdmin(models.ParticipantDataField),)
    inline_model_form_converter = ParticipantExtraDataModelConverter

    def create_form(self, obj=None):
        form = super().create_form(obj)
        form.location_set.query_factory = _get_usable_location_sets

        return form

    def edit_form(self, obj=None):
        form = super().edit_form(obj)
        form.location_set.query_factory = _get_usable_location_sets

        return form

    def after_model_change(self, form, model, is_created):
        _role = models.ParticipantRole.query.filter(
            models.ParticipantRole.name == "$FC", models.ParticipantRole.participant_set == model
        ).first()
        if not _role:
            _role = models.ParticipantRole.create(name="$FC", participant_set=model)

    @expose("/participants/<int:participant_set_id>", methods=["GET", "POST"])
    def participants_list(self, participant_set_id):
        return participant_list(participant_set_id, self)

    @expose("/participants/<int:participant_set_id>/import", methods=["POST"])
    def participants_import(self, participant_set_id):
        return participant_list_import(participant_set_id)

    @expose("/participants/<int:participant_set_id>/headers/<int:upload_id>", methods=["GET", "POST"])
    def participants_headers(self, participant_set_id, upload_id):
        return participant_headers(upload_id, participant_set_id, self)

    @expose("/participant/<int:id>", methods=["GET", "POST"])
    def participant_edit(self, id):
        return participant_edit(id, self)


class FormsView(BaseView):
    def is_accessible(self):
        """For checking if the admin view is accessible."""
        if current_user.is_anonymous:
            return False

        deployment = current_user.deployment
        role = models.Role.query.filter_by(deployment_id=deployment.id, name="admin").first()
        return current_user.has_role(role)

    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for_security("login", next=request.url))

    @expose("/", methods=["GET", "POST"])
    def index(self):
        return forms_list(self)

    @expose("/<int:form_id>/export")
    def form_export(self, form_id):
        return export_form(form_id)

    @expose("/new", methods=["GET", "POST"])
    def create_form(self):
        return new_form(self)

    @expose("/<int:form_id>", methods=["GET", "POST"])
    def edit_form(self, form_id):
        return edit_form(self, form_id)

    @expose("/import", methods=["POST"])
    def import_form(self):
        return import_form_schema()

    @expose("/builder/<int:form_id>", methods=["GET", "POST"])
    def builder(self, form_id):
        return form_builder(self, form_id)

    @expose("/init", methods=["POST"])
    def init(self):
        return checklist_init()

    @expose("/survey_init", methods=["POST"])
    def init_surveys(self):
        return survey_init()

    @expose("/qa/<int:form_id>", methods=["GET"])
    def qc(self, form_id):
        return quality_controls(self, form_id)

    @expose("/qa/<int:form_id>/<string:qa>", methods=["GET", "POST", "DELETE"])
    def quality_control_edit(self, form_id, qa):
        if request.method == "DELETE":
            return quality_control_delete(self, form_id, qa)
        else:
            return quality_control_edit(self, form_id, qa)

    @expose("/qa/<int:form_id>/new", methods=["GET", "POST"])
    def quality_control_add(self, form_id):
        return quality_control_edit(self, form_id)

    @expose("/qa/ordering/<int:form_id>", methods=["POST"])
    def quality_control_ordering(self, form_id):
        return sort_quality_controls(self, form_id)


class TaskView(BaseView):
    @expose("/")
    @login_required
    @roles_required("admin")
    def index(self):
        context = {"channel": session.get("_id")}
        template_name = "admin/tasks.html"

        return self.render(template_name, **context)


admin.add_view(EventAdminView(models.Event, db.session, _("Events")))
admin.add_view(LocationSetAdminView(models.LocationSet, db.session, _("Location Sets")))
admin.add_view(ParticipantSetAdminView(models.ParticipantSet, db.session, _("Participant Sets")))
admin.add_view(FormsView(name=_("Forms")))
admin.add_view(UserAdminView(models.User, db.session, _("Users")))
admin.add_view(RoleAdminView(models.Role, db.session, _("Roles")))
admin.add_view(DeploymentAdminView(models.Deployment, db.session, _("Deployment")))
admin.add_view(TaskView(name=_("Tasks")))
