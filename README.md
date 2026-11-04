# PageDrop

PageDrop 是一个自托管的静态页面限时分享工具。使用本地 CLI 上传静态目录，服务返回公开链接；有效期从上传成功时开始计算，到期后新请求返回 404。

## 功能

- **限时分享**：每次发布都是独立快照，支持 10 分钟至 30 天的有效期。
- **自托管**：Python 服务通过 Docker Compose 运行，使用宿主机 Caddy 提供 HTTPS 和反向代理。
- **本地 CLI**：使用 `publish` 预检、发布、查询和删除；上传结果不确定时可按规则恢复。
- **AI 技能**：可选的 [PageDrop 技能包](skills/page-drop/SKILL.md) 自带 CLI，可完整复制目录独立安装；未指定有效期时，技能默认使用 3 天。

## 安全与使用边界

本项目不提供公共服务器；使用者需要自行准备服务器、域名和 Token。管理 API 使用 Token，**发布页面本身不鉴权**：知道链接的人可以访问、转发或保存内容。不要发布需要鉴权的材料，也不要托管不可信第三方 HTML/JavaScript。

删除或到期只阻止后续请求，无法撤回已下载的副本。项目不提供用户体系、动态应用托管或 SPA 路由回落；修改页面需要重新发布并获得新链接。

## 快速开始

1. 准备 Linux 服务器、Docker Compose、Caddy、域名和时间同步。
2. 配置服务端 Token 与 HTTPS 地址，部署服务并验证健康状态。
3. 配置本地 CLI，预检静态目录后发布；按需安装 AI 技能。

**[查看完整部署与使用指南](docs/guide.md)**，包含操作步骤、技能安装、备份和故障排查。自托管前请阅读指南中的权限及安全说明。

技能包自带仅依赖 Python 3 标准库的 CLI，无需安装全局命令；完整复制 `skills/page-drop/` 即可在仓库外使用，服务、地址和凭据仍需另行配置。仓库中的 `cli/publish` 是兼容入口，转发到技能包内的唯一实现。

CLI 需要显式指定有效期；以下 `3d` 仅为示例，`./dist` 应替换为含根级 `index.html` 的静态目录：

```bash
python3 cli/publish ./dist --ttl 3d --dry-run --json
# 确认内容可以公开且预检结果可接受后执行
python3 cli/publish ./dist --ttl 3d --json
```

## 文档

- [部署与使用指南](docs/guide.md)：部署、CLI、技能安装、运维和开发测试。
- [技术契约](docs/specification.md)：架构、HTTP API、数据模型、配额、恢复规则及验收矩阵。
- [CLI 与技能使用参考](skills/page-drop/references/usage.md)：命令、凭据规则和恢复场景。

## 开发与测试

项目根目录下创建 Python 虚拟环境并安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r service/requirements.txt -r service/requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

部分部署配置测试需要 Docker Compose CLI；测试不能替代实际部署环境验收，详见[开发与测试](docs/guide.md#7-开发与测试)。

## 许可证

[MIT License](LICENSE)。
