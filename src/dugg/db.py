"""SQLite + FTS5 database layer for Dugg."""

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = Path.home() / ".dugg" / "dugg.db"


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
                PRIMARY KEY (collection_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS dugg_instances (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                topic TEXT DEFAULT '',
                access_mode TEXT DEFAULT 'invite' CHECK(access_mode IN ('public', 'invite')),
                endpoint_url TEXT DEFAULT '',
                rate_limit_initial INTEGER DEFAULT 5,
                rate_limit_growth INTEGER DEFAULT 2,
                owner_id TEXT NOT NULL REFERENCES users(id),
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
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'delivering', 'delivered', 'failed')),
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 5,
                next_retry_at TEXT NOT NULL,
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK(event_type IN ('resource_added', 'resource_published', 'member_joined', 'member_banned', 'publish_delivered')),
                instance_id TEXT REFERENCES dugg_instances(id),
                collection_id TEXT REFERENCES collections(id),
                actor_id TEXT REFERENCES users(id),
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL REFERENCES dugg_instances(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                callback_url TEXT NOT NULL,
                event_types TEXT DEFAULT '[]',
                secret TEXT DEFAULT '',
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'failed')),
                failure_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(instance_id, user_id, callback_url)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(
                title, description, author, transcript, note,
                content='resources',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS resources_ai AFTER INSERT ON resources BEGIN
                INSERT INTO resources_fts(rowid, title, description, author, transcript, note)
                VALUES (new.rowid, new.title, new.description, new.author, new.transcript, new.note);
            END;

            CREATE TRIGGER IF NOT EXISTS resources_ad AFTER DELETE ON resources BEGIN
                INSERT INTO resources_fts(resources_fts, rowid, title, description, author, transcript, note)
                VALUES ('delete', old.rowid, old.title, old.description, old.author, old.transcript, old.note);
            END;

            CREATE TRIGGER IF NOT EXISTS resources_au AFTER UPDATE ON resources BEGIN
                INSERT INTO resources_fts(resources_fts, rowid, title, description, author, transcript, note)
                VALUES ('delete', old.rowid, old.title, old.description, old.author, old.transcript, old.note);
                INSERT INTO resources_fts(rowid, title, description, author, transcript, note)
                VALUES (new.rowid, new.title, new.description, new.author, new.transcript, new.note);
            END;
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- Users ---

    def create_user(self, name: str) -> dict:
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

    def add_collection_member(self, collection_id: str, user_id: str, role: str = "member", invited_by: Optional[str] = None):
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO collection_members (collection_id, user_id, role, invited_by, status, joined_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (collection_id, user_id, role, invited_by, now),
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
    ) -> dict:
        res_id = _uuid()
        now = _now()
        meta_json = json.dumps(raw_metadata or {})
        self.conn.execute(
            """INSERT INTO resources
               (id, url, title, description, thumbnail, source_type, author, transcript, raw_metadata, note, submitted_by, collection_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (res_id, url, title, description, thumbnail, source_type, author, transcript, meta_json, note, submitted_by, collection_id, now, now),
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
                        payload={"resource_id": res_id, "url": url, "title": title, "source_type": source_type})
        return result

    def update_resource(self, resource_id: str, **fields) -> Optional[dict]:
        allowed = {"title", "description", "thumbnail", "source_type", "author", "transcript", "raw_metadata", "note", "enriched_at"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_resource(resource_id)
        updates["updated_at"] = _now()
        if "raw_metadata" in updates and isinstance(updates["raw_metadata"], dict):
            updates["raw_metadata"] = json.dumps(updates["raw_metadata"])
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

    def search(self, query: str, user_id: str, collection_id: Optional[str] = None, tags: Optional[list[str]] = None, limit: int = 20) -> list[dict]:
        """Full-text search across resources the user has access to."""
        accessible = self._accessible_collection_ids(user_id)
        if not accessible:
            return []
        if collection_id:
            if collection_id not in accessible:
                return []
            accessible = [collection_id]

        placeholders = ",".join("?" for _ in accessible)

        if query.strip():
            # FTS5 search
            fts_query = query.replace('"', '""')
            sql = f"""
                SELECT r.*, resources_fts.rank
                FROM resources_fts
                JOIN resources r ON r.rowid = resources_fts.rowid
                WHERE resources_fts MATCH ?
                  AND r.collection_id IN ({placeholders})
                ORDER BY resources_fts.rank
                LIMIT ?
            """
            params = [fts_query] + accessible + [limit]
        else:
            sql = f"""
                SELECT r.*, 0 as rank
                FROM resources r
                WHERE r.collection_id IN ({placeholders})
                ORDER BY r.created_at DESC
                LIMIT ?
            """
            params = accessible + [limit]

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
        """Get all resources across collections the user has access to, respecting share rules."""
        accessible = self._accessible_collection_ids(user_id)
        if not accessible:
            return []

        placeholders = ",".join("?" for _ in accessible)
        rows = self.conn.execute(
            f"SELECT * FROM resources WHERE collection_id IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
            accessible + [limit],
        ).fetchall()

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

    def get_related(self, resource_id: str, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM resource_edges
               WHERE resource_a = ? OR resource_b = ?
               ORDER BY confidence DESC LIMIT ?""",
            (resource_id, resource_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

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
        self.conn.execute(
            """INSERT OR IGNORE INTO reactions (id, resource_id, user_id, reaction_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (react_id, resource_id, user_id, reaction_type, now),
        )
        self.conn.commit()
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
                        rate_limit_initial: int = 5, rate_limit_growth: int = 2) -> dict:
        inst_id = _uuid()
        now = _now()
        self.conn.execute(
            "INSERT INTO dugg_instances (id, name, topic, access_mode, rate_limit_initial, rate_limit_growth, owner_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (inst_id, name, topic, access_mode, rate_limit_initial, rate_limit_growth, owner_id, now, now),
        )
        # Owner is auto-subscribed
        self.conn.execute(
            "INSERT INTO instance_subscribers (instance_id, user_id, subscribed_at) VALUES (?, ?, ?)",
            (inst_id, owner_id, now),
        )
        self.conn.commit()
        return {"id": inst_id, "name": name, "topic": topic, "access_mode": access_mode,
                "rate_limit_initial": rate_limit_initial, "rate_limit_growth": rate_limit_growth,
                "owner_id": owner_id, "created_at": now}

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
        allowed = {"name", "topic", "access_mode", "endpoint_url", "rate_limit_initial", "rate_limit_growth"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return inst
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [instance_id]
        self.conn.execute(f"UPDATE dugg_instances SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_instance(instance_id)

    def subscribe_to_instance(self, instance_id: str, user_id: str) -> dict:
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO instance_subscribers (instance_id, user_id, subscribed_at) VALUES (?, ?, ?)",
            (instance_id, user_id, now),
        )
        self.conn.commit()
        return {"instance_id": instance_id, "user_id": user_id, "subscribed_at": now}

    # --- Invite Tree ---

    def invite_member(self, collection_id: str, inviter_id: str, invitee_id: str) -> dict:
        """Add a member via invitation, tracking who invited them."""
        now = _now()
        self.conn.execute(
            """INSERT OR IGNORE INTO collection_members (collection_id, user_id, role, invited_by, status, joined_at)
               VALUES (?, ?, 'member', ?, 'active', ?)""",
            (collection_id, invitee_id, inviter_id, now),
        )
        self.conn.commit()
        self.emit_event("member_joined", actor_id=inviter_id, collection_id=collection_id,
                        payload={"user_id": invitee_id, "invited_by": inviter_id})
        return {"collection_id": collection_id, "user_id": invitee_id, "invited_by": inviter_id, "joined_at": now}

    def get_invite_tree(self, collection_id: str, user_id: str) -> list[dict]:
        """Get all members in the invite subtree below a user (recursive)."""
        rows = self.conn.execute(
            """WITH RECURSIVE tree AS (
                SELECT user_id, invited_by, role, status, joined_at, 1 as depth
                FROM collection_members
                WHERE collection_id = ? AND invited_by = ?
                UNION ALL
                SELECT cm.user_id, cm.invited_by, cm.role, cm.status, cm.joined_at, t.depth + 1
                FROM collection_members cm
                JOIN tree t ON cm.invited_by = t.user_id
                WHERE cm.collection_id = ?
            )
            SELECT * FROM tree""",
            (collection_id, user_id, collection_id),
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
        """Calculate credit score: submissions + reactions received in this collection."""
        submissions = self.conn.execute(
            "SELECT COUNT(*) as count FROM resources WHERE collection_id = ? AND submitted_by = ?",
            (collection_id, user_id),
        ).fetchone()["count"]

        reactions_received = self.conn.execute(
            """SELECT COUNT(*) as count FROM reactions re
               JOIN resources r ON re.resource_id = r.id
               WHERE r.collection_id = ? AND r.submitted_by = ?""",
            (collection_id, user_id),
        ).fetchone()["count"]

        return {"user_id": user_id, "submissions": submissions, "reactions_received": reactions_received, "total": submissions + reactions_received}

    def ban_member(self, collection_id: str, user_id: str, cascade: bool = True, credit_threshold: int = 5) -> dict:
        """Ban a user. With cascade, prunes invite tree: depth 1 = hard ban, depth 2+ = credit score decides."""
        # Ban the target user
        self.conn.execute(
            "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
            (collection_id, user_id),
        )

        banned = [user_id]
        survived = []

        if cascade:
            tree = self.get_invite_tree(collection_id, user_id)
            for member in tree:
                mid = member["user_id"]
                depth = member["depth"]
                if depth == 1:
                    # Hard prune — directly invited by banned user
                    self.conn.execute(
                        "UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?",
                        (collection_id, mid),
                    )
                    banned.append(mid)
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

        self.conn.commit()
        self.emit_event("member_banned", collection_id=collection_id,
                        payload={"banned": banned, "survived": survived, "cascade": cascade})
        return {"banned": banned, "survived": survived}

    # --- Appeals ---

    def appeal_ban(self, collection_id: str, user_id: str) -> Optional[dict]:
        """Submit an appeal. Only banned members can appeal."""
        member = self.get_member_status(collection_id, user_id)
        if not member or member["status"] != "banned":
            return None
        self.conn.execute(
            "UPDATE collection_members SET status = 'appealing' WHERE collection_id = ? AND user_id = ?",
            (collection_id, user_id),
        )
        self.conn.commit()
        score = self.get_member_credit_score(collection_id, user_id)
        return {"user_id": user_id, "status": "appealing", **score}

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
        """Approve an appeal — re-root under owner, set active."""
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
        self.conn.commit()
        return {"user_id": user_id, "status": "active", "invited_by": owner_id}

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
        """Mark a queue entry as successfully delivered."""
        self.conn.execute(
            "UPDATE publish_queue SET status = 'delivered', updated_at = ? WHERE id = ?",
            (_now(), queue_id),
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

    # --- Event Log ---

    def emit_event(self, event_type: str, actor_id: Optional[str] = None,
                   instance_id: Optional[str] = None, collection_id: Optional[str] = None,
                   payload: Optional[dict] = None) -> dict:
        """Log an event. Returns the event dict."""
        event_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO event_log (id, event_type, instance_id, collection_id, actor_id, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event_type, instance_id, collection_id, actor_id, json.dumps(payload or {}), now),
        )
        self.conn.commit()
        return {"id": event_id, "event_type": event_type, "actor_id": actor_id,
                "instance_id": instance_id, "collection_id": collection_id,
                "payload": payload or {}, "created_at": now}

    def get_events(self, user_id: str, event_types: Optional[list[str]] = None,
                   since: Optional[str] = None, limit: int = 50) -> list[dict]:
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

    # --- Webhook Subscriptions ---

    def subscribe_webhook(self, instance_id: str, user_id: str, callback_url: str,
                          event_types: Optional[list[str]] = None, secret: str = "") -> dict:
        """Subscribe a webhook to an instance's events."""
        sub_id = _uuid()
        now = _now()
        types_json = json.dumps(event_types or [])
        self.conn.execute(
            """INSERT INTO webhook_subscriptions (id, instance_id, user_id, callback_url, event_types, secret, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(instance_id, user_id, callback_url) DO UPDATE SET
               event_types = ?, secret = ?, status = 'active', failure_count = 0, updated_at = ?""",
            (sub_id, instance_id, user_id, callback_url, types_json, secret, now, now, types_json, secret, now),
        )
        self.conn.commit()
        return {"id": sub_id, "instance_id": instance_id, "user_id": user_id,
                "callback_url": callback_url, "event_types": event_types or [], "status": "active"}

    def get_webhooks_for_event(self, event_type: str, instance_id: Optional[str] = None) -> list[dict]:
        """Get active webhook subscriptions that match an event type."""
        if instance_id:
            rows = self.conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE instance_id = ? AND status = 'active'",
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

    def ingest_remote_publish(self, resource_data: dict, source_instance_id: str, target_collection_id: str) -> Optional[dict]:
        """Receive a published resource from a remote Dugg instance.

        Stores the resource in the target collection with the source tracked.
        Deduplicates by URL within the collection.
        """
        url = resource_data.get("url", "")
        if not url:
            return None

        # Deduplicate: check if this URL already exists in this collection
        existing = self.conn.execute(
            "SELECT id FROM resources WHERE url = ? AND collection_id = ?",
            (url, target_collection_id),
        ).fetchone()
        if existing:
            return {"id": existing["id"], "status": "duplicate", "url": url}

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

        self.conn.execute(
            """INSERT INTO resources
               (id, url, title, description, thumbnail, source_type, author, transcript, raw_metadata, note, submitted_by, collection_id, created_at, updated_at, enriched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (res_id, url,
             resource_data.get("title", ""), resource_data.get("description", ""),
             resource_data.get("thumbnail", ""), resource_data.get("source_type", "unknown"),
             resource_data.get("author", ""), resource_data.get("transcript", ""),
             meta_json, resource_data.get("note", ""),
             coll["created_by"], target_collection_id, now, now,
             resource_data.get("enriched_at")),
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
