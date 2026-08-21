# Linux Docker Compose 部署

基础设施和应用分离部署：`docker-compose.infra.yml` 管数据库和向量库，`docker-compose.yml` 管应用和反代。两者通过共享的 `enterprise_backend` 网络通信。

## 1. 前置条件

- Linux 主机已安装 Docker Engine 和 Compose V2：`docker compose version`
- 已准备数据库凭据和模型 API Key
- 已备份并确认要迁移的 MySQL、Neo4j、Milvus 数据
- DNS 已指向服务器公网 IP
- 防火墙/安全组放行 80 和 443 入站

## 2. 配置

```bash
cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

必须填的：

- `MYSQL_ROOT_PASSWORD`
- `NEO4J_PASSWORD`
- `REDIS_PASSWORD`
- `DEEPSEEK_API_KEY`
- `ZHIPU_API_KEY`
- `SILICONFLOW_API_KEY`

MinIO 用默认值即可（不对外暴露端口）。

## 3. 启动

```bash
# 先启动基础设施（等所有变 healthy）
docker compose -f docker-compose.infra.yml up -d
docker compose -f docker-compose.infra.yml ps

# 再启动应用
docker compose up -d --build
docker compose ps

# 验证
curl https://www.kubezzp.com/healthz
```

## 4. 日常操作

```bash
# 重建应用（不影响数据库）
docker compose up -d --build

# 查看应用日志
docker compose logs -f --tail=200 app

# 查看基础设施日志
docker compose -f docker-compose.infra.yml logs -f --tail=200 mysql

# 重启应用
docker compose restart app

# 停止应用
docker compose down

# 停止基础设施
docker compose -f docker-compose.infra.yml down
```

不要使用 `docker compose down -v`，会删除数据卷。

## 5. 数据迁移

```bash
# 导出 MySQL
mysqldump -h 旧主机 -u root -p rag_service > rag_service.sql

# 导入到容器
docker compose -f docker-compose.infra.yml exec -T mysql mysql -uroot -p rag_service < rag_service.sql

# 核验
docker compose -f docker-compose.infra.yml exec mysql mysql -uroot -p -e "USE rag_service; SHOW TABLES; SELECT COUNT(*) FROM employee;"
```

## 6. 启用飞书

`.env` 里：

```env
FEISHU_ENABLED=true
FEISHU_APP_ID=你的飞书应用ID
FEISHU_APP_SECRET=你的飞书应用密钥
```

重启应用：

```bash
docker compose restart app
docker compose logs --tail=30 app | grep -i feishu
```

飞书消息走 WebSocket 长连接（应用主动连出去），不需要公网回调地址。只有三方审批回调才需要公网 HTTPS。

## 7. 安全

- 依赖端口（3306/6379/7474/19530/9000/9001）不对外发布，只在 Docker 内网
- 只有 Caddy 的 80/443 对外
- `.env` 权限设为 600
- 不要把密码、API Key 写进 Dockerfile、Caddyfile 或 git
- `certs/` 目录已在 `.dockerignore` 和 `.gitignore` 中排除
