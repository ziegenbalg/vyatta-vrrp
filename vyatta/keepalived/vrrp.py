#! /usr/bin/env python3

# Copyright (c) 2020 AT&T Intellectual Property.
# All rights reserved.
# SPDX-License-Identifier: GPL-2.0-only


from typing import Dict
import vyatta.keepalived.util as util


class VrrpGroup:
    """
    Simple VRRP group representation

    Used to represent the keepalived config for each individual
    VRRP group
    """

    def __init__(
            self, name: str, delay: str, group_config: Dict,
            rfc_num: int = -1):
        """
        Constructor for the class

        Arguments:
            name (str):
                Name of the interface the VRRP group is configured on
            delay (str):
                Start delay configured for the interface
            group_config (Dict):
                YANG Dictionary for the group's config
        """
        # Default values from existing code required for minimal
        # config
        self._group_config = group_config
        if "priority" not in self._group_config:
            self._group_config["priority"] = 100
        if "advertise-interval" not in self._group_config \
                and "fast-advertise-interval" not in self._group_config:
            self._group_config["adv"] = 1
        elif "advertise-interval" in self._group_config:
            self._group_config["adv"] = \
                group_config["advertise-interval"]
            del self._group_config["advertise-interval"]
        elif "fast-advertise-interval" in self._group_config:
            self._group_config["adv"] = \
                int(float(group_config["fast-advertise-interval"]) / 1000)
            del self._group_config["fast-advertise-interval"]

        # Values outwith the dictionary
        self._group_config["intf"] = name
        self._group_config["delay"] = delay
        self._group_config["state"] = "BACKUP"

        # Autogenerated values from minimal config
        self._group_config["vrid"] = group_config["tagnode"]
        self._group_config["accept"] = group_config["accept"]

        first_addr = group_config["virtual-address"][0].split("/")[0]
        ip_version = util.what_ip_version(first_addr)
        if ip_version == 4:
            self._group_config["vips"] = """
        """.join(sorted(group_config["virtual-address"]))
        else:
            self._group_config["vips"] = """
        """.join(util.vrrp_ipv6_sort(group_config["virtual-address"]))
        del self._group_config["virtual-address"]

        # Template required for minimal config
        self._template = """
vrrp_instance {instance} {{
    state {state}
    interface {intf}
    virtual_router_id {tagnode}
    version {version}
    start_delay {delay}
    priority {priority}
    advert_int {adv}
    virtual_ipaddress {{
        {vips}
    }}"""

        if ip_version == 6:
            self._template += """
    native_ipv6"""

        # Optional config
        if self._group_config["accept"]:
            self._template += """
    accept"""

        if not self._group_config["preempt"]:
            self._template += """
    nopreempt"""

        if "rfc-compatibility" in self._group_config:
            self._group_config["vmac"] = "{}vrrp{}".format(name[:3], rfc_num)
            if len(self._group_config["vmac"]) > 15:
                print("Warning: generated interface name is longer than 15 " +
                      "characters\n")
            else:
                self._template += """
    use_vmac {vmac}
    vmac_xmit_base"""

        if "preempt-delay" in self._group_config:
            self._group_config["preempt_delay"] = \
                self._group_config["preempt-delay"]
            del self._group_config["preempt-delay"]
            self._template += """
    preempt_delay {preempt_delay}"""
            if "preempt" in self._group_config and \
                    self._group_config["preempt"] is False:
                print("Warning: preempt delay is ignored when preempt=false\n")

        if "hello-source-address" in self._group_config:
            self._group_config["mcast_src_ip"] = \
                self._group_config["hello-source-address"]
            del self._group_config["hello-source-address"]
            self._template += """
    mcast_src_ip {mcast_src_ip}"""

        if "authentication" in self._group_config:
            self._group_config["auth_pass"] = \
                self._group_config["authentication"]["password"]
            if self._group_config["authentication"]["type"] == \
                    "plaintext-password":
                auth_type = "PASS"
            else:
                auth_type = "AH"
            self._group_config["auth_type"] = auth_type
            self._template += """
    authentication {{
        auth_type {auth_type}
        auth_pass {auth_pass}
    }}"""

        if "track-interface" in self._group_config:
            if "track" in self._group_config:
                if "interface" in self._group_config["track"]:
                    self._group_config["track"]["interface"].append(
                        *self._group_config["track-interface"]
                    )
                else:
                    self._group_config["track"]["interface"] = \
                        self._group_config["track-interface"]
            else:
                self._group_config["track"] = {}
                self._group_config["track"]["interface"] = \
                    self._group_config["track-interface"]
            del self._group_config["track-interface"]

        if "track" in self._group_config:
            self._generate_track_string(self._group_config["track"])

        if "notify" in self._group_config:
            self._template += """
    notify {{"""
            if "ipsec" in self._group_config["notify"]:
                self._template += """
        /opt/vyatta/sbin/vyatta-ipsec-notify.sh"""
            if "bgp" in self._group_config["notify"]:
                self._template += """
        /opt/vyatta/sbin/notify-bgp"""
            self._template += """
    }}"""

        # Generate instance name (TODO change to f-string with python 3.7)
        self._instance = "vyatta-{intf}-{vrid}".format(
            intf=name, vrid=group_config["tagnode"]
        )
        self._group_config["instance"] = self._instance

        self._template += "\n}}"

    @property
    def instance_name(self):
        """Name of this group in the config file"""
        return self._instance

    def _generate_track_string(self, track_dict):
        self._template += """
    track {{"""
        if "interface" in track_dict:
            self._generate_track_interfaces(track_dict["interface"])
        if util.PATHMON_YANG_NAME in track_dict:
            self._generate_track_pathmon(track_dict[util.PATHMON_YANG_NAME])
        self._template += """
    }}"""

    def _generate_track_interfaces(self, intf_dict):
        self._template += """
        interface {{"""
        for interface in intf_dict:
            track_string = """
            {}""".format(interface["name"])
            if "weight" in interface:
                if interface["weight"]["type"] == "decrement":
                    multiplier = -1
                else:
                    multiplier = 1
                value = multiplier * interface["weight"]["value"]
                track_string += "   weight  {:+d}".format(value)
            self._template += track_string
        self._template += """
        }}"""  # Close interface brace

    def _generate_track_pathmon(self, pathmon_dict):
        self._template += """
        pathmon {{"""
        for monitor in pathmon_dict["monitor"]:
            monitor_name = monitor["name"]
            for policy in monitor["policy"]:
                track_string = """
            monitor {}    policy {}""".format(monitor_name, policy["name"])
                if "weight" in policy:
                    if policy["weight"]["type"] == "decrement":
                        multiplier = -1
                    else:
                        multiplier = 1
                    value = multiplier * policy["weight"]["value"]
                    track_string += "      weight  {:+d}".format(value)
                self._template += track_string
        self._template += """
        }}"""  # Close pathmon brace

    def __repr__(self):
        completed_config = self._template.format(
            **self._group_config
        )
        return completed_config
