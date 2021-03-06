# Copyright (c) 2012 VMware, Inc.
# Copyright (c) 2011 Citrix Systems, Inc.
# Copyright 2011 OpenStack Foundation
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

"""
Utility functions for ESX Networking.
"""
from oslo.vmware import exceptions as vexc

from nova import exception
from nova.i18n import _
from nova.openstack.common import log as logging
from nova.virt.vmwareapi import vim_util
from nova.virt.vmwareapi import vm_util

LOG = logging.getLogger(__name__)


def get_network_with_the_name(session, network_name="vmnet0", cluster=None):
    """Gets reference to the network whose name is passed as the
    argument.
    """
    host = vm_util.get_host_ref(session, cluster)
    if cluster is not None:
        vm_networks_ret = session._call_method(vim_util,
                                               "get_dynamic_property", cluster,
                                               "ClusterComputeResource",
                                               "network")
    else:
        vm_networks_ret = session._call_method(vim_util,
                                               "get_dynamic_property", host,
                                               "HostSystem", "network")

    # Meaning there are no networks on the host. suds responds with a ""
    # in the parent property field rather than a [] in the
    # ManagedObjectReference property field of the parent
    if not vm_networks_ret:
        LOG.debug("No networks configured on host!")
        return
    vm_networks = vm_networks_ret.ManagedObjectReference
    network_obj = {}
    LOG.debug("Configured networks: %s", vm_networks)
    for network in vm_networks:
        # Get network properties
        if network._type == 'DistributedVirtualPortgroup':
            props = session._call_method(vim_util,
                                         "get_dynamic_property", network,
                                         "DistributedVirtualPortgroup",
                                         "config")
            # NOTE(asomya): This only works on ESXi if the port binding is
            # set to ephemeral
            # For a VLAN the network name will be the UUID. For a VXLAN
            # network this will have a VXLAN prefix and then the network name.
            if network_name in props.name:
                network_obj['type'] = 'DistributedVirtualPortgroup'
                network_obj['dvpg'] = props.key
                dvs_props = \
                    session._call_method(vim_util,
                                         "get_dynamic_property",
                                         props.distributedVirtualSwitch,
                                         "VmwareDistributedVirtualSwitch",
                                         "uuid")
                network_obj['dvsw'] = dvs_props
        else:
            props = session._call_method(vim_util,
                                         "get_dynamic_property", network,
                                         "Network", "summary.name")
            if props == network_name:
                network_obj['type'] = 'Network'
                network_obj['name'] = network_name
    if (len(network_obj) > 0):
        return network_obj
    LOG.debug("Network %s not found on host!", network_name)


def get_vswitch_for_vlan_interface(session, vlan_interface, cluster=None):
    """Gets the vswitch associated with the physical network adapter
    with the name supplied.
    """
    # Get the list of vSwicthes on the Host System
    host_mor = vm_util.get_host_ref(session, cluster)
    vswitches_ret = session._call_method(vim_util,
                                         "get_dynamic_property", host_mor,
                                         "HostSystem",
                                         "config.network.vswitch")
    # Meaning there are no vSwitches on the host. Shouldn't be the case,
    # but just doing code check
    if not vswitches_ret:
        return
    vswitches = vswitches_ret.HostVirtualSwitch
    # Get the vSwitch associated with the network adapter
    for elem in vswitches:
        try:
            for nic_elem in elem.pnic:
                if str(nic_elem).split('-')[-1].find(vlan_interface) != -1:
                    return elem.name
        # Catching Attribute error as a vSwitch may not be associated with a
        # physical NIC.
        except AttributeError:
            pass


def check_if_vlan_interface_exists(session, vlan_interface, cluster=None):
    """Checks if the vlan_interface exists on the esx host."""
    host_mor = vm_util.get_host_ref(session, cluster)
    physical_nics_ret = session._call_method(vim_util,
                                             "get_dynamic_property",
                                             host_mor,
                                             "HostSystem",
                                             "config.network.pnic")
    # Meaning there are no physical nics on the host
    if not physical_nics_ret:
        return False
    physical_nics = physical_nics_ret.PhysicalNic
    for pnic in physical_nics:
        if vlan_interface == pnic.device:
            return True
    return False


def get_vlanid_and_vswitch_for_portgroup(session, pg_name, cluster=None):
    """Get the vlan id and vswicth associated with the port group."""
    host_mor = vm_util.get_host_ref(session, cluster)
    port_grps_on_host_ret = session._call_method(vim_util,
                                                 "get_dynamic_property",
                                                 host_mor,
                                                 "HostSystem",
                                                 "config.network.portgroup")
    if not port_grps_on_host_ret:
        msg = _("ESX SOAP server returned an empty port group "
                "for the host system in its response")
        LOG.error(msg)
        raise exception.NovaException(msg)
    port_grps_on_host = port_grps_on_host_ret.HostPortGroup
    for p_gp in port_grps_on_host:
        if p_gp.spec.name == pg_name:
            p_grp_vswitch_name = p_gp.vswitch.split("-")[-1]
            return p_gp.spec.vlanId, p_grp_vswitch_name


def create_port_group(session, pg_name, vswitch_name, vlan_id=0, cluster=None):
    """Creates a port group on the host system with the vlan tags
    supplied. VLAN id 0 means no vlan id association.
    """
    client_factory = session._get_vim().client.factory
    add_prt_grp_spec = vm_util.get_add_vswitch_port_group_spec(
        client_factory,
        vswitch_name,
        pg_name,
        vlan_id)
    host_mor = vm_util.get_host_ref(session, cluster)
    network_system_mor = session._call_method(vim_util,
                                              "get_dynamic_property", host_mor,
                                              "HostSystem",
                                              "configManager.networkSystem")
    LOG.debug("Creating Port Group with name %s on "
              "the ESX host", pg_name)
    try:
        session._call_method(session._get_vim(),
                             "AddPortGroup", network_system_mor,
                             portgrp=add_prt_grp_spec)
    except vexc.AlreadyExistsException:
        # There can be a race condition when two instances try
        # adding port groups at the same time. One succeeds, then
        # the other one will get an exception. Since we are
        # concerned with the port group being created, which is done
        # by the other call, we can ignore the exception.
        LOG.debug("Port Group %s already exists.", pg_name)
    LOG.debug("Created Port Group with name %s on "
              "the ESX host", pg_name)


def create_dvportgroup_task(
        session, pg_name, dvs_name, vlan_id=0, cluster=None):
    """Creates a dv port group on the dvswitch with the vlan tags
    supplied. VLAN id 0 means no vlan id association.
    """

    LOG.debug("Creating dv Port Group with name %s on "
              "the dvs %s", pg_name, dvs_name)

    dvs_ref = session._call_method(
        vim_util,
        "get_objects",
        "DistributedVirtualSwitch")

    dvs = _get_dvsmor(dvs_ref, dvs_name)
    client_factory = session._get_vim().client.factory
    config_spec = client_factory.create('ns0:DVPortgroupConfigSpec')
    port_config_spec = client_factory.create('ns0:VMwareDVSPortSetting')
    vlan_spec = client_factory.create(
        'ns0:VmwareDistributedVirtualSwitchVlanIdSpec')
    vlan_spec.vlanId = vlan_id
    vlan_spec.inherited = False
    port_config_spec.vlan = vlan_spec
    config_spec.name = pg_name
    config_spec.numPorts = 10
    config_spec.type = 'earlyBinding'
    config_spec.defaultPortConfig = port_config_spec

    try:
        pg_create_task = session._call_method(
            session._get_vim(),
            "CreateDVPortgroup_Task",
            dvs,
            spec=config_spec)
        task_info = session._wait_for_task(pg_create_task)
    except Exception as excep:
        # Nothing todo
        LOG.debug("Port Group %s already exists.", pg_name)


def delete_dvportgroup_task(session, pg_name, dvs_name, cluster=None):
    """Deletes a dv port group on the dvswitch with the vlan tags
    supplied.
    """

    dvpg_ref = _get_dvpg_ref_from_name(session, pg_name)

    try:
        LOG.debug("Destroying the dv Port Group %s", pg_name)
        destroy_task = session._call_method(
            session._get_vim(),
            "Destroy_Task", dvpg_ref)
        session._wait_for_task(destroy_task)
        LOG.debug("Destroyed the dv Port Group %s", pg_name)
    except Exception as excep:
        LOG.warn(_("In hybridvmwareapi:netowrk_util:delete_dvportgroup_task, "
                   "got this exception while destroying the dv pg: %s"), excep)


def _get_dvpg_ref_from_name(session, pg_name):
    """Get reference to the dvpg with the name specified."""
    dvpgs = session._call_method(vim_util, "get_objects",
                                 "DistributedVirtualPortgroup", ["name"])
    return _get_object_from_results(session, dvpgs, pg_name,
                                    _get_object_for_value)


def _get_object_from_results(session, results, value, func):
    while results:
        token = _get_token(results)
        object = func(results, value)
        if object:
            if token:
                session._call_method(vim_util,
                                     "cancel_retrieve",
                                     token)
            return object

        if token:
            results = session._call_method(vim_util,
                                           "continue_to_get_objects",
                                           token)
        else:
            return None


def _get_object_for_value(results, value):
    for object in results.objects:
        if object.propSet[0].val == value:
            return object.obj


def _get_token(results):
    """Get the token from the property results."""
    return getattr(results, 'token', None)


def _get_dvsmor(dvs_ref, dvs_name):
    """
    :param dvs_ref:
    :param dvs_name:
    :return:
    """
    for dvs in dvs_ref.objects:
        prop_sets = dvs.propSet
        for prop in prop_sets:
            if prop.name == 'name' and prop.val == dvs_name:
                return dvs.obj

    # find nothing
    return None
