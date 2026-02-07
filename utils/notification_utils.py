import asyncio
import json
from datetime import datetime
from typing import Dict, Any, Optional

from httpx import AsyncClient
from pydantic import BaseModel

from settings.conf import settings
from utils.default_logger import logger


logger = logger.bind(
    module='NotificationUtils',
)


def misc_notification_callback_result_handler(fut: asyncio.Future):
    try:
        r = fut.result()
    except Exception as e:
        logger.opt(exception=True).error(
            'Exception while sending callback or notification: {}', e,
        )
    else:
        logger.debug('Callback or notification result:{}', r)


def format_slack_message(message: BaseModel) -> Dict[str, Any]:
    """
    Format a Pydantic message model into Slack webhook format.
    Similar to local collector's Slack alert formatting.
    """
    message_dict = message.dict()
    
    # Determine issue type and severity
    issue_type = message_dict.get('issueType', 'Unknown')
    color = 'danger'  # Default to red for errors
    emoji = '🚨'
    severity = 'CRITICAL'
    
    # Map issue types to colors and emojis
    if 'Error' in issue_type or 'Failed' in issue_type:
        color = 'danger'
        emoji = '🚨'
        severity = 'CRITICAL'
    elif 'Warning' in issue_type:
        color = 'warning'
        emoji = '⚠️'
        severity = 'WARNING'
    else:
        color = 'warning'
        emoji = '⚠️'
        severity = 'WARNING'
    
    # Extract fields from message
    account_address = message_dict.get('accountAddress') or 'Unknown'
    epoch_begin = message_dict.get('epochBegin') or 'N/A'
    epoch_id = message_dict.get('epochId') or 'N/A'
    project_id = message_dict.get('projectId') or 'N/A'
    extra = message_dict.get('extra') or ''
    
    # Format extra data (truncate if too long)
    extra_text = extra
    if len(extra_text) > 1000:
        extra_text = extra_text[:1000] + '\n... (truncated)'
    
    # Build fields array
    fields = [
        {
            'title': 'Issue Type',
            'value': issue_type,
            'short': True
        },
        {
            'title': 'Severity',
            'value': severity,
            'short': True
        },
        {
            'title': 'Account Address',
            'value': account_address,
            'short': True
        },
        {
            'title': 'Epoch Begin',
            'value': str(epoch_begin),
            'short': True
        }
    ]
    
    # Add optional fields if present
    if epoch_id and epoch_id != 'N/A':
        fields.append({
            'title': 'Epoch ID',
            'value': str(epoch_id),
            'short': True
        })
    
    if project_id and project_id != 'N/A':
        fields.append({
            'title': 'Project ID',
            'value': str(project_id),
            'short': True
        })
    
    # Add extra details as a long field
    if extra_text:
        fields.append({
            'title': 'Error Details',
            'value': f'```{extra_text}```',
            'short': False
        })
    
    # Build attachment
    attachment = {
        'color': color,
        'title': f'{emoji} Epoch Manager Alert: {issue_type}',
        'text': f'*Severity:* {severity}\n*Time:* {datetime.utcnow().isoformat()}Z',
        'fields': fields,
        'ts': int(datetime.utcnow().timestamp()),
        'footer': 'Epoch Manager'
    }
    
    # Build Slack message
    slack_message = {
        'username': 'Epoch Manager',
        'icon_emoji': ':hourglass_flowing_sand:',
        'attachments': [attachment]
    }
    
    return slack_message


async def send_failure_notifications(client: AsyncClient, message: BaseModel):
    """
    Send failure notifications to configured services.
    
    For Slack, formats the message into proper Slack webhook format.
    For reporting service, sends the raw message dict.
    """
    if settings.reporting.service_url:
        f = asyncio.ensure_future(
            client.post(
                url=settings.reporting.service_url,
                json=message.dict(),
            ),
        )
        f.add_done_callback(misc_notification_callback_result_handler)

    if settings.reporting.slack_url:
        # Format message for Slack webhook
        slack_message = format_slack_message(message)
        f = asyncio.ensure_future(
            client.post(
                url=settings.reporting.slack_url,
                json=slack_message,
            ),
        )
        f.add_done_callback(misc_notification_callback_result_handler)
