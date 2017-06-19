# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_log import log
from six import moves
import sqlalchemy as sa

from neutron._i18n import _LE, _LI, _LW
from neutron.db import api as db_api
from neutron.plugins.common import constants as p_const
from neutron.plugins.common import utils as plugin_utils
from neutron.plugins.ml2.drivers import helpers
from neutron_lib.db import model_base
from neutron_lib import exceptions as exc

LOG = log.getLogger(__name__)


class L3OutVlanAllocation(model_base.BASEV2):
    """Represent allocation state of a vlan_id for the L3 out per VRF.

    If allocated is False, the vlan_id is available for allocation.
    If allocated is True, the vlan_id is in use.

    When an allocation is released, if the vlan_id is inside the pool
    described by network_vlan_ranges, then allocated is set to
    False. If it is outside the pool, the record is deleted.
    """

    __tablename__ = 'apic_ml2_l3out_vlan_allocation'
    __table_args__ = (
        sa.Index('apic_ml2_l3out_vlan_allocation_l3out_network_allocated',
                 'l3out_network', 'allocated'),
        model_base.BASEV2.__table_args__,)

    l3out_network = sa.Column(sa.String(64), nullable=False,
                              primary_key=True)
    vrf = sa.Column(sa.String(64), nullable=False,
                    primary_key=False)
    vlan_id = sa.Column(sa.Integer, nullable=False, primary_key=True,
                        autoincrement=False)
    allocated = sa.Column(sa.Boolean, nullable=False)


class NoVlanAvailable(exc.ResourceExhausted):
    message = _("Unable to allocate the vlan. "
                "No vlan is available for %(l3out_network)s external network")


# inherit from SegmentTypeDriver to reuse the code to reserve/release
# vlan IDs from the pool
class L3outVlanAlloc(helpers.SegmentTypeDriver):

    def __init__(self):
        super(L3outVlanAlloc, self).__init__(L3OutVlanAllocation)
        self.session = db_api.get_session()

    def _parse_vlan_ranges(self, ext_net_dict):
        self.l3out_vlan_ranges = {}
        for l3out_network in ext_net_dict.keys():
            try:
                ext_info = ext_net_dict.get(l3out_network)
                vlan_ranges_str = ext_info.get('vlan_range')
                if vlan_ranges_str:
                    vlan_ranges = vlan_ranges_str.strip().split(',')
                    for vlan_range_str in vlan_ranges:
                        vlan_min, vlan_max = vlan_range_str.strip().split(':')
                        vlan_range = (int(vlan_min), int(vlan_max))
                        plugin_utils.verify_vlan_range(vlan_range)
                        self.l3out_vlan_ranges.setdefault(
                            l3out_network, []).append(vlan_range)
            except Exception:
                LOG.exception(_LE("Failed to parse vlan_range for L3out %s"),
                              l3out_network)

        LOG.info(_LI("L3out VLAN ranges: %s"), self.l3out_vlan_ranges)

    def sync_vlan_allocations(self, ext_net_dict):
        self._parse_vlan_ranges(ext_net_dict)
        with self.session.begin(subtransactions=True):
            # get existing allocations for all L3 out networks
            allocations = dict()
            allocs = (self.session.query(L3OutVlanAllocation).
                      with_lockmode('update'))
            for alloc in allocs:
                if alloc.l3out_network not in allocations:
                    allocations[alloc.l3out_network] = set()
                allocations[alloc.l3out_network].add(alloc)

            # process vlan ranges for each configured l3out network
            for (l3out_network,
                 vlan_ranges) in self.l3out_vlan_ranges.items():
                # determine current configured allocatable vlans for
                # this l3out network
                vlan_ids = set()
                for vlan_min, vlan_max in vlan_ranges:
                    vlan_ids |= set(moves.xrange(vlan_min, vlan_max + 1))
                # remove from table unallocated vlans not currently
                # allocatable
                if l3out_network in allocations:
                    for alloc in allocations[l3out_network]:
                        try:
                            # see if vlan is allocatable
                            vlan_ids.remove(alloc.vlan_id)
                        except KeyError:
                            # it's not allocatable, so check if its allocated
                            if not alloc.allocated:
                                # it's not, so remove it from table
                                LOG.debug("Removing vlan %(vlan_id)s on "
                                          "l3out network "
                                          "%(l3out_network)s from pool",
                                          {'vlan_id': alloc.vlan_id,
                                           'l3out_network':
                                           l3out_network})
                                self.session.delete(alloc)
                    del allocations[l3out_network]

                # add missing allocatable vlans to table
                for vlan_id in sorted(vlan_ids):
                    alloc = L3OutVlanAllocation(l3out_network=l3out_network,
                                                vrf='',
                                                vlan_id=vlan_id,
                                                allocated=False)
                    self.session.add(alloc)

            # remove from table unallocated vlans for any unconfigured
            # l3out networks
            for allocs in allocations.itervalues():
                for alloc in allocs:
                    if not alloc.allocated:
                        LOG.debug("Removing vlan %(vlan_id)s on l3out "
                                  "network %(l3out_network)s from pool",
                                  {'vlan_id': alloc.vlan_id,
                                   'l3out_network':
                                   alloc.l3out_network})
                        self.session.delete(alloc)

    def get_type(self):
        return p_const.TYPE_VLAN

    def reserve_vlan(self, l3out_network, vrf, vrf_tenant=None):
        vrf_db = L3outVlanAlloc._get_vrf_name_db(vrf, vrf_tenant)
        with self.session.begin(subtransactions=True):
            query = (self.session.query(L3OutVlanAllocation).
                     filter_by(l3out_network=l3out_network,
                               vrf=vrf_db))
            count = query.update({"allocated": True})
            if count:
                LOG.debug("reserving vlan %(vlan_id)s for vrf "
                          "%(vrf)s on l3out network %(l3out_network)s from "
                          "pool. Totally %(count)s rows updated.",
                          {'vlan_id': query[0].vlan_id,
                           'vrf': vrf_db,
                           'l3out_network': l3out_network,
                           'count': count})
                return query[0].vlan_id

            # couldn't find this vrf, allocate vlan from the pool
            # then update the vrf field

            filters = {}
            filters['l3out_network'] = l3out_network
            alloc = self.allocate_partially_specified_segment(
                self.session, **filters)
            if not alloc:
                raise NoVlanAvailable(l3out_network=l3out_network)

            filters['vlan_id'] = alloc.vlan_id
            query = (self.session.query(L3OutVlanAllocation).
                     filter_by(allocated=True, **filters))
            count = query.update({"vrf": vrf_db})
            if count:
                LOG.debug("updating vrf %(vrf)s vlan "
                          "%(vlan_id)s on l3out network %(l3out_network)s to "
                          "pool. Totally %(count)s rows updated.",
                          {'vrf': vrf_db,
                           'vlan_id': alloc.vlan_id,
                           'l3out_network': l3out_network,
                           'count': count})

            LOG.debug("reserving vlan %(vlan_id)s "
                      "on l3out network %(l3out_network)s from pool",
                      {'vlan_id': alloc.vlan_id,
                       'l3out_network': l3out_network})
            return alloc.vlan_id

    def release_vlan(self, l3out_network, vrf, vrf_tenant=None):
        vrf_db = L3outVlanAlloc._get_vrf_name_db(vrf, vrf_tenant)
        with self.session.begin(subtransactions=True):
            query = (self.session.query(L3OutVlanAllocation).
                     filter_by(l3out_network=l3out_network,
                               vrf=vrf_db))
            count = query.update({"allocated": False})
            if count:
                LOG.debug("Releasing vlan %(vlan_id)s on l3out "
                          "network %(l3out_network)s to pool. "
                          "Totally %(count)s rows updated.",
                          {'vlan_id': query[0].vlan_id,
                           'l3out_network': l3out_network,
                           'count': count})
                return

        LOG.warning(_LW("No vlan_id found for vrf %(vrf)s on l3out "
                        "network %(l3out_network)s"),
                    {'vrf': vrf_db,
                     'l3out_network': l3out_network})

    # None is returned if not found
    @staticmethod
    def get_vlan_allocated(l3out_network, vrf, vrf_tenant=None):
        session = db_api.get_session()
        query = (session.query(L3OutVlanAllocation).
                 filter_by(l3out_network=l3out_network,
                           vrf=L3outVlanAlloc._get_vrf_name_db(
                               vrf, vrf_tenant),
                           allocated=True))
        if query.count() > 0:
            return query[0].vlan_id

    @staticmethod
    def _get_vrf_name_db(vrf, vrf_tenant):
        return vrf_tenant and ("%s/%s" % (vrf_tenant, vrf)) or vrf

    def initialize(self):
        return

    def is_partial_segment(self, segment):
        return True

    def validate_provider_segment(self, segment):
        return

    def reserve_provider_segment(self, session, segment):
        return

    def allocate_tenant_segment(self, session):
        return

    def release_segment(self, session, segment):
        return
