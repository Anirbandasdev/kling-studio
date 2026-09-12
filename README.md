# Kling Studio (v3)

Paste one master prompt, get frames, check them, get videos. Frames are drawn by
**Nano Banana 2** and animated by **Kling 3.0**, both on kie.ai. Polaris isn't
involved anywhere.

v3 runs completely separately from v2: its own folder, its own `.exe`, its own
settings (`%APPDATA%\KlingStudio`) and its own batches. v2 keeps working, and old
v2 batches stay in v2.

## The flow

1. **Claude writes the master prompt.** With the script final, run `/kling-studio`
   in the Claude chat and copy the block it prints (see `skill/SKILL.md`).
2. **Paste it** into the app. It reads the batch name, every image prompt, every
   motion prompt and any reference instructions.
3. **Generate frames.** Any clip that needs a reference picture asks for one first,
   in its own popup. Then the credits are confirmed once, and the frames are drawn.
4. **Check the frames.** Edit a prompt, redraw a single frame, or drop in your own
   picture for any clip.
5. **Generate videos.** Confirmed separately, and only frames that exist can be
   animated.

## Installing it for a coworker

The app opens in its own window using **Microsoft Edge**, which is built into
Windows 10 and 11 (Chrome also works).

**Option A: one `.exe`.** Build it once on your PC:

```bash
python -m pip install pyinstaller
build_exe.bat
```

Send them `dist\Kling Studio.exe`, or build `installer.iss` with Inno Setup for a
proper installer with a Start Menu shortcut.

**Option B: run from Python.** Python 3.9+ from python.org, copy this folder,
double-click `Kling Studio.pyw`. Nothing else to install.

## First launch

A new user gets a short built-in walkthrough — seven cards covering the kie.ai API key
and what things cost, where the master prompt comes from (`/kling-studio` in Claude,
already installed — nothing to set up), what `image:`, `motion:` and `ref:` do, both
generate steps, and how to fix a bad frame. **Load an example** on the last card fills the paste box with
a working master prompt so they can see the app read it back without spending
anything. It appears once per computer; **How it works** in the bottom-left corner
reopens it any time — six cards with a clickable step rail down the side, so anyone
can jump straight to the bit they forgot.

On first launch the app asks for a **kie.ai API key**. Each person uses their own
key, so credits are billed to their own account. The key is saved only on that PC
and is never shown again (Settings shows the last 4 characters).

## Shipping an update

Your coworker's copy checks a manifest you publish and offers them an Install button.
You never touch their PC.

```bash
publish.bat 3.1.0 --notes "Frames retry on a stalled download" "Tour covers the API key"
```

That bumps the version in `app.py` and `installer.iss`, builds the exe, writes
`dist\release\latest.json` with the SHA-256, and creates the GitHub release with both
files attached (needs the `gh` CLI: `winget install GitHub.cli`, then `gh auth login`).
Without `gh` it prints exactly which two files to upload by hand.

On their side: the app checks 25 seconds after launch and every six hours, or straight
away with **Settings → Updates → Check now**. A green chip appears above the sidebar
buttons; clicking it shows the notes and **Install and restart**. It downloads, refuses
anything whose hash doesn't match the manifest, then a small script waits for the app to
close, swaps the exe (keeping the old one as `.bak.exe`) and relaunches. No admin rights
are needed because the app installs under `%LOCALAPPDATA%`, and an update is never
applied while a batch is running.

Two things worth knowing: whoever can write to that release can ship code to that PC, so
keep the repo yours; and an unsigned exe may show a SmartScreen warning on first run
until the build earns reputation (a code-signing certificate is the only real fix).
The repo is set with `--repo` on `publish.bat`, or once in `APP_REPO` in `updater.py`.

## The master prompt

```
batch: c150
settings: 9:16 · 5s · pro · sound off · 2K

references:
G:\avatars\meera.png

1.
image: wide shot of a diya burning on a dark wooden table, warm rim light
motion: the flame flickers and dances, wisps of smoke sway

2.
image: close up of a steaming cup of coffee on the same table
motion: steam rises and curls slowly
ref: frame 1
```

| Line | What it does |
|------|--------------|
| `batch:` | Names the batch, the folder and every clip (`c150_clip01`…) |
| `settings:` | Optional. Only what differs from 9:16 · 5s · pro · sound off · 2K |
| `references:` | Optional. Real file paths, used for every frame in the batch |
| `image:` | Draws the still with Nano Banana 2 |
| `motion:` | Animates that still with Kling 3.0 |
| `ref: frame 1` | Draws frame 1 first, then uses it as the reference, so faces and products stay consistent |
| `ref: needed: …` | The app pops a window for that clip and waits for a picture |

Two numbered lists (`image prompts:` then `motion prompts:`) work just as well.
Anything the parser can't place is listed as an ignored line instead of being
dropped silently.

## Reviewing and fixing

Every clip card carries both stages and its own buttons:

| Button | What it does | Costs credits? |
|--------|--------------|----------------|
| **Prompts** | Edit the image and motion prompt | No |
| **Frame** | Redraw this frame, with the prompt open for a tweak first | Yes, one frame |
| **Video** | Re-render this video | Yes, one video |
| **Use my picture** | Replace the frame with your own image (drag and drop works too) | No |
| **Play** | Watch the finished clip | No |

The big button always shows the next step for the whole batch: add missing
references, generate frames, check frames, generate videos, or check renders.
Checking is free; anything that spends credits asks first.

## When something fails

| On the card | What to do | Costs credits? |
|-------------|------------|----------------|
| kie.ai failed it | **Frame** or **Video** on that card, after editing the prompt if you like | Yes, that one clip |
| Timed out, Download failed, Check failed | The big button offers a free re-check | No |
| Couldn't send | The big button sends it again | Yes, it was never sent |
| Needs a reference | Add the picture in the popup, or reuse another clip's frame | No |
| Reference missing | The frame it copies from never arrived: fix that frame first | No |

A rejected API key or an empty balance stops the run immediately, keeps whatever was
already paid for, and says so on the batch page.

## Where files go

The output folder (default `Documents\Kling Studio`, changeable in Settings):

```
<batch>/
  job.json               name, settings, clips with both prompts and references
  state.json             per clip: frame stage and video stage
  frames/<clip>.png      the stills, drawn or dropped in
  videos/<clip>.mp4      the finished clips
  refs/                  reference pictures you attached
  responses/             raw kie.ai response for anything that failed
  activity.log           everything shown under Activity
```

## Credits

- **Frames**: 12 credits per Nano Banana 2 image. Filled in by default; change it in
  **Settings → Credits per frame** if kie.ai moves the price.
- **Videos**: 18 credits per second in Pro, so 90 for a 5-second clip.
- So a 6-clip batch is about **72 credits of frames + 540 of video = 612**, and every
  confirmation shows the total before anything is sent.

## Known caveats

- **Nothing has been run against the real kie.ai yet.** The whole pipeline was
  tested against a fake. Do one clip for real first.
- **Result URLs are searched, not assumed.** If a clip misbehaves, send
  `responses/<clip>.<stage>.json` and `activity.log` from the batch folder.
- **Unverified options:** 9:16 · 5s · Pro · sound off (video) and 2K · 9:16 (frames)
  are the verified combination. Other aspect ratios and resolutions come straight
  from kie.ai's docs but haven't been run.
- **v2 batches don't open in v3.** The job format is different. v2 stays installed.

## Screens

The window works down to a phone-sized width: under about 920px the sidebar becomes a
top bar with a horizontal strip of batches, the clip grid drops to two columns, and the
dialogs and the walkthrough go full-width.

## How it works

| File | What it is |
|------|------------|
| `paste.py` | Reads the master prompt |
| `updater.py` | Reads the manifest, verifies the download, swaps the exe |
| `publish.bat`, `tools/publish.py` | Your side: build, hash, tag, upload |
| `pipeline.py` | kie.ai calls, batch folders, and the two-stage runner |
| `app.py` | Local server on `127.0.0.1` with a per-launch token; opens the window |
| `ui/index.html` | The whole UI in one file |
| `ui/logo.png`, `ui/mark.png`, `ui/app.ico` | The brand lockup, the square mark and the exe icon, all cut from `logo.png` |
| `skill/SKILL.md` | The skill that writes the master prompt |
| `Kling Studio.pyw` | Double-click launcher |
| `build_exe.bat`, `build_mac.sh`, `installer.iss` | Packaging |
| `tests/` | Offline tests with a fake kie.ai; they spend nothing |

Standard library only (Python 3.9+). To run the tests from this folder:

```bash
python -X utf8 -m unittest discover -s tests -v
```

To run the server without a window, for debugging:

```bash
python app.py --no-window --port 8766
```
