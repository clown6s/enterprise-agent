# Linux Docker Compose 部署

本部署文件将 `enterprise-agent`、MySQL、Neo4j、Redis 和 Milvus（etcd + MinIO + standalone）放入同一个 Docker Compose 网络。应用通过 Compose 服务名访问依赖，不使用宿主机 `127.0.0.1`。

> Redis 当前由基础设施编排，但 Python 应用尚未实际使用 Redis 客户端。不要把 Redis 当作业务事实源。

## 1. 前置条件

- Linux 主机已安装 Docker Engine 和 Compose V2：`docker compose version`
- 已准备模型、嵌入和数据库凭据；不要使用 Compose 文件中的默认密码（本文件没有默认密码回退）。
- 已备份并确认要迁移的 MySQL、Neo4j、Milvus 数据。
- MySQL schema/seed 已准备好。`deploy/mysql/init/` 只有占位说明；空 MySQL volume 不会自动获得应用全部业务表。

## 2. 配置

```bash
cp .env.example .env
chmod 600 .env
${EDITOR:-vi} .env
```

必须替换所有 `replace-me` 和 `replace-with-a-strong-password`。至少需要：

- `MYSQL_ROOT_PASSWORD`
- `NEO4J_PASSWORD`
- `REDIS_PASSWORD`
- `MINIO_ROOT_USER`
- `MINIO_ROOT_PASSWORD`
- `DASHSCOPE_API_KEY`
- `ZHIPU_API_KEY`
- `SILICONFLOW_API_KEY`

`.env` 不应提交到 Git。生产环境建议使用 Docker secrets 或受限的 secret manager，而不是普通文本文件。

## 3. 配置 schema

将经过审核的、幂等的 SQL 放入 `deploy/mysql/init/`，或对已存在的 `rag_service` 数据库单独执行迁移。初始化脚本只在 MySQL 数据目录第一次创建时执行；已有 volume 不会自动重跑。

部署前核验至少包括：

```sql
SHOW DATABASES LIKE 'rag_service';
USE rag_service;
SHOW TABLES;
SELECT COUNT(*) FROM employee;
SELECT COUNT(*) FROM query_schema;
```

审批、审计、历史和业务表请按当前项目迁移流程核验，不要为了重建 schema 删除生产 volume。

## 4. 检查 Compose

```bash
docker compose --env-file .env config
```

确认没有真实密码被输出到共享日志或 CI artifact。生产环境不建议把所有依赖端口发布到公网；当前 Compose 默认只将应用绑定到 `127.0.0.1:8080`，数据库端口在 Compose 网络内可用。

## 5. 启动

```bash
docker compose --env-file .env up -d --build
```

查看状态：

```bash
docker compose --env-file .env ps
```

Compose 会等待 MySQL、Neo4j、Milvus 和 Redis 健康后启动应用；首次启动 Milvus 可能需要较长时间。`depends_on` 不是对业务 schema 的验证，仍需执行下一节检查。

## 6. 健康检查

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:9091/healthz
curl -fsS http://127.0.0.1:7474 >/dev/null

docker compose exec mysql mysqladmin ping -h 127.0.0.1 -uroot -p
# 密码按提示输入，不要把密码写进 shell 历史

docker compose exec redis redis-cli ping
```

应用 `/healthz` 当前只表示 FastAPI 进程可响应，不代表 MySQL、Neo4j、Milvus 的业务数据和 schema 正确。生产验收应另外验证：

- MySQL 认证、`rag_service` 表和种子数据；
- Neo4j Bolt 连接与图谱数据；
- Milvus `documents_v2` 集合、维度和加载状态；
- 模型/API key 实际可用；
- 应用实际问答/数据查询链路。

## 7. 日志与运维

```bash
docker compose logs -f --tail=200 app
docker compose logs -f --tail=200 mysql neo4j milvus

docker compose restart app
docker compose pull
```

应用容器日志写到 `app_logs` volume，业务数据写到 `app_data` volume。不要在日志中打印密码、Authorization header、Cookie、完整 webhook 或 API key。

## 8. 停止与备份

先停止应用，再停止依赖：

```bash
docker compose stop app
# 备份完成后再停止全部服务
docker compose down
```

**不要使用 `docker compose down -v`**，它会删除 Compose 管理的数据卷。Milvus 数据、MySQL 数据、Neo4j 数据和 Redis AOF 都应在独立备份策略下管理。

## 9. 迁移现有四组 Compose

用户已有的 MySQL/Neo4j/Redis/Milvus Compose 是分开的，且各有独立网络。本项目 Compose 是一套新的编排，不会自动复用旧 Compose 的容器或 volume。迁移前：

1. 记录旧 Compose project name、volume、镜像版本和环境变量；
2. 备份 MySQL、Neo4j、Milvus 数据；
3. 停止旧应用/看门狗，避免两个应用同时监听或写同一业务库；
4. 决定是迁移数据到本 Compose 的命名卷，还是继续使用外部依赖；
5. 不复制 Windows 路径（例如 `C:/Users/...`）到 Linux Compose；
6. 先在测试主机执行 `config`、启动和真实 SQL/问答验收，再切换生产。

如果继续使用已有外部服务而不合并 Compose，需把 `.env` 中的 `MYSQL_HOST`、`NEO4J_HOST`、`MILVUS_HOST` 改为 Linux 主机可达的 DNS/IP，并将应用加入对应网络或开放受限端口；不要填容器内的 `127.0.0.1`。

## 10. 安全清单

- 依赖端口（3306、6379、7474、7687、9000、9001、9091、19530）默认不对公网发布；如需管理访问，使用内网/VPN/SSH 隧道。
- 轮换曾出现在本地环境文件、终端历史或共享文档中的凭据。
- 将 `.env` 权限设为 `600`，限制 Docker socket 和宿主机数据目录权限。
- 不要把 `NEO4J_PASSWORD`、MySQL root 密码、Redis 密码或模型 key 写进 Dockerfile、Compose、Git、issue 或日志。
- 应用当前不使用 Redis；如果以后引入 Redis 缓存，仍不能用它替代 MySQL 业务事实源。
