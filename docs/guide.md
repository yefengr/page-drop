# 部署与使用指南

PageDrop 由自托管的 Docker 服务、本地 `publish` CLI 和可选的 AI 技能组成。本指南以单台 Linux 服务器、宿主机 Caddy 和 macOS/Linux 客户端为例；`pages.example.com` 是示例域名，使用时须替换。

服务入口使用管理员自己的域名，管理 API 需要 Token；**发布页面本身不需要登录或 Token**。知道链接的人可以访问、转发或保存内容。不要发布敏感材料，也不要托管不可信的第三方 HTML/JavaScript。项目不提供公共服务器或每个发布独立设置域名的功能。

## 1. 准备

- 一台能通过 SSH 管理的 Linux 服务器，安装 Docker、Docker Compose 插件和 Caddy；Compose 需支持 `up --wait` 和 `--wait-timeout`。
- 一个指向服务器的域名；为 Caddy 的 HTTPS 证书准备可用的 DNS、80/443 端口和防火墙规则。
- 本地安装 Git、Python 3、SSH 和支持部署脚本所用选项的 `tar`；CLI 仅使用 Python 标准库。服务器需要 `curl`、用于生成 Token 的 `openssl`，并能够构建 Python 镜像和获取依赖。
- 确认服务器时间同步，`127.0.0.1:18090` 未被占用，`/srv` 所在磁盘有足够余量。服务设置了上传磁盘余量保护，并不代替容量规划；限制见 [README 的配额章节](../README.md#8-配额与容量)。
- 部署脚本通过 SSH 执行命令，不会自动使用 `sudo`。远程用户需要有权限创建和写入目标目录、执行 `install -o` 设置 UID 10001 的属主、运行 Docker；请按自己的安全策略配置权限。

在本地获取代码：

```bash
git clone https://github.com/yefengr/page-drop.git
cd page-drop
```

下文明确区分“服务器”和“本地”；不要将 Token、配置文件或实际发布链接提交到仓库。

## 2. 配置服务器

在服务器创建 `/srv/page-drop`，只允许授权管理员访问。参照 [`deploy/env.example`](../deploy/env.example) 创建 `/srv/page-drop/.env`，并将其设为 `0600`：

```dotenv
PAGE_DROP_TOKEN=<独立生成的随机 Token>
PAGE_DROP_PUBLIC_URL=https://pages.example.com
```

可在可信终端使用 `openssl rand -hex 32` 生成随机 Token。将实际值安全写入 `.env`，不要粘贴到公开命令、日志或 AI 对话；不要保留示例中的占位 Token。`PAGE_DROP_PUBLIC_URL` 必须替换为自己的公开 HTTPS 地址，并与后续 Caddy 域名及本地 CLI 的 `base_url` 一致。部署脚本要求 `.env` 已存在且非空，不会生成或覆盖它；已有配置时先备份，避免误覆盖。

将 [`deploy/Caddyfile.page-drop`](../deploy/Caddyfile.page-drop) 的站点块加入服务器已有 Caddy 配置，并将示例域名换成自己的域名。检查配置无冲突，然后按服务器实际 Caddy 配置路径验证并重新加载，例如：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
# 仅在校验通过且确认使用 systemd 管理 Caddy 时执行
sudo systemctl reload caddy
```

Caddy 负责 HTTPS 和反向代理，服务只绑定服务器回环地址，不需要将 `18090` 暴露到公网。保留已有站点配置；Caddy 配置变更前应按自己的运维流程备份。上述命令不会部署服务。

## 3. 部署和验收

在**本地项目目录**执行，将 SSH 主机替换为自己的主机名或 SSH 配置别名：

```bash
HOST=example-ssh-host ./deploy/deploy.sh
```

默认目标为服务器上的 `/srv/page-drop`。如需其他绝对路径，设置 `REMOTE`，并提前在该目录下准备 `.env`。脚本会检查 `.env`、同步源码、创建数据目录、构建并启动 Compose 服务；同步时会删除目标 `src/` 下旧的 `service/`、`deploy/`、`cli/` 和 `skills/` 后重新复制，**不要在这些目录保存服务器专属修改**。脚本不修改 Caddy，不覆盖 `.env` 或 `data/`，但构建或启动失败仍可能造成服务中断。

在服务器检查容器状态和健康接口：

```bash
docker ps --filter name=page-drop --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -i http://127.0.0.1:18090/healthz
```

健康接口应返回 HTTP 200；503 表示服务尚未就绪或必要存储不可用。也可在客户端访问 `https://你的域名/healthz`，检查 DNS、TLS 与反向代理。容器健康不代表上传一定有可用配额。部署后再用可信的临时静态目录完成一次发布和访问检查；完成后按需删除测试发布。

## 4. 配置本地 CLI

CLI 位于仓库的 `cli/publish`，无需安装 Python 第三方依赖。创建本地目录和配置文件，目录使用 `0700`、文件使用 `0600`：

```bash
mkdir -p "$HOME/.config/page-drop"
chmod 700 "$HOME/.config/page-drop"
```

创建 `~/.config/page-drop/config.json`，将域名替换为自己的服务地址：

```json
{"base_url":"https://pages.example.com"}
```

将与服务器相同的 Token 通过安全渠道保存到 `~/.config/page-drop/token`，然后设置权限：

```bash
chmod 600 "$HOME/.config/page-drop/config.json" "$HOME/.config/page-drop/token"
```

如果文件已存在，先检查用途，不要直接覆盖。也可以使用 `PAGE_DROP_TOKEN` 环境变量；凭据的完整优先级和旧配置兼容规则见 [CLI 使用参考](../skills/page-drop/references/usage.md#配置与凭据)。线上地址必须为 HTTPS；未配置地址时联网命令会失败，但本地 `--dry-run` 不需要地址或 Token。

在项目目录预检并发布自己的静态目录（须含根级 `index.html`）：

```bash
python3 cli/publish ./dist --ttl 7d --dry-run --json
# 确认内容可以公开、TTL 合适且预检结果可接受后执行
python3 cli/publish ./dist --ttl 7d --json
```

`./dist` 是示例，替换为实际目录。注意 stderr 中关于站点根路径资源的警告：页面位于 UUID 子路径，绝对资源路径可能加载失败。正式发布成功后，以 CLI 返回的链接和到期时间为准，并检查页面及关键资源。查询、删除与上传结果不确定时的恢复命令见 [CLI 使用参考](../skills/page-drop/references/usage.md#参数与命令)；`retry` 可能重发原归档，`discard-pending` 不会删除服务端发布。

如希望直接使用 `publish` 命令，可按 [安装参考](../skills/page-drop/references/usage.md#cli-缺失或不可执行) 将 CLI 链接到自己的 PATH；不要覆盖已有同名命令。

## 5. 安装 AI 技能（可选）

技能包在 [`skills/page-drop/`](../skills/page-drop/)，包含 `SKILL.md` 和引用资料。它编排 CLI，不会替代服务部署、CLI 安装或 Token 配置。安装前请阅读技能内容，并根据所用 AI 工具的技能发现规则安装整个目录，不要只复制 `SKILL.md`。

以 Pi 的默认用户级技能目录为例，在本地项目根目录执行：

```bash
mkdir -p "$HOME/.pi/agent/skills"
# 确认目标不存在；若已存在，先检查内容，不要覆盖
test ! -e "$HOME/.pi/agent/skills/page-drop" && test ! -L "$HOME/.pi/agent/skills/page-drop" && cp -R skills/page-drop "$HOME/.pi/agent/skills/page-drop"
```

Pi 自定义 agent 目录时，应使用该目录下的 `skills/`。在已有 Pi 会话中执行 `/reload`，或启动新会话；检查启动诊断和 `/skill:page-drop` 是否可用。若存在同名技能，先确认其来源和优先级，避免发现冲突。其他 AI 工具的安装路径和重载方式以该工具的文档为准。

## 6. 运行与维护

以下命令在**服务器**上执行，针对默认 Compose 项目名和容器名：

```bash
docker ps -a --filter name=page-drop
docker logs --tail 100 page-drop
curl -i http://127.0.0.1:18090/healthz
```

不要将包含链接或其他敏感信息的日志公开。常见情况：

| 现象 | 先检查 |
| --- | --- |
| 502 或 HTTPS 不通 | 容器和 `/healthz`、Caddy 反向代理、DNS、证书与防火墙。 |
| 401 | 本地 Token 来源是否与服务器一致；不要输出 Token。 |
| 503 | 服务日志、存储可用性和健康状态；数据库不可信时保留现场，不手工删除数据。 |
| 507 | 内容配额及服务器磁盘余量；不要为释放空间盲目删除数据目录。 |
| 上传结果不确定 | 保留本地 pending，先阅读 [恢复说明](../README.md#retry-与本地收尾)；不要直接重复发布或丢弃。 |

### 更新与备份

更新前阅读版本变更并确认兼容性，在本地取得需要部署的代码，然后使用同一 `HOST` 和 `REMOTE` 再次执行 `deploy/deploy.sh`。脚本会重建并启动容器，可能造成短暂中断；不自动备份或回滚。先保留可恢复的旧版本和一致性备份，更新后检查健康状态及已有链接。出现故障时保留日志和数据，先确认兼容性，再决定恢复；不要直接把仅有的旧数据库或旧内容目录覆盖到运行中服务。

数据目录默认是服务器的 `/srv/page-drop/data`，其中包含 SQLite 数据库、WAL 和发布文件；`.env` 另含敏感 Token。备份应由管理员在服务停止、没有写入时取得整个数据目录的一致副本，单独安全保存配置，并先在隔离环境验证恢复。停止服务会使链接不可访问；恢复前先停止目标服务、确认备份和目标路径，恢复完整一致的目录并核对 UID/GID 10001 的访问权限，再启动和验收。项目没有自动备份或恢复工具。不要并行启动两个实例，也不要用旧 `edge-publish` 数据目录直接启动 PageDrop。

## 7. 开发与测试

在本地项目根目录创建隔离环境，安装服务与测试依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r service/requirements.txt -r service/requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

部署测试使用本地假 SSH；部分 Compose 配置测试需要 Docker Compose CLI。没有 Docker 时，相关测试会跳过，不能将跳过视为部署验收。可只运行相关目录，例如 `.venv/bin/python -m pytest -q tests/cli tests/skills`。自动化测试不能替代目标服务器的 DNS、TLS、容器和真实页面验收。

项目边界、API、数据恢复和验收矩阵以 [README](../README.md) 为准；当前项目没有独立维护的 API SDK 或生产环境托管服务。
