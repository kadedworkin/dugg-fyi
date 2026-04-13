"""SQLite + FTS5 database layer for Dugg."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
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
        return {
            "id": res_id, "url": url, "title": title, "description": description,
            "source_type": source_type, "author": author, "note": note, "submitted_by": submitted_by,
            "collection_id": collection_id, "tags": tags or [], "created_at": now,
        }

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
        """Publish a resource to one or more named targets."""
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
        allowed = {"name", "topic", "access_mode", "rate_limit_initial", "rate_limit_growth"}
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

    # --- Helpers ---

    def _accessible_collection_ids(self, user_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT collection_id FROM collection_members WHERE user_id = ? AND status = 'active'", (user_id,)
        ).fetchall()
        return [r["collection_id"] for r in rows]
