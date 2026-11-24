import yaml
import logging
import os
import json
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

import db

# Get the directory containing the script
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, 'config.yaml')

# Load configuration
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)

# Configure logging
if config['logging']['enabled']:
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

logger = logging.getLogger('report_logger')

# Construct absolute paths
CSV_FILE = os.path.join(script_dir, config['csv_file_name'])
HTML_FILE = os.path.join(script_dir, config['report']['output_dir'], config['report']['output_file'])
TEMPLATE_FILE = os.path.join(script_dir, config['report']['template'])


def compute_summary(data):
    """Compute summary statistics for the KPI dashboard."""
    if not data:
        return {
            'total_pages': 0,
            'total_days': 0,
            'active_days': 0,
            'avg_daily': 0,
            'busiest_day_count': 0,
            'busiest_day_date': '',
            'first_date': '',
        }

    total_pages = data[-1]['total_page_count'] - data[0]['total_page_count'] + data[0]['net_increase']
    total_days = len(data)
    active_days = sum(1 for d in data if d['net_increase'] > 0)
    avg_daily = sum(d['net_increase'] for d in data) / total_days if total_days > 0 else 0

    busiest = max(data, key=lambda d: d['net_increase'])
    first_date = data[0]['date']

    return {
        'total_pages': total_pages,
        'total_days': total_days,
        'active_days': active_days,
        'avg_daily': round(avg_daily, 1),
        'busiest_day_count': busiest['net_increase'],
        'busiest_day_date': busiest['date'],
        'first_date': first_date,
    }


def generate_report():
    """Main report generation flow."""
    # Initialize database
    db.init_db(script_dir)
    db.ensure_schema()
    db.migrate_from_csv(CSV_FILE)

    # Read all data
    data = db.read_all_data()

    if not data:
        error_msg = "No data available to generate report."
        logger.error(error_msg)
        print(error_msg)
        return

    logger.info(f"Loaded {len(data)} records from database")

    # Compute summary stats
    summary = compute_summary(data)

    # Read template
    try:
        with open(TEMPLATE_FILE, 'r') as f:
            template = f.read()
    except FileNotFoundError:
        error_msg = f"Error: Template file '{TEMPLATE_FILE}' not found."
        logger.error(error_msg)
        print(error_msg)
        return

    # Inject data into template
    html = template.replace('{chart_data}', json.dumps(data))
    html = html.replace('{summary_data}', json.dumps(summary))
    html = html.replace('{generated_at}', datetime.now().strftime('%Y-%m-%d %H:%M'))

    # Ensure output directory exists
    os.makedirs(os.path.dirname(HTML_FILE), exist_ok=True)

    # Write HTML
    try:
        with open(HTML_FILE, 'w', encoding='utf-8') as f:
            f.write(html)
        success_msg = f"HTML report generated successfully at '{HTML_FILE}'"
        logger.info(success_msg)
        print(success_msg)
    except Exception as e:
        error_msg = f"Error writing HTML file: {e}"
        logger.error(error_msg)
        print(error_msg)


if __name__ == "__main__":
    generate_report()
