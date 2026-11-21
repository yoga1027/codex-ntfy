"""Regression checks for desktop login environment and safe queued recovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manage
from notification_queue import open_database


class SessionChecks(unittest.TestCase):
    def test_restart_imports_explicit_graphical_keys_before_start(self):
        with patch.dict(manage.os.environ, {'DISPLAY': ':9', 'XAUTHORITY': '/tmp/auth', 'XDG_SESSION_TYPE': 'x11', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/bus'}, clear=True), patch.object(manage.subprocess, 'run') as command:
            manage.systemctl('restart', manage.UNIT_NAME)
            calls = [c.args[0] for c in command.call_args_list]
        self.assertEqual(calls[0], ['systemctl', '--user', 'import-environment', 'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_SESSION_TYPE'])
        self.assertEqual(calls[1], ['systemctl', '--user', 'restart', manage.UNIT_NAME])

    def test_login_entry_is_idempotent_and_points_to_session_start(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'setup.desktop'
            with patch.object(manage, 'SESSION_AUTOSTART', file):
                manage.install_session_autostart()
                first = file.read_text()
                manage.install_session_autostart()
                self.assertEqual(file.read_text(), first)
                self.assertIn(' session-start\n', first)
                self.assertIn('Type=Application\n', first)

    def test_unrelated_autostart_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'setup.desktop'
            file.write_text('UNRELATED')
            with patch.object(manage, 'SESSION_AUTOSTART', file), self.assertRaises(ValueError):
                manage.install_session_autostart()
            self.assertEqual(file.read_text(), 'UNRELATED')

    def test_non_graphical_session_cannot_stop_the_service(self):
        with patch.dict(manage.os.environ, {}, clear=True), patch.object(manage, 'systemctl') as control:
            with self.assertRaises(ValueError): manage.session_start()
            control.assert_not_called()

    def test_login_recovery_preserves_sent_and_uncertain_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = root / 'unit.service'
            unit.write_text('unit')
            installed = root / 'installed.service'
            installed.symlink_to(unit)
            config = {'server': 'https://ntfy.sh', 'topic': 'test', 'sessions_dir': 'sessions', 'state_dir': 'state'}
            (root / 'config.json').write_text(json.dumps(config))
            db = open_database({'state_dir': root / 'state'})
            for key, next_attempt, sent, marker in [('retry', 100, None, None), ('sent', 200, 1, 'qq:id'), ('uncertain', 1e30, None, 'qq:attempting'), ('interrupted', 100, None, 'qq:attempting'), ('stale', 100, None, None)]:
                completed_at = time.time() - (301 if key == 'stale' else 20)
                db.execute('INSERT INTO notifications(event_key,completed_at,payload,next_attempt,sent_at,message_id) VALUES (?,?,\'{}\',?,?,?)', (key,completed_at,next_attempt,sent,marker))
            db.commit()
            with patch.object(manage, 'ROOT', root), patch.object(manage, 'UNIT_INSTALLED', installed), patch.object(manage, 'UNIT_SOURCE', unit), patch.dict(manage.os.environ, {'DISPLAY': ':9'}), patch.object(manage, 'systemctl') as control, patch.object(manage.subprocess, 'run') as process:
                process.return_value.returncode = 0
                manage.session_start()
                self.assertEqual([c.args[0] for c in control.call_args_list], ['stop','restart'])
            rows = {r['event_key']: r for r in db.execute('SELECT * FROM notifications')}
            self.assertEqual(rows['retry']['next_attempt'], 0)
            self.assertEqual(rows['sent']['next_attempt'], 200)
            self.assertEqual(rows['uncertain']['next_attempt'], 1e30)
            self.assertEqual(rows['interrupted']['next_attempt'], 100)
            self.assertIsNotNone(rows['stale']['expired_at'])
            self.assertEqual(rows['stale']['next_attempt'], 100)
            db.close()


if __name__ == '__main__': unittest.main()
