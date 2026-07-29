# OCI 免费实例控制台

面向 macOS 的 Oracle Cloud Always Free 容量重试工具。它使用 OCI 官方 Python SDK，在本机提供中文 Web 控制台，并以保守的单请求轮转方式尝试创建免费 ARM 和 AMD 实例。

> 本项目不会绕过 OCI 配额或容量限制，也不能保证一定抢到实例。使用前请在 Oracle 控制台确认账户类型、免费额度和计费状态。

## 主要功能

- 支持 `VM.Standard.A1.Flex` 和 `VM.Standard.E2.1.Micro`
- ARM、AMD、混合预设及自定义组合
- 自动发现区域子网、可用域和 ARM/AMD Ubuntu 镜像
- 查询账户真实实例和启动盘占用，创建前执行服务端额度校验
- 每个时间窗口只发送一个创建请求，依次轮换目标和可用域
- 每日请求上限、随机间隔、网络重试和 OCI 429 长退避
- 固定实例名称、幂等请求、本地进程锁和任务恢复
- 本地 Web 控制台启动/停止任务、查看实例、用量和活动记录
- macOS 系统通知，以及可选的 PushPlus 和 Telegram 通知

## 免费额度与方案

程序当前按以下 Always Free 上限保护：

- ARM A1.Flex：合计 `2 OCPU / 12 GB` 内存
- AMD E2.1.Micro：最多 2 台，每台固定约 `1 OCPU / 1 GB`
- 启动盘：账户所有活动启动盘合计不超过 200 GB

内置方案：

| 方案 | 实例 | 适用场景 |
|---|---|---|
| ARM 2C / 12G | 1 台 ARM，2C/12G | 推荐；单机性能最好，只需成功创建一次 |
| ARM 双机 | 2 台 ARM，每台 1C/6G | 需要两台独立主机或简单容灾 |
| AMD Micro 双机 | 2 台 AMD Micro | x86 兼容、小型服务、监控或跳板机 |
| ARM + AMD 混合 | 1 台 ARM 2C/12G + 2 台 AMD | 使用两个独立免费计算池 |

不能在免费额度内创建两台 `2C/12G` ARM；两台合计 `4C/24G`，可能被拒绝或在 PAYG 账户产生费用。程序会拒绝这种配置。

## 系统要求

- macOS 12 或更高版本
- Python 3.11 或更高版本
- 一个可正常使用 OCI API 的 Oracle Cloud 账户
- OCI 主区域中已有 VCN 和区域公共子网
- 本机已有 SSH 公钥，推荐 `~/.ssh/id_ed25519.pub`
- 需要长期运行时，Mac 应接通电源并保持联网、开盖

## 第一步：准备 SSH 密钥

如果 `~/.ssh/id_ed25519.pub` 不存在，在终端运行：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

按提示设置密码，或直接回车使用空密码。程序只读取 `.pub` 公钥并注入新实例，不会读取 SSH 私钥。

## 第二步：准备 OCI 网络

在 OCI 控制台确认主区域已经有区域子网：

1. 打开“网络” -> “虚拟云网络”。
2. 没有 VCN 时，使用“启动 VCN 向导”创建带互联网连接的 VCN。
3. 进入子网详情，确认子网是区域子网，而不是只绑定一个可用域的子网。
4. 若希望实例获得公网地址，子网应允许公共 IP，并配置互联网网关和路由规则。

自动配置器默认选择根区间中找到的第一个可用区域子网。

## 第三步：安装 OCI API 密钥

首次双击 `start.command` 时，程序会自动打开 `API_KEY_SETUP.html`。也可以提前按以下步骤准备：

1. 登录 OCI 控制台。
2. 点击右上角头像 -> “我的概要信息”或“用户设置”。
3. 打开“API 密钥” -> “添加 API 密钥”。
4. 选择“生成 API 密钥对”，下载 PEM 私钥并完成添加。
5. 下载配置文件；其中应包含 `[DEFAULT]`、`user`、`fingerprint`、`tenancy` 和 `region`。
6. 回到启动器，依次选择下载的配置文件和 PEM 私钥。

安装器会把文件保存为：

```text
~/.oci/config
~/.oci/oci_api_key.pem
```

两者权限会自动设为 `600`，私钥路径也会自动修正。不要把这些文件上传或发给他人。

## 第四步：首次启动

### Finder 启动

1. 下载或克隆项目。
2. 在 Finder 中进入项目目录。
3. 双击 `start.command`。
4. macOS 首次阻止时，右键 `start.command` -> “打开” -> 再确认。
5. 等待虚拟环境、依赖和 OCI 资源配置完成。
6. 浏览器会自动打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)。

### 终端启动

```bash
git clone https://github.com/1510952971/oci-free-compute-console.git
cd oci-free-compute-console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python auto_configure.py
caffeinate -dimsu python grab_a1.py
```

## 第五步：选择方案

推荐顺序：

1. 首先选择“ARM 2C / 12G”，点击“启动”。
2. ARM 创建成功后，如需使用 AMD 免费池，再选择“AMD Micro x 2”。
3. 需要两台 ARM 时改选“ARM 2 x 1C / 6G”。

面板可以关闭，后台进程仍会继续。停止任务请点击面板“停止”；彻底退出程序请回到终端按 `Ctrl+C`。

## 24 小时运行

`start.command` 使用以下方式启动：

```bash
caffeinate -dimsu python grab_a1.py
```

这能阻止 Mac 的正常空闲睡眠，但仍需注意：

- MacBook 合盖通常会睡眠，建议开盖并接通电源。
- 断网、注销、关机或关闭终端会中断程序。
- 网络恢复后，程序会按退避策略继续。
- 每日达到请求上限后会等待，午夜自动重置并继续。
- 任务状态保存在 `.grab_a1_state.json`，异常退出后重新启动可恢复未完成任务。

### 安装 macOS 后台服务

不希望依赖终端窗口时，首次配置完成后双击：

```text
install_background_service.command
```

它会在当前用户的 `~/Library/LaunchAgents` 安装服务，并使用 `launchd + caffeinate`：

- 登录 macOS 后自动启动
- 进程异常退出时自动重启
- 工作线程意外退出时由进程内看门狗自动恢复
- 工作线程超过 3 分钟无心跳时重启整个服务并续接任务
- 自动恢复 `.grab_a1_state.json` 中未完成的任务
- 标准日志写入 `~/Library/Logs/oci-free-compute-console.log`
- 错误日志写入 `~/Library/Logs/oci-free-compute-console-error.log`

后台模式不需要保持终端窗口或浏览器页面打开。Mac 仍需接通电源、保持联网，MacBook 最好保持开盖。

需要停止并移除后台服务时，双击：

```text
uninstall_background_service.command
```

卸载脚本不会删除配置、日志、项目文件或 OCI 云实例。

## 配置说明

首次运行会生成忽略提交的 `config.toml`。模板见 `config.example.toml`。

常用配置：

```toml
region = "<your-home-region>"
compartment_id = "<你的区间 OCID>"
subnet_id = "<你的子网 OCID>"
arm_image_id = "<ARM 镜像 OCID>"
micro_image_id = "<AMD 镜像 OCID>"
ssh_public_key_file = "~/.ssh/id_ed25519.pub"

display_name_prefix = "free-oci"
default_preset = "arm_full" # arm_full / arm_dual / micro_dual / mixed

retry_seconds = 480
jitter_seconds = 240
daily_attempt_limit = 180
```

请求间隔为 `retry_seconds + 0..jitter_seconds`。默认约 8-12 分钟，不建议改成秒级高频请求。

## 通知配置

macOS 通知默认可用。远程通知可在 `config.toml` 中配置：

```toml
pushplus_token = ""
pushplus_topic = ""
telegram_bot_token = ""
telegram_chat_id = ""
notification_proxy = "" # 仅 Telegram，例如 http://127.0.0.1:7890
```

配置后点击控制台顶部的通知测试按钮。通知令牌只保存在本机 `config.toml`，不会返回浏览器，也不会进入 Git。

## 请求与容错策略

- 每个时间窗口只发送一个 OCI `LaunchInstance` 请求。
- 目标实例和区域内可用域按游标轮换。
- 每个创建请求使用独立的 OCI 幂等令牌。
- `OutOfHostCapacity` 视为正常容量不足并继续轮换。
- 网络错误按 1、2、4、8、15 分钟退避。
- OCI `429` 从 30 分钟开始指数退避，最长 2 小时。
- 权限、镜像、参数和配额错误会停止任务，不会无限重试。
- 默认每日最多 180 个创建请求；最短 8 分钟间隔下可覆盖全天，午夜按本机日期重置。
- 程序只识别固定名称的目标，不会删除或修改其他实例。

## 命令行

```bash
# 只读查询账户资源并验证默认方案，不创建实例
python grab_a1.py --dry-run

# 运行指定预设
python grab_a1.py --preset arm_full
python grab_a1.py --preset arm_dual
python grab_a1.py --preset micro_dual
python grab_a1.py --preset mixed

# 只启动控制台，等待网页操作
python grab_a1.py --server-only

# 更换本地面板端口
python grab_a1.py --dashboard-port 9000

# 旧参数兼容，已按当前免费额度解释
python grab_a1.py --plan single
python grab_a1.py --plan dual
```

## 测试

```bash
python -m unittest discover -s tests -v
python -m py_compile grab_a1.py auto_configure.py discover_resources.py install_oci_credentials.py
```

测试不会连接 OCI，也不会创建云资源。`--dry-run` 会连接 OCI，但只执行查询。

## 常见问题

### 一直显示“暂无容量”

这是 OCI 当前可用域没有对应免费规格容量，并非程序错误。保持低频运行并等待库存释放即可。

### 达到 180 次后不再请求

这是每日安全上限。程序会等待到本机午夜，重置计数后自动继续。

### 合盖后停止更新

MacBook 合盖通常进入睡眠，`caffeinate` 不能保证合盖联网。请开盖接电，或使用满足合盖模式条件的外接显示器、电源和键鼠。

### 自动配置找不到子网

确认主区域根区间存在状态为 `AVAILABLE` 的区域子网。其他区间中的子网需要手动填写 `config.toml`。

### 自动配置找不到镜像

可以在 OCI 创建实例页面选择与规格架构匹配的 Ubuntu 镜像，并把 OCID 分别填入 `arm_image_id` 或 `micro_image_id`。

### 页面打不开

确认终端中的程序仍在运行，并检查端口是否被占用：

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

## 项目结构

```text
grab_a1.py                 核心引擎、本地 API 和 Web 服务
dashboard.html             中文控制台
auto_configure.py          自动发现子网、镜像和可用域
install_oci_credentials.py OCI 凭据安装器
API_KEY_SETUP.html         OCI API 密钥图文步骤
discover_resources.py      只读资源发现辅助脚本
config.example.toml        配置模板
start.command              macOS 一键启动器
install_background_service.command   安装 launchd 后台服务
uninstall_background_service.command 卸载 launchd 后台服务
tests/                     纯逻辑测试
```

## 安全说明

- Web 服务只监听 `127.0.0.1`，但 API 具备创建云资源的能力且没有登录鉴权。
- 不要将端口通过公网反向代理、路由器端口转发或隧道服务暴露出去。
- `.gitignore` 排除了 PEM、真实配置、运行状态、RTF、虚拟环境和缓存。
- 提交前仍应使用 `git status` 确认没有凭据文件。
- PAYG 账户超出免费额度可能产生费用，请定期检查 OCI 成本分析。

## 免责声明

本项目按现状提供，仅用于个人学习和管理自己的 OCI 账户。Oracle 的免费政策、规格和限额可能调整，请以 OCI 官方控制台和文档为准。使用者自行承担账户限制、资源费用和数据安全责任。
