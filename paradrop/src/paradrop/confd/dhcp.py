import ipaddress
import os

from paradrop.lib.utils import pdosq
from paradrop.base.output import out

from .base import ConfigObject, ConfigOption
from .command import Command, KillCommand


class ConfigDhcp(ConfigObject):
    typename = "dhcp"

    options = [
        ConfigOption(name="interface", required=True),
        ConfigOption(name="leasetime", required=True),
        ConfigOption(name="limit", type=int, required=True),
        ConfigOption(name="start", type=int, required=True),
        ConfigOption(name="dhcp_option", type=list)
    ]


class ConfigDomain(ConfigObject):
    typename = "domain"

    options = [
        ConfigOption(name="name", type=str),
        ConfigOption(name="ip", type=str)
    ]


class ConfigDnsmasq(ConfigObject):
    typename = "dnsmasq"

    options = [
        ConfigOption(name="domain", type=str),
        ConfigOption(name="interface", type=list),
        ConfigOption(name="leasefile", type=str),
        ConfigOption(name="noresolv", type=bool, default=False),
        ConfigOption(name="server", type=list)
    ]

    def apply(self, allConfigs):
        commands = list()

        # visibleName will be used in choosing file names for this dnsmasq
        # instance, must be unique if there are multiple dnsmasq instances
        visibleName = self.internalName

        if self.interface is None:
            interfaces = []
            for section in self.findByType(allConfigs, "dhcp", "dhcp"):
                interfaces.append(section.interface)
        else:
            interfaces = self.interface

        self.__leasefile = self.leasefile
        if self.__leasefile is None:
            self.__leasefile = "{}/dnsmasq-{}.leases".format(
                self.manager.writeDir, visibleName)
        pdosq.makedirs(os.path.dirname(self.__leasefile))

        pidFile = "{}/dnsmasq-{}.pid".format(
            self.manager.writeDir, visibleName)
        pdosq.makedirs(os.path.dirname(pidFile))

        outputPath = "{}/dnsmasq-{}.conf".format(
            self.manager.writeDir, visibleName)
        pdosq.makedirs(os.path.dirname(outputPath))

        with open(outputPath, "w") as outputFile:
            outputFile.write("#" * 80 + "\n")
            outputFile.write("# dnsmasq configuration file generated by "
                             "pdconfd\n")
            outputFile.write("# Source: {}\n".format(self.source))
            outputFile.write("# Section: {}\n".format(str(self)))
            outputFile.write("#" * 80 + "\n")
            outputFile.write("\n")
            outputFile.write("dhcp-leasefile={}\n".format(self.__leasefile))

            if self.domain:
                outputFile.write("domain={}\n".format(self.domain))

            if self.noresolv:
                outputFile.write("no-resolv\n")

            if self.server:
                for server in self.server:
                    outputFile.write("server={}\n".format(server))

            # TODO: Bind interfaces allows us to have multiple instances of
            # dnsmasq running, but it would probably be better to have one
            # running and reconfigure it when we want to add or remove
            # interfaces.  It is not very disruptive to reconfigure and restart
            # dnsmasq.
            outputFile.write("\n")
            outputFile.write("except-interface=lo\n")
            outputFile.write("bind-interfaces\n")

            for intfName in interfaces:
                interface = self.lookup(allConfigs, "network", "interface", intfName)
                outputFile.write("\n")
                outputFile.write("# Options for section interface {}\n".
                                 format(interface.name))
                outputFile.write("interface={}\n".format(
                                 interface.config_ifname))

                network = ipaddress.IPv4Network(u"{}/{}".format(
                    interface.ipaddr, interface.netmask), strict=False)

                dhcp = self.lookup(allConfigs, "dhcp", "dhcp", intfName)

                # TODO: Error checking!
                firstAddress = network.network_address + dhcp.start
                lastAddress = firstAddress + dhcp.limit

                outputFile.write("\n")
                outputFile.write("# Options for section dhcp {}\n".
                                 format(interface.name))
                outputFile.write("dhcp-range={},{},{}\n".format(
                    str(firstAddress), str(lastAddress), dhcp.leasetime))

                # Write options sections to the config file.
                if dhcp.dhcp_option:
                    for option in dhcp.dhcp_option:
                        outputFile.write("dhcp-option={}\n".format(option))

            outputFile.write("\n")
            for domain in self.findByType(allConfigs, "dhcp", "domain"):
                outputFile.write("address=/{}/{}\n".format(domain.name, domain.ip))

        cmd = ["dnsmasq", "--conf-file={}".format(outputPath),
               "--pid-file={}".format(pidFile)]
        commands.append((self.PRIO_START_DAEMON, Command(cmd, self)))

        self.pidFile = pidFile
        return commands

    def revert(self, allConfigs):
        commands = list()

        commands.append((-self.PRIO_START_DAEMON, 
            KillCommand(self.pidFile, self)))

        return commands
