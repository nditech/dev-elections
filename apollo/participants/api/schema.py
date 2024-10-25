# -*- coding: utf-8 -*-
import marshmallow as ma

from apollo.api.schema import BaseModelSchema
from apollo.participants.models import Participant


class ParticipantSchema(BaseModelSchema):
    role = ma.fields.Method("get_role")

    class Meta:
        """ParticipantSchema Meta."""

        model = Participant
        fields = (
            "id",
            "name",
            "full_name",
            "first_name",
            "other_names",
            "last_name",
            "participant_id",
            "role",
            "location.id",
            "location.name",
            "location.location_type.name",
        )

    def get_role(self, obj):
        return obj.role.name
