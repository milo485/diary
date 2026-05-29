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
            ("manage_diaries", lambda *_: self._show_manage_dialog()),
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

    def _manage_button(self):
        btn = Gtk.Button(icon_name="view-list-symbolic",
                         tooltip_text="Manage diaries",
                         css_classes=["flat"])
        btn.connect("clicked", lambda *_: self._show_manage_dialog())
        return btn

    def _status_page(self, icon_name, title, description=None):
        page = Adw.StatusPage()
        page.set_icon_name(icon_name)
        page.set_title(title)
        if description:
            page.set_description(description)
        header = Adw.HeaderBar(css_classes=["flat"])
        header.pack_end(self._manage_button())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.toast_overlay.set_child(toolbar)
        return page

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
        page = self._status_page(
            "accessories-text-editor-symbolic",
            "Welcome to Diary",
            "Create a new encrypted diary, or open an existing diary file.",
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      halign=Gtk.Align.CENTER)

        create_btn = Gtk.Button(label="Create new diary",
                                css_classes=["suggested-action", "pill"],
                                halign=Gtk.Align.CENTER)
        create_btn.connect("clicked", lambda *_: self._build_create())
        box.append(create_btn)

        open_btn = Gtk.Button(label="Open existing diary…",
                              css_classes=["pill"], halign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: self._open_dialog())
        box.append(open_btn)

        same = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        same.add_widget(create_btn)
        same.add_widget(open_btn)
        page.set_child(box)

    def _build_create(self):
        page = self._status_page(
            "accessories-text-editor-symbolic",
            "Create New Diary",
            "Choose a password — without it the entries cannot be recovered. "
            "At least 6 characters.",
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        group = Adw.PreferencesGroup()
        self.c_pw = Adw.PasswordEntryRow(title="Password")
        self.c_pw2 = Adw.PasswordEntryRow(title="Repeat password")
        self.c_pw.connect("entry-activated", lambda *_: self.c_pw2.grab_focus())
        self.c_pw2.connect("entry-activated", lambda *_: self._create_choose_location())
        group.add(self.c_pw)
        group.add(self.c_pw2)
        box.append(group)

        self.create_error = Gtk.Label(css_classes=["error"], visible=False, wrap=True)
        box.append(self.create_error)

        btn = Gtk.Button(label="Choose location & create…",
                         css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._create_choose_location())
        box.append(btn)

        back = Gtk.Button(label="Back", css_classes=["flat"], halign=Gtk.Align.CENTER)
        back.connect("clicked", lambda *_: self._build_welcome())
        box.append(back)

        clamp = Adw.Clamp(maximum_size=360, child=box)
        page.set_child(clamp)
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
        vault_path = core.get_vault_path() or ""
        page = self._status_page(
            "system-lock-screen-symbolic",
            "Unlock Diary",
            vault_path or None,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

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

        clamp = Adw.Clamp(maximum_size=360, child=box)
        page.set_child(clamp)
        self.u_pw.grab_focus()

    def _show_manage_dialog(self):
        dialog = Adw.Dialog()
        dialog.set_title("My Diaries")
        dialog.set_content_width(420)
        dialog.set_content_height(480)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_top(16)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        vault_group = Adw.PreferencesGroup(title="Vaults")
        self._fill_vault_group(vault_group, dialog)
        inner.append(vault_group)
        scroller.set_child(inner)
        outer.append(scroller)

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_box = Gtk.Box(spacing=8, margin_top=12, margin_bottom=12,
                          margin_start=16, margin_end=16, homogeneous=True)
        add_btn = Gtk.Button(label="Add existing…")
        add_btn.connect("clicked", lambda *_: (
            dialog.close(), GLib.idle_add(self._open_dialog)))
        create_btn = Gtk.Button(label="Create new…",
                                css_classes=["suggested-action"])
        create_btn.connect("clicked", lambda *_: (
            dialog.close(), GLib.idle_add(self._build_create)))
        btn_box.append(add_btn)
        btn_box.append(create_btn)
        outer.append(btn_box)

        toolbar_view.set_content(outer)
        dialog.set_child(toolbar_view)
        dialog.present(self)

    def _fill_vault_group(self, group, dialog):
        import os as _os
        vaults = core.get_known_vaults()
        active = core.get_vault_path()

        if not vaults:
            row = Adw.ActionRow(
                title="No diaries yet",
                subtitle="Create a new diary or add an existing file.")
            row.set_sensitive(False)
            group.add(row)
            return

        for path in vaults:
            name = _os.path.splitext(_os.path.basename(path))[0]
            row = Adw.ActionRow(title=name, subtitle=path)
            row.set_activatable(True)

            if active and _os.path.abspath(active) == _os.path.abspath(path):
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic",
                                         valign=Gtk.Align.CENTER,
                                         css_classes=["dim-label"]))

            trash = Gtk.Button(icon_name="user-trash-symbolic",
                               tooltip_text="Remove from list",
                               css_classes=["flat"],
                               valign=Gtk.Align.CENTER)

            def _make_remove(p, r, g, d):
                def on_remove(*_):
                    core.remove_known_vault(p)
                    g.remove(r)
                    if not core.get_known_vaults():
                        d.close()
                        GLib.idle_add(
                            self._build_unlock if core.vault_ready()
                            else self._build_welcome)
                return on_remove

            trash.connect("clicked", _make_remove(path, row, group, dialog))
            row.add_suffix(trash)

            def _make_switch(p, d):
                def on_activate(*_):
                    if active and _os.path.abspath(active) == _os.path.abspath(p):
                        d.close()
                        return
                    try:
                        core.open_vault(p)
                    except Exception:
                        self._toast("Could not open that diary file.")
                        return
                    d.close()
                    GLib.idle_add(self._lock)
                return on_activate

            row.connect("activated", _make_switch(path, dialog))
            group.add(row)

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
        files.append("Manage diaries…", "win.manage_diaries")
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
        today = datetime.now().strftime("%A, %B %-d")
        header.set_title_widget(Adw.WindowTitle(title="My Diary", subtitle=today))
        new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New entry",
                             css_classes=["flat"])
        new_btn.connect("clicked", lambda *_: self._compose_new())
        header.pack_start(new_btn)
        header.pack_end(self._manage_button())

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

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      css_classes=["toolbar"])

        icon_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                             css_classes=["linked"])
        for icon_name, tip, fn in (
            ("format-text-bold-symbolic", "Bold (**text**)", lambda: self._wrap("**")),
            ("format-text-italic-symbolic", "Italic (*text*)", lambda: self._wrap("*")),
        ):
            b = Gtk.Button(icon_name=icon_name, tooltip_text=tip)
            b.connect("clicked", lambda _w, f=fn: f())
            icon_group.append(b)
        bar.append(icon_group)

        text_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                             css_classes=["linked"])
        for label, tip, fn in (
            ("H", "Heading (# text)", lambda: self._prefix("# ")),
            ("•", "Bullet (- text)", lambda: self._prefix("- ")),
        ):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", lambda _w, f=fn: f())
            text_group.append(b)
        bar.append(text_group)

        bar.append(Gtk.Label(label="Markdown", css_classes=["dim-label"],
                             hexpand=True, halign=Gtk.Align.END))
        page.append(bar)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True,
                       css_classes=["card"])
        card.set_overflow(Gtk.Overflow.HIDDEN)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.editor.set_top_margin(12)
        self.editor.set_bottom_margin(12)
        self.editor.set_left_margin(12)
        self.editor.set_right_margin(12)
        self._setup_editor_tags()
        scroller.set_child(self.editor)
        card.append(scroller)
        page.append(card)

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

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_row.append(Gtk.Label(label="Entries", xalign=0, hexpand=True,
                                    css_classes=["heading"]))
        self.count_label = Gtk.Label(xalign=1, css_classes=["dim-label", "caption"])
        header_row.append(self.count_label)
        box.append(header_row)

        self.search = Gtk.SearchEntry(placeholder_text="Search …")
        self.search.connect("search-changed", lambda *_: self._refresh_sidebar())
        box.append(self.search)

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

    # --- Live markdown formatting in the editor ----------------------------
    def _setup_editor_tags(self):
        buf = self.editor.get_buffer()
        buf.create_tag("bold", weight=Pango.Weight.BOLD)
        buf.create_tag("italic", style=Pango.Style.ITALIC)
        buf.create_tag("code", family="Monospace")
        buf.create_tag("h1", weight=Pango.Weight.BOLD, scale=1.6)
        buf.create_tag("h2", weight=Pango.Weight.BOLD, scale=1.35)
        buf.create_tag("h3", weight=Pango.Weight.BOLD, scale=1.15)
        buf.create_tag("syntax", invisible=True)
        buf.connect("changed", self._reformat_editor)

    def _reformat_editor(self, buf):
        s = buf.get_start_iter()
        e = buf.get_end_iter()
        for name in ("bold", "italic", "code", "h1", "h2", "h3", "syntax"):
            buf.remove_tag_by_name(name, s, e)
        text = buf.get_text(s, e, False)
        offset = 0
        for line in text.split("\n"):
            self._reformat_line(buf, line, offset)
            offset += len(line) + 1

    def _reformat_line(self, buf, line, off):
        def itr(pos):
            return buf.get_iter_at_offset(off + pos)

        def apply(tag, a, b):
            if a < b:
                buf.apply_tag_by_name(tag, itr(a), itr(b))

        def syntax(a, b):
            apply("syntax", a, b)

        hm = re.match(r"(#{1,3})( .+)", line)
        if hm:
            level = len(hm.group(1))
            apply(f"h{level}", 0, len(line))
            syntax(0, level + 1)
            return

        bm = re.match(r"([-*] )", line)
        if bm:
            syntax(0, len(bm.group(1)))

        # bold before italic: prevents * inside ** from being matched as italic
        for m in re.finditer(r"\*\*(.+?)\*\*", line):
            apply("bold",   m.start(1), m.end(1))
            syntax(m.start(),    m.start() + 2)
            syntax(m.end() - 2,  m.end())

        for m in re.finditer(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", line):
            apply("italic", m.start(1), m.end(1))
            syntax(m.start(),    m.start() + 1)
            syntax(m.end() - 1,  m.end())

        for m in re.finditer(r"_(.+?)_", line):
            apply("italic", m.start(1), m.end(1))
            syntax(m.start(),    m.start() + 1)
            syntax(m.end() - 1,  m.end())

        for m in re.finditer(r"`(.+?)`", line):
            apply("code",   m.start(1), m.end(1))
            syntax(m.start(),    m.start() + 1)
            syntax(m.end() - 1,  m.end())

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
