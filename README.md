# Nimroo

A cross-platform, BBB-style exporter for Nima online-class replays.

Nimroo connects directly to the authenticated Nima replay service, downloads the original audio and timed whiteboard events, reconstructs the class, and exports it as an MP4 file.

It does **not** screen-record the browser.

## Why does this exist?

Because watching a Nima replay should not feel like a punishment for missing the class.

Seeking forward should not randomly kill the audio, refreshing the page should not be part of the playback controls, and replaying a lecture should not require the patience of a network engineer debugging production at 3 AM.

Nimroo exists because I hated the Nima replay experience enough to reconstruct the entire class myself.

## Features

* Exports Nima replays directly to MP4
* Downloads the original recorded audio
* Reconstructs timed whiteboard events
* Preserves whiteboard page changes and drawings
* Does not record the screen
* Does not require the replay to play in real time
* Reads authentication from an existing browser session
* Supports resumable exports
* Supports Windows, Linux, and macOS

## Supported Platforms

* Windows
* Linux
* macOS

Authentication is read from an existing saved browser session.

`browser-cookie3` supports common browsers across these platforms, and `--browser auto` attempts to locate a usable authenticated browser automatically.

## Requirements

* Python 3.10 or newer
* An existing authenticated Nima browser session
* FFmpeg, unless using a standalone Nimroo executable

## Install From Source

Clone the repository:

```bash
git clone https://github.com/moeinEN/Nimroo.git
cd Nimroo
```

Install Nimroo:

```bash
python -m pip install .
```

## Export a Replay

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" -o lecture.mp4
```

You must already be logged into Nima in a supported browser.

If the browser's cookie database is locked, fully close the browser before running Nimroo.

## Examples

Automatically find an authenticated browser:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE"
```

Choose the output filename:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" -o algorithm-class.mp4
```

Use Firefox authentication:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" --browser firefox
```

Use Chrome authentication:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" --browser chrome
```

Use a Netscape-format cookies file:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" --cookies-file cookies.txt
```

Keep intermediate work files:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" --keep-work
```

Resume an interrupted export:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" --resume-work
```

Restart an export from the beginning:

```bash
nima-export "https://lms.example.edu/room/ROOM_CODE" --force
```

## Work Directory

Intermediate files are stored separately for each replay under:

```text
.nima-export-work/<room-id>
```

These files may include:

* downloaded replay audio
* reconstructed whiteboard frames
* replay event metadata
* temporary FFmpeg files

Use `--keep-work` to preserve them after a successful export.

## How It Works

Nimroo connects directly to Nima's authenticated SockJS/DDP replay service.

It then:

1. Authenticates using an existing saved browser session.
2. Requests the complete recorded-event timeline.
3. Downloads the original recorded audio.
4. Processes whiteboard pages, presenter tabs, and timed whiteboard shapes.
5. Reconstructs the visual replay timeline.
6. Encodes the reconstructed visuals.
7. Merges the visuals and original audio into an MP4 file.

## Build a Standalone Executable

Install Nimroo and PyInstaller on the target operating system:

```bash
python -m pip install . pyinstaller
python scripts/build_standalone.py
```

The generated executable will appear inside:

```text
dist/
```

The standalone build includes the platform-specific FFmpeg binary from `imageio-ffmpeg`, so end users do not need to install Python or FFmpeg separately.

PyInstaller is not a cross-compiler:

* Build the Windows executable on Windows.
* Build the Linux executable on Linux.
* Build the macOS executable on macOS.

The included GitHub Actions workflow can build releases for all three platforms.

## Current replay support

Nimroo currently exports original recorded audio and timed whiteboard content, including drawings and page changes.

Webcam video, screen sharing, shared videos, and other presenter content are not yet supported in the final MP4 output. These streams may be downloaded when present, but are not currently rendered into the exported video.



## Contributions Welcome

If you have access to Nima replay recordings that include screen sharing, webcam video, shared media, or other unsupported content types, contributions are very welcome. Test samples, event metadata, bug reports, and pull requests can all help Nimroo support more replay formats. <3


## Authorization

Nimroo is intended only for exporting recordings that you are authorized to access.

Users are responsible for following their university's policies and applicable copyright rules.
