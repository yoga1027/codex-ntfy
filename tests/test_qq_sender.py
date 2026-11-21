"""Offline failure, restart, and no-duplicate checks; no real QQ sends."""
import unittest
from unittest.mock import patch
import test_codex_ntfy as fixtures
import qq_sender


class QQChecks(fixtures.BehaviorChecks):
    def prepare(self):
        self.config['transport'] = 'qq'
        self.start()
        self.write(self.sessions / 'rollout-live.jsonl', self.meta(), self.done())
        self.watcher.scan()

    def test_qq_dispatch_is_durable_before_ui_and_once(self):
        self.prepare()
        def send(*args, **kwargs):
            self.assertEqual(self.jobs()[0]['message_id'], 'qq:attempting')
            self.assertEqual(args[2], 'thread-1:turn-1')
            self.assertFalse(kwargs['verify_only'])
            return 'qq:verified'
        with patch('qq_sender.publish', side_effect=send) as sender, patch.object(fixtures.module, 'publish') as network:
            self.watcher.deliver_one()
            self.watcher.deliver_one()
            sender.assert_called_once()
            network.assert_not_called()
        self.assertIsNotNone(self.jobs()[0]['sent_at'])

    def test_gui_unavailable_retries_without_claiming_send(self):
        self.prepare()
        with patch('qq_sender.publish', side_effect=qq_sender.QQUnavailable('desktop locked')):
            self.watcher.deliver_one()
        row = self.jobs()[0]
        self.assertIsNone(row['sent_at'])
        self.assertIsNone(row['message_id'])
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_attempt'], 0)

    def test_uncertain_send_is_not_automatically_repeated(self):
        self.prepare()
        with patch('qq_sender.publish', side_effect=qq_sender.QQUncertain('interrupted after click')) as sender:
            self.watcher.deliver_one()
            self.watcher.deliver_one()
            sender.assert_called_once()
        self.assertIsNone(self.jobs()[0]['sent_at'])
        self.assertGreater(self.jobs()[0]['next_attempt'], 1e29)

    def test_restart_after_interruption_is_verification_only(self):
        self.prepare()
        with self.watcher.db:
            self.watcher.db.execute("UPDATE notifications SET message_id='qq:attempting'")
        self.watcher.close()
        self.watcher = None
        self.start()
        with patch('qq_sender.publish', return_value='qq:verified') as sender:
            self.watcher.deliver_one()
            self.assertTrue(sender.call_args.kwargs['verify_only'])

    def test_device_toolbar_accepts_shortcuts_and_rejects_named_friend(self):
        class Node:
            def __init__(self, role, name='', children=(), editable=False):
                self.role, self.name, self.children, self.editable = role, name, children, editable
                for c in children: c.parent = self
            def __iter__(self): return iter(self.children)
            def getRoleName(self): return self.role
            def getState(self): return self
            def contains(self, _): return self.editable
        class API: STATE_EDITABLE = 7
        header = Node('push button', '我的手机')
        composer = Node('section', editable=True)
        toolbar = Node('tool bar', '会话', [Node('push button', '表情'), Node('push button', '文件')])
        frame = Node('frame', children=[header, composer, toolbar])
        with self.assertRaises(qq_sender.QQUnavailable):
            qq_sender.controls(frame, API)
        for name in ['截图', '截图 Ctrl + Alt + A', '截图 Ctrl + Shift + B']:
            with self.subTest(screenshot_name=name):
                toolbar = Node('tool bar', '会话', [Node('push button', name), Node('push button', name+'弹出菜单'), Node('push button', '文件'), Node('push button', '聊天记录')])
                send = Node('push button', '发送')
                messages = Node('embedded', '消息列表')
                frame = Node('frame', children=[header, composer, toolbar, send, messages])
                self.assertEqual(qq_sender.controls(frame, API), (composer,send,messages))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(loader.loadTestsFromName(name, QQChecks)
                              for name in QQChecks.__dict__ if name.startswith('test_'))
