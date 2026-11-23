# PolyCopy Web 部署说明（Coolify）

## 架构

```
服务器 host
  /root/polycopy/data/paper_f705.db   ← 纸盘引擎（python -m polycopy run）写入
  /root/polycopy/configs/paper_f705.toml

Coolify / Docker
  polycopy-web 容器  :8040
    挂载 /root/polycopy → /app/project
    只读查询 SQLite；可写 configs（保存后引擎热加载）
```

Traefik（Coolify 自带）负责 `polycopy.bquant.com` 的 HTTPS 与转发。
纸盘引擎仍运行在 host 上；Web 容器只是读取同一个数据库。

## 一、Coolify 部署步骤（Docker Compose）

1. 把本项目推到 Git 仓库（或使用 Coolify 支持的部署源）。
2. **先停掉/删除旧的 Dockerfile 部署**（如果已经建过），避免占用
   `polycopy.bquant.com` 域名。然后 Coolify → **New Resource** →
   **Docker Compose**，选择本仓库的 `docker-compose.yml`。
3. 服务与域名：
   - 服务名：`polycopy-web`
   - 容器端口：**8040**（Compose 里已 `expose: 8040`）
   - 域名：`polycopy.bquant.com`，分配给 `polycopy-web` 服务
   - Coolify 会自动申请 Let's Encrypt 证书（DNS 已指向本机）
4. **持久化挂载**：`docker-compose.yml` 已包含
   `${POLY_HOST_DIR:-/root/polycopy}:/app/project`。如果项目在宿主机
   其他路径，在 Coolify 环境变量里设置 `POLY_HOST_DIR=<实际路径>`。
5. 环境变量（Coolify 面板设置，Compose 会用默认值兜底）：
   ```
   WEB_USERNAME=admin
   WEB_PASSWORD=<建议设置强密码；不设置则使用内置默认 poly@4321>
   WEB_PORT=8040
   WEB_SESSION_TTL_S=604800
   POLY_PROJECT_DIR=/app/project
   POLY_DB_PATH=/app/project/data/paper_f705.db
   POLY_CONFIG_PATH=/app/project/configs/paper_f705.toml
   POLY_HOST_DIR=/root/polycopy
   ```
   也可以复制 `.env.example` 为 `.env.docker` 后填写（`.env.docker` 已加入
   `.gitignore`，不要提交密码）。
6. 健康检查路径：`/api/health`（无需登录）。
7. 部署后访问 `https://polycopy.bquant.com`，使用 `admin / poly@4321`
   或你在 Coolify 设置的账号密码登录。

> Coolify 的 Compose 部署不会读取源码仓库之外的 `ports` 发布；本文件使用
> `expose: 8040` 让 Traefik 直接连容器。如果页面 502，检查域名是否绑定到
> `polycopy-web`、服务是否暴露 8040。

## 二、手工 Docker 运行（不使用 Coolify UI 时）

```bash
cd /root/polycopy
docker build -t polycopy-web .
docker run -d --name polycopy-web --restart unless-stopped \
  -p 127.0.0.1:8040:8040 \
  -v /root/polycopy:/app/project \
  -e WEB_USERNAME=admin -e WEB_PASSWORD='<设置登录密码>' \
  -e POLY_PROJECT_DIR=/app/project \
  polycopy-web
```

Traefik/Coolify 需要按平台方式添加路由；如果手工部署到 host 端口，
可以让 Coolify Traefik 指向 `host.docker.internal:8040` 或直接使用
Coolify 的应用管理。

## 三、页面

| 路径 | 内容 |
|---|---|
| `/` | 总权益、累计盈亏、今日已实现、当前持仓、最近交易、跳过原因、近 14 天盈亏 |
| `/trades` | 成交记录，按方向/钱包/日期筛选，分页 |
| `/positions` | 未平仓 / 已平仓 / 全部，最近结算 |
| `/signals` | 目标钱包的全部信号与跳过原因 |
| `/config` | 在线修改跟单参数（登录后），引擎自动热加载 |

## 四、配置文件热加载

Web 的“策略配置”保存后会写入 `configs/paper_f705.toml`（自动备份
`.bak`）。host 上的引擎在每次轮询（默认 20 秒）检查文件 mtime，
变化后自动重新加载，不需要重启进程。

## 五、验证

```bash
# 容器健康
curl -s https://polycopy.bquant.com/api/health
# 预期：{"status":"ok","bot_online":true,...}

# 登录
curl -i -c /tmp/c.txt -d 'username=admin&password=poly%404321&next=/' \
  https://polycopy.bquant.com/login
```

## 六、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 页面能开但数据全为 0 | 挂载路径不对，容器内 `/app/project/data/paper_f705.db` 不存在 |
| 引擎显示离线 | host 上的 `python3 -m polycopy run` 没运行，或心跳超过 90 秒 |
| 登录失败 | 默认账号 `admin / poly@4321`；如设置过 `WEB_PASSWORD` 则以环境变量为准 |
| 保存配置失败（Permission denied） | 容器用户对挂载目录无写权限；以 root 运行容器或调整目录权限 |
| 502 Bad Gateway | Coolify 路由端口不是 8040，或容器未启动 |
| 修改配置后未生效 | 等待一个轮询周期；或检查引擎日志是否有 config reload 报错 |

## 七、安全建议

- 代码内置了默认密码 `admin / poly@4321` 用于开箱即用。仓库是公开的，
  生产环境务必在 Coolify 环境变量中设置 `WEB_USERNAME` / `WEB_PASSWORD` 覆盖。
- 全站强制 HTTPS（Coolify 自动跳转）。
- Web 容器只读数据库，唯一写操作是配置文件。
- 不要在没有 TLS 的裸端口上长期暴露后台。
