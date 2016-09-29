###################################################################
# Copyright 2013-2015 All Rights Reserved
# Authors: The Paradrop Team
###################################################################

"""
Functions associated with deploying and cleaning up docker containers.
"""

from pdtools.lib.output import out
import docker
import json
import os
import re
import subprocess

from paradrop.lib import settings
from paradrop.lib.container.downloader import downloader
from pdtools.lib import nexus


DOCKER_CONF = """
# Docker systemd configuration
#
# This configuration file was automatically generated by Paradrop.  Any changes
# will be overwritten on startup.

# Tell docker not to start containers automatically on startup.
DOCKER_OPTIONS="--restart=false"
"""

# Used to match and suppress noisy progress messages from Docker output.
#
# Example:
# Extracting
# 862a3e9af0ae
# [================================================>  ] 64.06 MB/65.7 MB
suppress_re = re.compile("^(Downloading|Extracting|[a-z0-9]+|\[=*>?\s*\].*)$")


def getImageName(chute):
    if hasattr(chute, 'version'):
        return "{}:{}".format(chute.name, chute.version)
    else:
        # Compatibility with old chutes missing version numbers.
        return "{}:latest".format(chute.name)


def writeDockerConfig():
    """
    Write options to Docker configuration.

    Mainly, we want to tell Docker not to start containers automatically on
    system boot.
    """
    # First we have to find the configuration file.  On Snappy, it should be in
    # "/var/lib/apps/docker/{version}/etc/docker.conf", but version could
    # change.
    path = "/var/lib/apps/docker"
    if not os.path.exists(path):
        out.warn('No directory "{}" found'.format(path))
        return False

    written = False
    for d in os.listdir(path):
        finalPath = os.path.join(path, d, "etc/docker.conf")
        if not os.path.exists(finalPath):
            continue

        try:
            with open(finalPath, "w") as output:
                output.write(DOCKER_CONF)
            written = True
        except Exception as e:
            out.warn('Error writing to {}: {}'.format(finalPath, str(e)))

    if not written:
        out.warn('Could not write docker configuration.')
    return written


def buildImage(update):
    """
    Build the Docker image and monitor progress.
    """
    out.info('Building image for {}\n'.format(update.new))

    client = docker.Client(base_url="unix://var/run/docker.sock", version='auto')

    repo = getImageName(update.new)

    if hasattr(update, 'dockerfile'):
        buildSuccess = _buildImage(update, client, rm=True, tag=repo,
                fileobj=update.dockerfile)
    elif hasattr(update, 'download'):
        # download field should be an object with at least 'url' but may also
        # contain 'user' and 'secret' for authentication.
        download_args = update.download
        with downloader(**download_args) as dl:
            workDir, meta = dl.fetch()
            buildSuccess = _buildImage(update, client, rm=True, tag=repo,
                    path=workDir)
    else:
        raise Exception("No Dockerfile or download location supplied.")

    #If we failed to build skip creating and starting clean up and fail
    if not buildSuccess:
        raise Exception("Building docker image failed; check your Dockerfile for errors.")


def _buildImage(update, client, **buildArgs):
    """
    Build the Docker image and monitor progress (worker function).

    Returns True on success, False on failure.
    """
    output = client.build(**buildArgs)

    buildSuccess = True
    for line in output:
        #if we encountered an error make note of it
        if 'errorDetail' in line:
            buildSuccess = False

        for key, value in json.loads(line).iteritems():
            if isinstance(value, dict):
                continue
            else:
                msg = str(value).rstrip()
                if len(msg) > 0 and suppress_re.match(msg) is None:
                    update.progress(msg)

    return buildSuccess


def removeNewImage(update):
    """
    Remove the newly built image during abort sequence.
    """
    _removeImage(update.new)


def removeOldImage(update):
    """
    Remove the image for the old version of the chute.
    """
    _removeImage(update.old)


def _removeImage(chute):
    """
    Remove the image for a chute.
    """
    image = getImageName(chute)
    out.info("Removing image {}\n".format(image))

    try:
        client = docker.Client(base_url="unix://var/run/docker.sock",
                version='auto')
        client.remove_image(image=image)
    except Exception as error:
        out.warn("Error removing image: {}".format(error))


def startChute(update):
    """
    Create a docker container based on the passed in update.
    """
    _startChute(update.new)


def startOldContainer(update):
    """
    Create a docker container using the old version of the image.
    """
    _startChute(update.old)


def _startChute(chute):
    """
    Create a docker container based on the passed in chute object.
    """
    out.info('Attempting to start new Chute %s \n' % (chute.name))

    repo = getImageName(chute)
    name = chute.name

    c = docker.Client(base_url="unix://var/run/docker.sock", version='auto')

    host_config = build_host_config(chute, c)

    # Set environment variables for the new container.
    # PARADROP_ROUTER_ID can be used to change application behavior based on
    # what router it is running on.
    environment = prepare_environment(chute)

    try:
        container = c.create_container(
            image=repo, name=name, host_config=host_config,
            environment=environment
        )
        c.start(container.get('Id'))
        out.info("Successfully started chute with Id: %s\n" % (str(container.get('Id'))))
    except Exception as e:
        raise e

    setup_net_interfaces(chute)


def removeNewContainer(update):
    """
    Remove the newly started container during abort sequence.
    """
    name = update.new.name
    out.info("Removing container {}\n".format(name))

    try:
        client = docker.Client(base_url="unix://var/run/docker.sock",
                version='auto')
        client.remove_container(container=name, force=True)
    except Exception as error:
        out.warn("Error removing container: {}".format(error))


def removeChute(update):
    """
    Remove a docker container and the image it was built on based on the passed in update.

    :param update: The update object containing information about the chute.
    :type update: obj
    :returns: None
    """
    out.info('Attempting to remove chute %s\n' % (update.name))
    c = docker.Client(base_url='unix://var/run/docker.sock', version='auto')
    repo = getImageName(update.old)
    name = update.name

    try:
        c.remove_container(container=name, force=True)
    except Exception as e:
        update.progress(str(e))

    try:
        c.remove_image(image=repo)
    except Exception as e:
        update.progress(str(e))


def removeOldContainer(update):
    """
    Remove the docker container for the old version of a chute.

    :param update: The update object containing information about the chute.
    :type update: obj
    :returns: None
    """
    out.info('Attempting to remove chute %s\n' % (update.name))
    client = docker.Client(base_url='unix://var/run/docker.sock', version='auto')

    try:
        client.remove_container(container=update.old.name, force=True)
    except Exception as e:
        update.progress(str(e))


def stopChute(update):
    """
    Stop a docker container based on the passed in update.

    :param update: The update object containing information about the chute.
    :type update: obj
    :returns: None
    """
    out.info('Attempting to stop chute %s\n' % (update.name))
    c = docker.Client(base_url='unix://var/run/docker.sock', version='auto')
    c.stop(container=update.name)

def restartChute(update):
    """
    Start a docker container based on the passed in update.

    :param update: The update object containing information about the chute.
    :type update: obj
    :returns: None
    """
    out.info('Attempting to restart chute %s\n' % (update.name))
    c = docker.Client(base_url='unix://var/run/docker.sock', version='auto')
    c.start(container=update.name)

    setup_net_interfaces(update.new)

def build_host_config(chute, client=None):
    """
    Build the host_config dict for a docker container based on the passed in update.

    :param chute: The chute object containing information about the chute.
    :type chute: obj
    :param client: Docker client object.
    :returns: (dict) The host_config dict which docker needs in order to create the container.
    """
    if client is None:
        client = docker.Client(base_url="unix://var/run/docker.sock", version='auto')

    if not hasattr(chute, 'host_config') or chute.host_config == None:
        config = dict()
    else:
        config = chute.host_config

    host_conf = client.create_host_config(
        #TO support
        port_bindings=config.get('port_bindings'),
        dns=config.get('dns'),
        #not supported/managed by us
        #network_mode=update.host_config.get('network_mode'),
        network_mode='bridge',
        #extra_hosts=update.host_config.get('extra_hosts'),
        #binds=config.get('binds'),
        #links=config.get('links'),
        restart_policy={'MaximumRetryCount': 5, 'Name': 'on-failure'},
        devices=[],
        lxc_conf={},
        publish_all_ports=False,
        privileged=False,
        dns_search=[],
        volumes_from=None,
        cap_add=['NET_ADMIN'],
        cap_drop=[]
    )
    return host_conf


def setup_net_interfaces(chute):
    """
    Link interfaces in the host to the internal interface in the docker container using pipework.

    :param chute: The chute object containing information about the chute.
    :type update: obj
    :returns: None
    """
    interfaces = chute.getCache('networkInterfaces')
    for iface in interfaces:
        if iface.get('netType') == 'wifi':
            IP = iface.get('ipaddrWithPrefix')
            internalIntf = iface.get('internalIntf')
            externalIntf = iface.get('externalIntf')
        else: # pragma: no cover
            continue

        # Construct environment for pipework call.  It only seems to require
        # the PATH variable to include the directory containing the docker
        # client.  On Snappy this was not happening by default, which is why
        # this code is here.
        env = {"PATH": os.environ.get("PATH", "")}
        if settings.DOCKER_BIN_DIR not in env['PATH']:
            env['PATH'] += ":" + settings.DOCKER_BIN_DIR

        cmd = ['/apps/paradrop/current/bin/pipework', externalIntf, '-i',
               internalIntf, chute.name,  IP]
        out.info("Calling: {}\n".format(" ".join(cmd)))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
            for line in proc.stdout:
                out.info("pipework: {}\n".format(line.strip()))
            for line in proc.stderr:
                out.warn("pipework: {}\n".format(line.strip()))
        except OSError as e:
            out.warn('Command "{}" failed\n'.format(" ".join(cmd)))
            out.exception(e, True)
            raise e


def prepare_environment(chute):
    """
    Prepare environment variables for a chute container.
    """
    env = getattr(chute, 'environment', {})

    env['PARADROP_CHUTE_NAME'] = chute.name
    env['PARADROP_ROUTER_ID'] = nexus.core.info.pdid

    if hasattr(chute, 'version'):
        env['PARADROP_CHUTE_VERSION'] = chute.version

    return env