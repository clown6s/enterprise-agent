# MySQL initialization placeholder

Place reviewed, idempotent schema/seed SQL files here before first deployment.

The application requires the `rag_service` database and tables such as `sys_user`,
`employee`, `query_schema`, approval tables, audit/history tables, and any other
schema used by the selected features. This repository does not assume that an
empty MySQL volume contains those tables.

MySQL runs files in this directory only when its data directory is initialized
for the first time. Existing volumes are not migrated or overwritten by this
placeholder. Back up and verify the target database before applying migrations.
