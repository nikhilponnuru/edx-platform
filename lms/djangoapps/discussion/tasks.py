"""
Defines asynchronous celery task for sending email notification (through edx-ace)
pertaining to new discussion forum comments.
"""
import logging

from celery import task
from crum import CurrentRequestUserMiddleware
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site

from celery_utils.logged_task import LoggedTask
from edx_ace import ace
from edx_ace.message import MessageType
from edx_ace.recipient import Recipient
from opaque_keys.edx.keys import CourseKey
from lms.djangoapps.django_comment_client.utils import permalink
import lms.lib.comment_client as cc

from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.schedules.template_context import get_base_template_context, GoogleAnalyticsTrackingPixel, \
    absolute_url, CampaignTrackingInfo
from openedx.core.djangoapps.theming.middleware import CurrentSiteThemeMiddleware
from openedx.core.lib.celery.task_utils import emulate_http_request

log = logging.getLogger(__name__)


DEFAULT_LANGUAGE = 'en'
ROUTING_KEY = getattr(settings, 'ACE_ROUTING_KEY', None)


class ResponseNotification(MessageType):
    pass


@task(base=LoggedTask, routing_key=ROUTING_KEY)
def send_ace_message(context):
    context['course_id'] = CourseKey.from_string(context['course_id'])
    context['site'] = Site.objects.get(id=context['site_id'])
    campaign = CampaignTrackingInfo(source='discussions')
    if _should_send_message(context):
        thread_author = User.objects.get(id=context['thread_author_id'])
        middleware_classes = [
            CurrentRequestUserMiddleware,
            CurrentSiteThemeMiddleware,
        ]
        with emulate_http_request(site=context['site'], user=thread_author, middleware_classes=middleware_classes):
            message_context = _build_message_context(context, thread_author, campaign)
            message = ResponseNotification().personalize(
                Recipient(thread_author.username, thread_author.email),
                _get_course_language(context['course_id']),
                message_context
            )
            log.info('Sending forum comment email notification with context %s', message_context)
            ace.send(message)


def _should_send_message(context):
    cc_thread_author = cc.User(id=context['thread_author_id'], course_id=context['course_id'])
    return _is_user_subscribed_to_thread(cc_thread_author, context['thread_id'])


def _is_user_subscribed_to_thread(cc_user, thread_id):
    paginated_result = cc_user.subscribed_threads()
    thread_ids = {thread['id'] for thread in paginated_result.collection}

    while paginated_result.page < paginated_result.num_pages:
        next_page = paginated_result.page + 1
        paginated_result = cc_user.subscribed_threads(query_params={'page': next_page})
        thread_ids.update(thread['id'] for thread in paginated_result.collection)

    return thread_id in thread_ids


def _get_course_language(course_id):
    course_overview = CourseOverview.objects.get(id=course_id)
    language = course_overview.language or DEFAULT_LANGUAGE
    return language


def _build_message_context(context, thread_author, campaign):
    message_context = get_base_template_context(context['site'])
    message_context.update(context)
    message_context['post_link'] = _get_thread_url(context, campaign)

    pixel = GoogleAnalyticsTrackingPixel(
        site=context['site'],
        user_id=thread_author.id,
        document_path='/email/discussions/thread/updated',
        event_label=context['course_id'],
        campaign_source=campaign.source,
    )
    message_context['ga_tracking_pixel_url'] = pixel.image_url
    return message_context


def _get_thread_url(context, campaign):
    thread_content = {
        'type': 'thread',
        'course_id': context['course_id'],
        'commentable_id': context['thread_commentable_id'],
        'id': context['thread_id'],
    }
    return absolute_url(context['site'], permalink(thread_content), campaign=campaign)
