# 📔 Diary

A lightweight, **encrypted** personal diary for the GNOME desktop, distributed
as a **Flatpak**. Every entry is automatically timestamped and stored encrypted
with your master password.

Your whole diary is a single **portable vault file** that **you choose where to
store**. Move it to another computer, open it in Diary and type your password.
You can also export backup copies anytime.

## Install (recommended)

Download `diary.flatpak` from the [latest release](../../releases/latest), then:

```bash
flatpak install diary.flatpak
flatpak run org.diary.Diary
```

The app then appears in your application menu as **Diary**.

> Requires Flatpak and the GNOME 48 runtime, which is fetched automatically from
> Flathub during install. If you don't have Flathub yet:
> ```bash
> flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
> ```

## Build from source

Requirements (one-time, from Flathub):

```bash
flatpak install flathub org.gnome.Platform//48 org.gnome.Sdk//48 org.flatpak.Builder
```

Then build and install into the user scope:

```bash
./flatpak/build.sh
```

The `cryptography` dependency is bundled as pre-downloaded wheels in
`flatpak/wheels/`, so the build runs fully offline (no Rust toolchain needed).

## Using the app

**First start** shows a welcome screen with two choices:

- **Create new diary** – pick a master password, then choose **where to save**
  the diary file. Diary remembers that location and uses it from then on.
- **Open existing diary…** – pick a `.vault` file from anywhere, then enter its
  password.

**Day to day:**

- **Write** in the editor on the left and click **Save entry** (or press
  **Ctrl+Enter**). Each entry is timestamped automatically.
- **Formatting:** the editor understands simple Markdown. The **B**, **I**,
  **H** and **•** buttons add bold, italic, headings and bullet lists.
- **Read:** the list of entries is on the right. Click one to read it, rendered
  with its formatting.
- **Delete:** open an entry and click the trash icon. A confirmation dialog
  appears before anything is removed.
- **Search:** filter the list with the search box.
- **Menu bar → Diary:**
  - **New diary…** – create another diary at a location you choose.
  - **Open diary…** – switch to a different `.vault` file.
  - **Export a copy…** – save a backup copy elsewhere.
  - **Lock** – return to the password screen.

## Portable vault file

The entire diary is one self-contained file (JSON):

```json
{ "format": "diary-vault", "version": 1, "salt": "…", "data": "…" }
```

Because the random salt is stored alongside the encrypted data, the file works
on any machine: copy it over, **Open** it, type your password. Without the
password the contents cannot be read.

## Where is my data?

Wherever **you** chose to save it (e.g. `~/Documents/mydiary.vault`). The active
location is remembered in:

```
~/.var/app/org.diary.Diary/config/diary/config.json
```

If you don't pick a folder, the suggested default is
`~/.local/share/diary/diary.vault` (inside the sandbox).

> The Flatpak is granted access to your **home directory** so it can read and
> write the vault at the location you choose. Save your vault somewhere under
> your home folder.

## Security

- Encryption with **Fernet (AES-128 CBC + HMAC)** from the `cryptography` library.
- The key is derived with **scrypt** (n=2¹⁵) from your password + a random salt.
- The password is **never stored** – it only lives in memory while running.
- Files are written atomically with permissions `600` (owner only).

> ⚠ **Important:** without your password the entries are **not** recoverable.
> Use **Export a copy** regularly to keep a backup.

## Project files

| File | Contents |
|------|----------|
| `diary_gtk.py` | GTK4/libadwaita application |
| `diarycore.py` | cryptography + portable-vault storage (with chosen location) |
| `flatpak/` | Flatpak manifest, icon, desktop entry, build script, wheels |

## License

[GPL-3.0-or-later](LICENSE). © 2026 Diary contributors.
