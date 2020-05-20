#! /usr/bin/python3

# Copyright (c) 2019-2020 AT&T Intellectual Property.
# All rights reserved.
# SPDX-License-Identifier: GPL-2.0-only
"""
Vyatta VCI component to configure keepalived to provide VRRP functionality
"""

import contextlib
import json
import logging
import os
from enum import Enum
from typing import Any, Dict, List, Tuple, Union

import pydbus

import vyatta.abstract_vrrp_classes as AbstractConfig
import vyatta.keepalived.dbus.vrrp_group_connection as vrrp_dbus
import vyatta.keepalived.util as util
import vyatta.keepalived.vrrp as vrrp


class KeepalivedConfig(AbstractConfig.ConfigFile):
    """
    Implementation to convert vyatta YANG to Keepalived configuration

    This file is the concrete implementation of the VRRP abstract class
    ConfigFile for the Keepalived implementation of VRRP. It is to be used
    by the Vyatta VCI infrastructure to configure Keepalived. It does this
    by converting the YANG representation given to it into the Keepalived
    config format and writing it to the config file, it also converts
    from the config file format back to the YANG representation.

    The Vyatta VCI infrastructure sends, and expects to receive, configuration
    in a JSON 7951 format. For the simplest configured VRRP group the
    infrastructure will send the following JSON to the registered VCI unit
    which will then convert it to the shown Keepalived.conf file.

    Simplest config:
        interfaces {
            dataplane dp0p1s1 {
                address 10.10.1.1/24
                vrrp {
                    vrrp-group 1 {
                        virtual-address 10.10.1.100/25
                    }
                }
            }
        }

    VCI JSON 7951 format:
        {
            "vyatta-interfaces-v1:interfaces":{
                "vyatta-interfaces-dataplane-v1:dataplane":
                    [
                        {
                            "tagnode":"dp0p1s1",
                            "vyatta-vrrp-v1:vrrp": {
                                "start-delay": 0,
                                "vrrp-group":[
                                    {
                                        "tagnode":1,
                                        "accept":false,
                                        "preempt":true,
                                        "version":2,
                                        "virtual-address":["10.10.1.100/25"]
                                    }
                                ]
                            }
                        }
                    ]
                }
        }

    Generated Keepalived.conf file:
        #
        # autogenerated by /opt/vyatta/sbin/vyatta-vrrp-vci
        #

        global_defs {
            enable_traps
            enable_dbus
            snmp_socket tcp:localhost:705:1
            enable_snmp_keepalived
            enable_snmp_rfc
        }
        vrrp_instance vyatta-dp0p1s1-1 {
            state BACKUP
            interface dp0p1s1
            virtual_router_id 1
            version 2
            start_delay 0
            priority 100
            advert_int 1
            virtual_ipaddress {
                10.10.1.100/25
            }
        }

    These are two very different formats and contain some curiosities from
    legacy implementations that need to be maintained. Converting from
    YANG to Keepalived.conf is relatively easily with most of the work passed
    on to a VRRP group class that returns a string in the correct format.
    This class then writes all of the generated strings to the file.

    Converting from Keepalived.conf to YANG is more complex due to the flat
    structure of Keepalived.conf and the nested format of the YANG.
    The majority of this file is dedicated to this effort.

    Unit testing:
        See the README under vyatta-vrrp on how to set up the testing
        environment. If you're not familiar with pytest I recommend
        starting here https://docs.pytest.org/en/latest/example/simple.html
        once you have your environment set up.
        The test file for this class is
        vyatta-vrrp/tests/test_keepalived_config-file.py

    Acceptance testing:
        To be added.

    Regression testing:
        Use the VRRP developer regression setup, documentation for this can be
        found on the internal AT&T wiki.
    """

    def __init__(
            self,
            config_file_path: str = "/etc/keepalived/keepalived.conf"
    ) -> None:
        """
        KeepalivedConfig constructor

        Arguments:
            config_file_path (str):
                Path to where the keepalived config file path is, defaults
                to the standard file but can be overwritten.

        Attributes:
            config_string (str):
                String to write to the config file, contains some
                autogenerated text for global defines
            config_file (str):
                Internal string for the config file path
            implementation_name (str):
                Name of the implementation that provides the VRRP support.
            vrrp_instances:
                A list of VRRP group Objects that have been found in the
                config passed to this object
                Currently this is modified in the update call.
            vif_yang_name, vrrp_yang_name (str):
                Name of the YANG paths that are used for dictionary keys to
                avoid magic strings
        """

        self.log: logging.Logger = logging.getLogger("vyatta-vrrp-vci")
        self.config_string: str = """
#
# Autogenerated by /opt/vyatta/sbin/vyatta-vrrp
#


global_defs {
        enable_traps
        enable_dbus
        snmp_socket tcp:localhost:705:1
        enable_snmp_keepalived
        enable_snmp_rfc
}"""
        self.config_file: str = config_file_path
        self.implementation_name: str = "Keepalived"
        self._vrrp_instances: List[vrrp.VrrpGroup] = []
        self._sync_instances: Dict[str, List[str]] = {}
        self._rfc_interfaces: int = 0
        self._vrrp_connections: Dict[str, vrrp_dbus.VrrpConnection] = {}

    @property
    def vrrp_instances(self) -> List[vrrp.VrrpGroup]:
        return self._vrrp_instances

    @vrrp_instances.setter
    def vrrp_instances(self, new_value) -> None:
        self._vrrp_instances = new_value

    @property
    def vrrp_connections(self) -> Dict[str, vrrp_dbus.VrrpConnection]:
        return self._vrrp_connections

    def config_file_path(self) -> str:
        """Path to the keepalived config file"""
        return self.config_file

    def impl_name(self) -> str:
        """Name of the VRRP implementation"""
        return self.implementation_name

    def update(self, new_config: Dict) -> None:
        """
        Update the list of VRRP instances for the object

        Arguments:
            new_config (dictionary):
                A dictionary containing the new config passed from
                the infrastructure. Used to create VRRP group objects

        Create new VRRP group Objects from the config passed in
        and replace the vrrp_instances list with the new config
        """

        self._rfc_interfaces = 0
        self.vrrp_instances = []
        self._vrrp_connections = {}
        self._sync_instances = {}
        if util.INTERFACE_YANG_NAME not in new_config:
            return
        intf_types: Dict = new_config[util.INTERFACE_YANG_NAME]

        intf_type: str
        for intf_type in intf_types:
            intf: Dict
            for intf in intf_types[intf_type]:
                if "vif" in intf:
                    # As we've already sanitized the data and moved
                    # vif interfaces to their own intf_type if any
                    # exist in the interface config this is a problem.
                    raise ValueError(
                        "VIF interfaces shouldn't be present under" +
                        " another interface")

                intf_name: str = intf["tagnode"]
                vrrp_conf: Dict = intf[util.VRRP_YANG_NAME]
                if vrrp_conf["vrrp-group"] == []:
                    break
                start_delay: str = vrrp_conf["start-delay"]
                group: Dict
                for group in vrrp_conf["vrrp-group"]:
                    first_vip: str = group["virtual-address"][0]
                    if "/" in first_vip:
                        first_vip = first_vip.split("/")[0]
                    if "disable" in group:
                        continue
                    if "rfc-compatibility" in group:
                        self._rfc_interfaces += 1
                        self.vrrp_instances.append(
                            vrrp.VrrpGroup(
                                intf_name, start_delay, group,
                                self._rfc_interfaces))
                    else:
                        self.vrrp_instances.append(
                            vrrp.VrrpGroup(
                                intf_name, start_delay, group))
                    af_type: int = util.what_ip_version(
                        first_vip
                    )
                    connection: vrrp_dbus.VrrpConnection = \
                        vrrp_dbus.VrrpConnection(
                            intf_name, group["tagnode"],
                            af_type, pydbus.SystemBus()
                        )
                    instance_name: str = \
                        f"vyatta-{intf_name}-{group['tagnode']}"
                    self._vrrp_connections[instance_name] = \
                        connection
                    if "sync-group" in group:
                        sync_group_name: str = group["sync-group"]
                        if sync_group_name not in self._sync_instances:
                            self._sync_instances[sync_group_name] = []
                        self._sync_instances[sync_group_name].append(
                            self._vrrp_instances[-1].instance_name)

    def write_config(self) -> None:
        """
        Write config to the file at self.config_file

        Invoke the str method for this object and write it to the config
        file provided at instantiation. If there is a problem writing the
        file an error is thrown.
        """
        keepalived_config: str = self.config_string
        sync_group: str
        for sync_group in self._sync_instances:
            keepalived_config += f"""
vrrp_sync_group {sync_group} {{
    group {{"""
            instance: str
            for instance in self._sync_instances[sync_group]:
                keepalived_config += f"""
        {instance}"""
            keepalived_config += """
    }
}
"""
        group: vrrp.VrrpGroup
        for group in self.vrrp_instances:
            keepalived_config += str(group)
        with open(self.config_file, "w") as file_handle:
            file_handle.write(keepalived_config)

    def read_config(self) -> str:
        """Read config from file at config_file and return to caller"""
        config_string: str = ""
        with open(self.config_file, "r") as file_handle:
            config_string = file_handle.read()
        return config_string

    def convert_to_vci_format_dict(self, config_string: str) -> Dict:
        """
        Given a string of keepalived config convert to YANG format return
        it as a python dictionary

        Arguments:
            config_string:
                A string of Keepalived config, any string can be passed in but
                this string should have been retrieved using read_config
        Returns:
            A JSON string of the values found in the config string. This
            dictionary will be in the python format, before returning to the
            infrastructure it should be converted to JSON
        """
        config_lines: List[str] = config_string.splitlines()
        vrrp_group_start_indexes: List[int] = util.get_config_indexes(
            config_lines, "vrrp_instance")
        if vrrp_group_start_indexes == []:
            return {}

        sync_group_start_indexes: List[int] = util.get_config_indexes(
            config_lines, "vrrp_sync_group")
        sync_group_instances: Dict = {}
        if sync_group_start_indexes != []:
            sync_group_config: List[str] = util.get_config_blocks(
                config_lines[:vrrp_group_start_indexes[0]],
                sync_group_start_indexes
            )
            sync_group: str
            for sync_group in sync_group_config:
                group_name_exists: Tuple[bool, str] = \
                    util.find_config_value(sync_group, "vrrp_sync_group")
                if not group_name_exists[0]:
                    continue
                group_name: str = group_name_exists[1].split()[0]
                instance_start: int = sync_group.index("group {")
                instance_end: int = sync_group.index("}", instance_start)
                for instance in sync_group[instance_start + 1:instance_end]:
                    sync_group_instances[instance] = group_name

        group_config: List[List[str]] = util.get_config_blocks(
            config_lines, vrrp_group_start_indexes)

        yang_representation: Dict[str, Dict] = {
            util.INTERFACE_YANG_NAME: {}
        }
        group: List[str]
        for group in group_config:

            intf_name: str = util.find_config_value(
                group, "interface")[1]
            vrid: str = util.find_config_value(
                group, "virtual_router_id")[1]
            instance_name: str = f"vyatta-{intf_name}-{vrid}"

            if instance_name in sync_group_instances:
                group.append(
                    f"sync_group {sync_group_instances[instance_name]}"
                )
            vif_number: str = ""
            if "." in intf_name:
                vif_sep: List[str] = intf_name.split(".")
                intf_name = vif_sep[0]
                vif_number = vif_sep[1]

            interface_list: Dict[Any, Any] = \
                yang_representation[util.INTERFACE_YANG_NAME]
            # Find the interface type for the interface name, right now this
            # is just a guess, there might be a better method of doing this
            # than regexes
            intf_type: str = util.intf_name_to_type(intf_name)[0]
            if intf_type not in interface_list:
                interface_list[intf_type] = []
            interface_list = interface_list[intf_type]

            # Hackery to find the reference to the interface this vrrp
            # group should be added to.
            insertion_reference: List[Dict] = util.find_interface_in_yang_repr(
                intf_name, vif_number, interface_list)

            # All groups should have the same start delay but check and
            # store the largest delay found
            new_group_start_delay: str = \
                util.find_config_value(group, "start_delay")[1]
            current_start_delay: str = \
                insertion_reference[util.VRRP_YANG_NAME]["start-delay"]

            if new_group_start_delay != current_start_delay and \
               int(current_start_delay) < int(new_group_start_delay):
                insertion_reference[util.VRRP_YANG_NAME]["start-delay"] = \
                    new_group_start_delay

            insertion_reference[util.VRRP_YANG_NAME]["vrrp-group"].append(
                self._convert_keepalived_config_to_yang(group))
        return yang_representation

    def convert_to_vci_format(self, config_string: str) -> str:
        """
        Given a string of keepalived config convert to yang format

        Arguments:
            config_string:
                A string of keepalived config, any string can be passed in but
                this string should have been retrieved using read_config
        Returns:
            A JSON string of the values found in the config string.
        """

        yang_representation = self.convert_to_vci_format_dict(config_string)
        return json.dumps(yang_representation)

    def _convert_keepalived_config_to_yang(
            self,
            config_block: List[str]
    ) -> dict:
        """
        Converts a Keepalived VRRP block of config into YANG

        Arguments:
            config_block (List[str]):
                 The lines of VRRP config to be converted to the YANG
                 representation

        Return:
            A python dictionary representing the config found in the strings,
            N.B. the caller should convert this to JSON before sending it to
            the VCI infra
        """

        if config_block == []:
            return {}
        config_dict: Dict = {
            "accept": "accept",
            "preempt": "preempt",
            "priority": "priority",
            "tagnode": "virtual_router_id",
            "version": "version",
            "hello-source-address": "mcast_src_ip",
            "rfc-compatibility": "vmac_xmit_base",
            "advertise-interval": "advert_int",  # advert_int used for v2 & v3
            "preempt-delay": "preempt_delay",
            "sync-group": "sync_group"
        }

        # Single line config code
        key: str
        for key in config_dict:
            # Search for each term in the config
            config_exists: Tuple[bool, Union[List[None], str]] = \
                util.find_config_value(config_block, config_dict[key])
            if not config_exists[0]:
                # Accept and preempt are required defaults in the YANG called
                # out as a special case here if they don't explicitly exist in
                # the config block
                if key == "accept":
                    config_dict[key] = False
                elif key == "preempt":
                    config_dict[key] = True
                else:
                    config_dict[key] = config_exists[1]  # NOTFOUND
            elif isinstance(config_exists[1], list):
                # Term exists in config and is presence
                config_dict[key] = config_exists[1]
            elif config_exists[1].isdigit():
                # Term exists in config and has a numerical value
                config_dict[key] = int(config_exists[1])
            else:
                config_dict[key] = config_exists[1]

        # Remove defaults
        # TODO: Test what is currently returned for defaults
        # may need to put these back in
        if "advertise-interval" in config_dict and \
                config_dict["advertise-interval"] == 1:
            del config_dict["advertise-interval"]
        if "priority" in config_dict and \
                config_dict["priority"] == 100:
            del config_dict["priority"]

        # Multi line config code, look for the block start and then the next }
        vips_start: int = config_block.index("virtual_ipaddress {")
        vips_end: int = config_block.index("}", vips_start)
        config_dict["virtual-address"] = config_block[vips_start + 1:vips_end]

        # Version specific code
        if config_dict["version"] == 2:
            self._convert_authentication_config(
                config_block, config_dict)
        else:
            if "advertise-interval" in config_dict:
                fast_advertise: Union[int, float] = \
                    float(config_dict["advertise-interval"]) * 1000
                config_dict["fast-advertise-interval"] = int(fast_advertise)
                del config_dict["advertise-interval"]

        # Find interface so we know which yang name to use for tracking
        # augments
        conf_tuple: Tuple[bool, Union[List[None], str]] = \
            util.find_config_value(config_block, "interface")
        intf_type = None
        if conf_tuple[0] and isinstance(conf_tuple[1], str):
            intf_type = conf_tuple[1]
        else:
            return {}
        self._convert_tracking_config(
            config_block, config_dict, intf_type)

        self._convert_notify_proto_config(
            config_block, config_dict)

        config_dict = \
            {key: val for key, val in config_dict.items() if val != "NOTFOUND"}
        # Sort dictionary alphabetically for unit tests
        config_dict = \
            {key: config_dict[key] for key in sorted(config_dict.keys())}
        return config_dict

    @staticmethod
    def _convert_authentication_config(
            block: List[str], config_dict: Dict
    ) -> None:
        """
        Convert the authentication block into key:value pairs in the
        config dictionary, do nothing if the config isn't in the
        string.
        """

        try:
            block.index("authentication {")
        except ValueError:
            # Authentication doesn't exist in this group
            return

        auth_type: Tuple[bool, Union[List[None], str]] = \
            util.find_config_value(block, "auth_type")
        auth_pass: Tuple[bool, Union[List[None], str]] = \
            util.find_config_value(block, "auth_pass")
        if auth_type[0] and auth_pass[0]:
            if auth_type[1] == "PASS":
                auth_type = (auth_type[0], "plaintext-password")
            if isinstance(auth_type[1], str):
                config_dict["authentication"] = {
                    "password": auth_pass[1],
                    "type": str.lower(auth_type[1])
                }

    @staticmethod
    def _convert_notify_proto_config(
            block: List[str], config_dict: Dict
    ) -> None:
        """
        Convert notify block into key:value pairs in the
        config dictionary, do nothing if the config isn't in the
        string.
        """

        try:
            config_start: int = block.index("notify {")
        except ValueError:
            # Notify doesn't exist in this group
            return
        else:
            config_end: int = block.index("}", config_start)
            notify_config: List[str] = block[config_start + 1:config_end]
            config_dict["notify"] = {}
            if "/opt/vyatta/sbin/notify-bgp" in notify_config:
                config_dict["notify"]["bgp"] = [None]
            if "/opt/vyatta/sbin/vyatta-ipsec-notify.sh" in \
                    notify_config:
                config_dict["notify"]["ipsec"] = [None]

    def _convert_tracking_config(
            self, block: List[str], config_dict: Dict, intf_type: str
    ) -> None:
        """
        Convert tracking config blocks into key:value pairs in the
        config dictionary, do nothing if the config isn't in the
        string.

        Because there's several types of tracking available we need
        to call each track helper function to generate the final entry
        into the config_dict["track"] value.
        """
        try:
            config_start: int = block.index("track {")
        except ValueError:
            # No tracking config in this group
            return
        else:
            config_dict["track"] = {}
            self._convert_interface_tracking_config(
                block, config_dict, config_start)
            intf_enum: Enum = util.intf_name_to_type(intf_type)[1]
            self._convert_pathmon_tracking_config(
                block, config_dict, config_start, intf_enum)
            self._convert_route_to_tracking_config(
                block, config_dict, config_start, intf_enum)

    @staticmethod
    def _convert_interface_tracking_config(
            block: List[str], config_dict: Dict, start: int
    ) -> None:
        """
        Convert track interface block into key:value pairs in the
        config dictionary, do nothing if the config isn't in the
        string.
        """

        try:
            config_start: int = block.index("interface {", start)
        except ValueError:
            # Interface tracking doesn't exist in this group
            return
        else:
            interface_list: List[Dict] = []
            config_end: int = block.index("}", config_start)
            track_intf_config: List[str] = \
                block[config_start + 1:config_end]
            for line in track_intf_config:
                if "weight" not in line:
                    interface_list.append({"name": line})
                    continue
                tokens: List[str] = line.split()
                weight: int = int(tokens[-1])
                if weight < 0:
                    weight_type = "decrement"
                else:
                    weight_type = "increment"
                interface_list.append(
                    {"name": tokens[0], "weight": {
                        "type": weight_type,
                        "value": abs(weight)
                    }}
                )
            config_dict["track"]["interface"] = interface_list

    @staticmethod
    def _convert_pathmon_tracking_config(
            block: List[str], config_dict: Dict, start: int,
            intf_type: Enum
    ) -> None:
        """
        Convert track pathmon block into key:value pairs in the
        config dictionary, do nothing if the config isn't in the
        string.
        """

        try:
            config_start: int = block.index("pathmon {", start)
        except ValueError:
            # Pathmon tracking doesn't exist in this group
            return
        else:
            config_end: int = block.index("}", config_start)
            track_pathmon_config: List[str] = \
                block[config_start + 1: config_end]
            pathmon_dict: Dict = {"monitor": []}
            line: str
            for line in track_pathmon_config:
                tokens: List[str] = line.split()
                monitor_name: str = tokens[1]
                policy_name: str = tokens[3]
                insertion_dictionary: Dict[str, Union[Any, List[Dict]]] = {}
                monitor: Dict
                for monitor in pathmon_dict["monitor"]:
                    if monitor_name == monitor["name"]:
                        insertion_dictionary = monitor
                        break
                if insertion_dictionary == {}:
                    insertion_dictionary["name"] = monitor_name
                    insertion_dictionary["policy"] = []
                    pathmon_dict["monitor"].append(insertion_dictionary)
                policy_dict: Dict[str, Union[str, Dict[str, Union[str, int]]]]\
                    = {"name": policy_name}
                if "weight" in line:
                    weight = int(tokens[-1])
                    if weight < 0:
                        weight_type = "decrement"
                    else:
                        weight_type = "increment"
                    policy_dict["weight"] = {
                        "type": weight_type,
                        "value": abs(weight)
                    }
                insertion_dictionary["policy"].append(policy_dict)
            if intf_type.name == "dataplane":
                config_dict["track"][util.PATHMON_DATAPLANE_YANG_NAME] =\
                    pathmon_dict
            elif intf_type.name == "bonding":
                config_dict["track"][util.PATHMON_BONDING_YANG_NAME] =\
                    pathmon_dict
            elif intf_type.name == "switch":
                config_dict["track"][util.PATHMON_SWITCH_YANG_NAME] =\
                    pathmon_dict

    @staticmethod
    def _convert_route_to_tracking_config(
            block: List[str], config_dict: Dict, start: int,
            intf_type: Enum
    ) -> None:
        """
        Convert track route block into key:value pairs in the
        config dictionary, do nothing if the config isn't in the
        string.
        """

        try:
            config_start: int = block.index("route_to {", start)
        except ValueError:
            # Interface tracking doesn't exist in this group
            return
        else:
            route_list: List[Dict] = []
            config_end: int = block.index("}", config_start)
            track_route_config: List[str] = \
                block[config_start + 1:config_end]
            line: str
            for line in track_route_config:
                if "weight" not in line:
                    route_list.append({"route": line})
                    continue
                tokens: List[str] = line.split()
                weight: int = int(tokens[-1])
                weight_type: str
                if weight < 0:
                    weight_type = "decrement"
                else:
                    weight_type = "increment"
                route_list.append(
                    {"route": tokens[0], "weight": {
                        "type": weight_type,
                        "value": abs(weight)
                    }}
                )
            if intf_type.name == "dataplane":
                config_dict["track"][util.ROUTE_DATAPLANE_YANG_NAME] = \
                    route_list
            elif intf_type.name == "bonding":
                config_dict["track"][util.ROUTE_BONDING_YANG_NAME] = \
                    route_list
            elif intf_type.name == "switch":
                config_dict["track"][util.ROUTE_SWITCH_YANG_NAME] = \
                    route_list

    def shutdown(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.config_file)
