# Local AI Video Studio project plan

Use `[x]` for work that meets its acceptance check. The existing Postiz checklist records features verified in source. Check off Studio features after their relevant tests pass, and check off live integrations after a connected-account test. Record the test or evidence next to each completed item.

## Agreed scope

- Start with one niche, kids' 3D-style animated shorts. Store each niche's style, rules, assets, prompts, and examples in its own profile so future niches use the same pipeline.
- Run Studio on localhost with Docker Compose, Python, SQLite, and FFmpeg. Keep projects and final media on disk. Enter generation provider and Postiz API keys in Studio Settings. The backend persists them in a local `.env` file mounted into Studio.
- Produce 30-second, 720 × 1280 vertical MP4s with 4- or 5-second scenes. Use a 4-second clip for the quality pilot so both video routes can animate the same keyframe and prompt.
- Require idea approval before production, keyframe approval before generating paid clips, and final approval before scheduling or publishing.
- Use three Studio screens. Idea Inbox holds generated ideas and Prompt Schedule. Video Workspace holds storyboard, keyframe review, preview, revisions, publishing, and per-video analytics. Settings holds credentials, provider controls, budgets, the idea-generation schedule, and the Asset Library.
- Start with one primary video route, Veo 3.1 via fal.ai, and one independent backup, Seedance via EvoLink. Rank the routes using the pilot output for our characters. Use Claude Sonnet for writing and visual QA, Nano Banana Pro via fal.ai for keyframes, and ElevenLabs via fal.ai for voice.
- Publish through Postiz to YouTube Shorts, Facebook Page Reels, Instagram Reels, and TikTok. Reuse the same master video with channel-specific metadata.

## Existing Postiz building blocks

Source verified on `warg` at `bd8353f0`.

- [x] Self-hosted Postiz Compose configuration and a local upload volume. [Compose](docker-compose.yaml)
- [x] Public API for upload, channel discovery, drafts, scheduling, and per-post analytics. [Public API controller](apps/backend/src/public-api/routes/v1/public.integrations.controller.ts)
- [x] YouTube resumable video upload and Made for Kids setting. [YouTube adapter](libraries/nestjs-libraries/src/integrations/social/youtube.provider.ts#L587)
- [x] Standard TikTok video file upload. [TikTok adapter](libraries/nestjs-libraries/src/integrations/social/tiktok.provider.ts#L840)
- [x] Facebook and Instagram video publishing paths that send a media URL. [Facebook adapter](libraries/nestjs-libraries/src/integrations/social/facebook.provider.ts#L827) · [Instagram adapter](libraries/nestjs-libraries/src/integrations/social/instagram.provider.ts#L788)
- [x] Existing Seedance via EvoLink and ElevenLabs examples in the Postiz media flow. The Seedance example requests 8-second clips. [Seedance](libraries/nestjs-libraries/src/videos/seedance/seedance.ts#L38) · [ElevenLabs](libraries/nestjs-libraries/src/videos/images-slides/images.slides.ts#L105)

## Studio build checklist

Native startup served all three Studio screens. All 103 tests passed, including localhost asset uploads and Workspace overrides, mocked provider and Postiz flows, and native FFmpeg fixture rendering. Docker CLI was unavailable in the audited shell. Compose startup and authenticated integration tests are pending.

### 1. Local foundation and idea inbox

- [x] Create the `video-studio/` foundation with a Python app, web UI, niche profiles, tests, `Dockerfile`, `docker-compose.yml`, `.env.example`, and `README.md`. Verified by the native HTTP and SQLite test suite. Pipeline and adapters are tracked in the following sections.
- [ ] Bind the Studio UI to localhost and persist SQLite and media across Compose restarts.
- [x] Add the initial SQLite migration for `ideas`, `jobs`, `assets`, `providers`, `memory`, `analytics`, and `niches`. Verified with fresh and reopened databases.
- [x] Persist production stages, approval states, attempts, and spend. Verified restart recovery and reuse of saved provider task IDs.
- [x] Seed the kids' 3D profile with style guidance, audience and brand rules, sample prompts, and an asset library of characters, places, and items. Verified profile loading, SVG reference paths, and idempotent seeding.
- [x] Add manual trend input and daily or weekly idea schedules. Fixture tests verify scheduled dispatch and generated ideas waiting in the inbox.
- [x] Write the Claude idea prompt using trends, niche rules, and Memory. Fixture tests verify this context and ready-to-use prompts.
- [x] Build the idea inbox with **Accept**, **Reject**, and **Later**. Deferred ideas can be accepted anytime. Verified via localhost HTTP and duplicate-accept tests.
- [x] Test the manual approval flow and prompt scheduling with fixtures. Verified that accepting an idea twice creates one job and restarting preserves the queue.

#### Asset Library and production presets

- [x] Add an Asset Library section to Settings for creating characters, places, and items with stable IDs and fixed descriptions. Tested separate entries for look-alike characters and localhost form submission.
- [x] Allow multiple PNG/JPG references per appearance, each with an angle label and one primary selection. Verified persisted paths and metadata, concurrent uploads, and rejection of invalid images.
- [x] Save character outfit and color variants and named voice styles with a default, speed, and optional expressiveness. Verified that the Mother and Grandpa fixture selects each speaker's own voice and references.
- [x] Save niche defaults for visual style and palette, script tone, subtitle style, and music/opening SFX. Allow per-video overrides in Video Workspace. Verified selected defaults are stored with the plan, and native FFmpeg tests render subtitle styling and mix the audio.
- [x] Use each scene's asset IDs and camera framing to select a primary and relevant angle references. Verified multi-character prompt labels, preservation of bundled single-image assets and legacy narration, and same-keyframe video fallback.

#### Prompt Schedule

- [x] Add a **Prompt Schedule** section to Idea Inbox. Review a batch of up to 100 pasted prompts, then set a start date, daily time, timezone, and daily count. Verified with 100 stored prompts and the HTTP form.
- [x] Use **Activate Schedule** to approve the batch and enqueue due prompts as production jobs. Show progress counts and support pause and resume. Verified that draft schedules cannot dispatch and paused schedules stay paused.
- [x] Persist prompt schedule progress in SQLite. Verified that a 100-prompt batch set to one video per day queues one job per local day, resumes after a restart, and prevents duplicate dispatch and excess daily jobs.
- [x] Connect scheduled jobs to the planner. Fixture tests verify budget stops and daily production limits when a backlog resumes.

Keyframe approval is an open decision for scheduled batches. The current approval rule queues planning and keyframes automatically, then waits for manual keyframe approval before paid clip generation. Unattended generation would require approval for an opt-in rule that passes only keyframes that meet visual QA. Final publishing requires its own approval in either case.

### 2. Providers, budget, and scene planning

- [x] Define a shared 720p-default provider request with prompt, optional reference media, and duration. Verified the LLM, image, video, and TTS mappings with transport fixtures.
- [x] Add the provider registry with credential references, priority, rate and billing unit, and model capabilities. Verified registry seeding and updates.
- [x] Build Settings for adding, replacing, and removing provider and Postiz API keys. Localhost tests verify masking. Storage tests verify restricted `.env` permissions and persistence.
- [x] Add provider on/off and priority controls and daily/per-video budget settings. Verified route eligibility and configuration persistence.
- [x] Implement Claude, generic fal.ai, and EvoLink adapters. Mocked transport tests cover Claude structured output, Nano Banana Pro, Veo, Kling, ElevenLabs, and Seedance polling.
- [x] Filter by key, model capability, budget, and priority. Production fixtures verify failover to EvoLink with the same approved keyframe.
- [x] Reserve estimated spend before paid calls, including parallel work. Tests verify both limits, reservation reconciliation, and daily rollover. Production uses conservative configured estimates where actual billed cost is unavailable.
- [x] Write the Claude planner prompt with niche style, examples, and assets. Verified the structured scene contract and prompt inputs.
- [x] Add the scene JSON schema and validator. Tests verify asset IDs, unique scene IDs, exact duration, and a bounded correction loop.
- [x] Generate and review keyframes before starting scene clips. Production fixtures verify the manual gate and the keyframe passed to fallback.
- [ ] Verify Claude, fal.ai, and EvoLink with real API credentials and confirm billed-cost behavior.

### 3. Workers, render, QA, and preview

- [x] Run scene video and voice work in parallel using durable SQLite tasks. Render and subtitle timing begin when scene media is ready.
- [x] Implement fixed narration, optional licensed local music, and SRT from approved text and word timings. Render fixtures verify timing and mixing. A local Whisper model is required for real spoken audio.
- [x] Compose and verify a 30-second, 720 × 1280 MP4 with FFmpeg and `ffprobe`. The native fixture uses seven scenes and exercises joining, audio mix, burned captions, and stored media.
- [x] Write the Claude vision QA prompt and implement sampled-frame checks, preview routing, human review, and eligible scene retries. Prompt and production fixtures verify the QA handoff and preview result.
- [x] Surface job errors, persist provider task IDs, and cap paid retries per failed scene task. Tests verify confirmed fallback and restart-safe polling.
- [x] Build the Video Workspace with preview playback, QA results, and **Revise**. A revision replans the job and returns to the keyframe approval gate. The workspace can retry a failed local render using saved scene media.
- [ ] Run a live 4-second pilot on both video routes, set priority from visual quality, and verify local Whisper on actual narration.

### 4. Four-platform publishing and analytics

- [x] Add Postiz handoff for drafts on all four platforms and YouTube scheduling. Fixtures verify upload and post payloads, per-channel metadata, and saved IDs.
- [x] Require final approval before Postiz handoff. Tests verify that repeated approval and uncertain responses cannot create duplicate posts.
- [ ] Verify YouTube Shorts upload and scheduling with a connected channel, including the Made for Kids setting.
- [ ] Add TikTok Direct Post account-specific privacy and consent controls, then verify scheduling with a connected account. The current Studio handoff creates Postiz drafts. [TikTok guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
- [ ] Add binary upload for local MP4s to the Postiz Facebook Page Reels path and verify with a connected Page. [Meta local Reel upload](https://www.postman.com/meta/facebook/request/ps1ahe4/2-a-upload-local-reel)
- [ ] Add resumable upload for local MP4s to the Postiz Instagram Reels path and verify with a connected account. [Meta resumable-upload sample](https://github.com/fbsamples/reels_publishing_apis/blob/main/insta_reels_publishing_api_sample/README.md)
- [x] Implement scheduled collection and normalize available engagement metrics. Fixtures verify that missing metrics stay distinct from zero values.
- [ ] Verify live analytics with connected accounts and identify a data source for per-video retention.
- [x] Compare measured engagement by topic, hook, length, and style. Tests verify idempotent niche Memory and that Memory reaches future idea prompts.

### 5. Setup and end-to-end verification

- [x] Document Compose and native setup, Settings credentials, budget limits, local media requirements, Postiz handoff, test commands, and data backup in the Studio README.
- [ ] Verify a fresh Compose start after Docker is available. Test restart recovery and run the full path from idea approval to a local preview.
- [ ] Verify one approved test video through each connected platform and record the publishing and analytics results.
