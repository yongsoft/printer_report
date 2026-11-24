import socket
import asyncio
import yaml
import logging
from datetime import datetime, timedelta
from pysnmp.hlapi.asyncio import getCmd, SnmpEngine, CommunityData, UdpTransportTarget, ContextData
from pysnmp.smi.rfc1902 import ObjectType, ObjectIdentity

import os
import db

# Get the directory containing the script
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, 'config.yaml')

# Read YAML config
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)

# Construct absolute path for CSV file (legacy / migration source)
CSV_FILE = os.path.join(script_dir, config['csv_file_name'])

# Configure logging
logger = logging.getLogger('report_logger')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)

if config['logging']['enabled']:
    from logging.handlers import TimedRotatingFileHandler

    log_dir = os.path.join(script_dir, config['logging']['directory'])
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger('report_logger')
    logger.setLevel(getattr(logging, config['logging']['level']))

    log_file = os.path.join(log_dir, 'report.log')
    handler = TimedRotatingFileHandler(
        log_file,
        when='midnight',
        interval=1,
        backupCount=config['logging']['max_days']
    )
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)


async def check_network_connectivity(ip_address, community_string='public', port=161, timeout=2):
    """
    Check if the target IP address is reachable via SNMP.
    Uses a test SNMP GET request to verify connectivity.
    """
    try:
        test_oid = ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0'))

        error_indication, error_status, error_index, var_binds = await getCmd(
            SnmpEngine(),
            CommunityData(community_string),
            UdpTransportTarget((ip_address, port), timeout=timeout),
            ContextData(),
            test_oid
        )

        if error_indication:
            logger.error(f"SNMP connectivity test failed: {error_indication}")
            return False
        elif error_status:
            logger.error(f'SNMP error status: {error_status.prettyPrint()} at {error_index}')
            return False

        return True

    except Exception as e:
        logger.error(f"SNMP connectivity test error: {e}")
        return False


async def get_printer_page_count(ip_address, community_string):
    """
    Query the printer for the page count using SNMP.
    """
    current_date = datetime.now().strftime("%Y-%m-%d")

    # Skip if data already exists for today
    if db.is_data_exists_for_date(current_date):
        logger.info(f"Data already exists for {current_date}, skipping")
        return

    if not await check_network_connectivity(ip_address, community_string, port=161, timeout=timeout):
        logger.error(f"Printer at {ip_address} is not reachable. Please check the network connection.")
        previous_page_count, previous_date = db.read_previous_page_count()
        if previous_page_count > 0:
            db.write_page_count(current_date, previous_page_count, 0)
            logger.info(f'Using previous page count from {previous_date}: {previous_page_count}')
        return

    oid = ObjectType(ObjectIdentity('1.3.6.1.2.1.43.10.2.1.4.1.1'))
    try:
        error_indication, error_status, error_index, var_binds = await getCmd(
            SnmpEngine(),
            CommunityData(community_string),
            UdpTransportTarget((ip_address, 161)),
            ContextData(),
            oid
        )
        if error_indication:
            logger.error(f'Query failed: {error_indication}')
            previous_page_count, previous_date = db.read_previous_page_count()
            if previous_page_count > 0:
                db.write_page_count(current_date, previous_page_count, 0)
                logger.info(f'Using previous page count from {previous_date}: {previous_page_count}')
            return
        elif error_status:
            logger.error(f'Query failed: {error_status.prettyPrint()} at index {error_index}')
            previous_page_count, previous_date = db.read_previous_page_count()
            if previous_page_count > 0:
                db.write_page_count(current_date, previous_page_count, 0)
                logger.info(f'Using previous page count from {previous_date}: {previous_page_count}')
        else:
            for var_bind in var_binds:
                page_count = int(var_bind[1])
                previous_page_count, _ = db.read_previous_page_count()
                net_increase = page_count - previous_page_count if previous_page_count >= 0 else 0
                db.write_page_count(current_date, page_count, net_increase)
                logger.info(f'Print page count: {page_count}, Net increase: {net_increase}')
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        previous_page_count, previous_date = db.read_previous_page_count()
        if previous_page_count > 0:
            db.write_page_count(current_date, previous_page_count, 0)
            logger.info(f'Using previous page count from {previous_date}: {previous_page_count}')


if __name__ == "__main__":
    printer_config = config['printer']

    printer_ip = printer_config['ip_address']
    port = printer_config['port']
    community = printer_config['community_string']
    timeout = printer_config['timeout']

    # Initialize database
    db.init_db(script_dir)
    db.ensure_schema()
    db.migrate_from_csv(CSV_FILE)

    async def main():
        if not await check_network_connectivity(printer_ip, community, port, timeout):
            logger.error(f"Failed to connect to printer at {printer_ip}:{port}")
            exit(1)

        await get_printer_page_count(printer_ip, community)

        # Fill missing dates
        db.fill_missing_dates()

    asyncio.run(main())
