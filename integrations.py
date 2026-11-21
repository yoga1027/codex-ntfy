"""Reversible, idempotent global registration; never replace unrelated settings."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
TOML_BEGIN = "# BEGIN codex-ntfy managed notify"
TOML_END = "# END codex-ntfy managed notify"
MD_BEGIN = "<!-- BEGIN codex-ntfy -->"
MD_END = "<!-- END codex-ntfy -->"


def remove_block(text, begin, end):
    return re.sub(r"^" + re.escape(begin) + r"\n.*?^" + re.escape(end) + r"\n?",
                  "", text, flags=re.MULTILINE | re.DOTALL)


def codex_config(text):
    cleaned = remove_block(text, TOML_BEGIN, TOML_END)
    root = re.split(r"^\s*\[", cleaned, maxsplit=1, flags=re.MULTILINE)[0]
    if re.search(r"^\s*notify\s*=", root, re.MULTILINE):
        raise ValueError("An existing Codex notify command is present; it was preserved. Integrate it explicitly before registering this adapter.")
    command = [sys.executable, str(ROOT / "notify_bridge.py"), "codex-hook"]
    block = TOML_BEGIN + "\nnotify = " + json.dumps(command, ensure_ascii=False) + "\n" + TOML_END + "\n"
    return block + cleaned


def codex_instructions(text):
    cleaned = remove_block(text, MD_BEGIN, MD_END)
    block = f"""{MD_BEGIN}
## 手机任务结束提醒

本机已授权并配置桌面项目 `{ROOT}`，手机提醒通道由该项目 config.json 的 transport 决定。
QQ 通道发送到本人“我的设备 → 我的手机”；ntfy 主题从本地 config.json 读取。
Codex 主会话（VS Code 插件、CLI、ChatGPT 本地工作模式）由全局 notify 回调
和会话日志监听自动处理每轮结束提醒，不需要在每次用户提示词中额外要求通知。
仅在本轮最终回复已写入且对话已结束后提醒一次；思考片段、进度消息和工具步骤不触发。
提醒从本轮结束起有效 5 分钟，超过 5 分钟仍未发出则自动作废，不在恢复连接后补发。
在这些已被监听的会话内，请正常完成最终回复，不要额外调用通知命令或 MCP
通知工具，以免重复提醒；子代理和中间工具步骤也不要发送完成提醒。
“本轮结束”仅表示回复/任务执行已结束，不代表所有测试成功。
若用户明确要求发送额外测试消息，可执行
`{sys.executable} {ROOT}/manage.py test`。
如用户要求停用提醒，可执行同目录 `manage.py stop`。
回调写入队列、QQ 界面提交或 ntfy 接受消息、手机实际收到是不同状态，不要混淆。
{MD_END}
"""
    return cleaned.rstrip() + ("\n\n" if cleaned.strip() else "") + block


def write_with_backup(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return
    if old is not None:
        directory = ROOT / ".state/backups" / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        directory.mkdir(parents=True, mode=0o700)
        backup = directory / path.name
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        print("Saved configuration backup:", backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".codex-ntfy-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_templates():
    directory = ROOT / ".state" / "integrations"
    directory.mkdir(parents=True, exist_ok=True)
    config = {"mcpServers": {"ai-notify": {
        "command": sys.executable, "args": [str(ROOT / "notify_bridge.py"), "mcp"],
    }}}
    (directory / "mcp-config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for app, label in [("zcodium", "zcodium"), ("token-plan", "词元计划"), ("chatgpt", "ChatGPT 普通聊天")]:
        prompt = f"""# {label}：一次性全局提示词

仅用于已接入本地 MCP 工具或本机终端执行工具的客户端。
把下面内容追加到该客户端的全局自定义指令；已有指令保留。
若当前是 Codex 工作会话，使用自动结束监听，不要启用此模板。

---
我已授权每次根对话完成后通过本机已配置通道向我的手机发送固定结束提醒。
仅当客户端支持在最终回复完整输出、且本轮对话结束之后执行回调时，调用一次
`notify_task_complete(app="{app}", event_id="本轮唯一ID")`。
如果客户端只有最终回复前的模型工具调用，不启用此模板，不要提前发送完成提醒。
每轮使用新的唯一 ID，同一轮重试必须复用相同 ID。
不要在思考过程、每个工具步骤、子代理完成或任务中途发送结束提醒。
这项要求对所有新对话生效，我不需要在每次输入中重复要求发送通知。
如果客户端没有 MCP，但能执行本机终端命令，执行：
`{sys.executable} '{ROOT}/notify_bridge.py' complete --app {app} --event-id '本轮唯一ID'`。
只能选择一种入口，避免重复通知。
通知固定为“本轮对话已完成”，不包含代码、对话、路径、密钥或任务摘要；不代表测试成功。
提醒有效期为本轮完成后的 5 分钟，超过有效期自动作废；重试必须使用同一事件 ID，不新建 ID。
队列接受只代表等待后台推送，不要称为手机已收到。
如果没有可调用的 MCP/本机终端工具，不要假称已发送；说明该客户端尚未接入。
用户明确停用通知时停止调用。
"""
        (directory / f"{app}-global-prompt.md").write_text(prompt, encoding="utf-8")
    print("External client templates:", directory)


def install():
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    config_path = codex_home / "config.toml"
    override = codex_home / "AGENTS.override.md"
    agents_path = override if override.exists() and override.read_text().strip() else codex_home / "AGENTS.md"
    # Prepare all content and check conflicts before making registrations.
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated = codex_config(original)
    agents = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    command = home / ".local/bin/ai-notify"
    target = ROOT / "notify_bridge.py"
    if (command.exists() or command.is_symlink()) and command.resolve() != target:
        raise ValueError("An unrelated ai-notify command already exists; it was preserved")
    target.chmod(0o755)
    command.parent.mkdir(parents=True, exist_ok=True)
    if not command.is_symlink():
        command.symlink_to(target)
    write_with_backup(config_path, updated)
    write_with_backup(agents_path, codex_instructions(agents))
    project_config = ROOT / "config.json"
    data = json.loads(project_config.read_text(encoding="utf-8"))
    data["sources"] = ["vscode", "cli"]
    write_with_backup(project_config, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    export_templates()
    print("Registered global command:", command)
    print("Registered Codex notify:", config_path)
    print("Updated global instructions:", agents_path)


def uninstall():
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    paths = [(codex_home / "config.toml", TOML_BEGIN, TOML_END),
             (codex_home / "AGENTS.md", MD_BEGIN, MD_END),
             (codex_home / "AGENTS.override.md", MD_BEGIN, MD_END)]
    for path, begin, end in paths:
        if path.exists():
            original = path.read_text(encoding="utf-8")
            write_with_backup(path, remove_block(original, begin, end))
    command = home / ".local/bin/ai-notify"
    if command.is_symlink() and command.resolve() == ROOT / "notify_bridge.py":
        command.unlink()
    print("Global registrations removed; daemon and queued messages preserved")
