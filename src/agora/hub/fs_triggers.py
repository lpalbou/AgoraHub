"""Opt-in VFS revision notifications through the existing durable inbox.

The archive is the event source, so a crash between fs commit and its audit
cannot lose subscription work. No model, separate broker or workspace watcher.
"""
from __future__ import annotations

import logging

from ..db import DuplicateMessage
from ..models import Kind


class FsTriggersMixin:
    def fs_subscribe(self, agent, channel, path, events=None, urgency="next_turn"):
        from .service import HubError

        with self.db.orchestration_lock:
            self.require_membership(channel, agent.id)
            self._require_unpaused(agent, channel)
            self._require_not_archived(channel)
            norm = self._normalize_fs_path(path)
            if events is None:
                events = ["created", "updated", "deleted"]
            if (not isinstance(events, list) or not 1 <= len(events) <= 3
                    or any(type(e) is not str or e not in ("created", "updated", "deleted") for e in events)
                    or len(set(events)) != len(events)):
                raise HubError(400, "events must select distinct created, updated and/or deleted events")
            if urgency not in ("inbox", "next_turn", "interrupt"):
                raise HubError(400, "urgency must be inbox, next_turn or interrupt; subscriptions create FYI, not asks")
            try:
                row = self.db.fs_subscribe(channel, norm, agent.id, sorted(events), urgency)
            except ValueError as exc:
                raise HubError(400, str(exc)) from exc
            return {**row, "notice": "Future VFS revisions only; local files and attachments do not count. "
                    "Identical retries retain pending events; changing policy starts at the current revision. "
                    "Notifications create awareness, never approval or reply debt."}

    def fs_unsubscribe(self, agent, channel, path):
        # Ownership comes from authentication. Permit cancellation after losing
        # room access; no inspection or modification of another seat's policy.
        norm = self._normalize_fs_path(path)
        with self.db.orchestration_lock:
            return {"unsubscribed": self.db.fs_unsubscribe(channel, norm, agent.id),
                    "notice": "Future notices stopped; previously published messages remain."}

    def fs_subscriptions(self, agent, channel, path=None):
        self.require_membership(channel, agent.id)
        norm = self._normalize_fs_path(path) if path is not None else None
        return self.db.fs_subscriptions(channel, norm)

    def _fs_trigger_sweep(self):
        """Replay bounded archive batches; advance only after publish + wake attempt.

        Duplicate ledger insertion recovers and re-wakes the original message.
        Notify streams may repeat the same id/seq after a crash; their ordinary
        deduplication applies. No receipt here certifies model observation/action.
        """
        from .service import HubError

        if self.hub_paused() is not None:
            return []
        fired = []
        with self.db.orchestration_lock:
            for sub in self.db.fs_pending_subscriptions():
                # An inaccessible/closed/failed recipient must not monopolize
                # the bounded query and starve healthy newer subscriptions.
                self.db.fs_subscription_rotate(sub['id'])
                channel, who = sub['channel'], sub['agent_id']
                try:
                    self.require_membership(channel, who)
                except HubError:
                    continue
                if self.channel_state(channel) in ("closed", "archived"):
                    continue
                after = sub['after_version']
                for revision in self.db.fs_revision_events(channel, sub['path'], after):
                    version, event = revision['version'], revision['event_type']
                    if event in sub['events'] and revision['updated_by'] != who:
                        details = {"subscription_id": sub['id'], "path": sub['path'],
                                   "event": event, "version": version,
                                   "previous_version": version-1 or None,
                                   "actor": revision['updated_by'], "published_at": revision['updated_at'],
                                   "summary": revision['summary']}
                        if event != 'deleted':
                            details['read'] = {"tool": "fs_read", "arguments": {
                                "channel": channel, "path": sub['path'], "version": version}}
                        else:
                            details['read'] = {"tool": "fs_read", "arguments": {
                                "channel": channel, "path": sub['path'], "version": version-1}}
                            details['read_note'] = "Prior live revision; the event version is a deletion tombstone."
                        body = (f"VFS {event}: {sub['path']} @v{version}, by {revision['updated_by']}. "
                                "Your file subscription requested this FYI. Inspect the revision and effects on your work; "
                                "availability is not acceptance and does not clear other dependencies.")
                        if revision['summary']:
                            body += '\nAuthor-supplied change summary: ' + revision['summary']
                        try:
                            message = self.db.insert_message(channel, 'hub', kind=Kind.system.value,
                                status='fyi', urgency=sub['urgency'], to=[who],
                                title=f"VFS {event}: {sub['path']} @v{version}", body=body,
                                data={"vfs_trigger": details}, reply_to=None,
                                dedupe_key=f"vfs-subscription:{sub['id']}:{version}")
                        except DuplicateMessage as exc:
                            message = self.db.get_message(exc.message_id)
                        if not self._wake(message, require_notify=who):
                            break  # retained cursor retries notification, not ledger publication
                        fired.append(message.id)
                    if not self.db.fs_subscription_advance(sub['id'], after, version):
                        break
                    after = version
        return fired

    def _fs_trigger_after_write(self):
        try:
            self._fs_trigger_sweep()
        except Exception:
            logging.getLogger('agora.hub.fs').exception(
                'VFS subscription delivery failed; committed revisions will retry on the collection sweep')
