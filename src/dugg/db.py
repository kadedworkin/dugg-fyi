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
                PRIMARY KEY (collection_id, user_id)
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
            "INSERT INTO collection_members (collection_id, user_id, role) VALUES (?, ?, 'owner')",
            (coll_id, user_id),
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

    def add_collection_member(self, collection_id: str, user_id: str, role: str = "member"):
        self.conn.execute(
            "INSERT OR IGNORE INTO collection_members (collection_id, user_id, role) VALUES (?, ?, ?)",
            (collection_id, user_id, role),
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

    # --- Helpers ---

    def _accessible_collection_ids(self, user_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT collection_id FROM collection_members WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [r["collection_id"] for r in rows]
