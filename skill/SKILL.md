---
name: kling-studio
description: After a script or shot list is final, build the ready-to-paste master prompt for the Kling Studio app — gathers the per-clip image prompts and motion prompts from the conversation, decides whether reference images are needed, asks only for a batch name, and outputs one copy-paste block. Use when the user says "master prompt", "kling studio", "handoff", "package this for the app", "build the kling prompt", or asks how to send finished shots off for frames and video.
---

# Master prompt → Kling Studio

Turns a finished script or shot list into ONE copy-paste block the user drops into
the Kling Studio app, which draws every frame with Nano Banana 2 and then animates
it with Kling 3.0.

This skill NEVER generates images or video, never calls kie.ai, never spends
credits. It only assembles text.

Style: no narration, no preamble. Ask the one question, then output the block.

## Step 1 — Gather from the conversation (don't ask)

Pull these from earlier in the chat. Only ask if genuinely missing:

- **Image prompts** — one per clip, in cut order: what the still frame shows.
  Written as a photograph, not as an action: subject, framing, light, setting.
- **Motion prompts** — one per clip, in the same order: what moves in the video.
  Camera behaviour plus the movement in the shot.
- **References** — any avatar, product, or style image already used or named in the
  conversation, and its file path if one exists on disk.
- **Settings** — default 9:16 · 5s · pro · sound off, frames at 1K. Carry over
  anything the user changed earlier in the conversation.
- **Per-clip lengths** — if the conversation measured a narration line per clip, or
  the script names clip lengths, keep them: a clip can run at its own length.
- **An anchor** — a description of the character the batch locks onto, if one exists.

If the conversation only has motion prompts (an older Polaris-style session), write
the image prompts from the frame descriptions in the script. Say in one line that you
did, so the user can check them.

## Step 2 — Decide references, per clip

Only ask for a reference when the batch actually needs one:

- **A person, product or place recurs across clips** → the batch needs one identity.
  - A file exists (path known in the conversation) → put it under `references:`.
  - No file exists → mark **clip 1** as the anchor and give every other clip that
    shows the same subject `ref: frame 1`. The app draws frame 1 first and reuses it,
    so the face or product stays the same without any upload.
  - The user must supply it (their own avatar, a real product photo) → put
    `ref: needed: <short description>` on the clips that need it. The app pops a
    window for exactly those clips and waits.
- **Every clip is its own scene, nothing recurs** → no `references:` line, no `ref:`
  lines. Don't add friction that isn't needed.

Never invent a file path. If you aren't sure a path is real, use `needed:` instead.

## Step 3 — Ask ONE question

Use AskUserQuestion (or one short line) to ask only for the **batch name** (short
slug, e.g. `c150`, `sci1`, `gym4`). Suggest a sensible default drawn from the
script or angle being worked on.

Do not ask about anything else. If something else is genuinely missing (no motion
prompts written yet), ask for that in the same single turn.

## Step 4 — Check before emitting

- **Image prompt count must equal motion prompt count.** If they differ, say so
  plainly, show both numbers, and stop. Do not pad, invent, or trim prompts.
- **Cut order, always.** The app pairs by position and names clips
  `<batch>_clip01…NN` in that order.
- **A clip that only recuts existing footage** has no frame: leave it out.

## Step 5 — Output the block

Output exactly one fenced code block, nothing else inside it, starting on the
`batch:` line. Do NOT prepend a slash command.

```
batch: <slug>

anchor: <the character the batch locks onto, only when one recurs>

references:
<full path to an image on disk, one per line, only when a real file exists>

1.
image: <what the still shows>
motion: <what moves>
duration: <seconds, only when this clip differs from the batch>
end: next

2.
image: <...>
motion: <...>
ref: frame 1
```

Rules for the block:

- **`image:` is the still, `motion:` is the movement.** Never mix them: an image
  prompt describing motion wastes a frame, a motion prompt describing the scene
  wastes a video.
- Prompts are copied VERBATIM from the conversation when they already exist. Never
  reword, trim, merge or "improve" them.
- Add `ref:` only on the clips that need it (`frame N`, a real path, or
  `needed: <description>`).
- **`anchor:`** is the character description, written once above the clips. The app
  drops it into its Draw-a-reference box, so the user can draw the character without
  retyping it. Only when a character actually recurs.
- **`duration:`** gives one clip its own length in seconds — use it when a clip has to
  land on a measured line of narration, and leave it off otherwise so the clip takes
  the batch's. Kling renders 3–15 seconds; Veo renders 4, 6 or 8 and snaps anything
  else to the nearest.
- **`end: next`** makes a clip finish on the picture the following clip opens with, so
  the two join with no visible cut. `end: frame N` names a specific one. Use it for a
  continuous camera journey; leave it off when the ad is meant to cut. The last clip
  has no successor, so it takes no `end`.
- **Veo instead of Kling** — say `veo` in the `settings:` line when a character has to
  speak on camera, because Kling cannot lip-sync. Veo is billed per clip (65 credits at
  its Fast · 1080p default), takes 4/6/8-second clips, and has no mode or sound setting.
- Add a `settings:` line only when something differs from 9:16 · 5s · pro · sound
  off · 1K, and then state only what changed. Per-clip notes that are not settings
  (motion strength, "low motion", stylistic reminders) belong INSIDE that clip's
  prompt text.
- No file names, no frame names, no `—` prefixes inside prompt lines.

## Step 6 — One line after the block

After the code block, at most two short lines:

- clip count and the credits. Frames are priced by resolution — **8 credits at 1K**,
  12 at 2K, 18 at 4K — and Kling video by the second: **18/s in pro** (90 for a 5s
  clip), 14/s in std, 27/s with sound on. Give both numbers and the total, e.g.
  “6 clips — 48 credits of frames + 540 of video, about 588 in all.” Add up per-clip
  lengths rather than multiplying, if the clips differ.
- The app corrects those estimates to kie.ai's own `creditsConsumed` once each job
  finishes, so a slightly stale number is a wrong quote, never a wrong total.
- if any clip uses `needed:`, one line naming which clips will ask for a picture.

Nothing else. No explanation of the app, no next-step lecture.

## Rules

- Never emit a slash command line. The block's first line is always `batch:`.
- Never hand-assemble URLs or file paths.
- Never suggest running anything automatically: the user pastes the block, presses
  Generate frames, checks them, then presses Generate videos. Each step asks for
  confirmation in the app.
- If the user asks for a different target (raw job JSON, for example), output that
  instead with the same content.
