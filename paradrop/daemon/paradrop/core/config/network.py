import collections
import ipaddress
import itertools

from paradrop.base.output import out
from paradrop.base import pdutils, settings
from paradrop.lib.utils import addresses, datastruct, uci
from paradrop.lib.misc import resources

from . import configservice, uciutils

# TODO: Instead of being a constant, look at device capabilities.
MAX_AP_INTERFACES = 8

MAX_INTERFACE_NAME_LEN = 15

# We use a 4-digit hex string, which gives us this maximum.
MAX_INTERFACE_NUMBERS = 65536

# Size of subnets to assign to chutes.
SUBNET_SIZE = 24

# Extra Wi-Fi interface options that we will pass on directly to confd.
# Deprecated: move to 'options' object after 1.0 release.
IFACE_EXTRA_OPTIONS = set([
    "auth_server",
    "auth_secret",
    "acct_server",
    "acct_secret",
    "acct_interval"
])


def getInterfaceDict(chute):
    """
    Return interfaces from a chute as a dict with interface names as the keys.
    Returns an empty dict if chute is None or it had no interfaces.
    """
    oldInterfaces = dict()
    if chute is not None:
        cachedInterfaces = chute.getCache('networkInterfaces')
        oldInterfaces = {iface['name']: iface for iface in cachedInterfaces}
    return oldInterfaces


def reclaimNetworkResources(chute):
    """
    Reclaim network resources for a previously running chute.

    This function only applies to the special case in which pd starts up and
    loads a list of chutes that were running.  This function marks their IP
    addresses and interface names as taken so that new chutes will not use the
    same values.
    """
    pass


def getWifiKeySettings(cfg, iface):
    """
    Read encryption settings from cfg and transfer them to iface.
    """
    # If 'key' is present, but 'encryption' is not, then default to
    # psk2.  If 'key' is not present, and 'encryption' is psk or psk2,
    # then we have an error.
    iface['encryption'] = "none"
    if 'key' in cfg:
        iface['key'] = cfg['key']
        iface['encryption'] = 'psk2'  # default to psk2
    if 'encryption' in cfg:
        iface['encryption'] = cfg['encryption']
        if cfg['encryption'].startswith('psk') and 'key' not in cfg:
            out.warn("Key field must be defined "
                     "when encryption is enabled.")
            raise Exception("No key field defined for WiFi encryption")


def chooseSubnet(update, cfg, iface):
    reservations = resources.getSubnetReservations()

    # Check if the chute configuration requests a specific network.
    #
    # TODO: Implement a mode where a chute can request a preferred network, but
    # do not fail if it is unavailable.
    requested = cfg.get('ipv4_network', None)
    if requested is not None:
        network = ipaddress.ip_network(unicode(requested))
        if reservations.isAvailable(network):
            return network
        else:
            raise Exception("Could not assign network {}, network already in use".format(
                requested))

    # Get subnet configuration settings from host configuration.
    host_config = update.new.getCache('hostConfig')
    network_pool = datastruct.getValue(host_config,
            "system.chuteSubnetPool", settings.DYNAMIC_NETWORK_POOL)
    prefix_size = datastruct.getValue(host_config,
            "system.chutePrefixSize", SUBNET_SIZE)

    network = ipaddress.ip_network(unicode(network_pool))
    if prefix_size < network.prefixlen:
        raise Exception("Router misconfigured: prefix size {} is invalid for network {}".format(
            prefix_size, network))

    subnets = network.subnets(new_prefix=prefix_size)
    for subnet in subnets:
        if reservations.isAvailable(subnet):
            return subnet

    raise Exception("Could not find an available subnet")


def chooseExternalIntf(update, iface):
    if iface['mode'] == "ap":
        # Generate initial portion (prefix) of interface name.
        #
        # NOTE: We add a "v" in front of the interface name to avoid triggering
        # the udev persistent net naming rules, which are hard-coded to certain
        # typical strings such as "eth*" and "wlan*" but not "veth*" or
        # "vwlan*".  We do NOT want udev renaming our virtual interfaces.
        prefix = "vwlan"

        # This name should be unique on the system, so the hash is very unlikely to
        # collide with anything.  It still can collide, but this will be our first
        # choice for an interface number.
        name = "{}:{}".format(update.new.name, iface['internalIntf'])
        base = hash(name)

        reservations = resources.getInterfaceReservations()

        for i in range(MAX_INTERFACE_NUMBERS):
            number = (base + i) % MAX_INTERFACE_NUMBERS
            intf = "{}{:04x}".format(prefix, number)
            if intf not in reservations:
                return intf

        raise Exception("Could not find an available interface name")

    else:
        # For monitor and managed mode, use the existing primary interface.
        devices = update.new.getCache('networkDevicesByName')
        if iface['device'] not in devices:
            raise Exception("Could not find device {}".format(iface['device']))

        device = devices[iface['device']]
        if 'primary_interface' not in device:
            raise Exception("Primary interface for {} not detected".format(
                device['name']))

        return device['primary_interface']


def getNetworkConfigWifi(update, name, cfg, iface):
    iface['mode'] = cfg.get("mode", "ap")

    # Make a dictionary of old interfaces.  Any new interfaces that are
    # identical to an old one do not need to be changed.
    oldInterfaces = getInterfaceDict(update.old)
    if name in oldInterfaces:
        oldIface = oldInterfaces[iface['name']]
        subnet = oldIface['subnet']
        iface['externalIntf'] = oldIface['externalIntf']

    else:
        # Claim a subnet for this interface from the pool.
        subnet = chooseSubnet(update, cfg, iface)

        # Generate a name for the new interface in the host.
        iface['externalIntf'] = chooseExternalIntf(update, iface)
        if len(iface['externalIntf']) > MAX_INTERFACE_NAME_LEN:
            out.warn("Interface name ({}) is too long\n".format(iface['externalIntf']))
            raise Exception("Interface name is too long")

    # Generate internal (in the chute) and external (in the host)
    # addresses.
    #
    # Example:
    # subnet: 192.168.30.0/24
    # netmask: 255.255.255.0
    # external: 192.168.30.1
    # internal: 192.168.30.2
    hosts = subnet.hosts()
    iface['subnet'] = subnet
    iface['netmask'] = str(subnet.netmask)
    iface['externalIpaddr'] = str(hosts.next())
    iface['internalIpaddr'] = str(hosts.next())

    # Generate the internal IP address with prefix length (x.x.x.x/y) for
    # convenience of other code that expect that format (e.g. pipework).
    iface['ipaddrWithPrefix'] = "{}/{}".format(
            iface['internalIpaddr'], subnet.prefixlen)

    if iface['mode'] in ["ap", "sta"]:
        # Check for required fields.
        res = pdutils.check(cfg, dict, ['ssid'])
        if res:
            out.warn('WiFi network interface definition {}\n'.format(res))
            raise Exception("Interface definition missing field(s)")

        iface['ssid'] = cfg['ssid']

    # Optional encryption settings
    getWifiKeySettings(cfg, iface)

    # Give a warning if the dhcp block is missing, since it is likely
    # that developers will want a DHCP server to go with their AP.
    if 'dhcp' not in cfg:
        out.warn("No dhcp block found for interface {}; "
                 "will not run a DHCP server".format(name))


def getNetworkConfigVlan(update, name, cfg, iface):
    res = pdutils.check(cfg, dict, ['vlan_id'])
    if res:
        raise Exception("Interface definition missing field(s)")

    # Make a dictionary of old interfaces.  Any new interfaces that are
    # identical to an old one do not need to be changed.
    oldInterfaces = getInterfaceDict(update.old)
    if name in oldInterfaces:
        oldIface = oldInterfaces[iface['name']]
        subnet = oldIface['subnet']
        iface['externalIntf'] = oldIface['externalIntf']

    else:
        # Claim a subnet for this interface from the pool.
        subnet = chooseSubnet(update, cfg, iface)

        # Generate a name for the new interface in the host.
        iface['externalIntf'] = "br-lan.{}".format(cfg['vlan_id'])

        # Prevent multiple chutes from using the same VLAN.
        reservations = resources.getInterfaceReservations()
        if iface['externalIntf'] in reservations:
            raise Exception("Interface {} already in use".format(
                iface['externalIntf']))

    # Generate internal (in the chute) and external (in the host)
    # addresses.
    #
    # Example:
    # subnet: 192.168.30.0/24
    # netmask: 255.255.255.0
    # external: 192.168.30.1
    # internal: 192.168.30.2
    hosts = subnet.hosts()
    iface['subnet'] = subnet
    iface['netmask'] = str(subnet.netmask)
    iface['externalIpaddr'] = str(hosts.next())
    iface['internalIpaddr'] = str(hosts.next())

    # Generate the internal IP address with prefix length (x.x.x.x/y) for
    # convenience of other code that expect that format (e.g. pipework).
    iface['ipaddrWithPrefix'] = "{}/{}".format(
            iface['internalIpaddr'], subnet.prefixlen)


def getNetworkConfigLan(update, name, cfg, iface):
    # Make a dictionary of old interfaces.  Any new interfaces that are
    # identical to an old one do not need to be changed.
    oldInterfaces = getInterfaceDict(update.old)
    if name in oldInterfaces:
        oldIface = oldInterfaces[iface['name']]
        subnet = oldIface['subnet']
        iface['externalIntf'] = oldIface['externalIntf']

    else:
        # Claim a subnet for this interface from the pool.
        subnet = chooseSubnet(update, cfg, iface)
        iface['externalIntf'] = iface['device']

    # Generate internal (in the chute) and external (in the host)
    # addresses.
    #
    # Example:
    # subnet: 192.168.30.0/24
    # netmask: 255.255.255.0
    # external: 192.168.30.1
    # internal: 192.168.30.2
    hosts = subnet.hosts()
    iface['subnet'] = subnet
    iface['netmask'] = str(subnet.netmask)
    iface['externalIpaddr'] = str(hosts.next())
    iface['internalIpaddr'] = str(hosts.next())

    # Generate the internal IP address with prefix length (x.x.x.x/y) for
    # convenience of other code that expect that format (e.g. pipework).
    iface['ipaddrWithPrefix'] = "{}/{}".format(
            iface['internalIpaddr'], subnet.prefixlen)


def fulfillDeviceRequest(cfg, devices):
    """
    Find a physical device that matches the requested device type.

    Raises an exception if one cannot be found.
    """
    dtype = cfg['type']
    mode = cfg.get('mode', 'ap')

    # Get list of devices by requested type.
    devlist = devices.get(dtype, [])

    reservations = resources.getDeviceReservations()

    bestDevice = None
    bestScore = -1

    for device in devlist:
        dname = device['name']

        # Monitor, station, and airshark mode interfaces require exclusive
        # access to the device.
        if dtype == "wifi" and mode in ["monitor", "sta", "airshark"]:
            if reservations[dname].count() > 0:
                continue

            # Choose the first one that matches.
            bestDevice = device
            break

        # AP mode interfaces can share a device, but not with monitor mode or
        # station mode.
        elif dtype == "wifi" and mode == "ap":
            if reservations[dname].count(mode="monitor") > 0:
                continue
            if reservations[dname].count(mode="sta") > 0:
                continue

            apcount = reservations[dname].count(mode="ap")

            # Avoid exceeding the max. number of AP interfaces.
            if apcount >= MAX_AP_INTERFACES:
                continue

            # Otherwise, prefer interfaces that have at least one AP already.
            # This preference leaves interfaces available for other purposes
            # (e.g. monitor mode).
            if apcount > bestScore:
                bestDevice = device
                bestScore = apcount

        else:
            # Handle other devices types, namely "lan".  Assume they require
            # exclusive access.
            if reservations[dname].count() > 0:
                continue
            else:
                bestDevice = device
                break

    if bestDevice is not None:
        out.info("Assign device {} for requested type {}".format(bestDevice['name'], dtype))
        return bestDevice

    raise Exception("Could not satisfy requirement for device of type {}.".format(dtype))


def getExtraOptions(cfg):
    """
    Get dictionary of extra wifi-iface options that we are not interpreting but
    just passing on to pdconf.
    """
    # Using an 'options' object in the configuration is more extensible than
    # enumerating the possible extra options.
    options = cfg.get('options', {})

    # Deprecated: remove after 1.0 release.
    for key, value in cfg.iteritems():
        if key in IFACE_EXTRA_OPTIONS:
            options[key] = value

    return options


def getNetworkConfig(update):
    """
    For the Chute provided, return the dict object of a 100% filled out
    configuration set of network configuration. This would include determining
    what the IP addresses, interfaces names, etc...

    Store configuration in networkInterfaces cache entry.
    """

    # Notes:
    #
    # Fill in the gaps of knowledge between what the dev provided us in their
    # config and what would be required to add all the config changes to get
    # the chute working By the end of this function there should be a
    # cache:networkInterfaces key containing a list of dictionary objects for
    # each interface we will need to create, including netmasks IP addresses,
    # etc.. this is important to allow all other modules to know what the IP
    # addr is for this chute, etc..
    #
    # old code under lib.internal.chs.chutelxc same function name

    interfaces = list()

    # Put the list in the cache now (shared reference), so that if we fail out
    # of this function after partial completion, the abort function can take
    # care of what made it into the list.
    update.new.setCache('networkInterfaces', interfaces)
    if not hasattr(update.new, 'net'):
        return None

    # Make a dictionary of old interfaces.  Any new interfaces that are
    # identical to an old one do not need to be changed.
    oldInterfaces = getInterfaceDict(update.old)

    devices = update.new.getCache('networkDevices')

    for name, cfg in update.new.net.iteritems():
        # Check for required fields.
        res = pdutils.check(cfg, dict, ['intfName', 'type'])
        if res:
            out.warn('Network interface definition {}\n'.format(res))
            raise Exception("Interface definition missing field(s)")

        iface = {
            'name': name,                           # Name (not used?)
            'netType': cfg['type'],                 # Type (wan, lan, wifi)
            'internalIntf': cfg['intfName']         # Interface name in chute
        }

        if cfg['type'] == "wifi":
            oldIface = oldInterfaces.get(name, None)
            if oldIface is None or oldIface['netType'] != iface['netType']:
                # Try to find a physical device of the requested type.
                #
                # Note: we try this first because it can fail, and then we will not try
                # to allocate any resources for it.
                device = fulfillDeviceRequest(cfg, devices)
                iface['device'] = device['name']
                iface['phy'] = device['phy']
            else:
                iface['device'] = oldIface['device']
                iface['phy'] = oldIface['phy']

            getNetworkConfigWifi(update, name, cfg, iface)

        elif cfg['type'] == "vlan":
            getNetworkConfigVlan(update, name, cfg, iface)


        elif cfg['type'] == "lan":
            oldIface = oldInterfaces.get(name, None)
            if oldIface is None or oldIface['netType'] != iface['netType']:
                device = fulfillDeviceRequest(cfg, devices)
                iface['device'] = device['name']
            else:
                iface['device'] = oldIface['device']

            getNetworkConfigLan(update, name, cfg, iface)

        else:
            raise Exception("Unsupported network type, {}".format(cfg['type']))

        # Pass on DHCP configuration if it exists.
        if 'dhcp' in cfg:
            iface['dhcp'] = cfg['dhcp']

        # TODO: Refactor!  The problem here is that `cfg` contains a mixture of
        # fields, some that we will interpret and some that we will pass on to
        # pdconf.  The result is a lot of logic that tests and copies without
        # actually accomplishing much.
        iface['options'] = getExtraOptions(cfg)

        interfaces.append(iface)

    update.new.setCache('networkInterfaces', interfaces)


def abortNetworkConfig(update):
    """
    Release resources claimed by chute network configuration.
    """
    pass


def getOSNetworkConfig(update):
    """
    Takes the network interface obj created by
    NetworkManager.getNetworkConfiguration and returns a properly formatted
    object to be passed to the UCIConfig class.  The object returned is a
    list of tuples (config, options).
    """
    # Notes:
    #
    # Take the cache:networkInterfaces key and generate the UCI specific OS
    # config we need to inact the chute changes, should create a
    # cache:osNetworkConfig key
    #
    # old code under lib.internal.chs.chutelxc same function name

    # Make a dictionary of old interfaces, then remove them as we go
    # through the new interfaces.  Anything remaining should be freed.
    removedInterfaces = getInterfaceDict(update.old)

    interfaces = update.new.getCache('networkInterfaces')

    osNetwork = list()

    for iface in interfaces:
        config = {'type': 'interface', 'name': iface['externalIntf']}

        if iface['netType'] == "wifi" and iface.get('mode', 'ap') == "monitor":
            # Monitor mode is a special case - do not configure an IP address
            # for it.
            options = {
                'proto': 'none',
                'ifname': iface['externalIntf']
            }

        else:
            options = {
                'proto': 'static',
                'ipaddr': iface['externalIpaddr'],
                'netmask': iface['netmask'],
                'ifname': iface['externalIntf']
            }

        # Add to our OS Network
        osNetwork.append((config, options))

        # This interface is still in use, so take it out of the remove set.
        if iface['name'] in removedInterfaces:
            del removedInterfaces[iface['name']]

    update.new.setCache('osNetworkConfig', osNetwork)


def setOSNetworkConfig(update):
    """
    Takes a list of tuples (config, opts) and saves it to the network config
    file.
    """

    # Notes:
    #
    # Takes the config generated (cache:osNetworkConfig) and uses the UCI
    # module to actually push it to the disk
    #
    # old code under lib.internal.chs.chutelxc same function name

    changed = uciutils.setConfig(update.new, update.old,
                                 cacheKeys=['osNetworkConfig'],
                                 filepath=uci.getSystemPath("network"))


def getVirtNetworkConfig(update):
    out.warn('TODO implement me\n')
    # old code under lib.internal.chs.chutelxc same function name
    # Takes any network specific config and sets up the cache:virtNetworkConfig
    # this would be the place to put anything into HostConfig or the dockerfile
    # we need
