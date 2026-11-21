# coding love · Codex / Claude Code 手机助手

在 Linux 电脑上监听 AI 会话，通过 Tailscale 把完成提醒发到 Android 手机。手机还能查看和续接桌面会话、查额度、管理项目，并在有完整补丁记录时预览、撤回或重新应用单轮改动。只需要通知的话，也可以用 ntfy、QQ 或 OPPO PUSH。

## 快速部署（Linux + Android）

### 1. 克隆并准备本地配置

电脑需要 Git、Python 3.10+、用户级 systemd 和 Tailscale。手机需要 Android 8.0（API 26）或更新版本。电脑端监听和手机端会话操作都在运行 Codex 的 Linux 用户下执行。

```bash
git clone https://github.com/YOUR_ACCOUNT/codex-ntfy.git
cd codex-ntfy
install -m 600 config.example.json config.json
```

把第一行替换成你实际克隆的 GitHub 地址。上面的配置复制命令用于首次安装。更新时保留已有的 `config.json`，按示例补充新增字段。

### 2. 配对电脑和手机

让两台设备加入同一个 Tailscale 网络并连接。电脑运行 `tailscale ip -4`，手机在 Tailscale App 或管理后台的设备列表中查看自己的 IPv4 地址。编辑根目录的 `config.json`。

| 字段 | 填写方式 |
| --- | --- |
| `transport` | 使用内置 Android App 时填 `tailscale`。 |
| `tailscale.listen_address` | 电脑的 Tailscale IPv4。 |
| `tailscale.peer_address` | 手机的 Tailscale IPv4，和电脑地址不同。 |
| `tailscale.port` | 默认 `8765`，可改为 1024–65535 内未占用的端口。 |
| `sessions_dir` | 默认 `~/.codex/sessions`。自定义了 `CODEX_HOME` 就填该目录下的 `sessions` 路径。 |
| `sources` | 默认 `["vscode", "cli"]`。只监听特定来源时可保留其中一个。 |
| `state_dir` | 默认 `.state`。相对路径以配置文件所在目录为准。 |
| `max_notification_age_seconds` | 默认 300 秒，可设为大于 0 且不超过 300。 |

`server` 和 `topic` 用于 ntfy，Tailscale 部署可以保留示例值。确保 tailnet 访问规则和电脑防火墙允许手机连接到电脑的这个 TCP 端口。 [查找设备的 Tailscale 地址](https://tailscale.com/docs/concepts/ip-and-dns-addresses)

服务只在 Linux 的 `tailscale0` 接口绑定指定电脑地址，并按手机地址校验连接。App 通过 Tailscale VPN 访问 HTTP，明文 HTTP 例外仅允许配置中的电脑地址。配对手机可访问会话内容并发起桌面操作，请只配对自己的受信设备。保持端口在 tailnet 内，避免端口转发、Funnel 或公开反向代理。

### 3. 启动电脑服务

```bash
python3 manage.py check
python3 manage.py install
python3 manage.py doctor
```

`install` 生成本机 systemd 单元、启用并启动用户服务。默认在用户会话启动时运行。服务需要能读取 Codex 记录，离开电脑期间也需要电脑保持开机、联网且服务在运行。首次监听跳过安装前的历史任务，手机会话列表仍可读取已有历史。

可选的 Codex 全局回调注册命令如下。

```bash
python3 manage.py integrate
```

它注册 `~/.local/bin/ai-notify`，在 `CODEX_HOME`（默认 `~/.codex`）下添加顶层 `notify` 和受标记管理的全局指令，并启用 `vscode` 与 `cli` 两种日志来源。如果已使用其他 `notify`，注册会保留原配置并报冲突，此时可以继续用日志监听。修改前的文件备份在 `.state/backups/`。注册后重启相应的 Codex 客户端，或开启新会话加载配置。 [Codex 全局通知配置](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)

### 4. 构建并安装 Android App

在构建电脑安装 JDK 17 和 Android SDK Command-line Tools，设置 `JAVA_HOME`、`ANDROID_HOME`，并把 `sdkmanager` 所在目录和 `$ANDROID_HOME/platform-tools` 加入 `PATH`。构建使用 Android SDK Platform 35、Build Tools 34.0.0 和仓库内的 Gradle 8.9 Wrapper。SDK 下载脚本还需要 `curl`、`unzip` 和 `sha256sum`。

从仓库根目录运行以下命令。

```bash
sdkmanager 'platforms;android-35' 'build-tools;34.0.0' platform-tools
sdkmanager --licenses
cd android
./fetch-oppo-sdk.sh
./gradlew :app:assembleDebug :app:lintDebug
adb devices
adb install -r app/build/outputs/apk/debug/app-debug.apk
cd ..
```

当前源码把可选的 OPPO 注册入口也编译进 App，构建时需下载并校验 OPPO SDK。使用 Tailscale 时直接跳过 OPPO 账号和凭据配置。首次构建还需要访问 Gradle、Google Maven 和 Maven Central。也可在系统提示后手动安装生成的 APK。

Gradle 从根目录的本地 `config.json` 读取 Tailscale 地址和端口，生成连接参数及网络安全配置。没有本地配置或选用其他通道时，可构建未配对 APK。换电脑、换手机或改端口后，更新同一份配置，重启电脑服务并重新构建、覆盖安装。

默认安装包名为 `local.codexntfy.opush`。升级请保持包名和签名一致，妥善保存本机的 `~/.android/debug.keystore` 或自用发布签名。配对后的 APK 内含设备地址，适合自己安装。发布通用 APK 时用未配对配置构建。

### 5. 开始接收并验证

1. 手机上打开 Tailscale，完成 Android VPN 授权。打开 coding love，允许通知。在「连接」页点「开始接收」，等到显示「已连接电脑」。
2. 从新安装的项目目录运行 `python3 manage.py test` 发送一条真实测试提醒。Tailscale 模式先写入电脑队列，再由手机长轮询领取。
3. 确认手机通知栏实际看到该条提醒。再拔掉 USB，使用 Wi‑Fi 或移动数据并锁屏，让 Codex 完成一轮短会话，验证真实结束事件和后台接收。

App 退后台后由前台服务保持接收。按手机系统设置允许通知、锁屏展示和后台运行。若 ColorOS 等系统在锁屏后暂停接收，可给 coding love 和 Tailscale 解除电池限制，并确认 Tailscale 的应用分流没有排除 coding love。

## 日常使用

- 「消息」按会话汇总提醒。打开会话同步本地日志中的历史、用时、额度和改动。已缓存的历史可以离线查看。5 小时和 7 天额度以客户端实际提供的数据为准，缺失时显示「—」，时间以北京时间显示。
- 手机回复优先交给仍持有该会话的 VS Code Codex 客户端。兼容的会话也可通过电脑上的 Codex App Server 新建或续接。将已登录的 `codex` 加入服务的 `PATH`，或在 `config.json` 中用 `codex_executable` 填可执行文件绝对路径。若原窗口要求审批，在该窗口处理。客户端升级后，用可丢弃会话先验证续聊、终止和附件。
- 「项目」可读取兼容的 Codex Project Manager 数据库。可选字段 `project_manager_db` 用于指定自己的 `projects.sqlite`。文件预览限可访问的项目文本，单文件上限 8 MiB。撤回和重新应用依赖完整补丁、当前文件状态和同一对话的后续改动。
- 主 Codex 会话同轮有 `final_answer` 和 `task_complete` 才触发普通完成提醒，Tailscale 也支持明确记录的额度不足中断。中间消息、工具步骤和子代理不会触发。回调与日志按会话和轮次去重，超时提醒作废。收到提醒表示本轮结束，请通过最终回复和测试结果确认执行情况。

### 可选的 Claude Code 接入

在同一台 Linux 电脑上安装并登录 Claude Code。在 `config.json` 增加 `"claude_home": "~/.claude"`，若使用自定义配置目录就填写对应位置。可选的 `claude_executable` 接受可执行文件的绝对路径。备份 Claude 的 `settings.json` 后，在仓库根目录运行以下命令。

```bash
python3 -m pip install --target .state/claude-sdk -r requirements-claude.txt
python3 claude_stop_hook.py --install
python3 manage.py restart
```

命令会为 Claude 安装 `Stop` 和 `StopFailure` 钩子。用新的 Claude Code 窗口完成一轮并检查提醒。电脑交互进程仍持有 Claude 会话时，请在原窗口续聊或终止。移动项目目录或卸载时，从 Claude 的 `settings.json` 中删除指向旧 `claude_stop_hook.py` 路径的这两项命令，保留其他钩子。

如果修改过 `state_dir`，把上面 `--target` 的目录改为实际状态目录下的 `claude-sdk`。

## 其他通知通道

每次只配置一个 `transport`。修改本地配置后运行 `python3 manage.py restart`，并按所选通道验证实际手机接收。以下通道发送通知，内置 App 的会话同步需要 Tailscale。

### ntfy

将 `transport` 设为 `ntfy`，填写自己的 `server` 和 `topic`，在手机 ntfy App 上订阅同一服务和主题。主题可以包含英文字母、数字、连字符和下划线，最长 64 个字符。

服务需要身份验证或代理时，首次执行 `install -m 600 network.env.example network.env`，按需填写 `NTFY_TOKEN` 和 `HTTPS_PROXY`。授权订阅也要在手机端配置。公共主题的名称本身不能充当身份验证，建议给主题设置访问控制。 `manage.py test` 直接请求 ntfy，服务端接受后仍需查看手机。 [ntfy 发布接口](https://docs.ntfy.sh/publish/#publish-as-json)

### QQ

需要解锁的 Linux X11 桌面、已登录的 Linux QQ、系统 Python 的 `pyatspi`、`xdotool`、`wmctrl`、`gdbus` 和运行中的 CopyQ。运行 `python3 manage.py use-qq`，重启 QQ 使用户启动器的 `--force-renderer-accessibility` 生效。该通道会短暂激活 QQ，向「我的设备 → 我的手机」发送提醒，依赖客户端界面和无障碍控件。

如果日志提示 `DISPLAY MISSING`，在解锁的桌面终端运行 `python3 manage.py session-start`。若发送后结果不确定，先按 `[AI-…]` 标记在手机 QQ 中核对记录。也可使用其他无需图形会话的通道。

### OPPO PUSH

1. 准备已开通通知栏推送权限的 OPPO 应用。若平台登记的包名不同，构建时加 `-PoppoApplicationId=你登记的包名`，并使用符合平台绑定要求的签名。
2. 在「连接 → OPPO PUSH」中打开通知权限，用自己的客户端 AppKey 和 AppSecret 完成注册，在手机本地读取 `RegistrationID`。
3. 可运行 `python3 android/send_oppo_test.py` 进行独立单推，脚本会私下询问凭据。要由后台服务发送，在 `.state/oppo-credentials.json` 写入自己的 `app_key`、`master_secret`、`registration_id`，设置权限 `600`，并在 `config.json` 中将 `transport` 改为 `oppo`，用 `oppo_credentials_file` 指向该文件。服务端 MasterSecret 只留在电脑。
4. 查看手机通知栏和锁屏。API 返回 `messageId` 表示服务商接受请求，展示由应用权限、设备设置和 OPPO 账户限制共同决定。 [OPPO PUSH 通知栏消息](https://open.oppomobile.com/documentation/page/info?id=11236)

### 接入其他客户端

`python3 manage.py export` 把使用当前机器绝对路径的 stdio MCP 配置和指令模板写入 `.state/integrations/`。只有能在最终回复输出后调用本机命令或 MCP 的客户端才适合使用。普通网页聊天的提示词无法启动电脑上的脚本。

选择 `notify_task_complete(app, event_id)` 或本机命令 `ai-notify complete --app generic --event-id 轮次唯一ID` 中的一个入口。同一轮重试复用 ID，下一轮使用新 ID。已由 Codex 自动监听的会话无需再注册此模板。

## 运维与排查

| 命令 | 作用 |
| --- | --- |
| `python3 manage.py doctor` | 检查服务、通道、监听来源和队列状态。输出含本机路径和地址，分享日志前先脱敏。 |
| `python3 manage.py status` / `logs` | 查看用户服务和近期日志。 |
| `python3 manage.py check` | 运行离线测试。 |
| `python3 manage.py test` | 发送一条真实测试提醒。Tailscale、QQ、OPPO 模式先入队。 |
| `python3 manage.py stop` / `start` / `restart` | 暂停、启动或重启发送服务。暂停时全局回调仍可入队，有效期继续计算。 |
| `python3 manage.py remove-integrations` | 移除项目注册的 Codex 回调与全局命令，保留日志监听。 |
| `python3 manage.py uninstall` | 停用并移除 systemd 服务及项目注册的 Codex 接入，保留本地配置和数据。 |

- **App 连不上**。先检查电脑 `tailscale ip -4`、`manage.py doctor`、手机 VPN 和两端地址。手机浏览器可访问 `http://电脑的Tailscale IPv4:8765/health`，自定义了端口则用实际端口。只有配对手机能访问，验证成功会显示 `Tailscale link OK`。若问题出现在后台，检查电池管理和应用分流。
- **连接正常但收不到提醒**。测试提醒能收到时再检查 `sessions_dir`、监听来源和原生日志中的完整结束事件。Tailscale 队列的 `tailscale:posted` 表示手机 App 已向 Android 提交通知并回执。是否有横幅、声音或锁屏展示，以手机实际观察为准。
- **升级或迁移**。保留本地 `config.json`、`network.env`、`.state/` 与 APK 签名。等待活跃的手机发起轮次结束后重启服务。移动仓库前先卸载，移动后重新安装并注册回调，刷新外部模板和 Claude 钩子的路径。若从无图形会话的终端无法访问 `systemctl --user`，在该 Linux 用户已登录的终端执行管理命令。

## 公开源码与本地数据

`config.example.json` 和 `network.env.example` 可以提交。实际配置、生成的 systemd 单元、MCP 导出、队列数据库、会话日志、设备地址、凭据和签名仅保存在本地。构建产物位于 `android/app/build/`，包含配对信息的 APK 也请保存在本地。不要用 `git add -f` 将这些文件加入版本库。曾公开过的凭据应撤销并重新生成，同时清理包含它们的 Git 历史。

主要入口是 `manage.py`（部署）、`codex_ntfy.py`（日志监听）、`notification_queue.py`（队列）、`tailscale_receiver.py`（手机连接）和 `android/`（手机 App）。内置 Markdown 和公式渲染组件的许可文件保存在 `android/app/src/main/assets/chat/vendor/`。

结束事件桥接、幂等合并和配置备份参考了 [claude-mobile-notifier](https://github.com/zyli6269-cmyk/claude-mobile-notifier)。
