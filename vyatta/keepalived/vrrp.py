#! /usr/bin/env python3

# Copyright (c) 2020 AT&T Intellectual Property.
# All rights reserved.
# SPDX-License-Identifier: GPL-2.0-only


from typing import Dict


class VrrpGroup:
    """
    Simple VRRP group representation

    Used to represent the keepalived config for each individual
    VRRP group
    """

    def __init__(self, name: str, delay: str, group_config: Dict):
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
                and self._group_config["version"] == 2:
            self._group_config["adv"] = 1
        else:
            self._group_config["adv"] = \
                group_config["advertise-interval"]
            del(self._group_config["advertise-interval"])

        # Values outwith the dictionary
        self._group_config["intf"] = name
        self._group_config["delay"] = delay
        self._group_config["state"] = "BACKUP"

        # Autogenerated values from minimal config
        self._group_config["vrid"] = group_config["tagnode"]
        self._group_config["accept"] = group_config["accept"]

        self._group_config["vips"] = "\n".join(
            group_config["virtual-address"])
        del(self._group_config["virtual-address"])

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

        # Optional config
        if "rfc-compatibility" in self._group_config:
            self._template += """
    use_vmac {vmac}
    vmac_xmit_base"""
            # TODO: Generate rfc intf name
            self._group_config["vmac"] = "dp0vrrp1"

        if "preempt-delay" in self._group_config:
            self._group_config["preempt_delay"] = \
                self._group_config["preempt-delay"]
            del self._group_config["preempt-delay"]
            self._template += """
    preempt_delay {preempt_delay}"""

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
                auth_type = "OTHER"  # TODO: Fix for AH
            self._group_config["auth_type"] = auth_type
            self._template += """
    authentication {{
        auth_type {auth_type}
        auth_pass {auth_pass}
    }}"""

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
        if "path-monitor" in track_dict:
            self._generate_track_pathmon(track_dict["path-monitor"])
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
                track_string += "   weight  {}".format(value)
            self._template += track_string
        self._template += """
        }}"""  # Close "interface brace"

    def _generate_track_pathmon(self, pathmon_dict):
        self._template += """
        TO BE IMPLEMENTED"""

    def __repr__(self):
        completed_config = self._template.format(
            **self._group_config
        )
        return completed_config
