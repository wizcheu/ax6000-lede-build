# Redmi AX6000 · Lean LEDE build

基于 [coolsnowwolf/lede](https://github.com/coolsnowwolf/lede) 的个人精简编译配置，使用公开仓库的 GitHub Actions 标准 `ubuntu-24.04` runner。工作流编译、校验并发布固件，不连接路由器，也不执行刷机。

## 设备与布局

| 项目 | 选择 |
| --- | --- |
| 型号 | Xiaomi Redmi Router AX6000，MT7986 / 512 MB RAM |
| 平台 | `mediatek/filogic`，`aarch64_cortex-a53` |
| 唯一设备配置 | `xiaomi_redmi-router-ax6000` |
| NAND 布局 | NMBM；UBI offset `0x600000`，size `0x6e00000`（110 MiB） |
| 无线 | mt76 / `kmod-mt7915e`，完整 `wpad-openssl` |
| 内核 | 跟随所选 LEDE 提交的默认内核，初始为 6.12；不开 testing kernel |
| 输出 | squashfs sysupgrade `.bin`，无 Bootloader / 分区修改操作 |

此配置不适用于 `-stock` 双 UBI 布局或官方 OpenWrt 的 `-ubootmod` 布局。编译成功和镜像元数据校验不能代替真机启动、无线、拨号及迁移测试。流水线只构建和静态校验，不执行真机验证。

## 内置软件范围

- 中文 LuCI、Mono 默认主题（仅内置 Mono 和 Aeris，排除 Bootstrap、Argon 及 Argon 设置插件）、PPPoE、IPv6、firewall4/nftables、dnsmasq-full。
- OpenClash 及 Ruby/YAML、TUN、nft TPROXY 等依赖；内置上游 ARM64 Meta 核心和 Zashboard 面板。
- UPnP/NAT-PMP、DDNS（通用、Cloudflare、阿里云、DNSPod）、WOL、TurboACC、IGMP proxy。
- ttyd、htop、curl、SFTP 等管理和诊断工具。
- Tailcat 官方 ARM64 静态程序、procd 服务、设备密钥与启用状态的升级保留规则。
- 不预装 PassWall、SSR Plus、AdGuard Home、SmartDNS、VPN 服务端、ZeroTier、FRP、多拨、音乐解锁、Node、DAWN/mesh、应用过滤。

是否预装与是否启用是两回事。新镜像采用通用默认配置，OpenClash 默认关闭，没有订阅、拨号账号、无线密码、DHCP 绑定、端口转发或其他私人设置。后续迁移需逐项核对配置；现有 NAS/PT/STUN 映射不会由此仓库自动迁移。

不使用 Lean 的 `default-settings` 包：用一个简短的初始化脚本设置中文、Mono、UTC+8 和 OpenClash 核心架构；不预置共享 root 密码、不改软件仓库为旧镜像、不删除包签名校验。首次使用通用镜像时需在可信本地网络设置管理员密码。

## 运行编译

打开 **Actions → Build AX6000 LEDE → Run workflow**。

- 每次 push 触发的构建，以及手动 `refresh_sources=true`（默认）构建，都会先获取最新版本，再固定本次输入。编译过程中不继续追逐上游变化。
- `refresh_sources=false`：仅锁定其他上游组件；Mono 和 Aeris 仍在每次实际编译前分别查询最新正式发布版。
- 每次运行保存实际使用的完整锁文件；更新仅发生在 runner，不自动提交回仓库。核对某次构建时，以其 artifact 中的锁文件为准。关闭刷新可固定其他组件，但按当前策略，Mono 和 Aeris 始终更新，不提供旧主题重编译开关。
- 这是基于最新上游的配置，并非对任一第三方固件的逐字节复刻。源码固定也不保证构建时间、依赖镜像等因素下的逐字节可复现。

| 组件 | 每次刷新采用的版本 |
| --- | --- |
| Mono | `wizcheu/luci-theme-mono` 最新正式 release 的源码归档；验证 SHA256 后编译成固件内的主题包 |
| Aeris | `wizcheu/luci-theme-aeris` 最新正式 release 的源码归档；验证 SHA256 后编译成固件内的主题包 |
| LEDE、内核及内置软件 | LEDE master 最新提交所定义的版本 |
| packages、LuCI 及其软件包 | 对应 feed 分支的最新提交；LuCI 保持 openwrt-25.12 |
| OpenClash | 最新源码提交，读取其中的 `PKG_VERSION` 并编译插件 |
| Mihomo | OpenClash 官方 core 分支提供的最新 `master/meta` ARM64 核心，保持现有 Alpha 通道 |
| Zashboard | 官方最新稳定 release 的 `dist-cdn-fonts.zip`，替换 OpenClash 包内面板 |
| Tailcat | 官方最新稳定 release 的 `tailcat_<version>_linux_arm64.tar.gz` |

Mono 和 Aeris 在 `scripts/build.sh` 的入口分别检查最新正式发布版，直接重用已准备的构建目录也会刷新两套主题、选包和默认主题设置；查询、下载、源码版本或 SHA256 校验失败时终止编译，不回退旧主题。两套主题全部下载并验证后才替换构建目录中的主题包。每次构建记录最终使用的两个主题版本、标签、归档校验值和运行文件哈希。主题资源不再压缩，确保样式和最终文件哈希与发布源码一致；预览、测试和示例数据不进入固件。

Mihomo、Zashboard 和 Tailcat 使用上游发布的现成文件打入镜像。Mihomo 核对同一提交的 `checksums.txt`；Zashboard 和 Tailcat 核对 GitHub release asset 的 SHA256。脚本验证归档结构，并检查 Tailcat 是无需动态链接器的 AArch64 程序。版本查询、下载或校验失败会终止构建，不静默回退旧版本。源提交、版本、下载地址及校验值全部锁定后才更新锁文件。

LuCI 功能包采用显式选包，保留管理界面、软件包管理、防火墙和 Web 服务，避免 `luci` / `luci-light` 套装强制引入 Bootstrap。初始化时设定 Mono，并把保留配置中的主题列表重建为 Mono / Aeris，移除不存在的旧主题注册项。

构建会检查 `make defconfig` 是否保留全部必需项、是否重新启用了排除项、是否只选择一个设备，以及 DTS 中的 UBI 布局。产物还会验证型号元数据、kernel/root 归档结构、关键包清单、镜像大小和 SHA256，并从最终 squashfs 中读取 Mono、Aeris、Mihomo、Zashboard 和 Tailcat 文件，逐一核对哈希，防止构建时被旧文件覆盖。固件包清单必须恰好包含 Mono 和 Aeris 两个主题，不能包含 Argon 设置插件；根文件系统同时检查旧主题目录已移除、Mono / Aeris 的全部模板和默认设置脚本存在。Tailcat 的服务、迁移与升级保留脚本也在校验范围内，通用镜像不得带有设备密钥或 Tailcat 自启动链接。

Actions 成功后的摘要展示本次实际选择的主要版本；完整已安装包版本由编译产物的 manifest 和 `software-versions.json` 提供。固件内 `/etc/ax6000-build.json` 也记录六个内置组件的版本。Tailcat 通过文件覆盖层内置，不单独出现在 opkg 包清单。旧格式锁文件需要搭配当时的仓库提交复现，或先运行刷新脚本迁移到新格式。

运行成功后下载 `ax6000-lede-<run number>` artifact：

| 文件 | 用途 |
| --- | --- |
| `*-sysupgrade.bin` | 编译得到的升级镜像 |
| `SHA256SUMS` | 本次镜像的 SHA256 |
| `*.manifest` | 实际编入的软件包与版本 |
| `software-versions.json` | 全部实际软件包版本，以及 Mono / Aeris / OpenClash / Mihomo / Zashboard / Tailcat 版本与校验信息 |
| `config.full` / `config.diff` | 解析后的完整配置 / 最小配置 |
| `sources.lock.json` | 本次实际源码提交、组件版本及下载校验值 |
| `image-metadata.json` / `validation.json` | 设备匹配、大小、校验结果 |

失败或成功都保存 `ax6000-build-logs-<run number>`，artifact 保留 7 天。`main` 分支的构建成功后，独立的发布任务下载并重新核对固件，自动创建 `ax6000-build-<run number>-<attempt>` Release，长期保存上述固件和版本记录。每次运行及重试使用独立标签，不覆盖已发布固件；构建失败时不会发布。

固件以预发布（Pre-release）状态发布，因为尚未执行实机启动、无线和迁移测试。Release 说明包含实际组件版本、固件 SHA256、配置提交和 Actions 链接；日志仅保存在短期 artifact 中。编译任务保持只读权限，仅发布任务具有 `contents: write`。无定时任务、付费或自托管 runner，也不连接设备。

## Tailcat 初始化、连接地址与升级

全新刷机后，先让路由器联网，再通过路由器 SSH 或 LuCI 终端执行：

```sh
/etc/init.d/tailcat setup
/etc/init.d/tailcat address
```

`setup` 仅在设备没有密钥时创建独立身份，固定首次选择的中继区域，随后启用开机启动并启动服务。重复执行会复用原密钥；生成失败不会留下半个身份。通用固件、GitHub 仓库和编译日志中均不包含设备密钥或连接地址。服务只转发路由器本机的 `22,80,9090`，分别用于 SSH、LuCI 和 OpenClash。

`address` 输出的完整 `tc…` 字符串就是连接凭据，需整段保存。现有手动安装和新固件都可以直接读取：

```sh
cat /etc/tailcat/address
```

在装有 Tailcat 的电脑上，将下例占位内容替换为完整地址，保持命令运行：

```sh
tailcat --key=new forward '这里替换为完整的tc地址' 42022:22 42080:80 49090:9090
```

然后用 `ssh -p 42022 root@127.0.0.1` 连接 SSH，浏览器打开 `http://127.0.0.1:42080` 访问 LuCI；OpenClash 本地入口是 `http://127.0.0.1:49090`。这些服务仍使用各自原来的登录认证。

保留配置升级时，`/etc/tailcat/` 内的密钥、地址和服务启用链接会一起保留；保留原密钥与固定区域即可延续原地址。不要把 `router.private.json` 或完整地址上传到公开仓库。清空配置刷机前应私下备份整个 `/etc/tailcat/`，否则重新初始化会生成新地址。

此前手动安装的已知版本会自动迁移：仅在旧服务与旧程序的哈希均匹配时，替换为固件内置文件，并移除旧的程序/服务文件保留项，避免未来升级被旧文件覆盖。自定义服务脚本不会被强行替换。迁移逻辑经过隔离测试，实际刷机与联网验证需要另行执行。

## 修改软件与源码

编辑 `configs/ax6000.config`，增加 `CONFIG_PACKAGE_<name>=y` 或删除不需要的项后提交。校验脚本会阻止上游已改名/消失的必需包被静默忽略。大插件会增加编译耗时和镜像占用，应按需添加。

在装好 LEDE 编译依赖的 Ubuntu 24.04 上也可以执行：

```sh
python3 scripts/update_sources.py  # 可选：更新锁文件
python3 scripts/prepare.py        # 需要新的 work/ 目录
bash scripts/build.sh             # 始终刷新 Mono 和 Aeris 后编译
```

修改脚本后可运行 `python3 -m unittest discover -s scripts/tests -v`，检查主题更新与排除、下载校验、归档、密钥初始化和旧安装迁移。

上游软件包版本由各 feed 的维护者决定；“最新 feed 提交”不保证每个包都等于其原作者的最新 release。脚本不会批量篡改所有软件包 Makefile 的版本号，也不在路由器上执行 `opkg upgrade`。自编固件的内核模块必须匹配本次内核 ABI，不应从不同版本的公共 opkg 仓库随意混装。

来源：[Mono](https://github.com/wizcheu/luci-theme-mono)、[Aeris](https://github.com/wizcheu/luci-theme-aeris)、[Lean LEDE](https://github.com/coolsnowwolf/lede)、[packages](https://github.com/coolsnowwolf/packages)、[LuCI](https://github.com/coolsnowwolf/luci/tree/openwrt-25.12)、[OpenClash](https://github.com/vernesong/OpenClash)、[Mihomo](https://github.com/MetaCubeX/mihomo)、[Zashboard](https://github.com/Zephyruso/zashboard)、[Tailcat](https://github.com/tailscale/tailcat)。各上游代码和二进制遵循各自许可证；此仓库存放编译配置与编排脚本。Tailcat 的 MIT 许可证随固件放在 `/usr/share/licenses/tailcat/LICENSE`。
