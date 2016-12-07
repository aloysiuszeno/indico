# This file is part of Indico.
# Copyright (C) 2002 - 2016 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from flask import session

from indico.core import signals
from indico.core.logger import Logger
from indico.core.roles import ManagementRole
from indico.modules.events.features.base import EventFeature
from indico.modules.events.models.events import EventType, Event
from indico.util.i18n import _
from indico.web.flask.util import url_for
from indico.web.menu import SideMenuItem

logger = Logger.get('events.papers')


@signals.menu.items.connect_via('event-management-sidemenu')
def _extend_event_management_menu(sender, event, **kwargs):
    if not event.can_manage(session.user, role='paper_manager') or not PapersFeature.is_allowed_for_event(event):
        return
    return SideMenuItem('papers', _('Call for Papers'), url_for('papers.management', event),
                        section='organization')


@signals.event_management.management_url.connect
def _get_event_management_url(event, **kwargs):
    if event.can_manage(session.user, role='paper_manager'):
        return url_for('papers.management', event)


@signals.event.get_feature_definitions.connect
def _get_feature_definitions(sender, **kwargs):
    return PapersFeature


@signals.acl.get_management_roles.connect_via(Event)
def _get_management_roles(sender, **kwargs):
    return PaperManagerRole


class PapersFeature(EventFeature):
    name = 'papers'
    friendly_name = _('Call for Papers')
    description = _('Gives event managers the opportunity to open a "Call for Papers" and use the paper '
                    'reviewing workflow.')

    @classmethod
    def is_allowed_for_event(cls, event):
        return event.type_ == EventType.conference


class PaperManagerRole(ManagementRole):
    name = 'paper_manager'
    friendly_name = _('Paper Manager')
    description = _('Grants management rights for paper reviewing on an event.')