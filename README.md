# Internet Archive Download Manager

A small desktop app for downloading files in bulk. It has two tabs:

- **Internet Archive** - paste an [archive.org](https://archive.org) item link.
- **Open Directory** - paste the address of any open-directory website (the plain "Index of /..." file listings).

In both tabs you get a file list, tick the files you want, pick a folder and download. No Python is needed to run the packaged version.

![icon](icon.png)

## Features

- **Open Directory tab.** Paste the address of a folder listing, for example `https://example.com/files/music/`, and click **Load**. The app reads the page, follows the subfolders (untick *Include subfolders* to stay in the top folder) and lists every file it finds, with the same folder tree, filters, dark mode and progress window as the Internet Archive tab. Click **Stop** during a long scan to see what was found so far. Works with Apache, nginx, lighttpd, Caddy, `python -m http.server` and similar listings.
  - Sizes that a server rounds off (like `4.5M`) show with a `~`. The real size is used when downloading. Tick *Look up missing file sizes* to ask the server for exact sizes (slower), which is also useful for listings that show no sizes at all.
  - Links that lead outside the folder you entered (the "Parent Directory" link, other websites, sort links) are ignored.
- **Paste any Internet Archive item link** such as `https://archive.org/details/some-item`, or just the item name.
- **Full file list** with a large checkbox for every file, plus file sizes.
- **Folders are supported.** Folders in the archive show as expandable groups with big **+ / -** buttons. Ticking a folder ticks everything inside it, and a folder that is only partly ticked shows a dash. The same folder structure is recreated in your save location.
- **Select all**, **Expand all** and **Collapse all** buttons.
- **Select by type.** Open the *File types* dropdown and tick as many as you like: `mp3, wav, flac, mp4, mpg, avi, png, jpg, zip, rar, 7z`. Only matching files stay ticked. Type any other extensions into the *Other* box, separated by commas (for example `pdf, epub, txt`), then press **Apply** or Enter. **Clear** removes the filter and selects everything again.
- **Save location** defaults to a folder named after the archive item (inside your Downloads folder). Use **Browse...** to choose somewhere else.
- **Progress window** appears in the middle of the app and shows the current file, its progress, and the overall total with speed and time remaining. It has a Cancel button.
- **Resumable.** Interrupted downloads continue from where they stopped, files that are already complete are skipped, and failed files are retried 3 times.
- **Dark mode** checkbox at the top right. Your choice is remembered.
- **Help menu:** *About* (credits, the open-source software used, and a PayPal donation button) and *Donate*. If you enjoy the program, please consider a donation.
- **Automatic updates.** On start-up the app checks this project's GitHub releases. If a newer version exists it shows what's new and, if you click **Install update**, downloads it, replaces itself and restarts. Turn the start-up check off any time with **Help > Check for updates automatically**, or check by hand with **Help > Check for updates now**. Nothing is installed without your click. Updates only install in the packaged app (running the `.py` file just opens the release page).
- Alternating row shading and large, easy-to-read text. To change the size, edit `BASE_FONT_SIZE` near the top of `archive_downloader.py`.

## Using the packaged app

Download the build for your system from the project's **Releases** page, unzip it and run `InternetArchiveDownloadManager`.

| System  | File                                              |
|---------|---------------------------------------------------|
| Windows | `InternetArchiveDownloadManager-Windows.zip`      |
| macOS   | `InternetArchiveDownloadManager-macOS.zip`        |
| Linux   | `InternetArchiveDownloadManager-Linux.tar.gz`     |

The builds are not code-signed, so Windows SmartScreen or macOS Gatekeeper may warn you the first time. On Windows choose *More info > Run anyway*. On macOS right-click the app and choose *Open*.

## Running from source

Needs Python 3.8 or newer with tkinter (included in the standard Windows and macOS installers; on Linux install `python3-tk`).

```
python archive_downloader.py
```

No other packages are required.

## Building the app yourself

**On Windows:** put these files in one folder: `archive_downloader.py`, `icon.png`, `make_icons.py` and `build_exe.bat`. Double-click `build_exe.bat`. The result is `dist\InternetArchiveDownloadManager.exe`.

**With GitHub Actions (Windows, macOS and Linux):**

1. Put the files in a repo, with `build.yml` at `.github/workflows/build.yml`.
2. Either push a tag such as `v1.0.0`, or open the **Actions** tab and run *Build apps* manually (you can type a release tag, or leave it blank for a build number).
3. When it finishes, a **draft release** is created containing the three downloads. Review it, then publish.

The workflow stamps the release version (from the tag, e.g. `v1.2.0` becomes `1.2.0`) and your repository name into the app, so it knows what version it is and where to look for updates. The update check only sees **published** releases: open the draft the workflow creates and click *Publish release*. Each new tag must be higher than the last one.

If you build by hand instead of with the workflow, set `GITHUB_REPO = "your-name/your-repo"` (and bump `VERSION`) near the top of `archive_downloader.py`; with `GITHUB_REPO` empty, update checks are switched off.

The workflow builds with PyInstaller. `make_icons.py` converts `icon.png` into the `.ico` and `.icns` files used for the program icon.

## Files in this project

| File                  | Purpose                                              |
|-----------------------|------------------------------------------------------|
| `archive_downloader.py` | The application                                    |
| `icon.png`            | App icon (also used for the window icon)             |
| `make_icons.py`       | Makes `icon.ico` / `icon.icns` from `icon.png`       |
| `build_exe.bat`       | One-click Windows build                              |
| `.github/workflows/build.yml` | Automatic builds and draft release           |

## Notes

- The Open Directory tab only reads ordinary HTML file listings. Sites that require a login, or that build their listing with JavaScript, won't work.

- Files are downloaded straight from archive.org. Some items are restricted or "darkened" and cannot be downloaded without an account; those will show an error in the failed list.
- Please be considerate: archive.org is a free service run by a non-profit.
- Settings (dark mode, automatic updates) are stored in `.internet_archive_download_manager.json` in your home folder.
