#! /usr/bin/python3

# Copyright (c) 2019-2020 AT&T Intellectual Property.
# All rights reserved.
# SPDX-License-Identifier: GPL-2.0-only
"""
Vyatta VCI component to configure keepalived to provide VRRP functionality
"""

import logging
import json
import os
import pydbus
from pathlib import Path
from typing import Any, Dict, List, Tuple
import vyatta.abstract_vrrp_classes as AbstractConfig
import vyatta.keepalived.util as util
import vyatta.keepalived.vrrp as vrrp
import vyatta.keepalived.dbus.vrrp_group_connection as vrrp_dbus


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
            config_file_path: str = "/etc/keepalived/keepalived.conf"):
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
                Currently this is modified in the update call, there must
                be a better way to do this
            vif_yang_name, vrrp_yang_name (str):
                Name of the YANG paths that are used for dictionary keys to
                avoid magic strings
        """

        self.log = logging.getLogger("vyatta-vrrp-vci")
        self.config_string = """
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
        self.config_file = config_file_path  # type: str
        self.implementation_name = "Keepalived"  # type: str
        self._vrrp_instances = []  # type: List[Dict]
        self._sync_instances = {}  # type: Dict[List[str]]
        self._rfc_interfaces = 0  # type: int
        self._vrrp_connections = {}  # noqa: E501 type: Dict[str, vrrp_dbus.VrrpConnection]

    @property
    def vrrp_instances(self):
        return self._vrrp_instances

    @vrrp_instances.setter
    def vrrp_instances(self, new_value):
        self._vrrp_instances = new_value

    @property
    def vrrp_connections(self):
        return self._vrrp_connections

    def config_file_path(self) -> str:
        """Path to the keepalived config file returns string"""
        return self.config_file

    def impl_name(self) -> str:
        """Name of the VRRP implementation returns string"""
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
        self.vrrp_instances = []  # type: List[vrrp.VrrpGroup]
        self._vrrp_connections = {}  # noqa: E501 type: Dict[str, vrrp_dbus.VrrpConnection]
        if util.INTERFACE_YANG_NAME not in new_config:
            return
        intf_types = new_config[util.INTERFACE_YANG_NAME]

        for intf_type in intf_types:
            for intf in intf_types[intf_type]:
                if "vif" in intf:
                    # As we've already sanitized the data and moved
                    # vif interfaces to their own intf_type if any
                    # exist in the interface config this is a problem.
                    raise ValueError(
                        "VIF interfaces shouldn't be present under" +
                        " another interface")

                intf_name = intf["tagnode"]
                vrrp_conf = intf[util.VRRP_YANG_NAME]
                if vrrp_conf["vrrp-group"] == []:
                    break
                start_delay = vrrp_conf["start-delay"]
                for group in vrrp_conf["vrrp-group"]:
                    first_vip = group["virtual-address"][0]
                    if "/" in first_vip:
                        first_vip = first_vip.split("/")[0]
                    if "disable" in group:
                        break
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
                    af_type = util.what_ip_version(
                        first_vip
                    )
                    connection = vrrp_dbus.VrrpConnection(
                        intf_name, group["tagnode"],
                        af_type, pydbus.SystemBus()
                    )
                    instance_name = "vyatta-{}-{}".format(
                        intf_name, group["tagnode"]
                    )
                    self._vrrp_connections[instance_name] = \
                        connection
                    if "sync-group" in group:
                        sync_group_name = group["sync-group"]
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
        keepalived_config = self.config_string
        for sync_group in self._sync_instances:
            keepalived_config += """
vrrp_sync_group {} {{
    group {{""".format(sync_group)
            for instance in self._sync_instances[sync_group]:
                keepalived_config += """
        {}""".format(instance)
            keepalived_config += """
    }
}
"""
        for group in self.vrrp_instances:
            keepalived_config += str(group)
        with open(self.config_file, "w") as file_handle:
            file_handle.write(keepalived_config)

    def read_config(self) -> str:
        """Read config from file at config_file and return to caller"""
        config_string = ""
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
        config_lines = config_string.splitlines()  # type: List[str]
        vrrp_group_start_indexes = util.get_config_indexes(
            config_lines, "vrrp_instance")  # type: List[int]
        if vrrp_group_start_indexes == []:
            return {}

        sync_group_start_indexes = util.get_config_indexes(
            config_lines, "vrrp_sync_group")  # type: List[int]
        sync_group_instances = {}
        if sync_group_start_indexes != []:
            sync_group_config = util.get_config_blocks(
                config_lines[:vrrp_group_start_indexes[0]],
                sync_group_start_indexes
            )
            for sync_group in sync_group_config:
                group_name_exists = util.find_config_value(
                    sync_group, "vrrp_sync_group")
                if not group_name_exists[0]:
                    continue
                group_name = group_name_exists[1].split()[0]
                instance_start = sync_group.index("group {")
                instance_end = sync_group.index("}", instance_start)
                for instance in sync_group[instance_start+1:instance_end]:
                    sync_group_instances[instance] = group_name

        group_config = util.get_config_blocks(
            config_lines, vrrp_group_start_indexes)  # type: List[List[str]]

        # config_without_groups = \
        #    config_lines[:vrrp_group_start_indexes[0]]  # type: List[str]

        yang_representation = {util.INTERFACE_YANG_NAME: {}}
        for group in group_config:

            intf_name = util.find_config_value(
                group, "interface")[1]  # type: str
            vrid = util.find_config_value(
                group, "virtual_router_id")[1]  # type: str
            instance_name = "vyatta-{}-{}".format(intf_name, vrid)

            if instance_name in sync_group_instances:
                group.append("sync_group {}".format(
                    sync_group_instances[instance_name]
                ))
            vif_number = ""
            if "." in intf_name:
                vif_sep = intf_name.split(".")
                intf_name = vif_sep[0]
                vif_number = vif_sep[1]  # type: str

            interface_list = yang_representation[util.INTERFACE_YANG_NAME]
            # Find the interface type for the interface name, right now this
            # is just a guess, there might be a better method of doing this
            # than regexes
            intf_type = util.intf_name_to_type(intf_name)
            if intf_type not in interface_list:
                interface_list[intf_type] = []
            interface_list = interface_list[intf_type]

            # Hackery to find the reference to the interface this vrrp
            # group should be added to.
            insertion_reference = util.find_interface_in_yang_repr(
                intf_name, vif_number, interface_list)

            # All groups should have the same start delay but check and
            # store the largest delay found
            new_group_start_delay = \
                util.find_config_value(group, "start_delay")[1]
            current_start_delay = \
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
            config_block: List[str]) -> dict:
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
        config_dict = {
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
        }  # type: Any

        # Single line config code
        for key in config_dict:
            # Search for each term in the config
            config_exists = util.find_config_value(config_block,
                                                   config_dict[key])
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
                # Term exists in config and has a value
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
        vips_start = config_block.index('virtual_ipaddress {')  # type: int
        vips_end = config_block.index('}', vips_start)  # type: int
        config_dict["virtual-address"] = config_block[vips_start+1:vips_end]

        # Version specific code
        if config_dict["version"] == 2:
            self._convert_authentication_config(
                    config_block, config_dict)
        else:
            if "advertise-interval" in config_dict:
                config_dict["fast-advertise-interval"] = \
                    config_dict["advertise-interval"] * 1000
                del config_dict["advertise-interval"]

        self._convert_tracking_config(
            config_block, config_dict)

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
            block: List[str], config_dict: Dict) -> None:
        try:
            block.index('authentication {')  # type: int
        except ValueError:
            # Authentication doesn't exist in this group
            return

        auth_type = util.find_config_value(
            block, "auth_type")  # type: Tuple[bool, Any]
        auth_pass = util.find_config_value(block,
                                           "auth_pass")
        if auth_type[0] and auth_pass[0]:
            if auth_type[1] == "PASS":
                auth_type = (auth_type[0], "plaintext-password")
            config_dict["authentication"] = {
                "password": auth_pass[1],
                "type": str.lower(auth_type[1])
            }

    @staticmethod
    def _convert_notify_proto_config(
            block: List[str], config_dict: Dict) -> None:
        try:
            config_start = block.index('notify {')  # type: int
        except ValueError:
            # Notify doesn't exist in this group
            return
        else:
            config_end = block.index("}", config_start)  # type: int
            notify_config = block[config_start+1:config_end]  # type: List[str]
            config_dict["notify"] = {}
            if "/opt/vyatta/sbin/notify-bgp" in notify_config:
                config_dict["notify"]["bgp"] = [None]
            if "/opt/vyatta/sbin/vyatta-ipsec-notify.sh" in \
                    notify_config:
                config_dict["notify"]["ipsec"] = [None]

    def _convert_tracking_config(
            self, block: List[str], config_dict: Dict) -> None:
        try:
            config_start = block.index('track {')  # type: int
        except ValueError:
            # No tracking config in this group
            return
        else:
            config_dict["track"] = {}
            self._convert_interface_tracking_config(
                block, config_dict, config_start)
            self._convert_pathmon_tracking_config(
                block, config_dict, config_start)

    @staticmethod
    def _convert_interface_tracking_config(
            block: List[str], config_dict: Dict, start: int) -> None:
        try:
            config_start = block.index('interface {', start)  # type: int
        except ValueError:
            # Interface tracking doesn't exist in this group
            return
        else:
            interface_list = []
            config_end = block.index("}", config_start)  # type: int
            track_intf_config = \
                block[config_start+1:config_end]  # type: List[str]
            for line in track_intf_config:
                if "weight" not in line:
                    interface_list.append({"name": line})
                    continue
                tokens = line.split()
                weight = int(tokens[-1])
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
            block: List[str], config_dict: Dict, start: int) -> None:
        try:
            config_start = block.index('pathmon {', start)  # type: int
        except ValueError:
            # Pathmon tracking doesn't exist in this group
            return
        else:
            config_end = block.index("}", config_start)  # type: int
            track_pathmon_config = \
                block[config_start+1: config_end]  # type: List[str]
            pathmon_dict = {"monitor": []}  # type: Dict
            for line in track_pathmon_config:
                tokens = line.split()
                monitor_name = tokens[1]
                policy_name = tokens[3]
                insertion_dictionary = {}
                for monitor in pathmon_dict["monitor"]:
                    if monitor_name == monitor["name"]:
                        insertion_dictionary = monitor
                        break
                if insertion_dictionary == {}:
                    insertion_dictionary["name"] = monitor_name
                    insertion_dictionary["policy"] = []
                    pathmon_dict["monitor"].append(insertion_dictionary)
                policy_dict = {"name": policy_name}
                if "weight" in line:
                    policy_dict["weight"] = {}
                    weight = int(tokens[-1])
                    if weight < 0:
                        weight_type = "decrement"
                    else:
                        weight_type = "increment"
                    policy_dict["weight"]["type"] = weight_type
                    policy_dict["weight"]["value"] = abs(weight)
                insertion_dictionary["policy"].append(policy_dict)
            config_dict["track"][util.PATHMON_YANG_NAME] = pathmon_dict

    def shutdown(self):
        config_path = Path(self.config_file)
        if config_path.is_file():
            os.remove(self.config_file)
