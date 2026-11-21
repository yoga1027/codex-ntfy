#!/usr/bin/env python3
"""Install, run, and check this standalone Codex notification project."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sqlite3
import json
import integrations

ROOT = Path(__file__).resolve().parent
UNIT_NAME = "codex-ntfy.service"
UNIT_SOURCE = ROOT / "systemd" / UNIT_NAME
UNIT_INSTALLED = Path.home() / ".config/systemd/user" / UNIT_NAME
SESSION_AUTOSTART = Path.home() / '.config/autostart/codex-ntfy-session.desktop'
SESSION_MARKER = '# Managed by codex-ntfy: graphical session environment'


def systemctl(*args):
    if args[0] in {"restart", "start"}:
        keys = [k for k in ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE") if k in os.environ]
        if keys:
            subprocess.run(["systemctl", "--user", "import-environment", *keys], check=True)
    subprocess.run(["systemctl", "--user", *args], check=True)


def quote_unit_path(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def write_unit():
    UNIT_SOURCE.parent.mkdir(parents=True, exist_ok=True)
    UNIT_SOURCE.write_text(
        "[Unit]\n"
        "Description=Send AI conversation completion reminders\n\n"
        "[Service]\n"
        "Type=simple\n"
        "EnvironmentFile=-" + str(ROOT / "network.env").replace("%", "%%") + "\n"
        "ExecStart=" + quote_unit_path(sys.executable) + " "
        + quote_unit_path(ROOT / "codex_ntfy.py") + " watch --config "
        + quote_unit_path(ROOT / "config.json") + "\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=15\nUMask=0077\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )


def install_session_autostart():
    if SESSION_AUTOSTART.exists() and SESSION_MARKER not in SESSION_AUTOSTART.read_text():
        raise ValueError('An unrelated desktop autostart file exists; preserved')
    desktop = (SESSION_MARKER + '\n[Desktop Entry]\nType=Application\n'
               'Name=AI reminder session setup\n'
               'Comment=Connect the notification service to this logged-in desktop\n'
               'Exec=' + quote_unit_path(sys.executable) + ' ' + quote_unit_path(ROOT / 'manage.py') + ' session-start\n'
               'Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n')
    integrations.write_with_backup(SESSION_AUTOSTART, desktop)


def session_start():
    # Run from XDG desktop autostart, after the login session has its own
    # DISPLAY/XAUTHORITY. Values must be refreshed on every login, not pinned.
    if not os.environ.get('DISPLAY'):
        raise ValueError('No graphical DISPLAY is available; run from the logged-in desktop')
    if not UNIT_INSTALLED.is_symlink() or UNIT_INSTALLED.resolve() != UNIT_SOURCE:
        raise ValueError('The installed service does not belong to this project')
    enabled = subprocess.run(['systemctl', '--user', 'is-enabled', UNIT_NAME], capture_output=True, text=True)
    if enabled.returncode:
        print('Service is disabled; desktop setup did not start it')
        return
    from codex_ntfy import load_config
    config = load_config(ROOT / 'config.json')
    if config["transport"] in {"oppo", "tailscale"}:
        return
    systemctl('stop', UNIT_NAME)
    from notification_queue import open_database, expire_pending
    db = open_database(config)
    try:
        expire_pending(db, config)
        with db:
            # Wake retryable work. Never replay a send with an uncertain outcome.
            db.execute('UPDATE notifications SET next_attempt=0 WHERE sent_at IS NULL AND expired_at IS NULL AND message_id IS NULL AND next_attempt<1e29')
    finally:
        db.close()
    systemctl('restart', UNIT_NAME)
    print('Desktop environment refreshed; retryable reminders resumed')


def network_environment():
    environment = os.environ.copy()
    path = ROOT / "network.env"
    allowed = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NTFY_TOKEN",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in allowed:
                raise ValueError("Unsupported network.env setting: " + key)
            parsed = shlex.split(value)
            if len(parsed) != 1:
                raise ValueError("Invalid network.env value: " + key)
            environment[key] = parsed[0]
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "start", "stop", "restart", "status",
                                          "logs", "test", "check", "uninstall", "integrate",
                                          "remove-integrations", "export", "doctor", "use-qq", "use-ntfy",
                                          "session-start", "install-session-start"])
    args = parser.parse_args()
    if args.command in {"install", "integrate"}:
        from codex_ntfy import load_config
        try:
            load_config(ROOT / "config.json")
        except (OSError, ValueError) as error:
            parser.exit(1, str(error) + "\n")
    if args.command == 'session-start':
        session_start()
    elif args.command == 'install-session-start':
        install_session_autostart()
        print('Registered desktop login setup:', SESSION_AUTOSTART)
    elif args.command in {"use-qq", "use-ntfy"}:
        if args.command == "use-qq":
            launcher = Path.home() / ".local/share/applications/qq.desktop"
            original = launcher.read_text() if launcher.exists() else Path('/usr/share/applications/qq.desktop').read_text()
            if '--force-renderer-accessibility' not in original:
                if 'Exec=/opt/QQ/qq ' not in original:
                    raise ValueError('QQ launcher is unexpected; preserved without modification')
                integrations.write_with_backup(launcher, original.replace('Exec=/opt/QQ/qq ', 'Exec=/opt/QQ/qq --force-renderer-accessibility ', 1))
        config_path = ROOT / 'config.json'
        config = json.loads(config_path.read_text())
        config['transport'] = 'qq' if args.command == 'use-qq' else 'ntfy'
        integrations.write_with_backup(config_path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        install_session_autostart()
        write_unit()
        systemctl('daemon-reload')
        systemctl('restart', UNIT_NAME)
        print('Transport:', config['transport'])
        if args.command == "use-qq":
            print('QQ requires a logged-in, unlocked X11 desktop and QQ started with --force-renderer-accessibility.')
    elif args.command == "install":
        if (UNIT_INSTALLED.exists() or UNIT_INSTALLED.is_symlink()) and UNIT_INSTALLED.resolve() != UNIT_SOURCE:
            raise SystemExit("Another unit occupies " + str(UNIT_INSTALLED) + "; no file was overwritten.")
        write_unit()
        install_session_autostart()
        UNIT_INSTALLED.parent.mkdir(parents=True, exist_ok=True)
        if not UNIT_INSTALLED.is_symlink():
            UNIT_INSTALLED.symlink_to(UNIT_SOURCE)
        systemctl("daemon-reload")
        systemctl("enable", UNIT_NAME)
        systemctl("restart", UNIT_NAME)
        systemctl("status", UNIT_NAME, "--no-pager")
    elif args.command == "logs":
        subprocess.run(["journalctl", "--user", "-u", UNIT_NAME, "-n", "30", "--no-pager"], check=True)
    elif args.command == "test":
        subprocess.run([sys.executable, str(ROOT / "codex_ntfy.py"), "test",
                        "--config", str(ROOT / "config.json")], env=network_environment(), check=True)
    elif args.command == "check":
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-v"], check=True)
    elif args.command == "integrate":
        integrations.install()
        install_session_autostart()
        if UNIT_INSTALLED.is_symlink() and UNIT_INSTALLED.resolve() == UNIT_SOURCE:
            write_unit()
            systemctl("daemon-reload")
        systemctl("restart", UNIT_NAME)
    elif args.command == "remove-integrations":
        integrations.uninstall()
    elif args.command == "export":
        integrations.export_templates()
    elif args.command == "doctor":
        from codex_ntfy import load_config
        config = load_config(ROOT / "config.json")
        result = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True, text=True)
        print("Service:", result.stdout.strip() or "unknown")
        print('Desktop login setup:', 'registered' if SESSION_AUTOSTART.exists() and SESSION_MARKER in SESSION_AUTOSTART.read_text() else 'missing')
        service_pid = subprocess.run(['systemctl', '--user', 'show', UNIT_NAME, '--property=MainPID', '--value'], capture_output=True, text=True).stdout.strip()
        if config["transport"] == "qq" and service_pid.isdigit() and service_pid != '0':
            try:
                env = dict(item.split('=', 1) for item in Path('/proc', service_pid, 'environ').read_bytes().decode().split('\0') if '=' in item)
                print('Service graphical environment:', 'DISPLAY available' if env.get('DISPLAY') else 'DISPLAY MISSING')
            except (OSError, UnicodeError):
                print('Service graphical environment: not accessible')
        print("Transport:", config["transport"])
        if config["transport"] == "ntfy":
            print("Server:", config["server"], "Topic:", config["topic"])
        elif config["transport"] == "tailscale":
            local = config["tailscale"]
            print("Tailscale receiver:", f"{local['listen_address']}:{local['port']}", "Phone:", local["peer_address"])
        print("Watched root sources:", config["sources"])
        command = Path.home() / ".local/bin/ai-notify"
        print("Global command:", "registered" if command.is_symlink() and command.resolve() == ROOT / "notify_bridge.py" else "missing")
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        codex_config = codex_home / "config.toml"
        registered = codex_config.exists() and integrations.TOML_BEGIN in codex_config.read_text(encoding="utf-8")
        print("Codex global notify:", "registered (client reload required after changes)" if registered else "missing")
        db_path = config["state_dir"] / "state.sqlite3"
        if db_path.exists():
            with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
                pending, sent, expired = db.execute("SELECT COALESCE(SUM(sent_at IS NULL AND expired_at IS NULL),0), COALESCE(SUM(sent_at IS NOT NULL),0), COALESCE(SUM(expired_at IS NOT NULL),0) FROM notifications").fetchone()
            print("Queue: pending=", pending, "sent=", sent)
            print('Expired reminders (never replayed):', expired)
            with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
                uncertain = db.execute("SELECT COUNT(*) FROM notifications WHERE sent_at IS NULL AND expired_at IS NULL AND next_attempt>1e29").fetchone()[0]
            print('QQ outcomes requiring inspection:', uncertain)
        print("Phone receipt and banner status: not observable from this computer")
    elif args.command == "uninstall":
        if UNIT_INSTALLED.resolve() != UNIT_SOURCE:
            raise SystemExit("Installed unit does not belong to this project; no changes made.")
        systemctl("disable", "--now", UNIT_NAME)
        UNIT_INSTALLED.unlink()
        if SESSION_AUTOSTART.exists() and SESSION_MARKER in SESSION_AUTOSTART.read_text():
            SESSION_AUTOSTART.unlink()
        systemctl("daemon-reload")
        integrations.uninstall()
        print("Background service removed. Project files and notification state were preserved.")
    else:
        extra = ["--no-pager"] if args.command == "status" else []
        systemctl(args.command, UNIT_NAME, *extra)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
