-- collation 统一：8 张 OA 时代老表（utf8mb4_0900_ai_ci）→ 主体 utf8mb4_unicode_ci
-- 用途：消除 JOIN 的 collation 冲突（1267）；数据无损，每张表秒级完成
-- 在 MySQL 客户端连 rag_service 库后整段粘贴执行即可
ALTER TABLE biz_expense            CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE biz_metric_daily       CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE oa_approval_flow       CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE oa_approval_record     CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE oa_leave_application   CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE sys_dept               CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE sys_employee           CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE sys_employee_role      CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
