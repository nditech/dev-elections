# -*- coding: utf-8 -*-
import csv
from datetime import datetime
from io import StringIO

from flask_babel import gettext as _

from apollo import constants
from apollo.dal.service import Service
from apollo.messaging.models import Message


class MessageService(Service):
    __model__ = Message

    def log_message(self, event, direction, text, recipient="", sender="", timestamp=None, message_type="SMS"):
        if timestamp:
            try:
                msg_time = datetime.utcfromtimestamp(timestamp)
            except ValueError:
                msg_time = datetime.utcnow()
        else:
            msg_time = datetime.utcnow()

        return self.create(
            direction=direction,
            recipient=recipient,
            sender=sender,
            text=text,
            deployment_id=event.deployment_id,
            event=event,
            received=msg_time,
            message_type=message_type,
        )

    def export_list(self, query):
        headers = [_("Mobile"), _("Text"), _("Direction"), _("Created"), _("Delivered"), _("Type")]
        output = StringIO()
        output.write(constants.BOM_UTF8_STR)
        writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC)
        writer.writerow([str(i) for i in headers])
        yield output.getvalue()
        output.close()

        for message in query:
            # limit to three numbers for export and pad if less than three
            record = [
                message.sender if message.direction == "IN" else message.recipient,
                message.text,
                message.direction.code,
                message.received.strftime("%Y-%m-%d %H:%M:%S") if message.received else "",
                message.delivered.strftime("%Y-%m-%d %H:%M:%S") if message.delivered else "",
                message.message_type.value,
            ]

            output = StringIO()
            writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC)
            writer.writerow([str(i) for i in record])
            yield output.getvalue()
            output.close()
