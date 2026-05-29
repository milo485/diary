#!/usr/bin/env python3
"""Diary, a small encrypted journal for the GNOME desktop.

The window has a welcome screen, an unlock screen, and the main view: an
editor with a few formatting buttons on the left and the list of entries on
the right. Click an entry to read it, or delete it (with confirmation).
The crypto and file handling live in diarycore.
"""

import os
import re
import sys
from datetime import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Pango  # noqa: E402

from cryptography.fernet import InvalidToken  # noqa: E402
import diarycore as core  # noqa: E402

APP_ID = "org.diary.Diary"


# --- Tiny Markdown subset, rendered to Pango markup -------------------------
def _inline(s):
    s = GLib.markup_escape_text(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"_(.+?)_", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<tt>\1</tt>", s)
    return s


def markdown_to_pango(text):
    lines = []
    for line in text.split("\n"):
        head = re.match(r"(#{1,3})\s+(.*)", line)
        if head:
            size = {1: "x-large", 2: "large", 3: "medium"}[len(head.group(1))]
            lines.append(f'<span size="{size}" weight="bold">{_inline(head.group(2))}</span>')
            continue
        bullet = re.match(r"[-*]\s+(.*)", line)
        if bullet:
            lines.append("• " + _inline(bullet.group(1)))
            continue
        lines.append(_inline(line))
    return "\n".join(lines)


def preview_of(text):
    for line in text.split("\n"):
        clean = re.sub(r"^[#\-*\s]+", "", line).strip()
        if clean:
            return clean
    return "(empty)"


class DiaryWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.fernet = None
        self.entries = []
        self.current_index = None
        self._pending_pw = None
        self.set_title("Diary")
        self.set_default_size(900, 680)
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        self._install_actions()

        if core.vault_ready():
            self._build_unlock()
        else:
            self._build_welcome()

    # --- Menu-bar actions ---------------------------------------------------
    def _install_actions(self):
        for name, cb in (
            ("new_diary", lambda *_: self._build_create()),
            ("open_diary", lambda *_: self._open_dialog()),
            ("export_copy", lambda *_: self._export_dialog() if self.fernet
                else self._toast("Unlock a diary first.")),
            ("lock", lambda *_: self._lock()),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

    # --- Helpers ------------------------------------------------------------
    def _toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=3))

    def _home(self):
        return Gio.File.new_for_path(os.path.expanduser("~"))

    def _vault_filters(self):
        store = Gio.ListStore.new(Gtk.FileFilter)
        f = Gtk.FileFilter()
        f.set_name("Diary files (*.vault)")
        f.add_pattern("*.vault")
        store.append(f)
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        store.append(allf)
        return store

    def _centered(self, child, max_size=440):
        clamp = Adw.Clamp(child=child, maximum_size=max_size)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(css_classes=["flat"]))
        toolbar.set_content(clamp)
        self.toast_overlay.set_child(toolbar)

    def _column(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(40)
        box.set_margin_bottom(40)
        box.set_margin_start(24)
        box.set_margin_end(24)
        return box

    def _show(self, label, msg):
        label.set_text(msg)
        label.set_visible(True)

    # --- Welcome / create / open / unlock -----------------------------------
    def _build_welcome(self):
        box = self._column()
        box.append(Gtk.Label(label="📔", css_classes=["title-1"]))
        box.append(Gtk.Label(label="Welcome to Diary", css_classes=["title-1"]))
        box.append(Gtk.Label(
            label="Create a new encrypted diary, or open an existing diary file.",
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))

        create_btn = Gtk.Button(label="Create new diary",
                                css_classes=["suggested-action", "pill"],
                                halign=Gtk.Align.CENTER)
        create_btn.set_margin_top(8)
        create_btn.connect("clicked", lambda *_: self._build_create())
        box.append(create_btn)

        open_btn = Gtk.Button(label="Open existing diary…",
                              css_classes=["pill"], halign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: self._open_dialog())
        box.append(open_btn)

        same = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        same.add_widget(create_btn)
        same.add_widget(open_btn)
        self._centered(box)

    def _build_create(self):
        box = self._column()
        box.append(Gtk.Label(label="📔", css_classes=["title-1"]))
        box.append(Gtk.Label(label="Create new diary", css_classes=["title-2"]))

        group = Adw.PreferencesGroup()
        self.c_pw = Adw.PasswordEntryRow(title="Password")
        self.c_pw2 = Adw.PasswordEntryRow(title="Repeat password")
        self.c_pw.connect("entry-activated", lambda *_: self.c_pw2.grab_focus())
        self.c_pw2.connect("entry-activated", lambda *_: self._create_choose_location())
        group.add(self.c_pw)
        group.add(self.c_pw2)
        box.append(group)

        box.append(Gtk.Label(
            label="Next you'll choose where to save the diary file. Without this "
                  "password the entries cannot be recovered. (at least 6 characters)",
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))

        self.create_error = Gtk.Label(css_classes=["error"], visible=False, wrap=True)
        box.append(self.create_error)

        btn = Gtk.Button(label="Choose location & create…",
                         css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._create_choose_location())
        box.append(btn)

        back = Gtk.Button(label="Back", css_classes=["flat"], halign=Gtk.Align.CENTER)
        back.connect("clicked", lambda *_: self._build_welcome())
        box.append(back)

        self._centered(box)
        self.c_pw.grab_focus()

    def _create_choose_location(self):
        pw, pw2 = self.c_pw.get_text(), self.c_pw2.get_text()
        if len(pw) < 6:
            self._show(self.create_error, "Please use at least 6 characters.")
            return
        if pw != pw2:
            self._show(self.create_error, "The passwords do not match.")
            return
        self._pending_pw = pw
        dialog = Gtk.FileDialog(title="Create diary file",
                                initial_name=core.DEFAULT_VAULT_NAME)
        dialog.set_initial_folder(self._home())
        dialog.set_filters(self._vault_filters())
        dialog.save(self, None, self._create_done)

    def _create_done(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except Exception:
            return
        path = gfile.get_path()
        if not path.endswith(".vault"):
            path += ".vault"
        try:
            self.fernet = core.create_diary(self._pending_pw, path)
        except Exception as e:
            self._toast(f"Could not create diary: {e}")
            return
        finally:
            self._pending_pw = None
        self._build_main()
        self._toast(f"Diary created at {path}")

    def _open_dialog(self):
        dialog = Gtk.FileDialog(title="Open diary file")
        dialog.set_initial_folder(self._home())
        dialog.set_filters(self._vault_filters())
        dialog.open(self, None, self._open_done)

    def _open_done(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except Exception:
            return
        try:
            core.open_vault(gfile.get_path())
        except Exception:
            self._toast("That is not a valid Diary file.")
            return
        self.fernet = None
        self._build_unlock()
        self._toast("Diary opened. Enter its password to unlock.")

    def _build_unlock(self):
        box = self._column()
        box.append(Gtk.Label(label="📔", css_classes=["title-1"]))
        box.append(Gtk.Label(label="Unlock diary", css_classes=["title-2"]))
        box.append(Gtk.Label(label=core.get_vault_path() or "", wrap=True,
                             wrap_mode=Pango.WrapMode.WORD_CHAR,
                             justify=Gtk.Justification.CENTER,
                             css_classes=["dim-label", "caption"]))

        group = Adw.PreferencesGroup()
        self.u_pw = Adw.PasswordEntryRow(title="Password")
        self.u_pw.connect("entry-activated", lambda *_: self._do_unlock())
        group.add(self.u_pw)
        box.append(group)

        self.unlock_error = Gtk.Label(css_classes=["error"], visible=False, wrap=True)
        box.append(self.unlock_error)

        btn = Gtk.Button(label="Unlock", css_classes=["suggested-action", "pill"],
                         halign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._do_unlock())
        box.append(btn)

        other = Gtk.Button(label="Open a different diary…", css_classes=["flat"],
                           halign=Gtk.Align.CENTER)
        other.connect("clicked", lambda *_: self._open_dialog())
        box.append(other)

        self._centered(box)
        self.u_pw.grab_focus()

    def _do_unlock(self):
        try:
            self.fernet = core.unlock(self.u_pw.get_text())
        except InvalidToken:
            self._show(self.unlock_error, "Wrong password.")
            self.u_pw.set_text("")
            return
        self._build_main()

    def _lock(self):
        self.fernet = None
        self.entries = []
        self.current_index = None
        self._build_unlock() if core.vault_ready() else self._build_welcome()

    # --- Main view ----------------------------------------------------------
    def _menu_model(self):
        files = Gio.Menu()
        files.append("New diary…", "win.new_diary")
        files.append("Open diary…", "win.open_diary")
        files.append("Export a copy…", "win.export_copy")
        lock = Gio.Menu()
        lock.append("Lock", "win.lock")
        diary = Gio.Menu()
        diary.append_section(None, files)
        diary.append_section(None, lock)
        bar = Gio.Menu()
        bar.append_submenu("Diary", diary)
        return bar

    def _build_main(self):
        self.entries = core.load_entries(self.fernet)
        self.current_index = None

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="My Diary", subtitle=""))
        new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New entry")
        new_btn.connect("clicked", lambda *_: self._compose_new())
        header.pack_start(new_btn)

        split = Adw.OverlaySplitView()
        split.set_sidebar_position(Gtk.PackType.END)  # entry list on the right
        split.set_min_sidebar_width(260)
        split.set_max_sidebar_width(360)
        split.set_content(self._build_content())
        split.set_sidebar(self._build_sidebar())

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(Gtk.PopoverMenuBar.new_from_model(self._menu_model()))
        toolbar.set_content(split)
        self.toast_overlay.set_child(toolbar)

        self._refresh_sidebar()
        self._compose_new()

    # Content area: a stack with an editor page and a reader page.
    def _build_content(self):
        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(self._build_compose_page(), "compose")
        self.content_stack.add_named(self._build_view_page(), "view")
        return self.content_stack

    def _build_compose_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        # Formatting toolbar.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                      css_classes=["toolbar"])
        for label, tip, fn in (
            ("B", "Bold", lambda: self._wrap("**")),
            ("I", "Italic", lambda: self._wrap("*")),
            ("H", "Heading", lambda: self._prefix("# ")),
            ("•", "Bullet list", lambda: self._prefix("- ")),
        ):
            b = Gtk.Button(label=label, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", lambda _w, f=fn: f())
            bar.append(b)
        bar.append(Gtk.Label(label="Markdown supported", css_classes=["dim-label"],
                             hexpand=True, halign=Gtk.Align.END))
        page.append(bar)

        frame = Gtk.Frame(vexpand=True)
        scroller = Gtk.ScrolledWindow()
        self.editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.editor.set_top_margin(8)
        self.editor.set_bottom_margin(8)
        self.editor.set_left_margin(8)
        self.editor.set_right_margin(8)
        scroller.set_child(self.editor)
        frame.set_child(scroller)
        page.append(frame)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save = Gtk.Button(label="Save entry", css_classes=["suggested-action"])
        save.connect("clicked", lambda *_: self._save_new_entry())
        actions.append(save)
        actions.append(Gtk.Label(label="or Ctrl+Enter", css_classes=["dim-label"]))
        page.append(actions)

        save_shortcut = Gtk.ShortcutController()
        save_shortcut.add_shortcut(Gtk.Shortcut(
            trigger=Gtk.ShortcutTrigger.parse_string("<Control>Return"),
            action=Gtk.CallbackAction.new(lambda *_: (self._save_new_entry(), True)[1])))
        self.editor.add_controller(save_shortcut)
        return page

    def _build_view_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.view_ts = Gtk.Label(xalign=0, hexpand=True, css_classes=["title-4"])
        top.append(self.view_ts)
        del_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete entry",
                             css_classes=["flat"])
        del_btn.connect("clicked", lambda *_: self._confirm_delete())
        top.append(del_btn)
        page.append(top)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.view_body = Gtk.Label(xalign=0, yalign=0, wrap=True,
                                   wrap_mode=Pango.WrapMode.WORD_CHAR,
                                   selectable=True, use_markup=True)
        self.view_body.set_margin_top(4)
        scroller.set_child(self.view_body)
        page.append(scroller)
        return page

    def _build_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(8)
        box.set_margin_end(8)

        self.search = Gtk.SearchEntry(placeholder_text="Search entries …")
        self.search.connect("search-changed", lambda *_: self._refresh_sidebar())
        box.append(self.search)

        self.count_label = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        box.append(self.count_label)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                   css_classes=["navigation-sidebar"])
        self.listbox.connect("row-activated", self._on_row_activated)
        scroller.set_child(self.listbox)
        box.append(scroller)
        return box

    # --- Sidebar list -------------------------------------------------------
    def _refresh_sidebar(self):
        term = self.search.get_text().strip().lower()
        child = self.listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt

        shown = 0
        for idx in range(len(self.entries) - 1, -1, -1):  # newest first
            e = self.entries[idx]
            if term and term not in e["text"].lower():
                continue
            row = Adw.ActionRow(title=core.format_ts(e["timestamp"]),
                                subtitle=preview_of(e["text"]))
            row.set_activatable(True)
            row._index = idx
            self.listbox.append(row)
            shown += 1

        self.count_label.set_text(f"{shown} / {len(self.entries)} entries")

    def _on_row_activated(self, _listbox, row):
        self._show_entry(row._index)

    def _show_entry(self, index):
        self.current_index = index
        e = self.entries[index]
        self.view_ts.set_text(core.format_ts(e["timestamp"]))
        self.view_body.set_markup(markdown_to_pango(e["text"]))
        self.content_stack.set_visible_child_name("view")

    # --- Compose / save -----------------------------------------------------
    def _compose_new(self):
        self.current_index = None
        self.editor.get_buffer().set_text("")
        self.content_stack.set_visible_child_name("compose")
        if hasattr(self, "listbox"):
            self.listbox.unselect_all()
        self.editor.grab_focus()

    def _wrap(self, marker):
        buf = self.editor.get_buffer()
        bounds = buf.get_selection_bounds()
        if bounds:
            start, end = bounds
            off = start.get_offset()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_offset(off), f"{marker}{text}{marker}")
        else:
            buf.insert_at_cursor(marker + marker)
            cur = buf.get_iter_at_mark(buf.get_insert())
            cur.backward_chars(len(marker))
            buf.place_cursor(cur)
        self.editor.grab_focus()

    def _prefix(self, prefix):
        buf = self.editor.get_buffer()
        it = buf.get_iter_at_mark(buf.get_insert())
        it.set_line_offset(0)
        buf.insert(it, prefix)
        self.editor.grab_focus()

    def _save_new_entry(self):
        buf = self.editor.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            self._toast("No text entered.")
            return
        self.entries.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "text": text,
        })
        core.save_entries(self.fernet, self.entries)
        self._refresh_sidebar()
        self._show_entry(len(self.entries) - 1)
        self._toast("Entry saved.")

    # --- Delete -------------------------------------------------------------
    def _confirm_delete(self):
        if self.current_index is None:
            return
        e = self.entries[self.current_index]
        dialog = Adw.AlertDialog(
            heading="Delete entry?",
            body=f"The entry from {core.format_ts(e['timestamp'])} will be "
                 f"permanently deleted. This cannot be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_delete_response)
        dialog.present(self)

    def _on_delete_response(self, _dialog, response):
        if response != "delete" or self.current_index is None:
            return
        del self.entries[self.current_index]
        core.save_entries(self.fernet, self.entries)
        self.current_index = None
        self._refresh_sidebar()
        self._compose_new()
        self._toast("Entry deleted.")

    # --- Export -------------------------------------------------------------
    def _export_dialog(self):
        dialog = Gtk.FileDialog(title="Export a copy", initial_name="diary-backup.vault")
        dialog.set_initial_folder(self._home())
        dialog.set_filters(self._vault_filters())
        dialog.save(self, None, self._export_done)

    def _export_done(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except Exception:
            return
        path = gfile.get_path()
        if not path.endswith(".vault"):
            path += ".vault"
        try:
            core.export_vault(path)
            self._toast(f"Copy exported to {path}")
        except Exception as e:
            self._toast(f"Export failed: {e}")


class DiaryApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.props.active_window or DiaryWindow(self)
        win.present()


def main():
    return DiaryApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
