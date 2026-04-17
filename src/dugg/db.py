"""SQLite + FTS5 database layer for Dugg."""

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union


def dugg_email_address(api_key: str, server_url: str) -> str:
    """Build a Dugg email forwarding address from an API key and server URL.

    Format: {api_key}@{hostname-with-double-dashes}.dugg.fyi
    """
    from urllib.parse import urlparse
    hostname = urlparse(server_url).hostname or ""
    if not hostname:
        return ""
    slug = hostname.replace(".", "--")
    return f"{api_key}@{slug}.dugg.fyi"


def _read_dugg_env() -> dict:
    """Read .dugg-env from working dir ancestors or the install directory."""
    result = {}
    try:
        check = Path.cwd()
    except (OSError, PermissionError):
        check = None
    if check:
        for _ in range(10):
            env_file = check / ".dugg-env"
            try:
                if env_file.exists():
                    for line in env_file.read_text().splitlines():
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            result[k.strip()] = v.strip()
                    return result
            except (OSError, PermissionError):
                pass
            parent = check.parent
            if parent == check:
                break
            check = parent
    # Fall back to the dugg-fyi install directory
    install_env = Path(__file__).resolve().parent.parent.parent / ".dugg-env"
    try:
        if install_env.exists():
            for line in install_env.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    result[k.strip()] = v.strip()
    except (OSError, PermissionError):
        pass
    return result

_DUGG_ENV = _read_dugg_env()
DEFAULT_DB_PATH = Path(_DUGG_ENV["DUGG_DB_PATH"]) if "DUGG_DB_PATH" in _DUGG_ENV else Path.home() / ".dugg" / "dugg.db"
DEFAULT_API_KEY = _DUGG_ENV.get("DUGG_API_KEY", "")

# --- Configuration Constants ---
MAX_INVITE_DEPTH = 15
GRACE_PERIOD_DAYS = 14
EGRESS_TIMEOUT_DAYS = 60
PUBLISH_TTL_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


class DuggDB:
    """SQLite database with FTS5 full-text search."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_by TEXT NOT NULL REFERENCES users(id),
                visibility TEXT DEFAULT 'private' CHECK(visibility IN ('private', 'shared')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collection_members (
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                role TEXT DEFAULT 'member' CHECK(role IN ('owner', 'member')),
                invited_by TEXT REFERENCES users(id),
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'banned', 'appealing')),
                joined_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00Z',
                ip_address TEXT DEFAULT NULL,
                grace_expires_at TEXT DEFAULT NULL,
                last_seen_at TEXT DEFAULT NULL,
                PRIMARY KEY (collection_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS dugg_instances (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                topic TEXT DEFAULT '',
                access_mode TEXT DEFAULT 'invite' CHECK(access_mode IN ('invite')),
                auto_invite_endpoint TEXT DEFAULT NULL,
                endpoint_url TEXT DEFAULT '',
                rate_limit_initial INTEGER DEFAULT 5,
                rate_limit_growth INTEGER DEFAULT 2,
                read_horizon_base_days INTEGER DEFAULT 30,
                read_horizon_growth INTEGER DEFAULT 7,
                index_mode TEXT DEFAULT 'summary' CHECK(index_mode IN ('summary', 'full', 'metadata_only')),
                local_storage_cap_mb INTEGER DEFAULT 512,
                onboarding_mode TEXT DEFAULT 'graduated' CHECK(onboarding_mode IN ('graduated', 'full_access')),
                pruning_mode TEXT DEFAULT 'interaction' CHECK(pruning_mode IN ('none', 'interaction')),
                pruning_grace_days INTEGER DEFAULT 14,
                owner_id TEXT NOT NULL REFERENCES users(id),
                successor_id TEXT REFERENCES users(id) DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS instance_subscribers (
                instance_id TEXT NOT NULL REFERENCES dugg_instances(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                subscribed_at TEXT NOT NULL,
                PRIMARY KEY (instance_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS resources (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT DEFAULT '',
                description TEXT DEFAULT '',
                thumbnail TEXT DEFAULT '',
                source_type TEXT DEFAULT 'unknown',
                author TEXT DEFAULT '',
                transcript TEXT DEFAULT '',
                raw_metadata TEXT DEFAULT '{}',
                note TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                content_bytes INTEGER DEFAULT 0,
                content_evicted INTEGER DEFAULT 0,
                source_server TEXT DEFAULT '',
                submitted_by TEXT NOT NULL REFERENCES users(id),
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                enriched_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tags (
                id TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                source TEXT DEFAULT 'human' CHECK(source IN ('human', 'agent')),
                created_at TEXT NOT NULL,
                UNIQUE(resource_id, label)
            );

            CREATE TABLE IF NOT EXISTS share_rules (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                target_user_id TEXT NOT NULL REFERENCES users(id),
                include_tags TEXT DEFAULT '[]',
                exclude_tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                UNIQUE(collection_id, target_user_id)
            );

            CREATE TABLE IF NOT EXISTS resource_notes (
                id TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                source_server TEXT DEFAULT '',
                source_instance_id TEXT DEFAULT '',
                submitter_user_id TEXT DEFAULT '',
                submitter_name TEXT DEFAULT '',
                note TEXT NOT NULL,
                added_at TEXT NOT NULL,
                UNIQUE(resource_id, source_server, submitter_user_id, note)
            );

            CREATE TABLE IF NOT EXISTS resource_edges (
                id TEXT PRIMARY KEY,
                resource_a TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                resource_b TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                relationship_type TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_by TEXT REFERENCES users(id),
                created_at TEXT NOT NULL,
                UNIQUE(resource_a, resource_b, relationship_type)
            );

            CREATE TABLE IF NOT EXISTS publish_targets (
                id TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                target TEXT NOT NULL,
                published_at TEXT NOT NULL,
                UNIQUE(resource_id, target)
            );

            CREATE TABLE IF NOT EXISTS reactions (
                id TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                reaction_type TEXT DEFAULT 'tap' CHECK(reaction_type IN ('tap', 'star', 'thumbsup')),
                created_at TEXT NOT NULL,
                UNIQUE(resource_id, user_id, reaction_type)
            );

            CREATE TABLE IF NOT EXISTS publish_queue (
                id TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                target_instance_id TEXT NOT NULL REFERENCES dugg_instances(id),
                target_name TEXT NOT NULL,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'delivering', 'delivered', 'failed', 'cancelled')),
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 5,
                next_retry_at TEXT NOT NULL,
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invite_tokens (
                id TEXT PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id),
                redeemed_by TEXT REFERENCES users(id),
                name_hint TEXT DEFAULT '',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                redeemed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS event_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK(event_type IN ('resource_added', 'resource_published', 'resource_deleted', 'member_joined', 'member_banned', 'publish_delivered', 'invite_created', 'invite_redeemed', 'reaction_added')),
                instance_id TEXT REFERENCES dugg_instances(id),
                collection_id TEXT REFERENCES collections(id),
                actor_id TEXT REFERENCES users(id),
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                id TEXT PRIMARY KEY,
                instance_id TEXT REFERENCES dugg_instances(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                callback_url TEXT NOT NULL,
                event_types TEXT DEFAULT '[]',
                secret TEXT DEFAULT '',
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'failed')),
                failure_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, callback_url)
            );

            CREATE TABLE IF NOT EXISTS user_cursors (
                user_id TEXT NOT NULL REFERENCES users(id),
                cursor_type TEXT NOT NULL DEFAULT 'events',
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, cursor_type)
            );

            CREATE TABLE IF NOT EXISTS user_agents (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, agent_id)
            );

            CREATE TABLE IF NOT EXISTS server_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rss_subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                collection_id TEXT NOT NULL REFERENCES collections(id),
                feed_url TEXT NOT NULL,
                feed_title TEXT DEFAULT '',
                tag_label TEXT DEFAULT 'rss',
                poll_interval_seconds INTEGER DEFAULT 3600,
                last_polled_at TEXT DEFAULT '',
                etag TEXT DEFAULT '',
                last_modified TEXT DEFAULT '',
                seen_entry_ids TEXT DEFAULT '[]',
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, collection_id, feed_url)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(
                title, description, author, transcript, note, summary,
                content='resources',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS resources_ai AFTER INSERT ON resources BEGIN
                INSERT INTO resources_fts(rowid, title, description, author, transcript, note, summary)
                VALUES (new.rowid, new.title, new.description, new.author, new.transcript, new.note, new.summary);
            END;

            CREATE TRIGGER IF NOT EXISTS resources_ad AFTER DELETE ON resources BEGIN
                INSERT INTO resources_fts(resources_fts, rowid, title, description, author, transcript, note, summary)
                VALUES ('delete', old.rowid, old.title, old.description, old.author, old.transcript, old.note, old.summary);
            END;

            CREATE TRIGGER IF NOT EXISTS resources_au AFTER UPDATE ON resources BEGIN
                INSERT INTO resources_fts(resources_fts, rowid, title, description, author, transcript, note, summary)
                VALUES ('delete', old.rowid, old.title, old.description, old.author, old.transcript, old.note, old.summary);
                INSERT INTO resources_fts(rowid, title, description, author, transcript, note, summary)
                VALUES (new.rowid, new.title, new.description, new.author, new.transcript, new.note, new.summary);
            END;

            -- Sibling-note FTS index: separate virtual table so quarantined
            -- note text is searchable without being pulled into the primary
            -- `resources_fts` row. The publish payload builder reads neither
            -- index, so outbound federation is unaffected.
            CREATE VIRTUAL TABLE IF NOT EXISTS resource_notes_fts USING fts5(
                note,
                content='resource_notes',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS resource_notes_ai AFTER INSERT ON resource_notes BEGIN
                INSERT INTO resource_notes_fts(rowid, note) VALUES (new.rowid, new.note);
            END;

            CREATE TRIGGER IF NOT EXISTS resource_notes_ad AFTER DELETE ON resource_notes BEGIN
                INSERT INTO resource_notes_fts(resource_notes_fts, rowid, note)
                VALUES ('delete', old.rowid, old.note);
            END;

            CREATE TRIGGER IF NOT EXISTS resource_notes_au AFTER UPDATE ON resource_notes BEGIN
                INSERT INTO resource_notes_fts(resource_notes_fts, rowid, note)
                VALUES ('delete', old.rowid, old.note);
                INSERT INTO resource_notes_fts(rowid, note) VALUES (new.rowid, new.note);
            END;
        """)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Run idempotent schema migrations for new columns."""
        inst_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(dugg_instances)").fetchall()}
        if "read_horizon_base_days" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN read_horizon_base_days INTEGER DEFAULT 30")
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN read_horizon_growth INTEGER DEFAULT 7")
        if "index_mode" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN index_mode TEXT DEFAULT 'summary'")
        if "local_storage_cap_mb" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN local_storage_cap_mb INTEGER DEFAULT 512")
        if "onboarding_mode" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN onboarding_mode TEXT DEFAULT 'graduated'")
        if "successor_id" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN successor_id TEXT REFERENCES users(id) DEFAULT NULL")
        if "auto_invite_endpoint" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN auto_invite_endpoint TEXT DEFAULT NULL")
        if "pruning_mode" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN pruning_mode TEXT DEFAULT 'interaction'")
        if "pruning_grace_days" not in inst_cols:
            self.conn.execute("ALTER TABLE dugg_instances ADD COLUMN pruning_grace_days INTEGER DEFAULT 14")

        res_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(resources)").fetchall()}
        if "summary" not in res_cols:
            self.conn.execute("ALTER TABLE resources ADD COLUMN summary TEXT DEFAULT ''")
        if "content_bytes" not in res_cols:
            self.conn.execute("ALTER TABLE resources ADD COLUMN content_bytes INTEGER DEFAULT 0")
        if "content_evicted" not in res_cols:
            self.conn.execute("ALTER TABLE resources ADD COLUMN content_evicted INTEGER DEFAULT 0")
        if "source_server" not in res_cols:
            self.conn.execute("ALTER TABLE resources ADD COLUMN source_server TEXT DEFAULT ''")

        inv_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(invite_tokens)").fetchall()}
        if "onboarded_at" not in inv_cols:
            self.conn.execute("ALTER TABLE invite_tokens ADD COLUMN onboarded_at TEXT DEFAULT NULL")

        cm_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(collection_members)").fetchall()}
        if "ip_address" not in cm_cols:
            self.conn.execute("ALTER TABLE collection_members ADD COLUMN ip_address TEXT DEFAULT NULL")
        if "grace_expires_at" not in cm_cols:
            self.conn.execute("ALTER TABLE collection_members ADD COLUMN grace_expires_at TEXT DEFAULT NULL")
        if "last_seen_at" not in cm_cols:
            self.conn.execute("ALTER TABLE collection_members ADD COLUMN last_seen_at TEXT DEFAULT NULL")

        # Rebuild FTS5 triggers if summary column was added (triggers reference summary now)
        # This is safe — DROP TRIGGER IF EXISTS is idempotent
        if "summary" not in res_cols:
            self.conn.executescript("""
                DROP TRIGGER IF EXISTS resources_ai;
                DROP TRIGGER IF EXISTS resources_ad;
                DROP TRIGGER IF EXISTS resources_au;

                CREATE TRIGGER resources_ai AFTER INSERT ON resources BEGIN
                    INSERT INTO resources_fts(rowid, title, description, author, transcript, note, summary)
                    VALUES (new.rowid, new.title, new.description, new.author, new.transcript, new.note, new.summary);
                END;

                CREATE TRIGGER resources_ad AFTER DELETE ON resources BEGIN
                    INSERT INTO resources_fts(resources_fts, rowid, title, description, author, transcript, note, summary)
                    VALUES ('delete', old.rowid, old.title, old.description, old.author, old.transcript, old.note, old.summary);
                END;

                CREATE TRIGGER resources_au AFTER UPDATE ON resources BEGIN
                    INSERT INTO resources_fts(resources_fts, rowid, title, description, author, transcript, note, summary)
                    VALUES ('delete', old.rowid, old.title, old.description, old.author, old.transcript, old.note, old.summary);
                    INSERT INTO resources_fts(rowid, title, description, author, transcript, note, summary)
                    VALUES (new.rowid, new.title, new.description, new.author, new.transcript, new.note, new.summary);
                END;
            """)

        # Migrate webhook_subscriptions: make instance_id nullable, update unique constraint
        wh_cols = {row[1]: row[2] for row in self.conn.execute("PRAGMA table_info(webhook_subscriptions)").fetchall()}
        if wh_cols.get("instance_id") and self.conn.execute(
            "SELECT COUNT(*) FROM webhook_subscriptions"
        ).fetchone()[0] == 0:
            # Safe to recreate since no data exists
            needs_recreate = False
            for row in self.conn.execute("PRAGMA table_info(webhook_subscriptions)").fetchall():
                if row[1] == "instance_id" and row[3] == 1:  # notnull=1
                    needs_recreate = True
                    break
            if needs_recreate:
                self.conn.executescript("""
                    DROP TABLE IF EXISTS webhook_subscriptions;
                    CREATE TABLE webhook_subscriptions (
                        id TEXT PRIMARY KEY,
                        instance_id TEXT REFERENCES dugg_instances(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL REFERENCES users(id),
                        callback_url TEXT NOT NULL,
                        event_types TEXT DEFAULT '[]',
                        secret TEXT DEFAULT '',
                        status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'failed')),
                        failure_count INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(user_id, callback_url)
                    );
                """)

        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- Server Config ---

    def set_config(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO server_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, value, value),
        )
        self.conn.commit()

    def get_config(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM server_config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    # --- Shared Default Collection (hosted-server mode) ---

    def get_shared_default_id(self) -> Optional[str]:
        val = self.get_config("shared_default_collection_id", "")
        if not val:
            return None
        exists = self.conn.execute("SELECT 1 FROM collections WHERE id = ?", (val,)).fetchone()
        return val if exists else None

    def enable_shared_default(self, collection_id: str) -> None:
        """Mark a collection as the server's shared default. New users auto-join it,
        and ensure_default_collection will return this ID instead of creating a private one."""
        exists = self.conn.execute("SELECT 1 FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not exists:
            raise ValueError(f"Collection {collection_id} not found")
        self.set_config("shared_default_collection_id", collection_id)

    def ensure_default_collection(self, user_id: str) -> str:
        """Return the default collection id for this user.

        If the server has a shared default (hosted-instance mode), auto-joins the user
        and returns that id. Otherwise returns (creating if needed) the user's private
        'Default' collection."""
        shared_id = self.get_shared_default_id()
        if shared_id:
            self.add_collection_member(shared_id, user_id, role="member")
            return shared_id
        for c in self.list_collections(user_id):
            if c["name"] == "Default":
                return c["id"]
        return self.create_collection(
            "Default", user_id, description="Default collection", visibility="private"
        )["id"]

    # --- Users ---

    def create_user(self, name: str) -> dict:
        import re
        name = name.strip()[:100]
        name = re.sub(r'[<>&]', '', name)
        if not name:
            name = "New User"
        user_id = _uuid()
        api_key = f"dugg_{uuid.uuid4().hex}"
        now = _now()
        self.conn.execute(
            "INSERT INTO users (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, api_key, now),
        )
        self.conn.commit()
        return {"id": user_id, "name": name, "api_key": api_key, "created_at": now}

    def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE api_key = ?", (api_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def rotate_api_key(self, user_id: str) -> str:
        """Issue a fresh API key for user_id, invalidating the old one.
        Memberships, webhooks, invites, and resources are unaffected (all keyed by user_id)."""
        user = self.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")
        new_key = f"dugg_{uuid.uuid4().hex}"
        self.conn.execute("UPDATE users SET api_key = ? WHERE id = ?", (new_key, user_id))
        self.conn.commit()
        return new_key

    # --- RSS Subscriptions ---

    def add_rss_subscription(
        self,
        user_id: str,
        collection_id: str,
        feed_url: str,
        *,
        tag_label: str = "rss",
        poll_interval_seconds: int = 3600,
        feed_title: str = "",
    ) -> dict:
        """Register a feed for periodic polling. Returns the subscription row."""
        sub_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO rss_subscriptions
                 (id, user_id, collection_id, feed_url, feed_title, tag_label,
                  poll_interval_seconds, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sub_id, user_id, collection_id, feed_url, feed_title, tag_label,
             poll_interval_seconds, now),
        )
        self.conn.commit()
        return self.get_rss_subscription(sub_id)

    def get_rss_subscription(self, sub_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM rss_subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_rss_subscriptions(self, user_id: Optional[str] = None) -> list[dict]:
        if user_id:
            rows = self.conn.execute(
                "SELECT * FROM rss_subscriptions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM rss_subscriptions ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_due_rss_subscriptions(self) -> list[dict]:
        """Return enabled subscriptions whose last_polled_at + interval has passed."""
        rows = self.conn.execute(
            "SELECT * FROM rss_subscriptions WHERE enabled = 1"
        ).fetchall()
        now = datetime.now(timezone.utc)
        due: list[dict] = []
        for r in rows:
            last = r["last_polled_at"] or ""
            if not last:
                due.append(dict(r))
                continue
            try:
                parsed = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(dict(r))
                continue
            age = (now - parsed).total_seconds()
            if age >= (r["poll_interval_seconds"] or 3600):
                due.append(dict(r))
        return due

    def update_rss_subscription_state(
        self,
        sub_id: str,
        *,
        etag: str = "",
        last_modified: str = "",
        seen_entry_ids: str = "",
        feed_title: str = "",
    ) -> None:
        self.conn.execute(
            """UPDATE rss_subscriptions
               SET etag = ?, last_modified = ?, seen_entry_ids = ?,
                   feed_title = COALESCE(NULLIF(?, ''), feed_title),
                   last_polled_at = ?
               WHERE id = ?""",
            (etag, last_modified, seen_entry_ids or "[]", feed_title, _now(), sub_id),
        )
        self.conn.commit()

    def remove_rss_subscription(self, sub_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM rss_subscriptions WHERE id = ?", (sub_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_rss_subscription_enabled(self, sub_id: str, enabled: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE rss_subscriptions SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, sub_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # --- User Agents ---

    def create_agent_for_user(self, user_id: str, agent_name: Optional[str] = None) -> dict:
        """Create an agent account linked to a parent user. The agent is a separate user
        with its own API key, tracked in user_agents so bans cascade."""
        name = agent_name or f"{self.get_user(user_id)['name']}'s agent"
        agent = self.create_user(name)
        now = _now()
        self.conn.execute(
            "INSERT INTO user_agents (id, user_id, agent_id, created_at) VALUES (?, ?, ?, ?)",
            (_uuid(), user_id, agent["id"], now),
        )
        self.conn.commit()
        return agent

    def get_agents_for_user(self, user_id: str) -> list[dict]:
        """Get all agent accounts linked to a parent user."""
        rows = self.conn.execute(
            """SELECT u.* FROM users u
               JOIN user_agents ua ON u.id = ua.agent_id
               WHERE ua.user_id = ?
               ORDER BY u.created_at""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_parent_user(self, agent_id: str) -> Optional[dict]:
        """If this user is an agent, return its parent user. Otherwise None."""
        row = self.conn.execute(
            "SELECT user_id FROM user_agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if not row:
            return None
        return self.get_user(row["user_id"])

    def get_user_pair_ids(self, user_id: str) -> list[str]:
        """Return the human+agent ID set for a user. Works whether called by human or agent."""
        ids = [user_id]
        parent = self.get_parent_user(user_id)
        if parent:
            ids.append(parent["id"])
            for agent in self.get_agents_for_user(parent["id"]):
                if agent["id"] != user_id:
                    ids.append(agent["id"])
        else:
            for agent in self.get_agents_for_user(user_id):
                ids.append(agent["id"])
        return ids

    # --- Collections ---

    def create_collection(self, name: str, user_id: str, description: str = "", visibility: str = "private") -> dict:
        coll_id = _uuid()
        now = _now()
        self.conn.execute(
            "INSERT INTO collections (id, name, description, created_by, visibility, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (coll_id, name, description, user_id, visibility, now, now),
        )
        self.conn.execute(
            "INSERT INTO collection_members (collection_id, user_id, role, status, joined_at) VALUES (?, ?, 'owner', 'active', ?)",
            (coll_id, user_id, now),
        )
        self.conn.commit()
        return {"id": coll_id, "name": name, "description": description, "visibility": visibility, "created_by": user_id, "created_at": now}

    def list_collections(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT c.* FROM collections c
               JOIN collection_members cm ON c.id = cm.collection_id
               WHERE cm.user_id = ?
               ORDER BY c.updated_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_members(self, collection_id: str) -> list[dict]:
        """List all members of a collection with user names."""
        rows = self.conn.execute(
            """SELECT cm.*, u.name FROM collection_members cm
               JOIN users u ON cm.user_id = u.id
               WHERE cm.collection_id = ?
               ORDER BY cm.role DESC, cm.joined_at""",
            (collection_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_collection_member(self, collection_id: str, user_id: str, role: str = "member",
                              invited_by: Optional[str] = None, ip_address: Optional[str] = None):
        now = _now()
        grace = (datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO collection_members (collection_id, user_id, role, invited_by, status, joined_at, ip_address, grace_expires_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (collection_id, user_id, role, invited_by, now, ip_address, grace),
        )
        self.conn.commit()

    # --- Resources ---

    def add_resource(
        self,
        url: str,
        collection_id: str,
        submitted_by: str,
        note: str = "",
        title: str = "",
        description: str = "",
        thumbnail: str = "",
        source_type: str = "unknown",
        author: str = "",
        transcript: str = "",
        raw_metadata: Optional[dict] = None,
        tags: Optional[list[str]] = None,
        tag_source: str = "human",
        summary: str = "",
    ) -> dict:
        """Add a resource to a collection, or attach a sibling note on collision.

        Two paths:

        * **New URL:** inserts a ``resources`` row, tags, emits a
          ``resource_added`` event, returns ``{"status": <unset>, ...}``
          with full resource fields.
        * **URL already present in this collection** (regardless of
          submitter): attaches the note as a sibling record on the existing
          resource, unions the incoming tags, skips event emission and
          enrichment, returns the existing resource dict with
          ``status="sibling_note_added"``. Callers that drive post-add
          enrichment (author tagging, index policy application) should check
          this status and skip their work -- the existing resource has
          already been enriched.

        Collision semantics exist to preserve every submitter's note. A
        second user's highlight via the Chrome extension, a paste of the
        same URL, or a re-save with additional context all end up attached
        to the original resource as sibling notes, visible in the feed and
        findable in search.
        """
        now = _now()

        # Collision path: same URL already exists in this collection.
        # Attach the new submitter's note as a sibling note (quarantined from
        # outbound publish) and union their tags onto the existing resource.
        existing = self.conn.execute(
            "SELECT id, submitted_by, title FROM resources WHERE url = ? AND collection_id = ?",
            (url, collection_id),
        ).fetchone()
        if existing:
            submitter_row = self.conn.execute(
                "SELECT name FROM users WHERE id = ?", (submitted_by,)
            ).fetchone()
            submitter_name = submitter_row["name"] if submitter_row else ""
            self.add_resource_note(
                existing["id"], note,
                submitter_user_id=submitted_by,
                submitter_name=submitter_name,
            )
            if tags:
                for tag in tags:
                    self._add_tag(existing["id"], tag, tag_source, now)
                self.conn.commit()
            full = self.get_resource(existing["id"]) or {}
            full["status"] = "sibling_note_added"
            return full

        res_id = _uuid()
        meta_json = json.dumps(raw_metadata or {})
        content_bytes = len((transcript or "").encode("utf-8")) + len((description or "").encode("utf-8")) + len((summary or "").encode("utf-8"))
        self.conn.execute(
            """INSERT INTO resources
               (id, url, title, description, thumbnail, source_type, author, transcript, raw_metadata, note, summary, content_bytes, submitted_by, collection_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (res_id, url, title, description, thumbnail, source_type, author, transcript, meta_json, note, summary, content_bytes, submitted_by, collection_id, now, now),
        )
        if tags:
            for tag in tags:
                self._add_tag(res_id, tag, tag_source, now)
        self.conn.commit()
        result = {
            "id": res_id, "url": url, "title": title, "description": description,
            "source_type": source_type, "author": author, "note": note, "submitted_by": submitted_by,
            "collection_id": collection_id, "tags": tags or [], "created_at": now,
        }
        self.emit_event("resource_added", actor_id=submitted_by, collection_id=collection_id,
                        payload={"resource_id": res_id, "url": url, "title": title, "note": note, "source_type": source_type, "submitted_by": submitted_by})
        return result

    def update_resource(self, resource_id: str, **fields) -> Optional[dict]:
        allowed = {"title", "description", "thumbnail", "source_type", "author", "transcript", "raw_metadata", "note", "enriched_at", "summary", "content_evicted"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_resource(resource_id)
        updates["updated_at"] = _now()
        if "raw_metadata" in updates and isinstance(updates["raw_metadata"], dict):
            updates["raw_metadata"] = json.dumps(updates["raw_metadata"])
        # Recompute content_bytes if text fields changed
        text_fields = {"transcript", "description", "summary"}
        if text_fields.intersection(updates.keys()):
            current = self.get_resource(resource_id) or {}
            transcript = updates.get("transcript", current.get("transcript", "")) or ""
            description = updates.get("description", current.get("description", "")) or ""
            summary = updates.get("summary", current.get("summary", "")) or ""
            updates["content_bytes"] = len(transcript.encode("utf-8")) + len(description.encode("utf-8")) + len(summary.encode("utf-8"))
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [resource_id]
        self.conn.execute(f"UPDATE resources SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["tags"] = self._get_tags(resource_id)
        return result

    def list_resources(self, collection_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM resources WHERE collection_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (collection_id, limit, offset),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = self._get_tags(d["id"])
            results.append(d)
        return results

    def search(self, query: str, user_id: str, collection_id: Optional[str] = None, tags: Optional[list[str]] = None, submitted_by: Optional[Union[str, list[str]]] = None, limit: int = 20) -> list[dict]:
        """Full-text search across resources the user has access to.

        Matches are drawn from two FTS indexes:

        * ``resources_fts`` — the primary index over title / description /
          author / transcript / note / summary on the ``resources`` row.
        * ``resource_notes_fts`` — quarantined sibling notes attached to a
          resource (second submitter's note, cross-server enrichment). These
          live in a separate table so they never federate outbound, but they
          ARE searchable locally so nothing gets buried.

        Sibling-match filter: when a sibling note was contributed by a local
        user who was subsequently banned from the collection (directly or via
        cascade), that note drops out of search results at query time via a
        JOIN against ``collection_members``. No stored-state mutation is
        required on ban — the filter is applied at read time.
        """
        accessible = self._accessible_collection_ids(user_id)
        if not accessible:
            return []
        if collection_id:
            if collection_id not in accessible:
                return []
            accessible = [collection_id]

        placeholders = ",".join("?" for _ in accessible)
        horizon_filters = self._build_horizon_filters(user_id, accessible)

        # Build horizon WHERE clause
        horizon_sql, horizon_params = self._horizon_where_clause(user_id, accessible, horizon_filters)

        submitter_sql = ""
        submitter_params: list = []
        if submitted_by:
            if isinstance(submitted_by, list):
                ph = ",".join("?" for _ in submitted_by)
                submitter_sql = f" AND r.submitted_by IN ({ph})"
                submitter_params = submitted_by
            else:
                submitter_sql = " AND r.submitted_by = ?"
                submitter_params = [submitted_by]

        if query.strip():
            fts_query = query.replace('"', '""')
            # UNION ALL both FTS indexes, keep the best rank per resource, then
            # rejoin `resources` for the full row. Sibling matches are filtered
            # so banned local submitters' notes stop surfacing immediately.
            sql = f"""
                WITH all_matches AS (
                    SELECT r.id AS id, resources_fts.rank AS rank
                    FROM resources_fts
                    JOIN resources r ON r.rowid = resources_fts.rowid
                    WHERE resources_fts MATCH ?
                    UNION ALL
                    SELECT r.id AS id, resource_notes_fts.rank AS rank
                    FROM resource_notes_fts
                    JOIN resource_notes rn ON rn.rowid = resource_notes_fts.rowid
                    JOIN resources r ON r.id = rn.resource_id
                    LEFT JOIN collection_members cm
                           ON cm.user_id = rn.submitter_user_id
                          AND cm.collection_id = r.collection_id
                    WHERE resource_notes_fts MATCH ?
                      AND (rn.submitter_user_id = '' OR cm.status = 'active')
                ),
                best_rank AS (
                    SELECT id, MIN(rank) AS rank FROM all_matches GROUP BY id
                )
                SELECT r.*, best_rank.rank
                FROM best_rank
                JOIN resources r ON r.id = best_rank.id
                WHERE r.collection_id IN ({placeholders})
                  {horizon_sql}
                  {submitter_sql}
                ORDER BY best_rank.rank
                LIMIT ?
            """
            params = [fts_query, fts_query] + accessible + horizon_params + submitter_params + [limit]
        else:
            sql = f"""
                SELECT r.*, 0 as rank
                FROM resources r
                WHERE r.collection_id IN ({placeholders})
                  {horizon_sql}
                  {submitter_sql}
                ORDER BY r.created_at DESC
                LIMIT ?
            """
            params = accessible + horizon_params + submitter_params + [limit]

        rows = self.conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = self._get_tags(d["id"])
            if tags:
                resource_tags = {t["label"] for t in d["tags"]}
                if not resource_tags.intersection(set(tags)):
                    continue
            results.append(d)
        return results

    def get_feed(self, user_id: str, limit: int = 50) -> list[dict]:
        """Get all resources across collections the user has access to, respecting share rules and read horizon."""
        accessible = self._accessible_collection_ids(user_id)
        if not accessible:
            return []

        horizon_filters = self._build_horizon_filters(user_id, accessible)
        horizon_sql, horizon_params = self._horizon_where_clause(user_id, accessible, horizon_filters)

        placeholders = ",".join("?" for _ in accessible)
        # Use 'r' alias to match horizon clause
        sql = f"SELECT r.* FROM resources r WHERE r.collection_id IN ({placeholders}){horizon_sql} ORDER BY r.created_at DESC LIMIT ?"
        params = list(accessible) + horizon_params + [limit]

        rows = self.conn.execute(sql, params).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = self._get_tags(d["id"])
            if self._passes_share_rules(d, user_id):
                results.append(d)
        return results

    # --- Tags ---

    def tag_resource(self, resource_id: str, labels: list[str], source: str = "human") -> list[dict]:
        now = _now()
        for label in labels:
            self._add_tag(resource_id, label.lower().strip(), source, now)
        self.conn.commit()
        return self._get_tags(resource_id)

    def _add_tag(self, resource_id: str, label: str, source: str, now: str):
        tag_id = _uuid()
        self.conn.execute(
            "INSERT OR IGNORE INTO tags (id, resource_id, label, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (tag_id, resource_id, label.lower().strip(), source, now),
        )

    def _get_tags(self, resource_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT label, source FROM tags WHERE resource_id = ?", (resource_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Sibling Notes (collision-safe enrichment) ---
    #
    # Design: when the same URL is submitted a second time to a collection --
    # whether by another local user ("Rocco also saved this page and added his
    # own highlight") or via cross-server federation ("Server B published this
    # URL to us with its own submitter's note") -- the incoming note must NOT
    # overwrite the primary `resources.note`, and must NOT be dropped either.
    # Instead it lands in a dedicated `resource_notes` table and renders
    # alongside the primary note in the feed, /r/{id} viewer, and search.
    #
    # Quarantine guarantee: nothing in this table ever leaves the server. The
    # outbound publish payload builder at `sync.deliver_publish` reads only
    # `get_resource()` output, which does not touch `resource_notes`. That
    # means a note authored on Server B, federated into Server A as a sibling,
    # cannot then federate from A back out to C -- the boundary is enforced
    # by the schema, not by "remember to strip on the way out" discipline.
    #
    # Search: a parallel `resource_notes_fts` index makes sibling text
    # findable locally (see `search()`). Banned authors' sibling notes are
    # filtered at query time via a JOIN to `collection_members.status`, so
    # ban events do not trigger mass rewrites of stored state.

    def add_resource_note(self, resource_id: str, note: str, *,
                          source_server: str = "",
                          source_instance_id: str = "",
                          submitter_user_id: str = "",
                          submitter_name: str = "") -> Optional[dict]:
        """Attach a sibling note to an existing resource.

        A sibling note is a second-submitter or cross-server enrichment: the
        resource already exists (matched by URL + collection), and this call
        records the new note without duplicating the resource row.

        Idempotent: the table has a UNIQUE constraint on
        ``(resource_id, source_server, submitter_user_id, note)``, so a
        retried publish delivery or a redundant add from the same submitter
        with identical text will silently no-op.

        Args:
            resource_id: ID of the existing resource to attach the note to.
            note: The note text. Empty / whitespace-only notes are ignored.
            source_server: Origin server URL if this note came from
                cross-server federation (``""`` for same-server adds).
            source_instance_id: Remote instance ID that delivered the
                note (empty for same-server adds).
            submitter_user_id: Local user ID of the submitter (empty for
                cross-server notes, since the remote user is not a local
                principal). Used by the search ban-filter.
            submitter_name: Display name to render in the feed.

        Returns:
            A dict summarizing the stored note, or ``None`` if the note
            was empty. The returned dict does not leak the deduplicated
            outcome -- callers relying on "was this a new insert" should
            consult ``list_resource_notes`` before and after.
        """
        if not note or not note.strip():
            return None
        note_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT OR IGNORE INTO resource_notes
               (id, resource_id, source_server, source_instance_id,
                submitter_user_id, submitter_name, note, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (note_id, resource_id, source_server, source_instance_id,
             submitter_user_id, submitter_name, note, now),
        )
        self.conn.commit()
        return {"id": note_id, "resource_id": resource_id, "note": note,
                "source_server": source_server, "submitter_name": submitter_name,
                "added_at": now}

    def list_resource_notes(self, resource_id: str) -> list[dict]:
        """Return all sibling notes attached to a resource, oldest first.

        Callers: feed renderer, Atom feed, ``/r/{id}`` viewer. Do NOT call
        this from any code path that constructs an outbound publish payload
        -- sibling notes are local-only enrichment and must not federate.
        """
        rows = self.conn.execute(
            """SELECT id, source_server, source_instance_id, submitter_user_id,
                      submitter_name, note, added_at
               FROM resource_notes WHERE resource_id = ? ORDER BY added_at ASC""",
            (resource_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Share Rules ---

    def set_share_rule(self, collection_id: str, target_user_id: str, include_tags: list[str] = None, exclude_tags: list[str] = None) -> dict:
        rule_id = _uuid()
        now = _now()
        inc = json.dumps(include_tags or [])
        exc = json.dumps(exclude_tags or [])
        self.conn.execute(
            """INSERT INTO share_rules (id, collection_id, target_user_id, include_tags, exclude_tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(collection_id, target_user_id) DO UPDATE SET include_tags=?, exclude_tags=?""",
            (rule_id, collection_id, target_user_id, inc, exc, now, inc, exc),
        )
        self.conn.commit()
        return {"collection_id": collection_id, "target_user_id": target_user_id, "include_tags": include_tags or [], "exclude_tags": exclude_tags or []}

    def _passes_share_rules(self, resource: dict, user_id: str) -> bool:
        """Check if a resource passes share rules for a given user."""
        if resource.get("submitted_by") == user_id:
            return True

        row = self.conn.execute(
            "SELECT include_tags, exclude_tags FROM share_rules WHERE collection_id = ? AND target_user_id = ?",
            (resource["collection_id"], user_id),
        ).fetchone()

        if not row:
            # No share rule = see everything in collections you're a member of
            return True

        include = json.loads(row["include_tags"])
        exclude = json.loads(row["exclude_tags"])
        resource_tags = {t["label"] for t in resource.get("tags", [])}

        if exclude and resource_tags.intersection(set(exclude)):
            return False
        if include and not resource_tags.intersection(set(include)):
            return False
        return True

    # --- Resource Edges ---

    def link_resources(self, resource_a: str, resource_b: str, relationship_type: str, confidence: float = 1.0, created_by: Optional[str] = None) -> dict:
        edge_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT OR REPLACE INTO resource_edges (id, resource_a, resource_b, relationship_type, confidence, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (edge_id, resource_a, resource_b, relationship_type, confidence, created_by, now),
        )
        self.conn.commit()
        return {"id": edge_id, "resource_a": resource_a, "resource_b": resource_b, "relationship_type": relationship_type, "confidence": confidence}

    def get_related(self, resource_id: str, user_id: Optional[str] = None, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM resource_edges
               WHERE resource_a = ? OR resource_b = ?
               ORDER BY confidence DESC LIMIT ?""",
            (resource_id, resource_id, limit),
        ).fetchall()
        edges = [dict(r) for r in rows]
        if not user_id:
            return edges

        # Filter out edges pointing to resources outside the user's read horizon
        filtered = []
        for e in edges:
            other_id = e["resource_b"] if e["resource_a"] == resource_id else e["resource_a"]
            other = self.get_resource(other_id)
            if not other:
                continue
            # Own resources always visible
            if other.get("submitted_by") == user_id:
                filtered.append(e)
                continue
            inst = self._get_instance_for_collection_owner(other["collection_id"])
            if not inst:
                filtered.append(e)
                continue
            cutoff = self.visible_since(user_id, inst["id"])
            if cutoff is None or other["created_at"] >= cutoff:
                filtered.append(e)
        return filtered

    # --- Publishing ---

    def publish_resource(self, resource_id: str, targets: list[str]) -> list[dict]:
        """Publish a resource to one or more named targets. Also enqueues for remote delivery."""
        now = _now()
        results = []
        for target in targets:
            pub_id = _uuid()
            self.conn.execute(
                """INSERT OR IGNORE INTO publish_targets (id, resource_id, target, published_at)
                   VALUES (?, ?, ?, ?)""",
                (pub_id, resource_id, target.lower().strip(), now),
            )
            results.append({"resource_id": resource_id, "target": target.lower().strip(), "published_at": now})
        self.conn.commit()
        # Enqueue for remote delivery and emit events
        resource = self.get_resource(resource_id)
        coll_id = resource["collection_id"] if resource else None
        for target in targets:
            self.enqueue_publish(resource_id, target.lower().strip())
            self.emit_event("resource_published", actor_id=resource["submitted_by"] if resource else None,
                            collection_id=coll_id,
                            payload={"resource_id": resource_id, "target": target.lower().strip(),
                                     "title": resource.get("title", "") if resource else ""})
        return results

    def delete_resource(self, resource_id: str, collection_id: str, requester_id: str) -> dict:
        """Permanently delete a resource. Only the collection owner can delete.

        Cascades: removes tags, reactions, publish_targets, publish_queue entries,
        resource_edges, and the resource itself.
        """
        member = self.get_member_status(collection_id, requester_id)
        if not member or member["role"] != "owner":
            return {"error": "Only the collection owner can delete resources"}
        row = self.conn.execute(
            "SELECT id, url, title, submitted_by FROM resources WHERE id = ? AND collection_id = ?",
            (resource_id, collection_id),
        ).fetchone()
        if not row:
            return {"error": "Resource not found in this collection"}
        info = dict(row)
        # CASCADE handles tags, reactions, publish_targets, resource_edges via FK
        self.conn.execute("DELETE FROM publish_queue WHERE resource_id = ?", (resource_id,))
        self.conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))
        self.conn.commit()
        self.emit_event("resource_deleted", actor_id=requester_id,
                        collection_id=collection_id,
                        payload={"resource_id": resource_id, "url": info["url"],
                                 "title": info.get("title", ""), "submitted_by": info["submitted_by"]})
        return {"deleted": resource_id, "url": info["url"], "title": info.get("title", "")}

    def purge_user_resources(self, collection_id: str, user_ids: list[str]) -> int:
        """Delete all resources submitted by given users in a collection. Returns count deleted."""
        if not user_ids:
            return 0
        placeholders = ",".join("?" for _ in user_ids)
        rows = self.conn.execute(
            f"SELECT id FROM resources WHERE collection_id = ? AND submitted_by IN ({placeholders})",
            [collection_id] + user_ids,
        ).fetchall()
        count = len(rows)
        for row in rows:
            self.conn.execute("DELETE FROM publish_queue WHERE resource_id = ?", (row["id"],))
            self.conn.execute("DELETE FROM resources WHERE id = ?", (row["id"],))
        self.conn.commit()
        return count

    def unpublish_resource(self, resource_id: str, targets: Optional[list[str]] = None):
        """Remove a resource from specific targets, or all targets if none specified."""
        if targets:
            placeholders = ",".join("?" for _ in targets)
            self.conn.execute(
                f"DELETE FROM publish_targets WHERE resource_id = ? AND target IN ({placeholders})",
                [resource_id] + [t.lower().strip() for t in targets],
            )
        else:
            self.conn.execute("DELETE FROM publish_targets WHERE resource_id = ?", (resource_id,))
        self.conn.commit()

    def get_publish_targets(self, resource_id: str) -> list[dict]:
        """Get all publish targets for a resource."""
        rows = self.conn.execute(
            "SELECT target, published_at FROM publish_targets WHERE resource_id = ?", (resource_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_published_resources(self, target: str, limit: int = 50) -> list[dict]:
        """Get all resources published to a specific target."""
        rows = self.conn.execute(
            """SELECT r.* FROM resources r
               JOIN publish_targets pt ON r.id = pt.resource_id
               WHERE pt.target = ?
               ORDER BY pt.published_at DESC LIMIT ?""",
            (target.lower().strip(), limit),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = self._get_tags(d["id"])
            results.append(d)
        return results

    # --- Reactions ---

    def react_to_resource(self, resource_id: str, user_id: str, reaction_type: str = "tap") -> dict:
        """Add a silent reaction to a resource. Idempotent — same user+type is a no-op."""
        react_id = _uuid()
        now = _now()
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO reactions (id, resource_id, user_id, reaction_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (react_id, resource_id, user_id, reaction_type, now),
        )
        self.conn.commit()
        if cursor.rowcount == 1:
            resource = self.get_resource(resource_id)
            self.emit_event("reaction_added", actor_id=user_id,
                            collection_id=resource["collection_id"] if resource else None,
                            payload={
                                "resource_id": resource_id,
                                "reaction_type": reaction_type,
                                "resource_owner_id": resource["submitted_by"] if resource else None,
                            })
        return {"resource_id": resource_id, "user_id": user_id, "reaction_type": reaction_type, "created_at": now}

    def get_reactions(self, resource_id: str, requester_id: str) -> Optional[dict]:
        """Get reaction counts for a resource. Only the resource's submitter can see aggregates."""
        resource = self.get_resource(resource_id)
        if not resource:
            return None
        if resource["submitted_by"] != requester_id:
            return None  # Only publisher sees reactions

        rows = self.conn.execute(
            "SELECT reaction_type, COUNT(*) as count FROM reactions WHERE resource_id = ? GROUP BY reaction_type",
            (resource_id,),
        ).fetchall()
        total = sum(dict(r)["count"] for r in rows)
        breakdown = {dict(r)["reaction_type"]: dict(r)["count"] for r in rows}
        return {"resource_id": resource_id, "total": total, "breakdown": breakdown}

    def get_my_reactions_summary(self, user_id: str) -> list[dict]:
        """Get reaction totals across all resources submitted by this user."""
        rows = self.conn.execute(
            """SELECT r.id, r.title, r.url, re.reaction_type, COUNT(*) as count
               FROM reactions re
               JOIN resources r ON re.resource_id = r.id
               WHERE r.submitted_by = ?
               GROUP BY r.id, re.reaction_type
               ORDER BY count DESC""",
            (user_id,),
        ).fetchall()
        # Aggregate by resource
        by_resource: dict[str, dict] = {}
        for row in rows:
            d = dict(row)
            rid = d["id"]
            if rid not in by_resource:
                by_resource[rid] = {"resource_id": rid, "title": d["title"], "url": d["url"], "total": 0, "breakdown": {}}
            by_resource[rid]["breakdown"][d["reaction_type"]] = d["count"]
            by_resource[rid]["total"] += d["count"]
        return sorted(by_resource.values(), key=lambda x: x["total"], reverse=True)

    # --- Instances ---

    def create_instance(self, name: str, owner_id: str, topic: str = "", access_mode: str = "invite",
                        rate_limit_initial: int = 5, rate_limit_growth: int = 2,
                        read_horizon_base_days: int = 30, read_horizon_growth: int = 7,
                        index_mode: str = "summary", local_storage_cap_mb: int = 512,
                        onboarding_mode: str = "graduated") -> dict:
        inst_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO dugg_instances (id, name, topic, access_mode, rate_limit_initial, rate_limit_growth,
               read_horizon_base_days, read_horizon_growth, index_mode, local_storage_cap_mb, onboarding_mode, owner_id, created_at, updated_at)
               VALUES (?, ?, ?, 'invite', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (inst_id, name, topic, rate_limit_initial, rate_limit_growth,
             read_horizon_base_days, read_horizon_growth, index_mode, local_storage_cap_mb, onboarding_mode, owner_id, now, now),
        )
        # Owner is auto-subscribed
        self.conn.execute(
            "INSERT INTO instance_subscribers (instance_id, user_id, subscribed_at) VALUES (?, ?, ?)",
            (inst_id, owner_id, now),
        )
        self.conn.commit()
        return {"id": inst_id, "name": name, "topic": topic, "access_mode": "invite",
                "rate_limit_initial": rate_limit_initial, "rate_limit_growth": rate_limit_growth,
                "read_horizon_base_days": read_horizon_base_days, "read_horizon_growth": read_horizon_growth,
                "index_mode": index_mode, "local_storage_cap_mb": local_storage_cap_mb,
                "onboarding_mode": onboarding_mode, "owner_id": owner_id, "created_at": now}

    def get_instance(self, instance_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM dugg_instances WHERE id = ?", (instance_id,)).fetchone()
        return dict(row) if row else None

    def list_instances(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT di.* FROM dugg_instances di
               JOIN instance_subscribers isub ON di.id = isub.instance_id
               WHERE isub.user_id = ?
               ORDER BY di.updated_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_instance(self, instance_id: str, owner_id: str, **fields) -> Optional[dict]:
        inst = self.get_instance(instance_id)
        if not inst or inst["owner_id"] != owner_id:
            return None
        allowed = {"name", "topic", "endpoint_url", "rate_limit_initial", "rate_limit_growth",
                   "read_horizon_base_days", "read_horizon_growth", "index_mode", "local_storage_cap_mb",
                   "onboarding_mode", "auto_invite_endpoint", "pruning_mode", "pruning_grace_days"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return inst
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [instance_id]
        self.conn.execute(f"UPDATE dugg_instances SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_instance(instance_id)

    def get_instance_policy(self, instance_id: str) -> Optional[dict]:
        """Return the full policy dict for an instance."""
        inst = self.get_instance(instance_id)
        if not inst:
            return None
        return {
            "instance_id": inst["id"],
            "instance_name": inst["name"],
            "read_horizon_base_days": inst.get("read_horizon_base_days", 30),
            "read_horizon_growth": inst.get("read_horizon_growth", 7),
            "index_mode": inst.get("index_mode", "summary"),
            "local_storage_cap_mb": inst.get("local_storage_cap_mb", 512),
            "onboarding_mode": inst.get("onboarding_mode", "graduated"),
            "rate_limit_initial": inst.get("rate_limit_initial", 5),
            "rate_limit_growth": inst.get("rate_limit_growth", 2),
            "access_mode": inst.get("access_mode", "invite"),
            "pruning_mode": inst.get("pruning_mode", "interaction"),
            "pruning_grace_days": inst.get("pruning_grace_days", 14),
        }

    def get_instance_scope(self, instance_id: str) -> dict:
        """Derive the living scope of an instance from its actual content."""
        # Get collections owned by instance owner
        inst = self.get_instance(instance_id)
        if not inst:
            return {"top_tags": [], "recent_types": [], "resource_count": 0, "recent_count": 0}

        owner_id = inst["owner_id"]
        coll_rows = self.conn.execute(
            "SELECT id FROM collections WHERE owner_id = ?", (owner_id,)
        ).fetchall()
        coll_ids = [r["id"] for r in coll_rows]
        if not coll_ids:
            return {"top_tags": [], "recent_types": [], "resource_count": 0, "recent_count": 0}

        placeholders = ",".join("?" for _ in coll_ids)

        # Total resource count
        total = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM resources WHERE collection_id IN ({placeholders})", coll_ids
        ).fetchone()["cnt"]

        # Recent count (last 7 days)
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM resources WHERE collection_id IN ({placeholders}) AND created_at > ?",
            coll_ids + [seven_days_ago]
        ).fetchone()["cnt"]

        # Top tags by frequency
        tag_rows = self.conn.execute(
            f"""SELECT t.label, COUNT(*) as cnt FROM tags t
                JOIN resources r ON t.resource_id = r.id
                WHERE r.collection_id IN ({placeholders})
                GROUP BY t.label ORDER BY cnt DESC LIMIT 10""",
            coll_ids
        ).fetchall()
        top_tags = [r["label"] for r in tag_rows]

        # Recent source types
        type_rows = self.conn.execute(
            f"""SELECT source_type, COUNT(*) as cnt FROM resources
                WHERE collection_id IN ({placeholders}) AND created_at > ?
                GROUP BY source_type ORDER BY cnt DESC""",
            coll_ids + [seven_days_ago]
        ).fetchall()
        recent_types = [r["source_type"] for r in type_rows]

        return {
            "top_tags": top_tags,
            "recent_types": recent_types,
            "resource_count": total,
            "recent_count": recent,
        }

    def apply_onboarding_preset(self, instance_id: str, owner_id: str, mode: str) -> Optional[dict]:
        """Apply an onboarding mode preset. 'full_access' sets read_horizon=-1, storage=-1, index_mode='full'."""
        if mode == "full_access":
            return self.update_instance(instance_id, owner_id,
                                        onboarding_mode="full_access",
                                        read_horizon_base_days=-1,
                                        local_storage_cap_mb=-1,
                                        index_mode="full")
        elif mode == "graduated":
            return self.update_instance(instance_id, owner_id,
                                        onboarding_mode="graduated",
                                        read_horizon_base_days=30,
                                        read_horizon_growth=7,
                                        local_storage_cap_mb=512,
                                        index_mode="summary")
        return None

    def subscribe_to_instance(self, instance_id: str, user_id: str) -> dict:
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO instance_subscribers (instance_id, user_id, subscribed_at) VALUES (?, ?, ?)",
            (instance_id, user_id, now),
        )
        self.conn.commit()
        return {"instance_id": instance_id, "user_id": user_id, "subscribed_at": now}

    # --- Succession ---

    def set_successor(self, instance_id: str, owner_id: str, successor_id: str) -> Optional[dict]:
        """Owner designates a successor for the instance. Successor must be a subscriber."""
        inst = self.get_instance(instance_id)
        if not inst or inst["owner_id"] != owner_id:
            return None
        # Verify successor is a subscriber
        row = self.conn.execute(
            "SELECT 1 FROM instance_subscribers WHERE instance_id = ? AND user_id = ?",
            (instance_id, successor_id),
        ).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE dugg_instances SET successor_id = ?, updated_at = ? WHERE id = ?",
            (successor_id, _now(), instance_id),
        )
        self.conn.commit()
        return {"instance_id": instance_id, "successor_id": successor_id}

    def trigger_succession(self, instance_id: str) -> Optional[dict]:
        """Transfer ownership to the designated successor.

        Re-roots all collections owned by the current owner under the successor.
        Transfers the instance ownership.
        """
        inst = self.get_instance(instance_id)
        if not inst or not inst.get("successor_id"):
            return None
        old_owner = inst["owner_id"]
        new_owner = inst["successor_id"]
        now = _now()

        # Transfer instance ownership
        self.conn.execute(
            "UPDATE dugg_instances SET owner_id = ?, successor_id = NULL, updated_at = ? WHERE id = ?",
            (new_owner, now, instance_id),
        )

        # Re-root collection members: where old owner was invited_by, update to new owner
        self.conn.execute(
            """UPDATE collection_members SET invited_by = ?
               WHERE invited_by = ? AND collection_id IN (
                   SELECT id FROM collections WHERE created_by = ?
               )""",
            (new_owner, old_owner, old_owner),
        )

        # Transfer collection ownership roles
        self.conn.execute(
            """UPDATE collection_members SET role = 'member'
               WHERE user_id = ? AND role = 'owner' AND collection_id IN (
                   SELECT id FROM collections WHERE created_by = ?
               )""",
            (old_owner, old_owner),
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO collection_members (collection_id, user_id, role, status, joined_at)
               SELECT id, ?, 'owner', 'active', ? FROM collections WHERE created_by = ?""",
            (new_owner, now, old_owner),
        )

        # Transfer collection created_by
        self.conn.execute(
            "UPDATE collections SET created_by = ? WHERE created_by = ?",
            (new_owner, old_owner),
        )

        self.conn.commit()
        return {"instance_id": instance_id, "old_owner": old_owner, "new_owner": new_owner}

    # --- Invite Tree ---

    def invite_member(self, collection_id: str, inviter_id: str, invitee_id: str,
                      ip_address: Optional[str] = None) -> dict:
        """Add a member via invitation, tracking who invited them."""
        now = _now()
        grace = (datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)).isoformat()
        self.conn.execute(
            """INSERT OR IGNORE INTO collection_members (collection_id, user_id, role, invited_by, status, joined_at, ip_address, grace_expires_at)
               VALUES (?, ?, 'member', ?, 'active', ?, ?, ?)""",
            (collection_id, invitee_id, inviter_id, now, ip_address, grace),
        )
        # Auto-add the invitee's agents to the same collection
        for agent in self.get_agents_for_user(invitee_id):
            self.conn.execute(
                """INSERT OR IGNORE INTO collection_members (collection_id, user_id, role, invited_by, status, joined_at, ip_address, grace_expires_at)
                   VALUES (?, ?, 'member', ?, 'active', ?, ?, ?)""",
                (collection_id, agent["id"], inviter_id, now, ip_address, grace),
            )
        self.conn.commit()
        self.emit_event("member_joined", actor_id=inviter_id, collection_id=collection_id,
                        payload={"user_id": invitee_id, "invited_by": inviter_id})
        return {"collection_id": collection_id, "user_id": invitee_id, "invited_by": inviter_id, "joined_at": now}

    def get_invite_tree(self, collection_id: str, user_id: str) -> list[dict]:
        """Get all members in the invite subtree below a user (recursive).

        Includes depth cap (MAX_INVITE_DEPTH) and cycle detection via path tracking.
        """
        rows = self.conn.execute(
            """WITH RECURSIVE tree AS (
                SELECT user_id, invited_by, role, status, joined_at, 1 as depth,
                       ',' || user_id || ',' as path
                FROM collection_members
                WHERE collection_id = ? AND invited_by = ?
                UNION ALL
                SELECT cm.user_id, cm.invited_by, cm.role, cm.status, cm.joined_at, t.depth + 1,
                       t.path || cm.user_id || ','
                FROM collection_members cm
                JOIN tree t ON cm.invited_by = t.user_id
                WHERE cm.collection_id = ?
                  AND t.depth < ?
                  AND t.path NOT LIKE '%,' || cm.user_id || ',%'
            )
            SELECT user_id, invited_by, role, status, joined_at, depth FROM tree""",
            (collection_id, user_id, collection_id, MAX_INVITE_DEPTH),
        ).fetchall()
        return [dict(r) for r in rows]

    def check_ip_duplicate(self, collection_id: str, ip_address: str) -> list[dict]:
        """Check if an IP address is already used by an existing member in a collection."""
        if not ip_address:
            return []
        rows = self.conn.execute(
            """SELECT cm.user_id, u.name FROM collection_members cm
               JOIN users u ON cm.user_id = u.id
               WHERE cm.collection_id = ? AND cm.ip_address = ? AND cm.status = 'active'""",
            (collection_id, ip_address),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_member_status(self, collection_id: str, user_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM collection_members WHERE collection_id = ? AND user_id = ?",
            (collection_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    # --- Ban Cascade ---

    def get_member_credit_score(self, collection_id: str, user_id: str) -> dict:
        """Calculate credit score: submissions * distinct_human_reactors.

        Only counts reactions from human users (excludes agent accounts).
        Score = submissions * distinct_humans_who_reacted. A member with
        submissions but no human engagement scores 0.
        """
        submissions = self.conn.execute(
            "SELECT COUNT(*) as count FROM resources WHERE collection_id = ? AND submitted_by = ?",
            (collection_id, user_id),
        ).fetchone()["count"]

        distinct_human_reactors = self.conn.execute(
            """SELECT COUNT(DISTINCT re.user_id) as count FROM reactions re
               JOIN resources r ON re.resource_id = r.id
               LEFT JOIN user_agents ua ON re.user_id = ua.agent_id
               WHERE r.collection_id = ? AND r.submitted_by = ?
                 AND ua.agent_id IS NULL""",
            (collection_id, user_id),
        ).fetchone()["count"]

        total = submissions * distinct_human_reactors

        return {"user_id": user_id, "submissions": submissions,
                "distinct_human_reactors": distinct_human_reactors, "total": total}

    def ban_member(self, collection_id: str, user_id: str, cascade: bool = True,
                   credit_threshold: int = 5, purge: bool = False) -> dict:
        """Ban a user. With cascade, prunes invite tree: depth 1 = hard ban, depth 2+ = credit score decides.

        Owner cannot be banned. Members in their grace period (first 14 days)
        survive cascade bans regardless of credit score.

        If purge=True, all resources submitted by banned users are permanently deleted.
        """
        # Owner protection: cannot ban the collection owner
        member = self.get_member_status(collection_id, user_id)
        if member and member["role"] == "owner":
            return {"error": "Cannot ban the collection owner", "banned": [], "survived": []}

        # Ban the target user
        self.conn.execute(
            "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
            (collection_id, user_id),
        )

        banned = [user_id]
        survived = []

        if cascade:
            tree = self.get_invite_tree(collection_id, user_id)
            now = datetime.now(timezone.utc)
            for member in tree:
                mid = member["user_id"]
                depth = member["depth"]

                # Check grace period — members in grace period survive
                member_status = self.get_member_status(collection_id, mid)
                in_grace = False
                if member_status and member_status.get("grace_expires_at"):
                    try:
                        grace_end = datetime.fromisoformat(member_status["grace_expires_at"].replace("Z", "+00:00"))
                        in_grace = now < grace_end
                    except (ValueError, AttributeError):
                        pass

                if depth == 1:
                    # Hard prune — directly invited by banned user (no grace period protection)
                    self.conn.execute(
                        "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
                        (collection_id, mid),
                    )
                    banned.append(mid)
                elif in_grace:
                    # Grace period — depth 2+ members in grace period survive, re-root under owner
                    owner_row = self.conn.execute(
                        "SELECT user_id FROM collection_members WHERE collection_id = ? AND role = 'owner'",
                        (collection_id,),
                    ).fetchone()
                    if owner_row:
                        self.conn.execute(
                            "UPDATE collection_members SET invited_by = ? WHERE collection_id = ? AND user_id = ?",
                            (owner_row["user_id"], collection_id, mid),
                        )
                    survived.append(mid)
                else:
                    # Depth 2+: credit score decides
                    score = self.get_member_credit_score(collection_id, mid)
                    if score["total"] >= credit_threshold:
                        # Survivor — re-root under collection owner
                        owner_row = self.conn.execute(
                            "SELECT user_id FROM collection_members WHERE collection_id = ? AND role = 'owner'",
                            (collection_id,),
                        ).fetchone()
                        if owner_row:
                            self.conn.execute(
                                "UPDATE collection_members SET invited_by = ? WHERE collection_id = ? AND user_id = ?",
                                (owner_row["user_id"], collection_id, mid),
                            )
                        survived.append(mid)
                    else:
                        self.conn.execute(
                            "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
                            (collection_id, mid),
                        )
                        banned.append(mid)

        # Also ban all agents belonging to any banned user
        agent_banned = []
        for uid in banned:
            agents = self.get_agents_for_user(uid)
            for agent in agents:
                aid = agent["id"]
                agent_member = self.get_member_status(collection_id, aid)
                if agent_member and agent_member["status"] != "banned":
                    self.conn.execute(
                        "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
                        (collection_id, aid),
                    )
                    agent_banned.append(aid)

        banned.extend(agent_banned)

        # Cancel all pending publishes from banned users
        for uid in banned:
            self.conn.execute(
                """UPDATE publish_queue SET status = 'cancelled', updated_at = ?
                   WHERE resource_id IN (SELECT id FROM resources WHERE submitted_by = ?)
                     AND status IN ('pending', 'delivering')""",
                (_now(), uid),
            )

        purged_count = 0
        if purge:
            purged_count = self.purge_user_resources(collection_id, banned)

        self.conn.commit()
        self.emit_event("member_banned", collection_id=collection_id,
                        payload={"banned": banned, "survived": survived, "cascade": cascade,
                                 "purge": purge, "purged_resources": purged_count})
        return {"banned": banned, "survived": survived, "purged_resources": purged_count}

    # --- Appeals ---

    def appeal_ban(self, collection_id: str, user_id: str) -> Optional[dict]:
        """Submit an appeal. Only banned members can appeal.

        If the caller is an agent, the appeal is filed on behalf of their
        parent human user (the agent advocates for the pair).
        """
        # If the caller is an agent, appeal on behalf of the parent human
        appealing_for = user_id
        parent = self.get_parent_user(user_id)
        if parent:
            appealing_for = parent["id"]
        member = self.get_member_status(collection_id, appealing_for)
        if not member or member["status"] != "banned":
            return None
        self.conn.execute(
            "UPDATE collection_members SET status = 'appealing' WHERE collection_id = ? AND user_id = ?",
            (collection_id, appealing_for),
        )
        self.conn.commit()
        score = self.get_member_credit_score(collection_id, appealing_for)
        return {"user_id": appealing_for, "appealed_by": user_id, "status": "appealing", **score}

    def get_appeals(self, collection_id: str) -> list[dict]:
        """List all pending appeals with credit scores."""
        rows = self.conn.execute(
            "SELECT user_id, joined_at FROM collection_members WHERE collection_id = ? AND status = 'appealing'",
            (collection_id,),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            user = self.get_user(d["user_id"])
            score = self.get_member_credit_score(collection_id, d["user_id"])
            results.append({
                "user_id": d["user_id"],
                "name": user["name"] if user else d["user_id"],
                "joined_at": d["joined_at"],
                **score,
            })
        return results

    def approve_appeal(self, collection_id: str, user_id: str) -> Optional[dict]:
        """Approve an appeal — re-root under owner, set active.

        Cascades to any agent tokens that were banned alongside the user.
        """
        member = self.get_member_status(collection_id, user_id)
        if not member or member["status"] != "appealing":
            return None
        owner_row = self.conn.execute(
            "SELECT user_id FROM collection_members WHERE collection_id = ? AND role = 'owner'",
            (collection_id,),
        ).fetchone()
        owner_id = owner_row["user_id"] if owner_row else None
        self.conn.execute(
            "UPDATE collection_members SET status = 'active', invited_by = ? WHERE collection_id = ? AND user_id = ?",
            (owner_id, collection_id, user_id),
        )
        # Cascade unban to any agent tokens that were banned alongside this user
        agents_unbanned = []
        for agent in self.get_agents_for_user(user_id):
            aid = agent["id"]
            agent_member = self.get_member_status(collection_id, aid)
            if agent_member and agent_member["status"] == "banned":
                self.conn.execute(
                    "UPDATE collection_members SET status = 'active', invited_by = ? WHERE collection_id = ? AND user_id = ?",
                    (user_id, collection_id, aid),
                )
                agents_unbanned.append(aid)
        self.conn.commit()
        return {"user_id": user_id, "status": "active", "invited_by": owner_id, "agents_unbanned": agents_unbanned}

    def deny_appeal(self, collection_id: str, user_id: str) -> Optional[dict]:
        """Deny an appeal — set back to banned."""
        member = self.get_member_status(collection_id, user_id)
        if not member or member["status"] != "appealing":
            return None
        self.conn.execute(
            "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
            (collection_id, user_id),
        )
        self.conn.commit()
        return {"user_id": user_id, "status": "banned"}

    # --- Invite Tokens ---

    def _generate_invite_token(self) -> str:
        """Generate a short, human-friendly invite token slug."""
        import secrets
        import string
        alphabet = string.ascii_lowercase + string.digits
        parts = [''.join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
        return '-'.join(parts)

    def create_invite_token(self, created_by: str, name_hint: str = "", expires_hours: int = 72) -> dict:
        """Create an invite token for onboarding a new user."""
        token_id = _uuid()
        token = self._generate_invite_token()
        now = _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()
        self.conn.execute(
            "INSERT INTO invite_tokens (id, token, created_by, name_hint, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, token, created_by, name_hint, expires_at, now),
        )
        self.conn.commit()
        self.emit_event("invite_created", actor_id=created_by,
                        payload={"token_id": token_id, "name_hint": name_hint})
        return {"id": token_id, "token": token, "created_by": created_by,
                "name_hint": name_hint, "expires_at": expires_at, "created_at": now}

    def get_invite_token(self, token: str) -> Optional[dict]:
        """Look up an invite token by its slug."""
        row = self.conn.execute("SELECT * FROM invite_tokens WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def mark_invite_onboarded(self, user_id: str):
        """Mark the invite token used by this user as onboarded (first authenticated connection)."""
        now = _now()
        # Try direct match first, then resolve parent if caller is an agent
        target_id = user_id
        parent = self.get_parent_user(user_id)
        if parent:
            target_id = parent["id"]
        self.conn.execute(
            "UPDATE invite_tokens SET onboarded_at = ? WHERE redeemed_by = ? AND onboarded_at IS NULL",
            (now, target_id),
        )
        self.conn.commit()

    def list_invite_tokens(self, created_by: Optional[str] = None) -> list[dict]:
        """List invite tokens. Optionally filter by creator."""
        if created_by:
            rows = self.conn.execute(
                "SELECT * FROM invite_tokens WHERE created_by = ? ORDER BY created_at DESC", (created_by,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM invite_tokens ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def redeem_invite_token(self, token: str, name: str) -> Optional[dict]:
        """Redeem an invite token: create a new user and mark the token as used.

        Returns None if the token is invalid, expired, or already redeemed.
        Returns dict with user info and token details on success.
        """
        invite = self.get_invite_token(token)
        if not invite:
            return None
        if invite["redeemed_by"]:
            return None
        if datetime.fromisoformat(invite["expires_at"]) < datetime.now(timezone.utc):
            return None

        user = self.create_user(name)
        agent = self.create_agent_for_user(user["id"])
        now = _now()
        self.conn.execute(
            "UPDATE invite_tokens SET redeemed_by = ?, redeemed_at = ? WHERE token = ?",
            (user["id"], now, token),
        )
        self.conn.commit()
        shared_id = self.get_shared_default_id()
        if shared_id:
            self.add_collection_member(shared_id, user["id"], role="member", invited_by=invite["created_by"])
            self.add_collection_member(shared_id, agent["id"], role="member", invited_by=invite["created_by"])
        self.emit_event("invite_redeemed", actor_id=user["id"],
                        payload={"token": token, "invited_by": invite["created_by"], "name": name})
        return {"user": user, "agent": agent, "invite": {**invite, "redeemed_by": user["id"], "redeemed_at": now}}

    def get_instance_for_owner(self, owner_id: str) -> Optional[dict]:
        """Get the first instance owned by a user (for invite page context)."""
        row = self.conn.execute(
            "SELECT * FROM dugg_instances WHERE owner_id = ? ORDER BY created_at LIMIT 1",
            (owner_id,),
        ).fetchone()
        return dict(row) if row else None

    # --- Routing Manifest ---

    def get_routing_manifest(self, user_id: str) -> list[dict]:
        """Get topic descriptors for all instances the user is subscribed to — used by agents for auto-routing."""
        rows = self.conn.execute(
            """SELECT di.id, di.name, di.topic, di.access_mode
               FROM dugg_instances di
               JOIN instance_subscribers isub ON di.id = isub.instance_id
               WHERE isub.user_id = ?
               ORDER BY di.name""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Read Horizon ---

    def visible_since(self, user_id: str, instance_id: str) -> Optional[str]:
        """Compute the earliest date a user can see content from, based on join date + tenure.

        Returns an ISO timestamp string, or None if the user can see everything.
        A read_horizon_base_days of -1 means full history (no restriction).
        """
        inst = self.get_instance(instance_id)
        if not inst:
            return None

        # Owner sees everything
        if inst["owner_id"] == user_id:
            return None

        base_days = inst.get("read_horizon_base_days", 30)
        growth = inst.get("read_horizon_growth", 7)

        # -1 means full history
        if base_days == -1:
            return None

        # Find the user's earliest membership join date across collections owned by the instance owner
        row = self.conn.execute(
            """SELECT MIN(cm.joined_at) as earliest_join
               FROM collection_members cm
               JOIN collections c ON cm.collection_id = c.id
               WHERE cm.user_id = ? AND c.created_by = ? AND cm.status = 'active'""",
            (user_id, inst["owner_id"]),
        ).fetchone()

        if not row or not row["earliest_join"]:
            # Not a member — use base_days from now
            cutoff = datetime.now(timezone.utc) - timedelta(days=base_days)
            return cutoff.isoformat()

        now = datetime.now(timezone.utc)
        try:
            joined = datetime.fromisoformat(row["earliest_join"].replace("Z", "+00:00"))
            weeks_member = max(0, (now - joined).days // 7)
        except (ValueError, AttributeError):
            weeks_member = 0

        # Total visible days = base + (weeks_member * growth)
        visible_days = base_days + (weeks_member * growth)
        cutoff = now - timedelta(days=visible_days)
        return cutoff.isoformat()

    def _get_instance_for_collection_owner(self, collection_id: str) -> Optional[dict]:
        """Get the instance owned by a collection's owner (for horizon/policy lookups)."""
        coll = self.conn.execute("SELECT created_by FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not coll:
            return None
        row = self.conn.execute(
            "SELECT * FROM dugg_instances WHERE owner_id = ? ORDER BY created_at LIMIT 1",
            (coll["created_by"],),
        ).fetchone()
        return dict(row) if row else None

    # --- Index Mode ---

    def get_index_mode(self, collection_id: str) -> str:
        """Get the index mode for a collection based on its owner's instance config."""
        inst = self._get_instance_for_collection_owner(collection_id)
        if not inst:
            return "full"  # No instance = legacy full mode
        return inst.get("index_mode", "summary")

    def apply_index_policy(self, resource_id: str, collection_id: str,
                           enriched_description: str = "", enriched_transcript: str = "",
                           agent_summary: str = "") -> None:
        """Apply the instance's index_mode policy to a resource after enrichment.

        - 'full': keep everything (current behavior)
        - 'summary': store summary, clear full transcript/description from resource (keep in FTS via summary)
        - 'metadata_only': title + URL + tags only — clear description, transcript, summary
        """
        mode = self.get_index_mode(collection_id)

        if mode == "full":
            return  # Keep everything as-is

        if mode == "summary":
            # Generate summary if agent didn't provide one
            summary = agent_summary
            if not summary:
                # Auto-generate from description + transcript
                text = enriched_description or ""
                if enriched_transcript:
                    text = enriched_transcript if not text else f"{text} {enriched_transcript}"
                if text:
                    # Take first ~500 chars as a rough 2-3 sentence summary
                    sentences = text.replace("\n", " ").split(". ")
                    summary_parts = []
                    char_count = 0
                    for s in sentences:
                        if char_count > 400:
                            break
                        summary_parts.append(s.strip())
                        char_count += len(s)
                    summary = ". ".join(summary_parts)
                    if not summary.endswith("."):
                        summary += "."

            self.update_resource(resource_id, summary=summary, transcript="", description=summary)
        elif mode == "metadata_only":
            self.update_resource(resource_id, transcript="", description="", summary="")

    # --- Storage Cap & Eviction ---

    def get_total_content_bytes(self, owner_id: str) -> int:
        """Get total content_bytes across all collections owned by a user."""
        row = self.conn.execute(
            """SELECT COALESCE(SUM(r.content_bytes), 0) as total
               FROM resources r
               JOIN collections c ON r.collection_id = c.id
               WHERE c.created_by = ?""",
            (owner_id,),
        ).fetchone()
        return row["total"]

    def run_eviction(self, instance_id: str) -> int:
        """Evict coldest content when total storage exceeds cap.

        Returns the number of resources evicted.
        User's own contributions are NEVER evicted.
        """
        inst = self.get_instance(instance_id)
        if not inst:
            return 0

        cap_mb = inst.get("local_storage_cap_mb", 512)
        if cap_mb == -1:
            return 0  # Unlimited

        cap_bytes = cap_mb * 1024 * 1024
        owner_id = inst["owner_id"]
        total = self.get_total_content_bytes(owner_id)

        if total <= cap_bytes:
            return 0

        # Find eviction candidates: oldest, non-evicted, not submitted by owner
        # Order by created_at ASC (oldest first) — coldest content
        rows = self.conn.execute(
            """SELECT r.id, r.content_bytes
               FROM resources r
               JOIN collections c ON r.collection_id = c.id
               WHERE c.created_by = ?
                 AND r.content_evicted = 0
                 AND r.submitted_by != ?
                 AND r.content_bytes > 0
               ORDER BY r.created_at ASC""",
            (owner_id, owner_id),
        ).fetchall()

        evicted_count = 0
        for row in rows:
            if total <= cap_bytes:
                break
            rid = row["id"]
            freed = row["content_bytes"]
            # Evict: clear full text, keep summary + URL + metadata
            self.conn.execute(
                """UPDATE resources SET transcript = '', description = '',
                   content_evicted = 1, content_bytes = 0, updated_at = ?
                   WHERE id = ?""",
                (_now(), rid),
            )
            total -= freed
            evicted_count += 1

        if evicted_count > 0:
            self.conn.commit()
        return evicted_count

    # --- Rate Limiting ---

    def get_rate_limit_config(self, instance_id: str) -> Optional[dict]:
        """Get rate limit settings for an instance."""
        row = self.conn.execute(
            "SELECT rate_limit_initial, rate_limit_growth FROM dugg_instances WHERE id = ?",
            (instance_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_rate_limit(self, instance_id: str, owner_id: str, initial: Optional[int] = None, growth: Optional[int] = None) -> Optional[dict]:
        """Set rate limit config for an instance. Owner only."""
        inst = self.get_instance(instance_id)
        if not inst or inst["owner_id"] != owner_id:
            return None
        updates = {}
        if initial is not None:
            updates["rate_limit_initial"] = initial
        if growth is not None:
            updates["rate_limit_growth"] = growth
        if not updates:
            return inst
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [instance_id]
        self.conn.execute(f"UPDATE dugg_instances SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_instance(instance_id)

    def check_rate_limit(self, collection_id: str, user_id: str) -> dict:
        """Check if a user can submit to a collection based on tenure-based rate limits.

        Returns dict with: allowed (bool), current (int), cap (int), days_member (int).
        Rate limit is derived from the instance that owns this collection's owner.
        If no instance is configured, submissions are unlimited.
        """
        # Get member tenure
        member = self.get_member_status(collection_id, user_id)
        if not member:
            return {"allowed": False, "current": 0, "cap": 0, "days_member": 0, "reason": "not a member"}

        # Find the instance owned by the collection owner
        coll = self.conn.execute("SELECT created_by FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not coll:
            return {"allowed": True, "current": 0, "cap": -1, "days_member": 0, "reason": "no collection"}

        owner_id = coll["created_by"]
        # Find instances owned by the collection owner
        instances = self.conn.execute(
            "SELECT id, rate_limit_initial, rate_limit_growth FROM dugg_instances WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()

        if not instances:
            # No instance = no rate limit
            return {"allowed": True, "current": 0, "cap": -1, "days_member": 0, "reason": "no instance configured"}

        # Use the first instance's rate limit config (owner's primary instance)
        inst = dict(instances[0])
        initial = inst["rate_limit_initial"]
        growth = inst["rate_limit_growth"]

        # Calculate tenure in days
        joined_at = member["joined_at"]
        now = datetime.now(timezone.utc)
        try:
            joined = datetime.fromisoformat(joined_at.replace("Z", "+00:00"))
            days_member = max(0, (now - joined).days)
        except (ValueError, AttributeError):
            days_member = 0

        # Calculate cap: initial + (days * growth)
        cap = initial + (days_member * growth)

        # Count today's submissions by this user in this collection
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        current = self.conn.execute(
            "SELECT COUNT(*) as count FROM resources WHERE collection_id = ? AND submitted_by = ? AND created_at >= ?",
            (collection_id, user_id, today_start),
        ).fetchone()["count"]

        return {
            "allowed": current < cap,
            "current": current,
            "cap": cap,
            "days_member": days_member,
            "reason": "ok" if current < cap else "rate limit exceeded",
        }

    # --- Publish Queue ---

    def enqueue_publish(self, resource_id: str, target_name: str) -> list[dict]:
        """Enqueue a publish for delivery to all instances subscribed by the resource owner that match the target.

        Finds instances with endpoint_url set, creates queue entries for each.
        Returns list of queue entries created.
        """
        now = _now()
        resource = self.get_resource(resource_id)
        if not resource:
            return []

        # Find all instances with endpoint URLs (these are remote Dugg deployments)
        rows = self.conn.execute(
            "SELECT id, endpoint_url FROM dugg_instances WHERE endpoint_url != '' AND endpoint_url IS NOT NULL"
        ).fetchall()

        entries = []
        for row in rows:
            inst = dict(row)
            queue_id = _uuid()
            self.conn.execute(
                """INSERT OR IGNORE INTO publish_queue
                   (id, resource_id, target_instance_id, target_name, status, retry_count, max_retries, next_retry_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, 5, ?, ?, ?)""",
                (queue_id, resource_id, inst["id"], target_name, now, now, now),
            )
            entries.append({
                "id": queue_id, "resource_id": resource_id,
                "target_instance_id": inst["id"], "target_name": target_name,
                "status": "pending", "created_at": now,
            })
        self.conn.commit()
        return entries

    def get_pending_publishes(self, limit: int = 50) -> list[dict]:
        """Get publish queue entries ready for delivery (pending + next_retry_at <= now)."""
        now = _now()
        rows = self.conn.execute(
            """SELECT pq.*, di.endpoint_url, di.name as instance_name
               FROM publish_queue pq
               JOIN dugg_instances di ON pq.target_instance_id = di.id
               WHERE pq.status IN ('pending', 'delivering')
                 AND pq.next_retry_at <= ?
               ORDER BY pq.created_at ASC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_publish_delivering(self, queue_id: str):
        """Mark a queue entry as currently being delivered."""
        self.conn.execute(
            "UPDATE publish_queue SET status = 'delivering', updated_at = ? WHERE id = ?",
            (_now(), queue_id),
        )
        self.conn.commit()

    def mark_publish_delivered(self, queue_id: str):
        """Mark a queue entry as successfully delivered and update last_seen_at for the target."""
        now = _now()
        self.conn.execute(
            "UPDATE publish_queue SET status = 'delivered', updated_at = ? WHERE id = ?",
            (now, queue_id),
        )
        self.conn.commit()

    def mark_publish_retry(self, queue_id: str, error: str):
        """Increment retry count and schedule next attempt with exponential backoff."""
        row = self.conn.execute("SELECT retry_count, max_retries FROM publish_queue WHERE id = ?", (queue_id,)).fetchone()
        if not row:
            return
        retry_count = row["retry_count"] + 1
        if retry_count >= row["max_retries"]:
            self.conn.execute(
                "UPDATE publish_queue SET status = 'failed', retry_count = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (retry_count, error, _now(), queue_id),
            )
        else:
            # Exponential backoff: 30s, 2m, 8m, 32m, ~2h
            delay_seconds = 30 * (4 ** retry_count)
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
            self.conn.execute(
                "UPDATE publish_queue SET status = 'pending', retry_count = ?, next_retry_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (retry_count, next_retry, error, _now(), queue_id),
            )
        self.conn.commit()

    def get_publish_queue_status(self, user_id: Optional[str] = None) -> dict:
        """Get publish queue stats. Optionally filter by user's resources."""
        if user_id:
            rows = self.conn.execute(
                """SELECT pq.status, COUNT(*) as count
                   FROM publish_queue pq
                   JOIN resources r ON pq.resource_id = r.id
                   WHERE r.submitted_by = ?
                   GROUP BY pq.status""",
                (user_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) as count FROM publish_queue GROUP BY status"
            ).fetchall()
        stats = {r["status"]: r["count"] for r in rows}
        return {"pending": stats.get("pending", 0), "delivering": stats.get("delivering", 0),
                "delivered": stats.get("delivered", 0), "failed": stats.get("failed", 0)}

    def get_failed_publishes(self, limit: int = 20) -> list[dict]:
        """Get failed publish queue entries for inspection."""
        rows = self.conn.execute(
            """SELECT pq.*, di.name as instance_name, di.endpoint_url
               FROM publish_queue pq
               JOIN dugg_instances di ON pq.target_instance_id = di.id
               WHERE pq.status = 'failed'
               ORDER BY pq.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def retry_failed_publishes(self) -> int:
        """Reset all failed publishes back to pending for another round of attempts."""
        now = _now()
        cursor = self.conn.execute(
            "UPDATE publish_queue SET status = 'pending', retry_count = 0, next_retry_at = ?, updated_at = ? WHERE status = 'failed'",
            (now, now),
        )
        self.conn.commit()
        return cursor.rowcount

    def retry_publish_selective(self, publish_id: Optional[str] = None,
                                target_instance_id: Optional[str] = None) -> int:
        """Retry specific failed publishes — by ID or by target instance."""
        now = _now()
        if publish_id:
            cursor = self.conn.execute(
                "UPDATE publish_queue SET status = 'pending', retry_count = 0, next_retry_at = ?, updated_at = ? WHERE id = ? AND status = 'failed'",
                (now, now, publish_id),
            )
        elif target_instance_id:
            cursor = self.conn.execute(
                "UPDATE publish_queue SET status = 'pending', retry_count = 0, next_retry_at = ?, updated_at = ? WHERE target_instance_id = ? AND status = 'failed'",
                (now, now, target_instance_id),
            )
        else:
            return self.retry_failed_publishes()
        self.conn.commit()
        return cursor.rowcount

    def clear_failed_publishes(self, target_instance_id: Optional[str] = None) -> int:
        """Delete failed publish queue entries. Optionally scoped to a target instance."""
        if target_instance_id:
            cursor = self.conn.execute(
                "DELETE FROM publish_queue WHERE status = 'failed' AND target_instance_id = ?",
                (target_instance_id,),
            )
        else:
            cursor = self.conn.execute("DELETE FROM publish_queue WHERE status = 'failed'")
        self.conn.commit()
        return cursor.rowcount

    def purge_old_failed_publishes(self) -> int:
        """Delete failed publish entries older than PUBLISH_TTL_DAYS."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PUBLISH_TTL_DAYS)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM publish_queue WHERE status = 'failed' AND updated_at < ?",
            (cutoff,),
        )
        self.conn.commit()
        return cursor.rowcount

    def get_pending_publishes_fifo(self, limit: int = 50) -> list[dict]:
        """Get publish queue entries ready for delivery with FIFO gate.

        Per target instance, if the oldest pending/failed item hasn't succeeded,
        hold the rest. This prevents backlog leapfrogging.
        """
        now = _now()
        # Get all pending items ordered by creation
        rows = self.conn.execute(
            """SELECT pq.*, di.endpoint_url, di.name as instance_name
               FROM publish_queue pq
               JOIN dugg_instances di ON pq.target_instance_id = di.id
               WHERE pq.status IN ('pending', 'delivering')
                 AND pq.next_retry_at <= ?
               ORDER BY pq.created_at ASC
               LIMIT ?""",
            (now, limit * 3),  # fetch extra to account for FIFO filtering
        ).fetchall()

        # FIFO gate: per-target, only allow items if oldest for that target is ready
        seen_targets: dict[str, str] = {}  # target_id -> oldest created_at
        result = []
        for r in rows:
            d = dict(r)
            tid = d["target_instance_id"]
            if tid not in seen_targets:
                # This is the oldest item for this target — it's allowed
                seen_targets[tid] = d["id"]
                result.append(d)
            elif seen_targets[tid] == d["id"]:
                result.append(d)
            # Otherwise skip — older item for this target must complete first
            # Actually, allow items after the first one IF the first one is being delivered
            # The gate only blocks if the first item is still pending/failed
            if len(result) >= limit:
                break

        return result

    # --- Egress Timeout ---

    def update_last_seen(self, collection_id: str, user_id: str):
        """Update the last_seen_at timestamp for a member."""
        self.conn.execute(
            "UPDATE collection_members SET last_seen_at = ? WHERE collection_id = ? AND user_id = ?",
            (_now(), collection_id, user_id),
        )
        self.conn.commit()

    def touch_user(self, user_id: str):
        """Update last_seen_at on all active memberships for a user.
        If user_id is an agent, also touches the parent human's memberships."""
        now = _now()
        self.conn.execute(
            "UPDATE collection_members SET last_seen_at = ? WHERE user_id = ? AND status = 'active'",
            (now, user_id),
        )
        parent = self.get_parent_user(user_id)
        if parent:
            self.conn.execute(
                "UPDATE collection_members SET last_seen_at = ? WHERE user_id = ? AND status = 'active'",
                (now, parent["id"]),
            )
        self.conn.commit()

    def get_stale_members(self, days: int = None) -> list[dict]:
        """Get members who haven't been seen in EGRESS_TIMEOUT_DAYS days.
        Skips members in collections whose instance has pruning_mode='none'.
        """
        timeout = days or EGRESS_TIMEOUT_DAYS
        cutoff = (datetime.now(timezone.utc) - timedelta(days=timeout)).isoformat()
        rows = self.conn.execute(
            """SELECT cm.collection_id, cm.user_id, cm.last_seen_at, u.name
               FROM collection_members cm
               JOIN users u ON cm.user_id = u.id
               JOIN collections c ON cm.collection_id = c.id
               LEFT JOIN dugg_instances di ON di.owner_id = c.created_by
               WHERE cm.status = 'active'
                 AND (cm.last_seen_at IS NOT NULL AND cm.last_seen_at < ?)
                 AND COALESCE(di.pruning_mode, 'interaction') != 'none'""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Inactive Member Pruning ---

    def get_inactive_members(self, collection_id: str) -> list[dict]:
        """Get members past grace period with zero submissions, zero reactions,
        and no recent activity (feed visits, agent touches).
        Returns empty list if the instance pruning_mode is 'none'.
        """
        inst = self._get_instance_for_collection_owner(collection_id)
        if inst and inst.get("pruning_mode", "interaction") == "none":
            return []
        grace_days = inst.get("pruning_grace_days", GRACE_PERIOD_DAYS) if inst else GRACE_PERIOD_DAYS
        now = datetime.now(timezone.utc).isoformat()
        grace_cutoff = now
        seen_cutoff = (datetime.now(timezone.utc) - timedelta(days=grace_days)).isoformat()
        rows = self.conn.execute(
            """SELECT cm.user_id, cm.joined_at, cm.grace_expires_at, cm.last_seen_at, u.name
               FROM collection_members cm
               JOIN users u ON cm.user_id = u.id
               WHERE cm.collection_id = ? AND cm.status = 'active' AND cm.role != 'owner'
                 AND (cm.grace_expires_at IS NOT NULL AND cm.grace_expires_at < ?)
                 AND (cm.last_seen_at IS NULL OR cm.last_seen_at < ?)""",
            (collection_id, grace_cutoff, seen_cutoff),
        ).fetchall()

        inactive = []
        for row in rows:
            d = dict(row)
            uid = d["user_id"]
            subs = self.conn.execute(
                "SELECT COUNT(*) as c FROM resources WHERE collection_id = ? AND submitted_by = ?",
                (collection_id, uid),
            ).fetchone()["c"]
            reacts = self.conn.execute(
                """SELECT COUNT(*) as c FROM reactions re
                   JOIN resources r ON re.resource_id = r.id
                   WHERE r.collection_id = ? AND re.user_id = ?""",
                (collection_id, uid),
            ).fetchone()["c"]
            if subs == 0 and reacts == 0:
                d["submissions"] = 0
                d["reactions"] = 0
                inactive.append(d)

        return inactive

    def prune_inactive_members(self, collection_id: str) -> dict:
        """Remove members past grace period with zero activity. Returns pruned list."""
        inactive = self.get_inactive_members(collection_id)
        pruned = []
        for m in inactive:
            self.conn.execute(
                "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
                (collection_id, m["user_id"]),
            )
            pruned.append(m["user_id"])
        self.conn.commit()
        return {"pruned": pruned, "count": len(pruned)}

    # --- Event Log ---

    def emit_event(self, event_type: str, actor_id: Optional[str] = None,
                   instance_id: Optional[str] = None, collection_id: Optional[str] = None,
                   payload: Optional[dict] = None) -> dict:
        """Log an event and fire matching webhooks."""
        event_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO event_log (id, event_type, instance_id, collection_id, actor_id, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event_type, instance_id, collection_id, actor_id, json.dumps(payload or {}), now),
        )
        self.conn.commit()
        event = {"id": event_id, "event_type": event_type, "actor_id": actor_id,
                "instance_id": instance_id, "collection_id": collection_id,
                "payload": payload or {}, "created_at": now}
        self._pending_webhook_threads = self._dispatch_webhooks(event) or []
        return event

    def wait_for_webhooks(self, timeout: float = 15.0):
        """Block until pending webhook threads finish. Call before process exit."""
        for t in getattr(self, "_pending_webhook_threads", []):
            t.join(timeout=timeout)

    def _dispatch_webhooks(self, event: dict, sync: bool = False):
        """Fire webhooks matching this event. Async by default; sync=True waits for completion."""
        import threading
        hooks = self.get_webhooks_for_event(
            event["event_type"],
            event.get("instance_id"),
            collection_id=event.get("collection_id"),
        )
        if not hooks:
            return

        # Reaction events are author-only: only notify the resource's submitter
        payload = event.get("payload", {})
        if event["event_type"] == "reaction_added":
            owner_id = payload.get("resource_owner_id")
            if owner_id:
                hooks = [h for h in hooks if h["user_id"] == owner_id]
            if not hooks:
                return
            # Enrich payload with resource details and aggregate counts
            resource_id = payload.get("resource_id")
            if resource_id:
                res = self.get_resource(resource_id)
                if res:
                    payload = dict(payload)
                    payload["resource_title"] = res.get("title") or res.get("url", "")
                    payload["resource_url"] = res.get("url", "")
                    # Get total reaction count for this resource (bypass permission check)
                    counts = self.conn.execute(
                        "SELECT reaction_type, COUNT(*) as count FROM reactions WHERE resource_id = ? GROUP BY reaction_type",
                        (resource_id,),
                    ).fetchall()
                    total = sum(dict(r)["count"] for r in counts)
                    breakdown = {dict(r)["reaction_type"]: dict(r)["count"] for r in counts}
                    payload["reaction_total"] = total
                    payload["reaction_breakdown"] = breakdown
                    event = dict(event)
                    event["payload"] = payload

        actor_name = ""
        if event.get("actor_id"):
            row = self.conn.execute("SELECT name FROM users WHERE id = ?", (event["actor_id"],)).fetchone()
            if row:
                actor_name = row["name"]
        server_url = self.get_config("server_url", "")
        if sync:
            for hook in hooks:
                self._fire_webhook(hook, event, actor_name, server_url)
        else:
            threads = []
            for hook in hooks:
                t = threading.Thread(target=self._fire_webhook, args=(hook, event, actor_name, server_url), daemon=True)
                t.start()
                threads.append(t)
            return threads

    def _fire_webhook(self, hook: dict, event: dict, actor_name: str, server_url: str):
        """POST to a webhook callback URL."""
        import urllib.request
        import urllib.error
        payload = event.get("payload", {})
        url = payload.get("url", "")
        title = payload.get("title", url)
        note = payload.get("note", "")
        event_type = event["event_type"]

        source = payload.get("source_server", "")

        # Slack-formatted payload
        if "hooks.slack.com" in hook["callback_url"]:
            # For reaction_added events, look up the resource to get title/url
            if event_type == "reaction_added":
                reaction_type = payload.get("reaction_type", "tap")
                emoji = {"tap": "point_right", "star": "star", "thumbsup": "thumbsup"}.get(reaction_type, "sparkles")
                res_title = payload.get("resource_title", "a resource")
                res_url = payload.get("resource_url", "")
                total = payload.get("reaction_total", 0)
                breakdown = payload.get("reaction_breakdown", {})
                lines = [f":{emoji}: Your resource *{res_title}* got a {reaction_type}"]
                if res_url and not res_url.startswith("dugg://"):
                    lines.append(f"<{res_url}>")
                # Aggregate summary
                parts = [f"{v} {k}" for k, v in sorted(breakdown.items())]
                if total > 1:
                    lines.append(f"{total} total reactions ({', '.join(parts)})")
                body = json.dumps({"text": "\n".join(lines)}).encode()
            elif event_type == "resource_added":
                resource_id = payload.get("resource_id", "")
                lines = [f"*{title}*"]
                if url:
                    lines.append(f"<{url}>")
                meta = []
                if actor_name:
                    meta.append(f"Added by {actor_name}")
                if source:
                    meta.append(f"from {source}")
                if meta:
                    lines.append(" · ".join(meta))
                if note:
                    lines.append(f"_{note}_")
                text_fallback = "\n".join(lines)
                # Block Kit with reaction buttons
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": text_fallback}},
                ]
                if resource_id:
                    blocks.append({
                        "type": "actions",
                        "elements": [
                            {"type": "button", "text": {"type": "plain_text", "text": ":point_right: Tap", "emoji": True},
                             "action_id": "dugg_react_tap", "value": resource_id},
                            {"type": "button", "text": {"type": "plain_text", "text": ":star: Star", "emoji": True},
                             "action_id": "dugg_react_star", "value": resource_id},
                            {"type": "button", "text": {"type": "plain_text", "text": ":thumbsup: Nice", "emoji": True},
                             "action_id": "dugg_react_thumbsup", "value": resource_id},
                        ],
                    })
                body = json.dumps({"text": text_fallback, "blocks": blocks}).encode()
            else:
                lines = [f"*{title}*"]
                if url:
                    lines.append(f"<{url}>")
                meta = []
                if actor_name:
                    meta.append(f"Added by {actor_name}")
                if source:
                    meta.append(f"from {source}")
                if meta:
                    lines.append(" · ".join(meta))
                if note:
                    lines.append(f"_{note}_")
                body = json.dumps({"text": "\n".join(lines)}).encode()
        else:
            body = json.dumps({"event": event, "actor_name": actor_name, "server_url": server_url}).encode()

        req = urllib.request.Request(
            hook["callback_url"],
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if hook.get("secret"):
            import hashlib, hmac
            sig = hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
            req.add_header("X-Dugg-Signature", sig)
        try:
            urllib.request.urlopen(req, timeout=15)
            self._update_webhook_status(hook["id"], success=True)
        except Exception:
            self._update_webhook_status(hook["id"], success=False)

    def _update_webhook_status(self, webhook_id: str, success: bool):
        """Thread-safe webhook status update using its own connection."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            if success:
                conn.execute(
                    "UPDATE webhook_subscriptions SET failure_count = 0, updated_at = ? WHERE id = ?",
                    (_now(), webhook_id),
                )
            else:
                row = conn.execute("SELECT failure_count FROM webhook_subscriptions WHERE id = ?", (webhook_id,)).fetchone()
                if row:
                    new_count = row[0] + 1
                    new_status = "failed" if new_count >= 5 else "active"
                    conn.execute(
                        "UPDATE webhook_subscriptions SET failure_count = ?, status = ?, updated_at = ? WHERE id = ?",
                        (new_count, new_status, _now(), webhook_id),
                    )
            conn.commit()
        finally:
            conn.close()

    def get_events(self, user_id: str, event_types: Optional[list[str]] = None,
                   since: Optional[str] = None, actor_id: Optional[Union[str, list[str]]] = None, limit: int = 50) -> list[dict]:
        """Get events for instances the user is subscribed to."""
        # Get user's subscribed instance IDs
        inst_rows = self.conn.execute(
            "SELECT instance_id FROM instance_subscribers WHERE user_id = ?", (user_id,)
        ).fetchall()
        inst_ids = [r["instance_id"] for r in inst_rows]

        # Also get user's collection IDs
        coll_ids = self._accessible_collection_ids(user_id)

        if not inst_ids and not coll_ids:
            return []

        conditions = []
        params = []

        if inst_ids:
            placeholders = ",".join("?" for _ in inst_ids)
            conditions.append(f"e.instance_id IN ({placeholders})")
            params.extend(inst_ids)

        if coll_ids:
            placeholders = ",".join("?" for _ in coll_ids)
            conditions.append(f"e.collection_id IN ({placeholders})")
            params.extend(coll_ids)

        where = f"({' OR '.join(conditions)})"

        if event_types:
            type_placeholders = ",".join("?" for _ in event_types)
            where += f" AND e.event_type IN ({type_placeholders})"
            params.extend(event_types)

        if since:
            where += " AND e.created_at > ?"
            params.append(since)

        if actor_id:
            if isinstance(actor_id, list):
                ph = ",".join("?" for _ in actor_id)
                where += f" AND e.actor_id IN ({ph})"
                params.extend(actor_id)
            else:
                where += " AND e.actor_id = ?"
                params.append(actor_id)

        params.append(limit)
        rows = self.conn.execute(
            f"SELECT e.* FROM event_log e WHERE {where} ORDER BY e.created_at DESC LIMIT ?",
            params,
        ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
            results.append(d)
        return results

    # --- User Cursors ---

    def get_cursor(self, user_id: str, cursor_type: str = "events") -> Optional[str]:
        """Get the last-seen timestamp for a user's cursor. Returns ISO timestamp or None."""
        row = self.conn.execute(
            "SELECT last_seen_at FROM user_cursors WHERE user_id = ? AND cursor_type = ?",
            (user_id, cursor_type),
        ).fetchone()
        return row["last_seen_at"] if row else None

    def update_cursor(self, user_id: str, cursor_type: str = "events",
                      last_seen_at: Optional[str] = None) -> dict:
        """Advance the user's read cursor. Defaults to now."""
        now = _now()
        ts = last_seen_at or now
        self.conn.execute(
            """INSERT INTO user_cursors (user_id, cursor_type, last_seen_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, cursor_type) DO UPDATE SET
               last_seen_at = ?, updated_at = ?""",
            (user_id, cursor_type, ts, now, ts, now),
        )
        self.conn.commit()
        return {"user_id": user_id, "cursor_type": cursor_type, "last_seen_at": ts}

    def get_unseen_events(self, user_id: str, limit: int = 10,
                          oldest_first: bool = True) -> list[dict]:
        """Get events the user hasn't seen yet, based on their cursor."""
        cursor_ts = self.get_cursor(user_id)
        return self.get_events(
            user_id,
            since=cursor_ts,
            limit=limit,
        ) if not oldest_first else list(reversed(self.get_events(
            user_id,
            since=cursor_ts,
            limit=limit,
        )))

    # --- Webhook Subscriptions ---

    def subscribe_webhook(self, user_id: str, callback_url: str,
                          instance_id: Optional[str] = None,
                          event_types: Optional[list[str]] = None, secret: str = "") -> dict:
        """Subscribe a webhook. If instance_id is None, fires on all server events."""
        sub_id = _uuid()
        now = _now()
        types_json = json.dumps(event_types or [])
        self.conn.execute(
            """INSERT INTO webhook_subscriptions (id, instance_id, user_id, callback_url, event_types, secret, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(user_id, callback_url) DO UPDATE SET
               event_types = ?, secret = ?, status = 'active', failure_count = 0, updated_at = ?""",
            (sub_id, instance_id, user_id, callback_url, types_json, secret, now, now, types_json, secret, now),
        )
        self.conn.commit()
        return {"id": sub_id, "instance_id": instance_id, "user_id": user_id,
                "callback_url": callback_url, "event_types": event_types or [], "status": "active"}

    def get_webhooks_for_event(self, event_type: str, instance_id: Optional[str] = None,
                               collection_id: Optional[str] = None) -> list[dict]:
        """Get active webhook subscriptions that match an event type.

        When `collection_id` is provided, only returns webhooks whose owning
        user has an *active* membership in that collection — so banned users
        (directly or via cascade) stop receiving notifications as soon as
        their `collection_members.status` flips to 'banned'.

        Always includes server-wide (NULL instance_id) hooks. Events without
        a collection_id (system events like user_banned) bypass the
        membership filter.
        """
        if collection_id:
            if instance_id:
                rows = self.conn.execute(
                    """SELECT ws.* FROM webhook_subscriptions ws
                       INNER JOIN collection_members cm ON cm.user_id = ws.user_id
                       WHERE (ws.instance_id = ? OR ws.instance_id IS NULL)
                         AND ws.status = 'active'
                         AND cm.collection_id = ?
                         AND cm.status = 'active'""",
                    (instance_id, collection_id),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT ws.* FROM webhook_subscriptions ws
                       INNER JOIN collection_members cm ON cm.user_id = ws.user_id
                       WHERE ws.status = 'active'
                         AND cm.collection_id = ?
                         AND cm.status = 'active'""",
                    (collection_id,),
                ).fetchall()
        elif instance_id:
            rows = self.conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE (instance_id = ? OR instance_id IS NULL) AND status = 'active'",
                (instance_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE status = 'active'"
            ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d["event_types"] = json.loads(d["event_types"]) if d["event_types"] else []
            # Empty event_types = subscribe to all
            if not d["event_types"] or event_type in d["event_types"]:
                results.append(d)
        return results

    def list_webhooks(self, user_id: str) -> list[dict]:
        """List all webhook subscriptions for a user."""
        rows = self.conn.execute(
            "SELECT * FROM webhook_subscriptions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["event_types"] = json.loads(d["event_types"]) if d["event_types"] else []
            results.append(d)
        return results

    def unsubscribe_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Remove a webhook subscription. Returns True if found and deleted."""
        cursor = self.conn.execute(
            "DELETE FROM webhook_subscriptions WHERE id = ? AND user_id = ?",
            (webhook_id, user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_webhook_failure(self, webhook_id: str):
        """Increment failure count. Pause webhook after 5 consecutive failures."""
        row = self.conn.execute("SELECT failure_count FROM webhook_subscriptions WHERE id = ?", (webhook_id,)).fetchone()
        if not row:
            return
        new_count = row["failure_count"] + 1
        new_status = "failed" if new_count >= 5 else "active"
        self.conn.execute(
            "UPDATE webhook_subscriptions SET failure_count = ?, status = ?, updated_at = ? WHERE id = ?",
            (new_count, new_status, _now(), webhook_id),
        )
        self.conn.commit()

    def mark_webhook_success(self, webhook_id: str):
        """Reset failure count on successful delivery."""
        self.conn.execute(
            "UPDATE webhook_subscriptions SET failure_count = 0, updated_at = ? WHERE id = ?",
            (_now(), webhook_id),
        )
        self.conn.commit()

    # --- Inbound Publish (receiving from remote) ---

    def ingest_remote_publish(self, resource_data: dict, source_instance_id: str, target_collection_id: str, source_server: str = "") -> Optional[dict]:
        """Receive a published resource from a remote Dugg instance.

        Stores the resource in the target collection with the source tracked.
        Deduplicates by URL within the collection -- but the incoming note is
        captured into `resource_notes` (quarantined from outbound publish) on
        both first ingest and duplicate ingest, so cross-server enrichment is
        never lost.
        """
        url = resource_data.get("url", "")
        if not url:
            return None

        incoming_note = (resource_data.get("note") or "").strip()
        submitter_name = (resource_data.get("submitter_name")
                          or resource_data.get("author") or "")

        # Deduplicate: check if this URL already exists in this collection
        existing = self.conn.execute(
            "SELECT id FROM resources WHERE url = ? AND collection_id = ?",
            (url, target_collection_id),
        ).fetchone()
        if existing:
            note_added = False
            if incoming_note:
                self.add_resource_note(
                    existing["id"], incoming_note,
                    source_server=source_server,
                    source_instance_id=source_instance_id,
                    submitter_name=submitter_name,
                )
                note_added = True
            # Union tags from the incoming publish onto the existing resource
            now = _now()
            for tag in resource_data.get("tags", []) or []:
                label = tag if isinstance(tag, str) else tag.get("label", "")
                if label:
                    self._add_tag(existing["id"], label, "agent", now)
            self.conn.commit()
            return {"id": existing["id"], "status": "duplicate", "url": url,
                    "note_added": note_added}

        # Get the collection owner as the submitter
        coll = self.conn.execute("SELECT created_by FROM collections WHERE id = ?", (target_collection_id,)).fetchone()
        if not coll:
            return None

        res_id = _uuid()
        now = _now()
        meta = resource_data.get("raw_metadata", {})
        if isinstance(meta, dict):
            meta["_source_instance"] = source_instance_id
        meta_json = json.dumps(meta) if isinstance(meta, dict) else meta

        # Store resources.note = '' on cross-server first ingest: the note came
        # from the source server and belongs in resource_notes so it stays
        # quarantined. If this server later publishes the resource outbound,
        # the payload's `note` field will be empty rather than carrying a
        # foreign submitter's text.
        self.conn.execute(
            """INSERT INTO resources
               (id, url, title, description, thumbnail, source_type, author, transcript, raw_metadata, note, source_server, submitted_by, collection_id, created_at, updated_at, enriched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (res_id, url,
             resource_data.get("title", ""), resource_data.get("description", ""),
             resource_data.get("thumbnail", ""), resource_data.get("source_type", "unknown"),
             resource_data.get("author", ""), resource_data.get("transcript", ""),
             meta_json, "",
             source_server,
             coll["created_by"], target_collection_id, now, now,
             resource_data.get("enriched_at")),
        )

        if incoming_note:
            self.add_resource_note(
                res_id, incoming_note,
                source_server=source_server,
                source_instance_id=source_instance_id,
                submitter_name=submitter_name,
            )

        # Copy tags
        tags = resource_data.get("tags", [])
        for tag in tags:
            label = tag if isinstance(tag, str) else tag.get("label", "")
            if label:
                self._add_tag(res_id, label, "agent", now)

        self.conn.commit()
        return {"id": res_id, "status": "ingested", "url": url, "title": resource_data.get("title", "")}

    # --- Helpers ---

    def _accessible_collection_ids(self, user_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT collection_id FROM collection_members WHERE user_id = ? AND status = 'active'", (user_id,)
        ).fetchall()
        return [r["collection_id"] for r in rows]

    def _build_horizon_filters(self, user_id: str, collection_ids: list[str]) -> dict[str, str]:
        """Build a mapping of collection_id -> cutoff ISO timestamp for read horizon filtering.
        Only includes collections that have a horizon restriction."""
        filters = {}
        for coll_id in collection_ids:
            inst = self._get_instance_for_collection_owner(coll_id)
            if not inst:
                continue
            cutoff = self.visible_since(user_id, inst["id"])
            if cutoff is not None:
                filters[coll_id] = cutoff
        return filters

    def _horizon_where_clause(self, user_id: str, collection_ids: list[str],
                               horizon_filters: dict[str, str]) -> tuple[str, list]:
        """Build a SQL WHERE fragment for read horizon filtering.
        Returns (sql_fragment, params) where sql_fragment starts with AND.
        Own resources always pass."""
        if not horizon_filters:
            return "", []

        params = []
        # Resources pass if: submitted by user, or in unrestricted collection, or in restricted collection but after cutoff
        conditions = [f"r.submitted_by = ?"]
        params.append(user_id)

        unrestricted = [c for c in collection_ids if c not in horizon_filters]
        if unrestricted:
            placeholders = ",".join("?" for _ in unrestricted)
            conditions.append(f"r.collection_id IN ({placeholders})")
            params.extend(unrestricted)

        for coll_id, cutoff in horizon_filters.items():
            conditions.append("(r.collection_id = ? AND r.created_at >= ?)")
            params.extend([coll_id, cutoff])

        return f" AND ({' OR '.join(conditions)})", params

    # --- Export / Import ---

    def export_resources(
        self,
        user_id: str,
        collection_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        since: Optional[str] = None,
    ) -> list[dict]:
        """Export resources the user can access, with optional filters.

        Returns a list of dicts suitable for JSON serialization in the
        .dugg.json portable format.  Excludes sibling notes (quarantined),
        reactions, edges, and server-local state.
        """
        # Build accessible collection list
        if collection_id:
            coll_ids = [collection_id]
        else:
            coll_ids = [c["id"] for c in self.list_collections(user_id)]
        if not coll_ids:
            return []

        placeholders = ",".join("?" for _ in coll_ids)
        sql = f"SELECT * FROM resources WHERE collection_id IN ({placeholders})"
        params: list = list(coll_ids)

        if since:
            sql += " AND created_at >= ?"
            params.append(since)

        sql += " ORDER BY created_at ASC"
        rows = self.conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            tag_rows = self._get_tags(r["id"])
            tag_labels = [t["label"] for t in tag_rows]

            # Tag filter: resource must have ALL requested tags
            if tags:
                if not all(t in tag_labels for t in tags):
                    continue

            raw_meta = r.get("raw_metadata", "{}")
            if isinstance(raw_meta, str):
                try:
                    raw_meta = json.loads(raw_meta)
                except (json.JSONDecodeError, TypeError):
                    raw_meta = {}

            results.append({
                "url": r["url"],
                "title": r.get("title", ""),
                "description": r.get("description", ""),
                "source_type": r.get("source_type", "unknown"),
                "author": r.get("author", ""),
                "transcript": r.get("transcript", ""),
                "note": r.get("note", ""),
                "summary": r.get("summary", ""),
                "thumbnail": r.get("thumbnail", ""),
                "raw_metadata": raw_meta,
                "tags": tag_labels,
                "created_at": r.get("created_at", ""),
                "enriched_at": r.get("enriched_at", ""),
            })
        return results

    def import_resource(
        self,
        resource_data: dict,
        collection_id: str,
        submitted_by: str,
        on_conflict: str = "skip",
        extra_tags: Optional[list[str]] = None,
    ) -> dict:
        """Import a single resource from a .dugg.json payload.

        Returns a dict with ``status`` of ``imported``, ``skipped``, or
        ``updated`` plus resource fields.
        """
        url = resource_data["url"]
        tags = list(resource_data.get("tags") or [])
        if extra_tags:
            tags = list(set(tags + extra_tags))

        existing = self.conn.execute(
            "SELECT id FROM resources WHERE url = ? AND collection_id = ?",
            (url, collection_id),
        ).fetchone()

        if existing:
            if on_conflict == "skip":
                res = self.get_resource(existing["id"]) or {}
                res["status"] = "skipped"
                return res
            elif on_conflict == "update":
                updates = {}
                for field in ("title", "description", "source_type", "author",
                              "transcript", "note", "summary", "thumbnail",
                              "raw_metadata"):
                    val = resource_data.get(field)
                    if val:
                        updates[field] = val
                if updates:
                    self.update_resource(existing["id"], **updates)
                if tags:
                    now = _now()
                    for tag in tags:
                        self._add_tag(existing["id"], tag, "human", now)
                    self.conn.commit()
                res = self.get_resource(existing["id"]) or {}
                res["status"] = "updated"
                return res

        raw_meta = resource_data.get("raw_metadata")
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                raw_meta = {}

        result = self.add_resource(
            url=url,
            collection_id=collection_id,
            submitted_by=submitted_by,
            note=resource_data.get("note", ""),
            title=resource_data.get("title", ""),
            description=resource_data.get("description", ""),
            thumbnail=resource_data.get("thumbnail", ""),
            source_type=resource_data.get("source_type", "unknown"),
            author=resource_data.get("author", ""),
            transcript=resource_data.get("transcript", ""),
            raw_metadata=raw_meta,
            tags=tags,
            tag_source="human",
            summary=resource_data.get("summary", ""),
        )
        result["status"] = "imported"
        return result
