# Internet Archive Download Manager

A small desktop app for downloading files from an [archive.org](https://archive.org) item. Paste a link, tick the files you want, pick a folder and download. No Python is needed to run the packaged version.

![icon](icon.png)

## Features

- **Paste any item link** such as `https://archive.org/details/some-item`, or just the item name.
- **Full file list** with a large checkbox for every file, plus file sizes.
- **Folders are supported.** Folders in the archive show as expandable groups with big **+ / -** buttons. Ticking a folder ticks everything inside it, and a folder that is only partly ticked shows a dash. The same folder structure is recreated in your save location.
- **Select all**, **Expand all** and **Collapse all** buttons.
- **Select by type.** Open the *File types* dropdown and tick as many as you like: `mp3, wav, flac, mp4, mpg, avi, png, jpg, zip, rar, 7z`. Only matching files stay ticked. Type any other extensions into the *Other* box, separated by commas (for example `pdf, epub, txt`), then press **Apply** or Enter. **Clear** removes the filter and selects everything again.
- **Save location** defaults to a folder named after the archive item (inside your Downloads folder). Use **Browse...** to choose somewhere else.
- **Progress window** appears in the middle of the app and shows the current file, its progress, and the overall total with speed and time remaining. It has a Cancel button.
- **Resumable.** Interrupted downloads continue from where they stopped, files that are already complete are skipped, and failed files are retried 3 times.
- **Dark mode** checkbox at the top right. Your choice is remembered.
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

- Files are downloaded straight from archive.org. Some items are restricted or "darkened" and cannot be downloaded without an account; those will show an error in the failed list.
- Please be considerate: archive.org is a free service run by a non-profit.
- Settings (dark mode) are stored in `.internet_archive_download_manager.json` in your home folder.
