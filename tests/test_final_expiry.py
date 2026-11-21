"""Final-answer gating and expiration across retries, restarts, and QQ preparation."""
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_codex_ntfy as fixtures
import notification_queue as queue
import notify_bridge as bridge
import qq_sender


class CompletionChecks(fixtures.BehaviorChecks):
    def test_reasoning_progress_tools_and_complete_without_final_do_not_queue(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.write(path, self.meta(), self.record('turn_context', {'turn_id': 'turn-1'}),
                   self.record('event_msg', {'type': 'agent_reasoning', 'text': 'private'}),
                   self.record('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': [{'text': 'Progress only'}]}),
                   self.record('response_item', {'type': 'function_call', 'name': 'exec_command'}), self.done(), with_final=False)
        bridge.codex_hook(self.config, {'type': 'agent-turn-complete', 'thread-id': 'thread-1', 'turn-id': 'turn-1'})
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_final_reply_alone_waits_for_matching_completion(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.write(path, self.meta(), *self.final_records())
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])
        self.write(path, self.done('other-turn'), with_final=False)
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])
        self.write(path, self.done(), with_final=False)
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)

    def test_restart_keeps_final_evidence_and_queues_once(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.write(path, self.meta(), *self.final_records())
        self.watcher.scan()
        self.watcher.close(); self.watcher = None
        self.start()
        self.write(path, self.done(), self.done(), with_final=False)
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)

    def test_repeated_session_metadata_keeps_the_current_turn(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        context, final = self.final_records()
        self.write(path, self.meta(), context)
        self.watcher.scan()
        self.watcher.close()
        self.watcher = None
        self.start()
        self.write(path, self.meta(), self.quota(), final, self.done(), with_final=False)
        self.watcher.scan()
        bridge.codex_hook(self.config, {'type': 'agent-turn-complete', 'thread-id': 'thread-1', 'turn-id': 'turn-1'})
        self.assertEqual(len(self.jobs()), 1)
        self.assertIn('5 小时剩余 75% · 7 天剩余 71%', self.jobs()[0]['payload'])
        completion = bridge.confirmed_completion(self.config, 'thread-1', 'turn-1')
        self.assertEqual(completion.completed_at, self.jobs()[0]['completed_at'])
        self.assertEqual(completion.windows, {300: 75, 10080: 71})

    def test_different_session_metadata_clears_previous_final_reply(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.write(path, self.meta(), *self.final_records(), self.meta(ident='thread-2'),
                   self.done(), with_final=False)
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_upgrade_recovers_turn_lost_by_metadata_refresh(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        context, final = self.final_records()
        self.write(path, self.meta(), self.done('previous'))
        self.watcher.scan()
        self.write(path, context, self.meta(), self.quota())
        self.watcher.scan()
        with self.watcher.db:
            self.watcher.db.execute("DELETE FROM settings WHERE key='session_meta_refresh_migrated'")
            self.watcher.db.execute("UPDATE files SET active_turn=NULL, final_turn=NULL, quota_windows='[]'")
        self.watcher.close()
        self.watcher = None
        self.start()
        self.write(path, final, self.done(), with_final=False)
        self.watcher.scan()
        rows = {row['event_key']: row for row in self.jobs()}
        self.assertEqual(set(rows), {'thread-1:previous', 'thread-1:turn-1'})
        self.assertIn('5 小时剩余 75% · 7 天剩余 71%', rows['thread-1:turn-1']['payload'])

    def test_quota_survives_restart_and_does_not_carry_into_next_turn(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        context, final = self.final_records()
        self.write(path, self.meta(), context, self.quota(10, 20), final, self.quota())
        self.watcher.scan()
        self.watcher.close()
        self.watcher = None
        self.start()

        self.write(path, self.quota(99, 99, limit_id='premium'), self.done(), with_final=False)
        self.write(path, self.done('turn-2'))
        self.watcher.scan()
        messages = {row['event_key']: json.loads(row['payload'])['message'] for row in self.jobs()}
        self.assertIn('Codex 额度 5 小时剩余 75% · 7 天剩余 71%', messages['thread-1:turn-1'])
        self.assertNotIn('PRIVATE', messages['thread-1:turn-1'])
        self.assertNotIn('Codex 额度', messages['thread-1:turn-2'])

    def test_upgrade_recovers_active_quota_and_omits_reset_window(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        context, final = self.final_records()
        done = self.done()
        reset = int(fixtures.module.event_time(done)) - 1
        self.write(path, self.meta(), context, self.quota(resets_at=reset), final)
        self.watcher.scan()
        with self.watcher.db:
            self.watcher.db.execute('UPDATE files SET quota_windows=NULL')
        self.write(path, self.quota(True, '29'), self.quota(float('nan'), -1), done, with_final=False)
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)
        message = json.loads(self.jobs()[0]['payload'])['message']
        self.assertIn('Codex 额度 7 天剩余 71%', message)
        self.assertNotIn('5 小时', message)

    def test_previous_turn_final_cannot_authorize_next_turn(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.write(path, self.meta(), *self.final_records(),
                   self.record('event_msg', {'type': 'task_started', 'turn_id': 'turn-2'}),
                   self.done('turn-2'), with_final=False)
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_aborted_final_is_not_notified(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.write(path, self.meta(), *self.final_records(),
                   self.record('event_msg', {'type': 'turn_aborted', 'turn_id': 'turn-1'}),
                   self.done(), with_final=False)
        self.watcher.scan()
        self.assertEqual(self.jobs(), [])

    def test_native_hook_uses_actual_completion_time_and_deduplicates(self):
        path = self.sessions / 'rollout-thread-1.jsonl'
        self.start()
        done = self.done()
        self.write(path, self.meta(), done)
        hook = {'type': 'agent-turn-complete', 'thread-id': 'thread-1', 'turn-id': 'turn-1'}
        bridge.codex_hook(self.config, hook)
        bridge.codex_hook(self.config, hook)
        self.watcher.scan()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['completed_at'], fixtures.module.event_time(done))

    def test_300_second_boundary_and_301_second_expiry(self):
        self.start()
        with patch('notification_queue.time.time', return_value=1000):
            queue.enqueue(self.config, 'generic', 'boundary', completed_at=700)
            result = queue.enqueue(self.config, 'generic', 'stale', completed_at=699)
            self.assertTrue(result['expired']); self.assertFalse(result['queued'])
        with patch('codex_ntfy.time.time', return_value=1000), patch.object(fixtures.module, 'publish', return_value='ack') as sender:
            self.watcher.deliver_one()
            sender.assert_called_once()
        rows = {r['event_key']: r for r in self.jobs()}
        self.assertIsNotNone(rows['external:generic:boundary']['sent_at'])
        self.assertIsNone(rows['external:generic:stale']['sent_at'])
        self.assertIsNotNone(rows['external:generic:stale']['expired_at'])

    def test_old_backlog_expires_on_restart_including_uncertain_send(self):
        self.start()
        with patch('notification_queue.time.time', return_value=1000):
            for name in ['retry', 'uncertain']:
                queue.enqueue(self.config, 'generic', name, completed_at=990)
        with self.watcher.db:
            self.watcher.db.execute("UPDATE notifications SET message_id='qq:attempting',next_attempt=1e30 WHERE event_key LIKE '%uncertain'")
        self.watcher.close(); self.watcher = None
        self.start()
        with patch('codex_ntfy.time.time', return_value=1291), patch.object(fixtures.module, 'publish') as sender, patch('qq_sender.publish') as qq:
            self.watcher.deliver_one()
            sender.assert_not_called(); qq.assert_not_called()
        self.assertTrue(all(r['expired_at'] is not None and r['sent_at'] is None for r in self.jobs()))

    def test_expired_event_cannot_be_revived_with_same_id(self):
        self.start()
        with patch('notification_queue.time.time', return_value=1000):
            queue.enqueue(self.config, 'generic', 'id', completed_at=699)
            result = queue.enqueue(self.config, 'generic', 'id')
        self.assertTrue(result['duplicate']); self.assertTrue(result['expired'])
        self.assertFalse(result['queued'])
        self.assertEqual(self.jobs()[0]['completed_at'], 699)

    def test_failed_retry_expires_before_second_send(self):
        self.start()
        with patch('notification_queue.time.time', return_value=1000):
            queue.enqueue(self.config, 'generic', 'id', completed_at=990)
        with patch('codex_ntfy.time.time', return_value=1000), patch.object(fixtures.module, 'publish', side_effect=TimeoutError) as sender:
            self.watcher.deliver_one()
            self.assertEqual(sender.call_count, 1)
        with patch('codex_ntfy.time.time', return_value=1291), patch.object(fixtures.module, 'publish') as sender:
            self.watcher.deliver_one(); sender.assert_not_called()
        self.assertIsNotNone(self.jobs()[0]['expired_at'])

    def test_existing_database_upgrade_preserves_event_keys(self):
        directory = self.config['state_dir']; directory.mkdir()
        with sqlite3.connect(directory / 'state.sqlite3') as db:
            db.execute('CREATE TABLE notifications(event_key TEXT PRIMARY KEY,completed_at REAL NOT NULL,payload TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,sent_at REAL,message_id TEXT)')
            db.execute("INSERT INTO notifications(event_key,completed_at,payload) VALUES ('old',0,'{}')")
        db = queue.open_database(self.config)
        try:
            queue.expire_pending(db, self.config, now=1000)
            row = db.execute('SELECT * FROM notifications').fetchone()
            self.assertEqual(row['event_key'], 'old')
            self.assertIsNone(row['sent_at']); self.assertIsNotNone(row['expired_at'])
        finally: db.close()

    def test_expired_qq_call_never_touches_gui(self):
        with patch('qq_sender.time.time', return_value=301), patch('qq_sender.run') as command:
            with self.assertRaises(qq_sender.QQUnavailable):
                qq_sender.publish(self.config, {}, 'id', expires_at=300)
            command.assert_not_called()

    def test_qq_preparation_crossing_deadline_does_not_send(self):
        from unittest.mock import Mock
        api = Mock(); composer = Mock()
        composer.getState().contains.return_value = True
        with patch('qq_sender.time.time', return_value=301), patch('qq_sender.text', return_value='own reminder'), patch('qq_sender.run') as keyboard, patch('qq_sender.press') as send:
            with self.assertRaises(qq_sender.QQUnavailable):
                qq_sender.check_editor_deadline(composer, api, 'own reminder', 300)
            keyboard.assert_called_once_with('xdotool','key','--clearmodifiers','ctrl+a','BackSpace')
            send.assert_not_called()
        with patch('qq_sender.time.time', return_value=301), patch('qq_sender.text', return_value='user changed this draft'), patch('qq_sender.run') as keyboard:
            with self.assertRaises(qq_sender.QQUnavailable):
                qq_sender.check_editor_deadline(composer, api, 'own reminder', 300)
            keyboard.assert_not_called()

    def test_delayed_native_callback_cannot_restart_five_minute_window(self):
        self.start()
        path = self.sessions / 'rollout-thread-1.jsonl'
        done = self.done()
        self.write(path, self.meta(), done)
        completed_at = fixtures.module.event_time(done)
        with patch('notification_queue.time.time', return_value=completed_at+301):
            bridge.codex_hook(self.config, {'type': 'agent-turn-complete', 'thread-id': 'thread-1', 'turn-id': 'turn-1'})
        row = self.jobs()[0]
        self.assertEqual(row['completed_at'], completed_at)
        self.assertIsNotNone(row['expired_at']); self.assertIsNone(row['sent_at'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(CompletionChecks(name) for name in CompletionChecks.__dict__ if name.startswith('test_'))
