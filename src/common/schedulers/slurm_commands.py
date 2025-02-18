# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List

from common.utils import check_command_output, grouper, run_command, validate_subprocess_argument
from retrying import retry
from slurm_plugin.slurm_resources import (
    DynamicNode,
    InvalidNodenameError,
    PartitionStatus,
    SlurmNode,
    SlurmPartition,
    StaticNode,
    parse_nodename,
)

log = logging.getLogger(__name__)

PENDING_RESOURCES_REASONS = [
    "Resources",
    "Nodes required for job are DOWN, DRAINED or reserved for jobs in higher priority partitions",
    "BeginTime",
    "NodeDown",
    "Priority",
    "ReqNodeNotAvail, May be reserved for other job",
]

SQUEUE_FIELD_SIZE = 200
_SQUEUE_FIELDS = [
    "jobid",
    "statecompact",
    "numnodes",
    "numcpus",
    "numtasks",
    "cpus-per-task",
    "mincpus",
    "reason",
    "tres-per-job",
    "tres-per-task",
    "tres-per-node",
    "cpus-per-tres",
]
SQUEUE_FIELD_STRING = ",".join([field + ":{size}" for field in _SQUEUE_FIELDS]).format(size=SQUEUE_FIELD_SIZE)
SLURM_BINARIES_DIR = os.environ.get("SLURM_BINARIES_DIR", "/opt/slurm/bin")
SLURM_CONF_DIR = os.path.join(os.path.split(SLURM_BINARIES_DIR)[0], "etc")
SCONTROL = f"sudo {SLURM_BINARIES_DIR}/scontrol"
SINFO = f"{SLURM_BINARIES_DIR}/sinfo"

SCONTROL_OUTPUT_AWK_PARSER = (
    'awk \'BEGIN{{RS="\\n\\n" ; ORS="######\\n";}} {{print}}\' | '
    + "grep -oP '^(NodeName=\\S+)|(NodeAddr=\\S+)|(NodeHostName=\\S+)|(?<!Next)(State=\\S+)|"
    + "(Partitions=\\S+)|(SlurmdStartTime=\\S+)|(LastBusyTime=\\S+)|(Reason=.*)|(######)'"
)

# Set default timeouts for running different slurm commands.
# These timeouts might be needed when running on large scale
DEFAULT_GET_INFO_COMMAND_TIMEOUT = 30
DEFAULT_UPDATE_COMMAND_TIMEOUT = 60


class PartitionNodelistMapping:
    """
    Singleton class to represent the partition-nodelist mapping between PC-managed partitions and PC-managed nodes.

    The information contained in this singleton is used to make sure ParallelCluster only handles partitions and nodes
    it is supposed to manage (via Slurm commands).
    """

    partition_nodelist_mapping: Dict[str, str]
    _instance = None

    def __init__(self):
        self.partition_nodelist_mapping = {}

    def get_partition_nodelist_mapping(self) -> Dict[str, str]:
        """Retrieve partition-nodelist mapping from JSON file."""
        if not self.partition_nodelist_mapping:
            partition_nodelist_json = os.path.join(
                SLURM_CONF_DIR,
                "pcluster/parallelcluster_partition_nodelist_mapping.json",
            )
            with open(partition_nodelist_json, "r", encoding="utf-8") as file:
                self.partition_nodelist_mapping = json.load(file)
        return self.partition_nodelist_mapping

    def get_partitions(self) -> List[str]:
        """Retrieve partitions from JSON file."""
        return list(self.get_partition_nodelist_mapping().keys())

    @staticmethod
    def instance():
        """Return the singleton PartitionNodelistMapping instance."""
        if not PartitionNodelistMapping._instance:
            PartitionNodelistMapping._instance = PartitionNodelistMapping()
        return PartitionNodelistMapping._instance

    @staticmethod
    def reset():
        """Reset the instance to clear all caches."""
        PartitionNodelistMapping._instance = None


def is_static_node(nodename):
    """
    Check if the node is static or dynamic.

    Valid NodeName format: {queue_name}-{st/dy}-{instancetype}-{number}
    """
    _, node_type, _ = parse_nodename(nodename)
    return "st" == node_type


def update_nodes(
    nodes,
    nodeaddrs=None,
    nodehostnames=None,
    state=None,
    reason=None,
    raise_on_error=True,
    command_timeout=DEFAULT_UPDATE_COMMAND_TIMEOUT,
):
    """
    Update slurm nodes with scontrol call.

    Slurm can process 10000 nodes in range format.
    Max range is somewhere below 100000, then we see the following error:
    fatal: _parse_single_range: Too many hosts in range '1-100000'

    To safely execute update command, run in batches of 100.
    Inputs can be string or other iterables.

    When there is an error with scontrol update, slurm will try to update as much as it can.
    For example, if one node in a batch failed, the rest of the nodes will still be updated.
    With the node that failed, slurm will try to update attributes that do not have error.
    For example, if updating a state cause failure, but updating nodeaddr cause no failure.
    if we run scontrol update state=fail_state nodeaddr=good_addr nodename=name,
    the scontrol command will fail but nodeaddr will be updated to good_addr.
    """
    batched_node_info = _batch_node_info(nodes, nodeaddrs, nodehostnames, batch_size=100)

    update_cmd = f"{SCONTROL} update"
    if state:
        validate_subprocess_argument(state)
        update_cmd += f" state={state}"
    if reason:
        validate_subprocess_argument(reason)
        update_cmd += f' reason="{reason}"'
    for nodenames, addrs, hostnames in batched_node_info:
        validate_subprocess_argument(nodenames)
        node_info = f"nodename={nodenames}"
        if addrs:
            validate_subprocess_argument(addrs)
            node_info += f" nodeaddr={addrs}"
        if hostnames:
            validate_subprocess_argument(hostnames)
            node_info += f" nodehostname={hostnames}"
        # It's safe to use the function affected by B604 since the command is fully built in this code
        run_command(  # nosec B604
            f"{update_cmd} {node_info}", raise_on_error=raise_on_error, timeout=command_timeout, shell=True
        )


def update_partitions(partitions, state):
    succeeded_partitions = []
    # Validation to sanitize the input argument and make it safe to use the function affected by B604
    validate_subprocess_argument(state)
    for partition in partitions:
        try:
            # Validation to sanitize the input argument and make it safe to use the function affected by B604
            validate_subprocess_argument(partition)
            run_command(  # nosec B604
                f"{SCONTROL} update partitionname={partition} state={state}", raise_on_error=True, shell=True
            )
            succeeded_partitions.append(partition)
        except Exception as e:
            log.error("Failed when setting partition %s to %s with error %s", partition, state, e)

    return succeeded_partitions


def update_all_partitions(state, reset_node_addrs_hostname):
    """Update partitions to a state and reset nodesaddr/nodehostname if needed."""
    try:
        # Get all nodes from partition as opposed to ignoring power_down nodes
        partitions = get_partitions_info()
        partition_to_update = []
        for part in partitions:
            if PartitionStatus(part.state) != PartitionStatus(state):
                log.info("Setting partition %s state from %s to %s", part.name, part.state, state)
                if reset_node_addrs_hostname:
                    log.info("Resetting partition nodes %s", part.nodenames)
                    set_nodes_power_down(part.nodenames, reason="stopping cluster")
                partition_to_update.append(part.name)
        succeeded_partitions = update_partitions(partition_to_update, state)
        return succeeded_partitions == partition_to_update
    except Exception as e:
        log.error("Failed when updating partitions with error %s", e)
        return False


def _batch_attribute(attribute, batch_size, expected_length=None):
    """Parse an attribute into batches."""
    if type(attribute) is str:
        attribute = re.split("(?<=]),", attribute)
    if expected_length and len(attribute) != expected_length:
        raise ValueError

    return [",".join(batch) for batch in grouper(attribute, batch_size)]


def _batch_node_info(nodenames, nodeaddrs, nodehostnames, batch_size):
    """Group nodename, nodeaddrs, nodehostnames into batches."""
    if type(nodenames) is str:
        # Only split on , if there is ] before
        # For ex. "node-[1,3,4-5],node-[20,30]" should split into ["node-[1,3,4-5]","node-[20,30]"]
        nodenames = re.split("(?<=]),", nodenames)
    nodename_batch = _batch_attribute(nodenames, batch_size)
    nodeaddrs_batch = [None] * len(nodename_batch)
    nodehostnames_batch = [None] * len(nodename_batch)
    if nodeaddrs:
        try:
            nodeaddrs_batch = _batch_attribute(nodeaddrs, batch_size, expected_length=len(nodenames))
        except ValueError:
            log.error("Nodename %s and NodeAddr %s contain different number of entries", nodenames, nodeaddrs)
            raise
    if nodehostnames:
        try:
            nodehostnames_batch = _batch_attribute(nodehostnames, batch_size, expected_length=len(nodenames))
        except ValueError:
            log.error("Nodename %s and NodeHostname %s contain different number of entries", nodenames, nodehostnames)
            raise

    return zip(nodename_batch, nodeaddrs_batch, nodehostnames_batch)


def set_nodes_down(nodes, reason):
    """Place slurm node into down state, reason is required."""
    update_nodes(nodes, state="down", reason=reason)


def set_nodes_drain(nodes, reason):
    """Place slurm node into down state, reason is required."""
    update_nodes(nodes, state="drain", reason=reason)


@retry(stop_max_attempt_number=3, wait_fixed=1500)
def set_nodes_power_down(nodes, reason=None):
    """Place slurm node into power_down state and reset nodeaddr/nodehostname."""
    reset_nodes(nodes=nodes, state="power_down_force", reason=reason, raise_on_error=True)


def reset_nodes(nodes, state=None, reason=None, raise_on_error=False):
    """Reset nodeaddr and nodehostname to be equal to nodename."""
    update_nodes(
        nodes=nodes, nodeaddrs=nodes, nodehostnames=nodes, state=state, reason=reason, raise_on_error=raise_on_error
    )


def set_nodes_idle(nodes, reason=None, reset_node_addrs_hostname=False):
    """
    Place slurm node into idle state.

    Do not raise on error.
    Failure for resume command will fail if node already in IDLE, ignore failure.
    """
    if reset_node_addrs_hostname:
        # slurm supports updating multiple nodeaddr/nodehostname at the same time
        # however if number of nodeaddr/nodehostname entries != number of nodes update will fail
        # works: scontrol update nodename=c5.2xlarge-[1-2] nodeaddr=c5.2xlarge-[1-2]
        # works: scontrol update nodename=c5.2xlarge-[1-2] nodeaddr="some ip","some ip"
        # fails: scontrol update nodename=c5.2xlarge-[1-2] nodeaddr="some ip"
        reset_nodes(nodes, state="resume", reason=reason, raise_on_error=False)
    else:
        update_nodes(nodes=nodes, state="resume", reason=reason, raise_on_error=False)


def get_nodes_info(nodes: str = "", command_timeout=DEFAULT_GET_INFO_COMMAND_TIMEOUT) -> List[SlurmNode]:
    """
    Retrieve SlurmNode list from slurm nodelist notation.

    Sample slurm nodelist notation: queue1-dy-c5_xlarge-[1-3],queue2-st-t2_micro-5.
    If no nodes argument is provided, this function considers only nodes in partitions managed by ParallelCluster.
    It is responsibility of the caller to pass a nodes argument with only nodes in partitions managed by
    ParallelCluster.

    TODO: we can consider building a filter to be used in case a nodes argument is passed, in order to exclude nodes
     not managed by ParallelCluster.
    """
    if nodes == "":
        nodes = _get_all_partition_nodes(",".join(PartitionNodelistMapping.instance().get_partitions()))

    # Validation to sanitize the input argument and make it safe to use the function affected by B604
    validate_subprocess_argument(nodes)

    # awk is used to replace the \n\n record separator with '######\n'
    # Note: In case the node does not belong to any partition the Partitions field is missing from Slurm output
    show_node_info_command = f"{SCONTROL} show nodes {nodes} | {SCONTROL_OUTPUT_AWK_PARSER}"
    nodeinfo_str = check_command_output(show_node_info_command, timeout=command_timeout, shell=True)  # nosec B604

    return _parse_nodes_info(nodeinfo_str)


def get_partitions_info(command_timeout=DEFAULT_GET_INFO_COMMAND_TIMEOUT) -> List[SlurmPartition]:
    """
    Retrieve slurm partition info from scontrol.

    This function considers only partitions managed by ParallelCluster.
    """
    partitions = list(PartitionNodelistMapping.instance().get_partitions())
    grep_filter = _get_partition_grep_filter(partitions)
    show_partition_info_command = (
        f"{SCONTROL} show partitions -o {grep_filter} " + '| grep -oP "^PartitionName=\\K(\\S+)| State=\\K(\\S+)"'
    )
    # It's safe to use the function affected by B604 since the command is fully built in this code
    partition_info_str = check_command_output(
        show_partition_info_command, timeout=command_timeout, shell=True  # nosec B604
    )
    partitions_info = _parse_partition_name_and_state(partition_info_str)
    return [
        SlurmPartition(
            partition_name,
            _get_all_partition_nodes(partition_name),
            partition_state,
        )
        for partition_name, partition_state in partitions_info
    ]


def _get_partition_grep_filter(partitions: List[str]) -> str:
    grep_filter = ""
    if partitions:
        grep_filter += " | grep"
        for partition in partitions:
            grep_filter += f' -e "PartitionName={partition}"'
    return grep_filter


def resume_powering_down_nodes():
    """Resume nodes that are powering_down so that are set in power state right away."""
    # TODO: This function was added due to Slurm ticket 12915. The bug is not reproducible and the ticket was then
    #  closed. This operation may now be useless: we need to check this.
    log.info("Resuming powering down nodes.")
    powering_down_nodes = _get_slurm_nodes(states="powering_down")
    update_nodes(nodes=powering_down_nodes, state="resume", raise_on_error=False)


def _parse_partition_name_and_state(partition_info):
    """Parse partition name and state from scontrol output."""
    return grouper(partition_info.splitlines(), 2)


def _get_all_partition_nodes(partition_name, command_timeout=DEFAULT_GET_INFO_COMMAND_TIMEOUT):
    """Get all nodes in partition."""
    # Validation to sanitize the input argument and make it safe to use the function affected by B604
    validate_subprocess_argument(partition_name)

    show_all_nodes_command = f"{SINFO} -h -p {partition_name} -o %N"
    return check_command_output(show_all_nodes_command, timeout=command_timeout, shell=True).strip()  # nosec B604


def _get_slurm_nodes(states=None, partition_name=None, command_timeout=DEFAULT_GET_INFO_COMMAND_TIMEOUT):
    sinfo_command = f"{SINFO} -h -N -o %N"
    partition_name = partition_name or ",".join(PartitionNodelistMapping.instance().get_partitions())
    validate_subprocess_argument(partition_name)
    sinfo_command += f" -p {partition_name}"
    if states:
        validate_subprocess_argument(states)
        sinfo_command += f" -t {states}"
    # Every node is print on a separate line
    # It's safe to use the function affected by B604 since the command is fully built in this code
    return check_command_output(sinfo_command, timeout=command_timeout, shell=True).splitlines()  # nosec B604


def _parse_nodes_info(slurm_node_info: str) -> List[SlurmNode]:
    """Parse slurm node info into SlurmNode objects."""
    # [ec2-user@ip-10-0-0-58 ~]$ /opt/slurm/bin/scontrol show nodes compute-dy-c5xlarge-[1-3],compute-dy-c5xlarge-50001\
    # | awk 'BEGIN{{RS="\n\n" ; ORS="######\n";}} {{print}}' | grep -oP "^(NodeName=\S+)|(NodeAddr=\S+)
    # |(NodeHostName=\S+)|(?<!Next)(State=\S+)|(Partitions=\S+)|(SlurmdStartTime=\S+)|(LastBusyTime=\\S+)|(Reason=.*)\
    # |(######)"
    # NodeName=compute-dy-c5xlarge-1
    # NodeAddr=1.2.3.4
    # NodeHostName=compute-dy-c5xlarge-1
    # State=IDLE+CLOUD+POWER
    # Partitions=compute,compute2
    # SlurmdStartTime=2023-01-26T09:57:15
    # Reason=some reason
    # ######
    # NodeName=compute-dy-c5xlarge-2
    # NodeAddr=1.2.3.4
    # NodeHostName=compute-dy-c5xlarge-2
    # State=IDLE+CLOUD+POWER
    # Partitions=compute,compute2
    # SlurmdStartTime=2023-01-26T09:57:15
    # Reason=(Code:InsufficientInstanceCapacity)Failure when resuming nodes
    # ######
    # NodeName=compute-dy-c5xlarge-3
    # NodeAddr=1.2.3.4
    # NodeHostName=compute-dy-c5xlarge-3
    # State=IDLE+CLOUD+POWER
    # Partitions=compute,compute2
    # SlurmdStartTime=2023-01-26T09:57:15
    # ######
    # NodeName=compute-dy-c5xlarge-50001
    # NodeAddr=1.2.3.4
    # NodeHostName=compute-dy-c5xlarge-50001
    # State=IDLE+CLOUD+POWER
    # SlurmdStartTime=None
    # ######

    map_slurm_key_to_arg = {
        "NodeName": "name",
        "NodeAddr": "nodeaddr",
        "NodeHostName": "nodehostname",
        "State": "state",
        "Partitions": "partitions",
        "Reason": "reason",
        "SlurmdStartTime": "slurmdstarttime",
        "LastBusyTime": "lastbusytime",
    }

    date_fields = ["SlurmdStartTime", "LastBusyTime"]

    node_info = slurm_node_info.split("######\n")
    slurm_nodes = []
    for node in node_info:
        lines = node.splitlines()
        kwargs = {}
        for line in lines:
            key, value = line.split("=")
            if key in date_fields:
                if value not in ["None", "Unknown"]:
                    value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").astimezone(tz=timezone.utc)
                else:
                    value = None
            kwargs[map_slurm_key_to_arg[key]] = value
        if lines:
            try:
                if is_static_node(kwargs["name"]):
                    node = StaticNode(**kwargs)
                    slurm_nodes.append(node)
                else:
                    node = DynamicNode(**kwargs)
                    slurm_nodes.append(node)
            except InvalidNodenameError:
                log.warning("Ignoring node %s because it has an invalid name", kwargs["name"])

    return slurm_nodes
