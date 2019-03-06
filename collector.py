from dcim.models import Device, Interface, InventoryItem, Manufacturer, Platform, DeviceRole,Cable
from virtualization.models import Cluster, VirtualMachine, ClusterType
from ipam.models import IPAddress
import clitable
from netaddr import IPNetwork
from dcim.constants import *
from collector.settings import *
from math import pow
import ast
import logging
# import json
import re


# Class dummies for see the changes into objects
class __TempVm:
    def __init__(self, name):
        self.name = name
        self.status = DEVICE_STATUS_OFFLINE
        self.cluster_id = None
        self.platform_id = None
        self.role_id = None
        self.memory = 0
        self.vcpus = 0
        self.comments = ""
        self.disk = 0

    def __str__(self):
        return self.name


class __TempDevice:
    pass


def _diff_objects(source, dest):

    src_param = source.__dict__
    dst_param = dest.__dict__

    logger.debug("First object parameters is: {}".format(src_param))

    diff_fields = set(src_param) ^ set(dst_param)

    result_dict = {key: dst_param.get(key) for key in dst_param if key not in diff_fields}

    logger.debug("Second object parameters is: {}".format(result_dict))

    if result_dict == src_param:
        return 1
    else:
        return 0


# Init logginig settings
logger = logging.getLogger('collector')
logging.config.dictConfig(LOGGING_CONFIG)


def _get_process_function(parser, attrs):
    """ 'Velosiped' for return function name from index file
        I believe, will possible make it as another simple method
        But now I cannot know how =\

        :param parser: cli_table object
        :param attrs: dict {"Command": "some_command", "Vendor": some_vendor}
        :return: function or None
    """
    index = parser.index.index
    keys = index.header.values
    out = [dict(zip(keys, row)) for row in index]
    command = attrs['Command']
    vendor = attrs['Vendor']

    for item in out:
        if re.match(item['Vendor'], vendor) and re.match(item['Command'], command):
            # Tries to find function to process this command
            func = globals().get(item['Function'])
            logger.info("Function is %s", func)
            return func


def _get_vendor(vendor_name):
    """ Check if this Vendor present on Netbox

        :param vendor_name: String
        :rtype: object NetBox Manufacturer
    """
    vendor_name = str(vendor_name).strip()
    man = Manufacturer.objects.filter(name__icontains=vendor_name)
    if man:
        # Get only first...
        logger.debug("Found a vendor {} - return it".format(vendor_name))
        return man[0]
    else:
        logger.debug("Cannot a vendor {} - will be a NoName".format(vendor_name))
        # TODO: Add a check for NoName is exists or create it
        man = Manufacturer.objects.get(name = "--- NoName ---")
        return man


def _get_device(hostname):
    """ Get device object from Netbox

    :param hostname: String hostname
    :return:  object NetBox Device
    """
    try:
        device = Device.objects.get(name=hostname)
        return device
    except Exception as e:
        logger.error("Cannot get device. Error is: ".format(e))
        return None


def _compare_interfaces(src_iface, dst_iface):
    ''' This function compare shortest interface name
        with longest interface name.

        EXAMPLE:
        eth0/1 should match Ethernet0/1
    '''
    regex = '([a-z\-]+)([\d/\.]+)'
    regex = re.compile(regex, re.I)
    try:
        src_name, src_number = regex.match(src_iface).groups()
        dst_name, dst_number = regex.match(dst_iface).groups()
    except:
        return False

    nameMatch = src_name.lower().startswith(dst_name.lower())
    return ((src_number == dst_number) and nameMatch)


def _connect_interface(interface):
    ''' Get iface connection from iface description
        Description should be like "server|port"
    '''
    desc_regex = "^(.*)+\|(.*)+"
    desc_regex = re.compile(desc_regex)

    iface_descr = interface.description
    logger.info("Description reported as {}".format(iface_descr))
    if desc_regex.match(iface_descr):
        asset_tag, server_port = iface_descr.split('|')

        server = Device.objects.filter(asset_tag = asset_tag)
        if server:
            server = server[0]
            server_name = server.name

            # Find and compare ports
            for iface in server.interfaces.all():
                if _compare_interfaces(iface.name, server_port):
                    port = iface
                else:
                    port = None

            port = server.interfaces.filter(name = server_port)
            if port:
                cable = Cable()
                cable.termination_a = interface
                cable.termination_b = port[0]
                try:
                    cable.save()
                except Exception as e:
                    logger.error("Cannot do a connection {} to {} on {} - error is {}".format(interface.name, server_name, server_port, e))
                    return
                logger.info("Successfully connected interface {} to server {} on port {}".format(interface.name, server_name, server_port))
            else:
                logger.warning("Cannot found interface {} on server {}".format(server_port, server_name))
        else:
            logger.warning("Cannot find server by AssetTag {}".format(asset_tag))
    else:
        logger.info("Incorrect or None description on interface {} - cannot connect anything".format(interface.name))


def _get_interface_type(if_name):
    ''' Determine iface type (simple)
    '''
    v_regex = re.compile(VIRTUAL_REGEX)
    p_regex = re.compile(PORTCHANNEL_REGEX)
    if v_regex.match(if_name) == None:
        if p_regex.match(if_name) == None:
            return IFACE_FF_1GE_FIXED # no way how to properly determine interface type =(
        else:
            return IFACE_FF_LAG
    else:
        return IFACE_FF_VIRTUAL


def _return_command_list(parser):
    index = parser.index.index
    keys = index.header.values
    out = [dict(zip(keys, row)) for row in index]
    result = {}

    for item in out:
        logger.debug("Find command: {}".format(item.get('Command')))
        result[item.get('Command')] = item.get('Description')

    return True, result


def init_parser():
    """
    Init CliTable index and templates
        This function should be call first, before parsing

        :return: cliTable object or None
    """
    try:
        parser = clitable.CliTable('index', TEMPLATES_DIRECTORY)
        logger.info("Collector module loads: Im Alive!!!")
    except Exception as e:
        logger.error("Problem with parser init - check index file and templates directory. Error is %s", e)
        return None
    return parser


def parse_query(parser, query):
    """ Parsing command output

    :param parser: clitable object
    :param query: Dict
    :return: Bool,String
    """
    logger.debug("parse_query: Starting parse query, data is {}, type is {}".format(query, type(query)))

    action = query['action']

    if not action:
        return False, "Not provided any actions"

    # TODO: find a true way how to remake it
    if action == 'get_help':
        return _return_command_list(parser)

    elif action == 'sync':
        hostlist = query['data']

        for host in hostlist:
            try:
                command = host['command']
                data = host['data']
                hostname = host['hostname']
            except Exception as e:
                logger.error("One or all params in query failed. Detail: {}".format(e))
                return False, "Cannot parse a query - check all parameters"

            device = _get_device(hostname)
            logger.debug("parse_query: Device is: {}".format(device))

            if not device:
                return False, "Not found device by hostname: {}".format(hostname)

            # Get Device Vendor (by platform or type)
            if device.platform:
                vendor = device.platform.name
            else:
                vendor = device.device_type.manufacturer.name

            attrs = {"Command": command, "Vendor": vendor}
            logger.debug("parse_query: Will do it with next attrs: {}".format(attrs))
            process_function = _get_process_function(parser, attrs)
            logger.debug("parse_query: Found process function {}".format(process_function))

            if process_function:
                # Its parsing time!
                try:
                    logger.debug("parse_query: Go to ParseCMD...")
                    parser.ParseCmd(data, attrs)
                except Exception as e:
                    return False, "Error while parsing. {}".format(e)
                # I will use a named indexes to prevent order changes
                keys = parser.header.values
                result = [dict(zip(keys, row)) for row in parser]
                logger.debug("parse_query: Parser returns this: {}".format(result))

                if result:
                    # Process It!
                    return process_function(device, result)
                else:
                    return False, "Cannot parse a command output - check template or command"
            else:
                logger.warning("Cannot found a process function. Parser Agrs: {} Device: {}".format(attrs, device))
                return False, "Function for process this command or for this vendor is not implemented yet =("


def sync_interfaces(device, interfaces):
    """ Syncing interfaces

        :param device: object NetBox Device
        :param interfaces: list of lists

        interfaces:
            interface['NAME'] - Name of interface
            interface['MAC'] - Mac-Address
            interface['IP'] - List of IP-address
            interface['MTU'] - MTU
            interface['DESCR'] - Description of interfaces
            interface['TYPE'] - Physical type of interface (Default 1G-cooper - cannot get from linux)
            interface['STATE'] - UP|DOWN

        :return: status: bool, message: string
    """
    # Updated interface counter
    count = 0

    # Init interfaces filter
    iface_filter = device.cf().get('Interfaces filter')
    try:
        iface_regex = re.compile(iface_filter)
    except Exception as e:
        logger.warning("Cannot parse regex for interface filter: {}".format(e))
        iface_regex = re.compile('.*')

    for interface in interfaces:
        name = interface.get('NAME')
        mac = interface.get('MAC')
        ips = interface.get('IP')
        mtu = interface.get('MTU')
        description = interface.get('DESCR')
        iface_type = interface.get('TYPE')
        iface_state = interface.get('STATE')
        # TODO: add a bonding support
        iface_master = interface.get('BOND')

        # Check interface filter
        if not iface_regex.match(name):
            logger.debug("Iface {} not match with regex".format(name))
            continue

        # Get interface from device - for check if exist
        ifaces = device.interfaces.filter(name=name)
        if ifaces:
            logger.info("Interface {} is exist on device {}, will update".format(name, device.name))
            # TODO: I think, that only one item will be in filter, but need to add check for it
            iface = ifaces[0]
        else:
            logger.info("Interface {} is not exist on device {}, will create new".format(name, device.name))
            iface = Interface(name=name)
            iface.device = device

        logger.info("Will be save next parameters: Name:{name}, MAC: {mac}, MTU: {mtu}, Descr: {description}".format(
            name=name, mac=mac, mtu=mtu, description=description))
        
        if iface.description:
            iface.description = iface.description
        elif description:
            iface.description = description
        else:
            iface.description = ''
        
        iface.mac_address = mac

        # MTU should be less 32767
        if int(mtu) < MAX_MTU:
            iface.mtu = mtu

        logger.info("Interface state is {}".format(iface_state))
        iface.enabled = 'up' in iface_state.lower()
        iface.form_factor = _get_interface_type(name)

        try:
            iface.save()
        except Exception as e:
            logger.error("Cannot save interface, error is {}".format(e))
        else:
            count += 1
            logger.info("Interface {} was succesfully saved".format(name, device.name))

#        try:
#            _connect_interface(iface)
#        except:
#            logger.error("Problem with connection function")

        # IP syncing
        if len(ips) > 0:
            for address in ips:
                addr = IPAddress()
                addr.interface = iface
                logger.info("Address is: {}".format(addr))
                # TODO: Need a test ipv6 addresses
                try:
                    # tries to determine is this address exist
                    if iface.ip_addresses.filter(address=address):
                        continue
                    addr.address = IPNetwork(address)
                    addr.save()
                except:
                    logger.warning("Cannot set address {} on interface".format(address))

    if count == 0:
        return False, "Can't update any interface, see a log for details"
    return True, "Successfully updated {} interfaces".format(count)


def sync_inventory(device, inventory):
    """ Syncing Inventory in NetBox

        :return: status: bool, message: string
    """
    logger.debug("sync_inventory: Running sync inventory")
    is_changed = False

    for item in inventory:
        name = item['Name']
        descr = item['Descr']
        pid = item['PartID']
        serial = item['Serial']
        # Additional field to process name or device type
        try:
            case = item['Case']
        except:
            case = ''

        # Name or Case?
        if not name:
            name = case

        # Check if manufacturer is present
        if 'Vendor' in item.keys():
            if item['Vendor']:
                manufacturer = _get_vendor(item['Vendor'])
            else:
                manufacturer = None
        else:
            manufacturer = device.device_type.manufacturer

        # Check, if this item exists (by device, name and serial)
        item = InventoryItem.objects.filter(device=device, name=name, serial=serial, discovered=True)
        if item:
            logger.info("Device {} already have a discovered item {}".format(device.name, name))
            continue
        else:
            logger.info("Tries to add a item {} on device {}".format(name, device.name))
            item = InventoryItem()
            item.manufacturer = manufacturer
            item.name = name
            item.part_id = pid
            item.serial = serial
            item.description = descr
            item.device = device
            item.discovered = True
            try:
                item.save()
                is_changed = True
            except Exception as e:
                logger.warning(
                    "Error to save Inventory item with name {} to device {}. Error is {}".format(name, device.name, e))
    if is_changed:
        return True, "Device {} synced successfully".format(device.name)
    else:
        return False, "Device {} was not synced. May be all items already exists?".format(device.name)


def sync_vms(device, vms):
    """ Syncing VirtualMachines from device

    :param device:
    :param vms:
    :return:
    """
    if not vms:
        return False, "There is no VM to update"

    # TODO: cluster tenancy
    cluster = device.cluster
    if not cluster:
        # Will create a new cluster
        logger.debug("Creating a new cluster {}".format(device.name))
        cluster = Cluster()
        cluster.name = device.name
        cluster.type = ClusterType.objects.get(name=DEFAULT_CLUSTER_TYPE)
        cluster.site = device.site
        logger.debug("Saving a cluster {}".format(device.name))
        cluster.save()
        cluster.devices.add(device)
        cluster.save()
        logger.debug("Device was added to cluster {}".format(device.name))

    # Determine non-exists VMs and put it offline
    new_vm_names = [ vm_data['NAME'] for vm_data in vms]
    if new_vm_names:
        logger.debug("New VMS: {}".format(new_vm_names))
        non_exist_vms = VirtualMachine.objects.filter(cluster_id=cluster.id).exclude(name__in = new_vm_names)
        logger.debug("VM which located in netbox, but non-exist on current cluster: {}".format(non_exist_vms))

        if non_exist_vms:
            for vm in non_exist_vms:
                if vm.status != DEVICE_STATUS_STAGED:
                    vm.status = DEVICE_STATUS_STAGED
                    # vm.comments = vm.comments + "\n\nVM not exist on current cluster!\n"
                    vm.save()

    # TODO: role and platform
    # By default all VMS - is a linux VMs
    platform = Platform.objects.get(name='Linux')
    # with server roles
    role = DeviceRole.objects.get(name='Server')

    # TODO: Need a get vm disks from vm objects
    # for vm_instance in vms:
    #     pass

    for vm_data in vms:

        temp_vm = __TempVm(vm_data.get('NAME'))

        logger.debug("Will sync VM named: {}".format(temp_vm))

        temp_vm.cluster_id = cluster.id
        temp_vm.platform_id = platform.id
        temp_vm.role_id = role.id
        temp_vm.memory = int(vm_data['MEM'])/1024 # its a simple method - we need a MB
        temp_vm.vcpus = int(vm_data['VCPU'])

        # Determine VM state
        if int(vm_data['STATE']) == DEVICE_STATUS_ACTIVE:
            temp_vm.status = DEVICE_STATUS_ACTIVE

        # get disks
        names = vm_data.get('DISKNAMES')
        sizes = vm_data.get('DISKSIZES')
        paths = vm_data.get('DISKPATHS')

        # TODO: govnocode style - rewrite it for pythonic true way
        # This code parse a strings, like "{'diskindex': 1, 'name': 'sda'}"
        # Convert it to pythonic code
        # and split to one dict
        disks = []
        total_size = 0
        for name in names:
            name = ast.literal_eval(name)
            temp = name.copy()
            index = temp.get('diskindex')
            for size in sizes:
                size = ast.literal_eval(size)
                size_index = size.get('diskindex')
                if size_index == index:
                    temp.update(size)
            for path in paths:
                path = ast.literal_eval(path)
                path_index = path.get('diskindex')
                if path_index == index:
                    temp.update(path)
            disks.append(temp)
            del temp  # non-urgent

        logger.info("Disks is: {}, len is {}".format(disks, len(disks)))

        # Filling VM comments with Disk Section
        separator = SEPARATOR

        # saving comments
        # TODO: fixing adding comments with multiple separator
        # TODO: Change this
        try:
            original_comments = VirtualMachine.objects.get(name=temp_vm.name).comments
        except:
            logger.debug("Cannot find a VM for original comments")
            original_comments = ""
        try:
            logger.debug("Tries to detect if comments exist")
            comments = separator.join(original_comments.split(separator)[1:])
        except:
            logger.debug("Still no any other comments...")
            comments = original_comments

        disk_string = ""
        # and adding disks
        for disk in disks:
            try:
                logger.debug("Size of disk {} is {}".format(disk.get('name'),disk.get('size')))
                size = int(disk.get('size')) / pow(1024, 3)
                disk_string += "**Disk:** {}\t**Size:** {} GB\t**Path:** {}\n".format(disk.get('name'),
                                                                                  int(size),
                                                                                  disk.get('path'))
                total_size += size
            except Exception as e:
                logger.warning("Cannot add disk {} - error is {}".format(disk.get('name'), e))
        disk_string += separator
        temp_vm.comments = disk_string + comments
        temp_vm.disk = int(total_size)

        vm = VirtualMachine.objects.filter(name=temp_vm.name)

        logger.debug("found VM: {}".format(vm))
        logger.debug("VM Data: {}".format(vm.__dict__))

        if vm:
            logger.info("VM {} already exists. Will update this VM".format(temp_vm.name))
            vm = vm[0]
            if _diff_objects(temp_vm, vm) == 0:
                vm.cluster_id = temp_vm.cluster_id
                vm.status = temp_vm.status
                vm.platform_id = temp_vm.platform_id
                vm.role_id = temp_vm.role_id
                vm.memory = temp_vm.memory
                vm.vcpus = temp_vm.vcpus
                vm.comments = temp_vm.comments
                vm.disk = temp_vm.disk
                vm.save()
                logger.info("VM {} was updated".format(vm.name))
            else:
                logger.info("VM {} was not changed - nothing to change".format(temp_vm.name))
        else:
            logger.info("VM {} is not exist. Will create new VM".format(temp_vm.name))
            vm = VirtualMachine()
            vm.name = temp_vm.name
            vm.status = temp_vm.status
            vm.cluster_id = temp_vm.cluster_id
            vm.platform_id = temp_vm.platform_id
            vm.role_id = temp_vm.role_id
            vm.memory = temp_vm.memory
            vm.vcpus = temp_vm.vcpus
            vm.comments = temp_vm.comments
            vm.disk = temp_vm.disk
            vm.save()

            logger.info("Saved new VM: {}".format(vm.name))

    return True, "VMs successfully synced"



