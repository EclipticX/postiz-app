# Local AI Video Studio

Studio runs alongside Postiz. Use the navigation to open Idea Inbox, Video Workspace, Published Posts, or Settings. Studio plans 30-second vertical shorts at 720 × 1280 using 4- and 5-second scenes. SQLite stores approval decisions, prompt schedules, jobs, and their progress. Media files stay in the local Studio data directory.

## Start locally

From this folder, run `docker compose up --build`, then open [http://localhost:8090](http://localhost:8090). Copy `.env.example` to `.env` to change the published port or Postiz URL. Compose keeps Studio data in the `studio-data` volume.

For a native development run, use Python 3.9 or later and run `python3 -m app.server` from this folder. The server binds to `127.0.0.1` by default and saves data in `data/`. Set `STUDIO_DATA_DIR` to use a different directory. Stop Studio before copying its data directory for backup.

## Configure production

- Add the Claude, fal.ai, EvoLink, and Postiz API keys you have in **Settings**. Studio writes its own private `.env` inside the data directory and uses updated credentials for new jobs.
- Set the daily and per-video budget and enter the estimated cost for each paid route you enable. A zero limit or unconfigured rate holds paid requests. The guard uses estimates where provider billing is unavailable. Daily spend rolls over in `Asia/Dhaka`.
- Enable Seedance or Kling with its credential and rate to render 5-second scenes. The configured Veo route handles 4-second scenes.
- For native media generation, install FFmpeg with `ffprobe` and subtitle support, `rsvg-convert`, and the Python dependencies from `requirements.txt`. The Dockerfile installs these tools.
- Provide a local CTranslate2 Whisper model for spoken captions. Set `STUDIO_WHISPER_MODEL_PATH` to that directory for a native run. In Compose, copy the model into `/studio/data/whisper-model` inside the Studio container. Studio reads that directory offline. Optionally place a licensed `music.mp3` in the Studio data directory.
- Run Postiz and connect channels there first. The native default is `http://127.0.0.1:4007`. The Compose default reaches the host Postiz port from the Studio container.

## Set up assets and presets

1. Open **Settings → Asset Library** and create a character, place, or item with a stable ID and fixed description. Create separate entries for characters that look alike.
2. Upload PNG or JPEG references one at a time. Label the angles and pick a primary image for each appearance. Add an outfit or color variant when needed and assign its images to that variant.
3. Add character voice styles using an ElevenLabs voice name or ID, speed, and optional expressiveness. Mark one style as the character's default.
4. In **Settings → Production Presets**, save a visual style and palette, script tone, subtitle style, and audio presets. Audio accepts local MP3 or WAV files for looping background music and one opening sound effect. Mark the presets to use by default for new videos.
5. Use **Video Workspace → Production Options** to choose a different preset, appearance, or character voice for one video. Saving a change after planning starts a new plan and requires fresh keyframe approval. The planner saves the selected assets and defaults with each plan.

Uploaded assets and audio stay in the persistent Studio data directory. SQLite stores references, labels, variants, and presets. Studio upgrades older databases automatically. Before switching back to an older Studio version, restore the data-directory backup from before the upgrade.

## Try the inbox and scheduler

1. Open **Idea Inbox → Create** to add an idea, request ideas from Claude, or create a prompt schedule. Use the inbox filters to find a record and select its index to open the full details. **Approve** creates a job in Video Workspace. **Later** keeps an idea available for a future decision.
2. To create a prompt schedule, enter a title, paste one prompt per line, and choose a start date, daily time, timezone, and number of videos to start each day.
3. Open the draft batch by its index and select **Approve and activate**. Studio queues the next prompts when the local clock reaches the production start time. Keep Studio running at the scheduled time. After a restart, Studio resumes on the next eligible run and preserves the daily limit. The inbox **Publish date** shows the earliest confirmed Postiz schedule linked to the record. Delete moves an unapproved record to the **Deleted** status filter, where it can be restored.

4. In **Video Workspace**, review and approve the generated keyframes before the video worker starts. A scheduled prompt batch also waits at this step. Review the finished preview and visual QA, or use **Revise** to send feedback through the planner. Revised plans require fresh keyframe approval.
5. Load connected Postiz channels in Video Workspace. Review the caption, title, and hashtags and choose **Approve draft** for any supported channel or **Approve public YouTube schedule** for YouTube. Facebook and Instagram local-file publishing paths and TikTok direct-post settings are tracked in the project plan.

Open **Published Posts** after Studio confirms a publication from Postiz analytics. Select a video index to see the delivered video if it is saved locally, the selected channel's caption, and up to 14 saved analytics snapshots. **Open post** looks up the platform's original link in Postiz. Dates use the stored scheduled release time. Engagement counts likes plus comments on channels that report both. Missing metrics, including post-level follows, appear as `—`.

Follow the [project plan](../project%20plan.md) for checklist and live verification status.

## Run tests

```sh
python3 -m unittest discover -s tests -v
```

Provider requests use fixtures in the tests. FFmpeg tests render local media fixtures. The HTTP tests bind a temporary loopback port.
