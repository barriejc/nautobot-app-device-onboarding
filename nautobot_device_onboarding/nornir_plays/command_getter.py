"""CommandGetter."""

# pylint: disable=relative-beyond-top-level
from nautobot.dcim.models import Platform
from nautobot.extras.models import SecretsGroup
from nautobot_plugin_nornir.constants import NORNIR_SETTINGS
from nautobot_plugin_nornir.plugins.inventory.nautobot_orm import NautobotORMInventory
from nornir import InitNornir
from nornir.core.plugins.inventory import InventoryPluginRegister, TransformFunctionRegister
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command

from nautobot_device_onboarding.constants import NETMIKO_TO_NAPALM_STATIC
from nautobot_device_onboarding.nornir_plays.empty_inventory import EmptyInventory
from nautobot_device_onboarding.nornir_plays.logger import NornirLogger
from nautobot_device_onboarding.nornir_plays.processor import ProcessorDO
from nautobot_device_onboarding.utils.helper import add_platform_parsing_info
from nautobot_device_onboarding.utils.inventory_creator import _set_inventory

InventoryPluginRegister.register("nautobot-inventory", NautobotORMInventory)
InventoryPluginRegister.register("empty-inventory", EmptyInventory)
TransformFunctionRegister.register("transform_to_add_command_parser_info", add_platform_parsing_info)


def _get_commands_to_run(yaml_parsed_info, command_getter_job):
    """Load yaml file and look up all commands that need to be run."""
    commands = []
    for _, value in yaml_parsed_info[command_getter_job].items():
        # Deduplicate commands + parser key
        if value["command"] not in commands:
            commands.append(value["command"])
    return commands


def netmiko_send_commands(task: Task, command_getter_job: str):
    """Run commands specified in PLATFORM_COMMAND_MAP."""
    if not task.host.platform:
        return Result(host=task.host, result=f"{task.host.name} has no platform set.", failed=True)
    if task.host.platform not in list(NETMIKO_TO_NAPALM_STATIC.keys()):
        return Result(host=task.host, result=f"{task.host.name} has a unsupported platform set.", failed=True)
    commands = _get_commands_to_run(task.host.data["platform_parsing_info"], command_getter_job)
    for command in commands:
        task.run(
            task=netmiko_send_command,
            name=command["command"],
            command_string=command["command"],
            use_textfsm=command["use_textfsm"],
            read_timeout=60,
        )


def command_getter_do(job_result, log_level, kwargs):
    """Nornir play to run show commands."""
    logger = NornirLogger(job_result, log_level)

    if kwargs["csv_file"]:  # ip_addreses will be keys in a dict
        ip_addresses = []
        for ip_address in kwargs["csv_file"]:
            ip_addresses.append(ip_address)
    else:
        ip_addresses = kwargs["ip_addresses"].replace(" ", "").split(",")
        port = kwargs["port"]
        # timeout = kwargs["timeout"]
        secrets_group = kwargs["secrets_group"]
        platform = kwargs["platform"]

    # Initiate Nornir instance with empty inventory
    try:
        logger = NornirLogger(job_result, log_level=0)
        compiled_results = {}
        with InitNornir(
            runner=NORNIR_SETTINGS.get("runner"),
            logging={"enabled": False},
            inventory={
                "plugin": "empty-inventory",
            },
        ) as nornir_obj:
            nr_with_processors = nornir_obj.with_processors([ProcessorDO(logger, compiled_results, kwargs)])
            for entered_ip in ip_addresses:
                if kwargs["csv_file"]:
                    # get platform if one was provided via csv
                    platform = None
                    platform_id = kwargs["csv_file"][entered_ip]["platform"]
                    if platform_id:
                        platform = Platform.objects.get(id=platform_id)

                    # get secrets group if one was provided via csv
                    secrets_group = None
                    secrets_group_id = kwargs["csv_file"][entered_ip]["secrets_group"]
                    if secrets_group_id:
                        secrets_group = SecretsGroup.objects.get(id=secrets_group_id)
                    single_host_inventory_constructed = _set_inventory(
                        host_ip=entered_ip,
                        platform=platform,
                        port=kwargs["csv_file"][entered_ip]["port"],
                        secrets_group=secrets_group,
                    )
                else:
                    single_host_inventory_constructed = _set_inventory(entered_ip, platform, port, secrets_group)
                nr_with_processors.inventory.hosts.update(single_host_inventory_constructed)
            nr_with_processors.run(task=netmiko_send_commands, command_getter_job="device_onboarding")
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.error("Error: %s", err)
        return err
    return compiled_results


def command_getter_ni(job_result, log_level, kwargs):
    """Process onboarding task from ssot-ni job."""
    logger = NornirLogger(job_result, log_level)
    try:
        compiled_results = {}
        # qs = get_job_filter(kwargs)
        qs = kwargs["devices"]
        if not qs:
            return None
        with InitNornir(
            runner=NORNIR_SETTINGS.get("runner"),
            logging={"enabled": False},
            inventory={
                "plugin": "nautobot-inventory",
                "options": {
                    "credentials_class": NORNIR_SETTINGS.get("credentials"),
                    "queryset": qs,
                },
                "transform_function": "transform_to_add_command_parser_info",
            },
        ) as nornir_obj:
            nr_with_processors = nornir_obj.with_processors([ProcessorDO(logger, compiled_results, kwargs)])
            nr_with_processors.run(task=netmiko_send_commands, command_getter_job="network_importer")
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.info("Error: %s", err)
        return err
    return compiled_results
