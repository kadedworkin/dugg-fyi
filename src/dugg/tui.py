"""Dugg TUI — Textual-based admin interface for ban & appeal management."""

from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Static,
)

from dugg.db import DuggDB, DEFAULT_DB_PATH


class ConfirmScreen(ModalScreen[bool]):
    """Modal confirmation dialog."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._message, id="confirm-message")
            yield Label("[y] Yes  [n] No", id="confirm-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DuggAdmin(App):
    """Admin TUI for managing bans and appeals."""

    CSS = """
    #confirm-dialog {
        align: center middle;
        width: 60;
        height: auto;
        max-height: 12;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #confirm-message {
        width: 100%;
        text-align: center;
        margin-bottom: 1;
    }
    #confirm-hint {
        width: 100%;
        text-align: center;
        color: $text-muted;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    #main-area {
        height: 1fr;
    }
    #sidebar {
        width: 30;
        border-right: solid $accent;
        padding: 0 1;
    }
    #sidebar-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #detail {
        width: 1fr;
        padding: 0 1;
    }
    #detail-title {
        text-style: bold;
        margin-bottom: 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("c", "switch_collections", "Collections"),
        Binding("a", "show_appeals", "Appeals"),
        Binding("m", "show_members", "Members"),
        Binding("s", "show_resources", "Resources"),
        Binding("b", "ban_member", "Ban"),
        Binding("p", "ban_purge", "Ban+Purge"),
        Binding("x", "delete_resource", "Delete Resource"),
        Binding("u", "unban_member", "Unban/Approve"),
        Binding("d", "deny_appeal", "Deny"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, db_path: Optional[Path] = None, api_key: Optional[str] = None) -> None:
        super().__init__()
        self.db = DuggDB(db_path or DEFAULT_DB_PATH)
        self._api_key = api_key
        self._user = None
        self._collections = []
        self._selected_collection = None
        self._view = "members"  # "members" | "appeals"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-area"):
            with Vertical(id="sidebar"):
                yield Label("Collections", id="sidebar-title")
                yield DataTable(id="collection-table")
            with Vertical(id="detail"):
                yield Label("Members", id="detail-title")
                yield DataTable(id="detail-table")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        # Resolve user
        if self._api_key:
            self._user = self.db.get_user_by_api_key(self._api_key)
        else:
            self._user = self.db.get_user_by_api_key("dugg_local_default")
        if not self._user:
            self._set_status("No user found. Use --key or run 'dugg add-user'.")
            return

        self._set_status(f"Logged in as {self._user['name']}")

        # Set up collection table
        ct = self.query_one("#collection-table", DataTable)
        ct.add_columns("Name", "Visibility")
        ct.cursor_type = "row"
        self._refresh_collections()

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(f" {msg}")

    def _refresh_collections(self) -> None:
        if not self._user:
            return
        ct = self.query_one("#collection-table", DataTable)
        ct.clear()
        self._collections = self.db.list_collections(self._user["id"])
        for c in self._collections:
            ct.add_row(c["name"], c.get("visibility", "private"), key=c["id"])
        if self._collections:
            self._selected_collection = self._collections[0]
            self._refresh_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id
        if table_id == "collection-table":
            coll_id = str(event.row_key.value)
            self._selected_collection = next(
                (c for c in self._collections if c["id"] == coll_id), None
            )
            self._refresh_detail()

    def _refresh_detail(self) -> None:
        if self._view == "members":
            self._show_members()
        else:
            self._show_appeals()

    def _show_members(self) -> None:
        self._view = "members"
        dt = self.query_one("#detail-table", DataTable)
        dt.clear(columns=True)
        self.query_one("#detail-title", Label).update("Members")

        if not self._selected_collection:
            return

        dt.add_columns("Name", "Role", "Status", "Joined", "User ID")
        members = self.db.list_members(self._selected_collection["id"])
        for m in members:
            status = m["status"]
            if status == "banned":
                status = "[red]banned[/red]"
            elif status == "appealing":
                status = "[yellow]appealing[/yellow]"
            elif status == "active":
                status = "[green]active[/green]"
            dt.add_row(
                m["name"],
                m["role"],
                status,
                m["joined_at"][:10] if m.get("joined_at") else "",
                m["user_id"],
                key=m["user_id"],
            )

    def _show_appeals(self) -> None:
        self._view = "appeals"
        dt = self.query_one("#detail-table", DataTable)
        dt.clear(columns=True)
        self.query_one("#detail-title", Label).update("Pending Appeals")

        if not self._selected_collection:
            return

        dt.add_columns("Name", "Score", "Submissions", "Reactions", "Joined", "User ID")
        appeals = self.db.get_appeals(self._selected_collection["id"])
        for a in appeals:
            dt.add_row(
                a["name"],
                str(a["total"]),
                str(a["submissions"]),
                str(a["reactions_received"]),
                a["joined_at"][:10] if a.get("joined_at") else "",
                a["user_id"],
                key=a["user_id"],
            )
        if not appeals:
            self._set_status("No pending appeals.")
        else:
            self._set_status(f"{len(appeals)} pending appeal(s).")

    def action_switch_collections(self) -> None:
        self.query_one("#collection-table", DataTable).focus()

    def action_show_appeals(self) -> None:
        self._show_appeals()
        self.query_one("#detail-table", DataTable).focus()

    def action_show_members(self) -> None:
        self._show_members()
        self.query_one("#detail-table", DataTable).focus()

    def action_refresh(self) -> None:
        self._refresh_collections()
        self._set_status("Refreshed.")

    def _get_selected_user_id(self) -> Optional[str]:
        dt = self.query_one("#detail-table", DataTable)
        if dt.cursor_row is not None and dt.row_count > 0:
            row_key = dt.get_row_at(dt.cursor_row)
            # The last column is user_id
            return row_key[-1]
        return None

    def _get_selected_row_key(self) -> Optional[str]:
        dt = self.query_one("#detail-table", DataTable)
        try:
            cursor = dt.cursor_row
            if cursor is not None and dt.row_count > 0:
                keys = list(dt.rows.keys())
                if cursor < len(keys):
                    return str(keys[cursor].value)
        except Exception:
            pass
        return None

    def action_ban_member(self) -> None:
        if not self._selected_collection:
            self._set_status("No collection selected.")
            return
        user_id = self._get_selected_row_key()
        if not user_id:
            self._set_status("No member selected.")
            return
        user = self.db.get_user(user_id)
        name = user["name"] if user else user_id

        def do_ban(confirmed: bool) -> None:
            if confirmed:
                result = self.db.ban_member(self._selected_collection["id"], user_id, cascade=True)
                banned_count = len(result.get("banned", []))
                self._set_status(f"Banned {name} ({banned_count} total affected).")
                self._refresh_detail()
            else:
                self._set_status("Ban cancelled.")

        self.push_screen(ConfirmScreen(f"Ban {name} (with cascade)?"), do_ban)

    def action_unban_member(self) -> None:
        if not self._selected_collection:
            self._set_status("No collection selected.")
            return
        user_id = self._get_selected_row_key()
        if not user_id:
            self._set_status("No member selected.")
            return

        coll_id = self._selected_collection["id"]
        member = self.db.get_member_status(coll_id, user_id)
        if not member:
            self._set_status("Member not found.")
            return

        user = self.db.get_user(user_id)
        name = user["name"] if user else user_id

        if member["status"] == "appealing":
            def do_approve(confirmed: bool) -> None:
                if confirmed:
                    result = self.db.approve_appeal(coll_id, user_id)
                    if result:
                        agents = result.get("agents_unbanned", [])
                        msg = f"Approved appeal for {name}."
                        if agents:
                            msg += f" {len(agents)} agent(s) also restored."
                        self._set_status(msg)
                    else:
                        self._set_status("Failed to approve appeal.")
                    self._refresh_detail()
                else:
                    self._set_status("Approval cancelled.")

            self.push_screen(ConfirmScreen(f"Approve appeal for {name}?"), do_approve)
        elif member["status"] == "banned":
            # Direct unban — file an appeal and immediately approve it
            def do_unban(confirmed: bool) -> None:
                if confirmed:
                    self.db.appeal_ban(coll_id, user_id)
                    result = self.db.approve_appeal(coll_id, user_id)
                    if result:
                        agents = result.get("agents_unbanned", [])
                        msg = f"Unbanned {name}."
                        if agents:
                            msg += f" {len(agents)} agent(s) also restored."
                        self._set_status(msg)
                    else:
                        self._set_status("Failed to unban.")
                    self._refresh_detail()
                else:
                    self._set_status("Unban cancelled.")

            self.push_screen(ConfirmScreen(f"Unban {name}?"), do_unban)
        else:
            self._set_status(f"{name} is not banned or appealing.")

    def _show_resources(self) -> None:
        self._view = "resources"
        dt = self.query_one("#detail-table", DataTable)
        dt.clear(columns=True)
        self.query_one("#detail-title", Label).update("Resources")

        if not self._selected_collection:
            return

        dt.add_columns("Title", "URL", "Submitted By", "Added", "ID")
        rows = self.db.conn.execute(
            """SELECT r.id, r.url, r.title, r.submitted_by, r.created_at, u.name as submitter_name
               FROM resources r
               LEFT JOIN users u ON r.submitted_by = u.id
               WHERE r.collection_id = ?
               ORDER BY r.created_at DESC LIMIT 100""",
            (self._selected_collection["id"],),
        ).fetchall()
        for r in rows:
            r = dict(r)
            title = r.get("title") or r["url"][:40]
            url_short = r["url"][:50] + ("…" if len(r["url"]) > 50 else "")
            dt.add_row(
                title[:40],
                url_short,
                r.get("submitter_name") or r["submitted_by"],
                r["created_at"][:10],
                r["id"],
                key=r["id"],
            )
        self._set_status(f"{len(rows)} resource(s).")

    def action_show_resources(self) -> None:
        self._show_resources()
        self.query_one("#detail-table", DataTable).focus()

    def action_delete_resource(self) -> None:
        if not self._selected_collection:
            self._set_status("No collection selected.")
            return
        if self._view != "resources":
            self._set_status("Switch to Resources view first (s).")
            return
        resource_id = self._get_selected_row_key()
        if not resource_id:
            self._set_status("No resource selected.")
            return

        resource = self.db.get_resource(resource_id)
        title = resource.get("title", resource["url"][:40]) if resource else resource_id

        def do_delete(confirmed: bool) -> None:
            if confirmed:
                result = self.db.delete_resource(
                    resource_id, self._selected_collection["id"], self._user["id"]
                )
                if result.get("error"):
                    self._set_status(f"Error: {result['error']}")
                else:
                    self._set_status(f"Deleted: {title}")
                self._refresh_detail()
            else:
                self._set_status("Delete cancelled.")

        self.push_screen(ConfirmScreen(f"Delete resource: {title}?"), do_delete)

    def action_ban_purge(self) -> None:
        if not self._selected_collection:
            self._set_status("No collection selected.")
            return
        if self._view != "members":
            self._set_status("Switch to Members view first (m).")
            return
        user_id = self._get_selected_row_key()
        if not user_id:
            self._set_status("No member selected.")
            return
        user = self.db.get_user(user_id)
        name = user["name"] if user else user_id

        def do_ban_purge(confirmed: bool) -> None:
            if confirmed:
                result = self.db.ban_member(
                    self._selected_collection["id"], user_id, cascade=True, purge=True
                )
                banned_count = len(result.get("banned", []))
                purged = result.get("purged_resources", 0)
                self._set_status(
                    f"Banned {name} ({banned_count} affected, {purged} resource(s) purged)."
                )
                self._refresh_detail()
            else:
                self._set_status("Ban+purge cancelled.")

        self.push_screen(
            ConfirmScreen(f"Ban {name} AND delete all their resources?"), do_ban_purge
        )

    def action_deny_appeal(self) -> None:
        if not self._selected_collection:
            self._set_status("No collection selected.")
            return
        user_id = self._get_selected_row_key()
        if not user_id:
            self._set_status("No member selected.")
            return

        coll_id = self._selected_collection["id"]
        member = self.db.get_member_status(coll_id, user_id)
        if not member or member["status"] != "appealing":
            self._set_status("Selected member is not appealing.")
            return

        user = self.db.get_user(user_id)
        name = user["name"] if user else user_id

        def do_deny(confirmed: bool) -> None:
            if confirmed:
                self.db.deny_appeal(coll_id, user_id)
                self._set_status(f"Denied appeal for {name}.")
                self._refresh_detail()
            else:
                self._set_status("Deny cancelled.")

        self.push_screen(ConfirmScreen(f"Deny appeal for {name}?"), do_deny)

    def on_unmount(self) -> None:
        self.db.close()


def run_tui(db_path: Optional[Path] = None, api_key: Optional[str] = None) -> None:
    """Launch the Dugg admin TUI."""
    app = DuggAdmin(db_path=db_path, api_key=api_key)
    app.run()
