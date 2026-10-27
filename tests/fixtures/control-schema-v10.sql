-- 冻结的 control schema v10 DDL；升级测试不得从当前 MIGRATIONS 自动生成。
PRAGMA foreign_keys = ON;
BEGIN;
CREATE TABLE auth_failures (
            source TEXT NOT NULL,
            token_digest TEXT NOT NULL,
            attempted_at INTEGER NOT NULL
        );
CREATE TABLE broker_leases (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            token_digest TEXT NOT NULL,
            acquired_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (workspace_id, run_id)
                REFERENCES runs(workspace_id, id) ON DELETE RESTRICT
        );
CREATE TABLE broker_tokens (
            token_digest TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER, snapshot_id TEXT, route_json TEXT, encrypted_secrets BLOB,
            FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, id) ON DELETE RESTRICT
        );
CREATE TABLE conversations (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
            id TEXT NOT NULL,
            title TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, deleted_at INTEGER,
            PRIMARY KEY (workspace_id, id)
        );
CREATE TABLE invites (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            token_digest TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            redeemed_at INTEGER,
            revoked_at INTEGER,
            created_at INTEGER NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
        );
CREATE TABLE logins (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            token_digest TEXT NOT NULL UNIQUE,
            csrf_digest TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        );
CREATE TABLE managed_revisions (
            version INTEGER PRIMARY KEY,
            public_json TEXT NOT NULL,
            encrypted_secrets BLOB NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at INTEGER NOT NULL
        );
CREATE TABLE request_leases (
            lease_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
CREATE TABLE runs (
            workspace_id TEXT NOT NULL,
            id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            client_request_id TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'reserved', 'running', 'awaiting_permission',
                'done', 'failed', 'interrupted', 'cancelled')),
            config_version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            finished_at INTEGER,
            capacity_released_at INTEGER,
            release_count INTEGER NOT NULL DEFAULT 0, snapshot_json TEXT, dispatch_state TEXT NOT NULL DEFAULT 'pending', dispatch_nonce TEXT, worker_container_id TEXT, dispatched_at INTEGER, prepare_owner TEXT, prepare_expires_at INTEGER,
            PRIMARY KEY (workspace_id, id),
            UNIQUE (workspace_id, client_request_id),
            FOREIGN KEY (workspace_id, conversation_id)
                REFERENCES conversations(workspace_id, id) ON DELETE RESTRICT
        );
CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL
        );
CREATE TABLE users (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
            created_at INTEGER NOT NULL,
            disabled_at INTEGER
        );
CREATE TABLE worker_leases (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE RESTRICT,
            container_id TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'absent', 'starting', 'ready', 'draining', 'stopping',
                'stopped', 'failed')),
            heartbeat_at INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 0,
            lease_expires_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            last_activity_at INTEGER NOT NULL DEFAULT 0,
            active_requests INTEGER NOT NULL DEFAULT 0,
            upstream TEXT NOT NULL DEFAULT '',
            service_token TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE workspace_overrides (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
            channel TEXT NOT NULL CHECK (channel IN ('speech', 'image', 'video', 'music')),
            route_kind TEXT NOT NULL CHECK (route_kind IN ('complete', 'managed-model')),
            public_json TEXT NOT NULL,
            encrypted_secrets BLOB,
            version INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, channel)
        );
CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
            state TEXT NOT NULL CHECK (state IN ('provisioning', 'ready', 'stopped', 'failed')),
            config_version INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
CREATE INDEX auth_failures_lookup_idx
            ON auth_failures (source, token_digest, attempted_at)
        ;
CREATE INDEX broker_leases_expiry_idx ON broker_leases (expires_at)
        ;
CREATE INDEX broker_leases_workspace_idx
            ON broker_leases (workspace_id, expires_at)
        ;
CREATE INDEX broker_tokens_run_idx ON broker_tokens (workspace_id, run_id, channel)
        ;
CREATE UNIQUE INDEX broker_tokens_snapshot_idx
            ON broker_tokens (snapshot_id)
        ;
CREATE INDEX invites_user_idx ON invites (user_id, created_at)
        ;
CREATE INDEX logins_user_idx ON logins (user_id, revoked_at)
        ;
CREATE INDEX request_leases_expiry_idx ON request_leases (expires_at);
CREATE INDEX request_leases_workspace_idx
            ON request_leases (workspace_id, expires_at)
        ;
CREATE INDEX runs_active_idx ON runs (status, workspace_id)
        ;
CREATE INDEX runs_conversation_idx ON runs (workspace_id, conversation_id, created_at)
        ;
CREATE INDEX worker_leases_state_idx
            ON worker_leases (state, lease_expires_at)
        ;
INSERT INTO schema_migrations (version, applied_at) VALUES (1, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (2, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (3, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (4, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (5, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (6, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (7, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (8, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (9, 900);
INSERT INTO schema_migrations (version, applied_at) VALUES (10, 900);
PRAGMA user_version = 10;
COMMIT;
