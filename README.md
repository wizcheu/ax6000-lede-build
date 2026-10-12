# Redmi AX6000 · Lean LEDE build

基于 [coolsnowwolf/lede](https://github.com/coolsnowwolf/lede) 的个人精简编译配置，使用公开仓库的 GitHub Actions 标准 `ubuntu-24.04` runner。工作流只编译、校验、保存固件，不连接路由器，也不执行刷机。

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

## 首版软件范围

- 中文 LuCI、Argon 主题、PPPoE、IPv6、firewall4/nftables、dnsmasq-full。
- OpenClash 及 Ruby/YAML、TUN、nft TPROXY 等依赖；内置上游 ARM64 Meta 核心和 Zashboard 面板。
- UPnP/NAT-PMP、DDNS（通用、Cloudflare、阿里云、DNSPod）、WOL、TurboACC、IGMP proxy。
- ttyd、htop、curl、SFTP 等管理和诊断工具。
- 不预装 PassWall、SSR Plus、AdGuard Home、SmartDNS、VPN 服务端、ZeroTier、FRP、多拨、音乐解锁、Node、DAWN/mesh、应用过滤。

是否预装与是否启用是两回事。新镜像采用通用默认配置，OpenClash 默认关闭，没有订阅、拨号账号、无线密码、DHCP 绑定、端口转发或其他私人设置。后续迁移需逐项核对配置；现有 NAS/PT/STUN 映射不会由此仓库自动迁移。

不使用 Lean 的 `default-settings` 包：用一个简短的初始化脚本设置中文、Argon、UTC+8 和 OpenClash 核心架构；不预置共享 root 密码、不改软件仓库为旧镜像、不删除包签名校验。首次使用通用镜像时需在可信本地网络设置管理员密码。

## 运行编译

打开 **Actions → Build AX6000 LEDE → Run workflow**。

- 每次 push 触发的构建，以及手动 `refresh_sources=true`（默认）构建，都会先获取最新版本，再固定本次输入。编译过程中不继续追逐上游变化。
- `refresh_sources=false`：仅用于手动构建，使用仓库内 `sources.lock.json`，不查询最新版本。
- 每次运行保存实际使用的完整锁文件；更新仅发生在 runner，不自动提交回仓库。复现某次构建时，将其 artifact 中的锁文件放回仓库并关闭刷新。
- 这是基于最新上游的配置，并非对任一第三方固件的逐字节复刻。源码固定也不保证构建时间、依赖镜像等因素下的逐字节可复现。

| 组件 | 每次刷新采用的版本 |
| --- | --- |
| LEDE、内核及内置软件 | LEDE master 最新提交所定义的版本 |
| packages、LuCI 及其软件包 | 对应 feed 分支的最新提交；LuCI 保持 openwrt-25.12 |
| OpenClash | 最新源码提交，读取其中的 `PKG_VERSION` 并编译插件 |
| Mihomo | OpenClash 官方 core 分支提供的最新 `master/meta` ARM64 核心，保持现有 Alpha 通道 |
| Zashboard | 官方最新稳定 release 的 `dist-cdn-fonts.zip`，替换 OpenClash 包内面板 |

Mihomo 和 Zashboard 使用上游发布的现成文件打入镜像。前者核对同一提交的 `checksums.txt`，后者核对 GitHub release asset 的 SHA256；二者均验证归档结构。版本查询、下载或校验失败会终止构建，不静默回退旧版本。源提交、版本、下载地址及校验值全部锁定后才更新锁文件。

构建会检查 `make defconfig` 是否保留全部必需项、是否重新启用了排除项、是否只选择一个设备，以及 DTS 中的 UBI 布局。产物还会验证型号元数据、kernel/root 归档结构、关键包清单、镜像大小和 SHA256，并从最终 squashfs 中读取 Mihomo 和 Zashboard 文件，逐一核对哈希，防止构建时被旧文件覆盖。

Actions 摘要先展示本次选择的主要版本；完整已安装包版本由编译产物的 manifest 和 `software-versions.json` 提供。固件内 `/etc/ax6000-build.json` 也记录三个内置组件的版本。旧格式锁文件需要搭配当时的仓库提交复现，或先运行刷新脚本迁移到新格式。

运行成功后下载 `ax6000-lede-<run number>` artifact：

| 文件 | 用途 |
| --- | --- |
| `*-sysupgrade.bin` | 编译得到的升级镜像 |
| `SHA256SUMS` | 本次镜像的 SHA256 |
| `*.manifest` | 实际编入的软件包与版本 |
| `software-versions.json` | 全部实际软件包版本，以及 OpenClash / Mihomo / Zashboard 版本与校验信息 |
| `config.full` / `config.diff` | 解析后的完整配置 / 最小配置 |
| `sources.lock.json` | 本次实际源码提交、组件版本及下载校验值 |
| `image-metadata.json` / `validation.json` | 设备匹配、大小、校验结果 |

失败或成功都保存 `ax6000-build-logs-<run number>`。artifact 保留 7 天；请及时下载需要长期保留的产物。工作流不创建 GitHub Release，无定时任务，无付费或自托管 runner，无 SSH 或仓库写权限。

## 修改软件与源码

编辑 `configs/ax6000.config`，增加 `CONFIG_PACKAGE_<name>=y` 或删除不需要的项后提交。校验脚本会阻止上游已改名/消失的必需包被静默忽略。大插件会增加编译耗时和镜像占用，应按需添加。

在装好 LEDE 编译依赖的 Ubuntu 24.04 上也可以执行：

```sh
python3 scripts/update_sources.py  # 可选：更新锁文件
python3 scripts/prepare.py        # 需要新的 work/ 目录
bash scripts/build.sh
```

上游软件包版本由各 feed 的维护者决定；“最新 feed 提交”不保证每个包都等于其原作者的最新 release。脚本不会批量篡改所有软件包 Makefile 的版本号，也不在路由器上执行 `opkg upgrade`。自编固件的内核模块必须匹配本次内核 ABI，不应从不同版本的公共 opkg 仓库随意混装。

来源：[Lean LEDE](https://github.com/coolsnowwolf/lede)、[packages](https://github.com/coolsnowwolf/packages)、[LuCI](https://github.com/coolsnowwolf/luci/tree/openwrt-25.12)、[OpenClash](https://github.com/vernesong/OpenClash)、[Mihomo](https://github.com/MetaCubeX/mihomo)、[Zashboard](https://github.com/Zephyruso/zashboard)。各上游代码和二进制遵循各自许可证；此仓库存放编译配置与编排脚本。
