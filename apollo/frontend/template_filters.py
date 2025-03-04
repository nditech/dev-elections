# -*- coding: utf-8 -*-
import calendar
import re
from datetime import date, datetime

import numpy as np
import pandas as pd
from babel.numbers import format_number
from flask_babel import get_locale
from flask_babel import gettext as _
from geoalchemy2.shape import to_shape
from markupsafe import Markup

from apollo.process_analysis.common import generate_field_stats
from apollo.submissions.qa.query_builder import qa_status as _qa_status


def _clean(fieldname):
    """Returns a sanitized fieldname."""
    return re.sub(r"[^A-Z]", "", fieldname, flags=re.I)


def checklist_question_summary(form, field, location, dataframe):
    """Compute Urban/Rural statistics."""
    stats = {"urban": {}}
    stats.update(generate_field_stats(field, dataframe))

    try:
        for name, grp in dataframe.groupby("urban"):
            stats["urban"]["Urban" if name else "Rural"] = generate_field_stats(field, grp)
    except KeyError:
        pass

    return {"form": form, "location": location, "field": field, "stats": stats}


def get_location_for_type(submission, location_type, display_type=False):
    """Formatting for a location."""
    location = submission.location.make_path().get(location_type.name)

    if display_type:
        return Markup('{} &middot; <em class="muted">{}</em>').format(location, location_type.name) if location else ""
    else:
        return location if location else ""


def gen_page_list(pager, window_size=10):
    """Utility function for generating a list of pages numbers from a pager.

    Shamelessly ripped from django-bootstrap-pagination.
    """
    if window_size > pager.pages:
        window_size = pager.pages
    window_size -= 1
    start = max(pager.page - (window_size // 2), 1)
    end = min(pager.page + (window_size // 2), pager.pages)

    diff = end - start
    if diff < window_size:
        shift = window_size - diff
        if (start - shift) > 0:
            start -= shift
        else:
            end += shift
    return list(range(start, end + 1))


def percent_of(a, b, default=None):
    """Returns the percentage value of a fraction a/b."""
    a_ = float(a if (a and not (np.isinf(a) or np.isnan(a))) else 0)
    b_ = float(b if b else 0)
    try:
        return (a_ / b_) * 100
    except ZeroDivisionError:
        return default or 0


def mean_filter(value):
    """Return the rounded value or output a N/A."""
    if pd.isnull(value):
        return _("N/A")
    else:
        return int(round(value))


def mkunixtimestamp(dt):
    """Creates a unix timestamp from a datetime."""
    if type(dt) is datetime:
        return calendar.timegm(dt.utctimetuple())
    elif type(dt) is date:
        return calendar.timegm(datetime.combine(dt, datetime.min.time()).utctimetuple())
    else:
        return calendar.timegm(datetime.min.utctimetuple())


def number_format(number):
    """Return the number format based on the locale."""
    locale = get_locale()
    if locale is None:
        return format_number(number)
    return format_number(number, locale)


def reverse_dict(d):
    """Reverse the key -> value mapping in a dict."""
    return {v: k for k, v in list(d.items())}


def qa_status(submission, check):
    """Return the QA status."""
    return _qa_status(submission, check)


def longitude(geom):
    """Returns the longitudinal component of a geometrical point."""
    return to_shape(geom).x


def latitude(geom):
    """Returns the latitudinal component of a geometrical point."""
    return to_shape(geom).y
