from collections import defaultdict
from contextlib import suppress
from urllib.parse import quote

import pytz
from django.db import models, transaction
from django.template.loader import get_template
from django.utils.functional import cached_property
from django.utils.timezone import now, override as tzoverride
from django.utils.translation import override, ugettext_lazy as _

from pretalx.agenda.tasks import export_schedule_html
from pretalx.common.mixins import LogMixin
from pretalx.common.urls import EventUrls
from pretalx.mail.models import QueuedMail
from pretalx.person.models import User
from pretalx.submission.models import SubmissionStates


class Schedule(LogMixin, models.Model):
    event = models.ForeignKey(
        to='event.Event', on_delete=models.PROTECT, related_name='schedules'
    )
    version = models.CharField(
        max_length=190, null=True, blank=True, verbose_name=_('version')
    )
    published = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-published',)
        unique_together = (('event', 'version'),)

    class urls(EventUrls):
        public = '{self.event.urls.schedule}v/{self.url_version}/'

    @transaction.atomic
    def freeze(self, name, user=None, notify_speakers=True):
        from pretalx.schedule.models import TalkSlot

        if name in ['wip', 'latest']:
            raise Exception(f'Cannot use reserved name "{name}" for schedule version.')
        if self.version:
            raise Exception(
                f'Cannot freeze schedule version: already versioned as "{self.version}".'
            )
        if not name:
            raise Exception('Cannot create schedule version without a version name.')

        self.version = name
        self.published = now()
        self.save(update_fields=['published', 'version'])
        self.log_action('pretalx.schedule.release', person=user, orga=True)

        wip_schedule = Schedule.objects.create(event=self.event)

        # Set visibility
        self.talks.filter(
            start__isnull=False,
            submission__state=SubmissionStates.CONFIRMED,
            is_visible=False,
        ).update(is_visible=True)
        self.talks.filter(is_visible=True).exclude(
            start__isnull=False, submission__state=SubmissionStates.CONFIRMED
        ).update(is_visible=False)

        talks = []
        for talk in self.talks.select_related('submission', 'room').all():
            talks.append(talk.copy_to_schedule(wip_schedule, save=False))
        TalkSlot.objects.bulk_create(talks)

        if notify_speakers:
            self.notify_speakers()

        with suppress(AttributeError):
            del wip_schedule.event.wip_schedule
        with suppress(AttributeError):
            del wip_schedule.event.current_schedule

        if self.event.settings.export_html_on_schedule_release:
            export_schedule_html.apply_async(kwargs={'event_id': self.event.id})

        return self, wip_schedule

    @transaction.atomic
    def unfreeze(self, user=None):
        from pretalx.schedule.models import TalkSlot

        if not self.version:
            raise Exception('Cannot unfreeze schedule version: not released yet.')

        # collect all talks, which have been added since this schedule (#72)
        submission_ids = self.talks.all().values_list('submission_id', flat=True)
        talks = self.event.wip_schedule.talks.exclude(
            submission_id__in=submission_ids
        ).union(self.talks.all())

        wip_schedule = Schedule.objects.create(event=self.event)
        new_talks = []
        for talk in talks:
            new_talks.append(talk.copy_to_schedule(wip_schedule, save=False))
        TalkSlot.objects.bulk_create(new_talks)

        self.event.wip_schedule.talks.all().delete()
        self.event.wip_schedule.delete()

        with suppress(AttributeError):
            del wip_schedule.event.wip_schedule

        return self, wip_schedule

    @cached_property
    def scheduled_talks(self):
        return self.talks.filter(
            room__isnull=False, start__isnull=False, is_visible=True
        )

    @cached_property
    def slots(self):
        from pretalx.submission.models import Submission

        return Submission.objects.filter(
            id__in=self.scheduled_talks.values_list('submission', flat=True)
        )

    @cached_property
    def previous_schedule(self):
        queryset = self.event.schedules.exclude(pk=self.pk)
        if self.published:
            queryset = queryset.filter(published__lt=self.published)
        return queryset.order_by('-published').first()

    @cached_property
    def changes(self):
        tz = pytz.timezone(self.event.timezone)
        result = {
            'count': 0,
            'action': 'update',
            'new_talks': [],
            'canceled_talks': [],
            'moved_talks': [],
        }
        if not self.previous_schedule:
            result['action'] = 'create'
            return result

        # ******************************************
        # build list of all new talks without link to schedule
        # from pretalx.submission.models import Submission
        # qs = Submission.objects.filter(
        #     id__in=self.scheduled_talks.values_list('submission', flat=True)
        # )
        # print('qs')
        # for slot in qs:
        #     print(slot)
        # #
        #
        # print('****')

        new_slots_qs = self.talks.select_related(
            'submission', 'submission__event', 'room'
        ).all().filter(
            is_visible=True,
            room__isnull=False,
            # start__isnull=False,
        ).exclude(
            submission__state=SubmissionStates.DELETED,
        )
        # print('new_slots_qs')
        # for slot in new_slots_qs:
        #     print(slot)
        #
        #
        old_slots_qs = self.previous_schedule.talks.select_related(
            'submission', 'submission__event', 'room'
        ).all().filter(
            is_visible=True,
        ).exclude(
            submission__state=SubmissionStates.DELETED,
        )
        # from pretalx.schedule.models import TalkSlot
        # old_slots_qs = TalkSlot.objects.select_related(
        #     'submission', 'submission__event', 'room'
        # ).filter(
        #     submission__in=self.previous_schedule.talks.values_list('submission', flat=True),
        #     slot_index__in=self.previous_schedule.talks.values_list('slot_index', flat=True),
        # ).filter(
        #     is_visible=True,
        # ).exclude(
        #     submission__state=SubmissionStates.DELETED,
        # )
        # print('old_slots_qs')
        # for slot in old_slots_qs:
        #     print(slot)
        # print('')

        new_slots = set(
            talk
            for talk in self.talks.select_related(
            # for talk in self.scheduled_talks.select_related(
                'submission', 'submission__event', 'room'
            ).all()
            if talk.is_visible and not talk.submission.is_deleted
        )
        old_slots = set(
            talk
            for talk in self.previous_schedule.talks.select_related(
                'submission', 'submission__event', 'room'
            ).all()
            if talk.is_visible and not talk.submission.is_deleted
        )
        new_slots_helper = set(
            (talk.submission, talk.slot_index) for talk in new_slots
        )
        old_slots_helper = set(
            (talk.submission, talk.slot_index) for talk in old_slots
        )
        print('*'*42)
        print('new_slots')
        for slot in new_slots:
            print(slot)
        print('')
        print('old_slots')
        for slot in old_slots:
            print(slot)
        print('')
        print('new_slots_helper')
        for slot in new_slots_helper:
            print(slot)
        print('')
        print('old_slots_helper')
        for slot in old_slots_helper:
            print(slot)
        print('')

        # print('symmetric_difference qs ^ new_slots')
        # for slot in set(qs) ^ new_slots:
        #     print('*', slot)
        # print('****')
        # print('symmetric_difference qs ^ old_slots')
        # for slot in set(qs) ^ old_slots:
        #     print('*', slot)
        # print('****')



        # new_submissions = set(talk.submission for talk in new_slots)
        # old_submissions = set(talk.submission for talk in old_slots)
        #
        # new_slot_by_submission = {talk.submission: talk for talk in new_slots}
        # old_slot_by_submission = {talk.submission: talk for talk in old_slots}
        #
        # result['new_talks'] = [
        #     new_slot_by_submission.get(s) for s in new_submissions - old_submissions
        # ]
        # result['canceled_talks'] = [
        #     old_slot_by_submission.get(s) for s in old_submissions - new_submissions
        # ]


        # result['new_talks'] = [new_slots - old_slots]
        # result['canceled_talks'] = [old_slots - new_slots]

        # print('**** new_slots_helper - old_slots_helper')
        # for slot in new_slots_helper - old_slots_helper:
        #     print('*', slot)
        # print('****')

        from pretalx.schedule.models import TalkSlot
        # result['new_talks'] = [
        #     new_slots_qs.get(submission=s[0], slot_index=s[1]) for s in new_slots_helper - old_slots_helper
        # ]
        result['new_talks'] = []
        for s in new_slots_helper - old_slots_helper:
            with suppress(AttributeError, TalkSlot.DoesNotExist):
                result['new_talks'].append(
                    new_slots_qs.get(submission=s[0], slot_index=s[1]))

        # print('**** old_slots_helper - new_slots_helper')
        # for slot in old_slots_helper - new_slots_helper:
        #     print('*', slot)
        # print('****')
        # result['canceled_talks'] = [
        #     old_slots_qs.get(submission=s[0], slot_index=s[1]) for s in old_slots_helper - new_slots_helper
        # ]
        result['canceled_talks'] = []
        for s in old_slots_helper - new_slots_helper:
            with suppress(AttributeError, TalkSlot.DoesNotExist):
                result['canceled_talks'].append(
                    old_slots_qs.get(submission=s[0], slot_index=s[1]))


        # result['new_talks'] = [
        #     s for s in new_slots - old_slots
        # ]
        # result['canceled_talks'] = [
        #     s for s in old_slots - new_slots
        # ]
        print('new_talks')
        for slot in result['new_talks']:
            print(slot)
        print('canceled_talks')
        for slot in result['canceled_talks']:
            print(slot)
        print('****')

        # result['new_talks'] = []
        # for talk in new_slots:
        #     if talk.submission in old_slots:
        #         pass


        for talk in new_slots_helper & old_slots_helper:
            with suppress(AttributeError, TalkSlot.DoesNotExist):
                old_slot = old_slots_qs.get(submission=talk[0], slot_index=talk[1])
                new_slot = new_slots_qs.get(submission=talk[0], slot_index=talk[1])
                # if new_slot.room and not old_slot.room:
                #     result['new_talks'].append(new_slot)
                # elif not new_slot.room and old_slot.room:
                #     result['canceled_talks'].append(new_slot)
                # elif old_slot.start != new_slot.start or old_slot.room != new_slot.room:
                if old_slot.start != new_slot.start or old_slot.room != new_slot.room:
                    if new_slot.room:
                        result['moved_talks'].append(
                            {
                                'submission': talk[0],
                                'old_start': old_slot.start.astimezone(tz),
                                'new_start': new_slot.start.astimezone(tz),
                                'old_room': old_slot.room.name,
                                'new_room': new_slot.room.name,
                                'new_info': new_slot.room.speaker_info,
                            }
                        )

        # for submission in new_submissions & old_submissions:
        #     old_slot = old_slot_by_submission.get(submission)
        #     new_slot = new_slot_by_submission.get(submission)
        #     if new_slot.room and not old_slot.room:
        #         result['new_talks'].append(new_slot)
        #     elif not new_slot.room and old_slot.room:
        #         result['canceled_talks'].append(new_slot)
        #     elif old_slot.start != new_slot.start or old_slot.room != new_slot.room:
        #         if new_slot.room:
        #             result['moved_talks'].append(
        #                 {
        #                     'submission': submission,
        #                     'old_start': old_slot.start.astimezone(tz),
        #                     'new_start': new_slot.start.astimezone(tz),
        #                     'old_room': old_slot.room.name,
        #                     'new_room': new_slot.room.name,
        #                     'new_info': new_slot.room.speaker_info,
        #                 }
        #             )

        result['count'] = (
            len(result['new_talks'])
            + len(result['canceled_talks'])
            + len(result['moved_talks'])
        )
        return result

    @cached_property
    def warnings(self):
        warnings = {'talk_warnings': [], 'unscheduled': [], 'unconfirmed': [], 'no_track': []}
        for talk in self.talks.all():
            if not talk.start:
                warnings['unscheduled'].append(talk)
            elif talk.warnings:
                warnings['talk_warnings'].append(talk)
            if talk.submission.state != SubmissionStates.CONFIRMED:
                warnings['unconfirmed'].append(talk)
            if talk.submission.event.settings.use_tracks and not talk.submission.track:
                warnings['no_track'].append(talk)
        return warnings

    @cached_property
    def speakers_concerned(self):
        if self.changes['action'] == 'create':
            return {
                speaker: {
                    'create': self.talks.filter(submission__speakers=speaker),
                    'update': [],
                }
                for speaker in User.objects.filter(submissions__slots__schedule=self)
            }

        if self.changes['count'] == len(self.changes['canceled_talks']):
            return []

        speakers = defaultdict(lambda: {'create': [], 'update': []})
        for new_talk in self.changes['new_talks']:
            for speaker in new_talk.submission.speakers.all():
                speakers[speaker]['create'].append(new_talk)
        for moved_talk in self.changes['moved_talks']:
            for speaker in moved_talk['submission'].speakers.all():
                speakers[speaker]['update'].append(moved_talk)
        return speakers

    @cached_property
    def notifications(self):
        tz = pytz.timezone(self.event.timezone)
        mails = []
        for speaker in self.speakers_concerned:
            with override(speaker.locale), tzoverride(tz):
                text = get_template('schedule/speaker_notification.txt').render(
                    {'speaker': speaker, **self.speakers_concerned[speaker]}
                )
            mails.append(
                QueuedMail(
                    event=self.event,
                    to=speaker.email,
                    reply_to=self.event.email,
                    subject=_('New schedule!').format(event=self.event.slug),
                    text=text,
                )
            )
        return mails

    def notify_speakers(self):
        for notification in self.notifications:
            notification.save()

    @cached_property
    def url_version(self):
        return quote(self.version) if self.version else 'wip'

    @cached_property
    def is_archived(self):
        if not self.version:
            return False

        return self != self.event.current_schedule

    def __str__(self) -> str:
        """Help when debugging."""
        return f'Schedule(event={self.event.slug}, version={self.version})'
