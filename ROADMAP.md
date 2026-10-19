# Topolearn roadmap

Covers the app (`topolearn_app`), website (`topolearn_frontend`) and backend
(`topolearn_backend`). Rough effort: S = a day or less, M = a few days, L = a
week or more. Order within a section is the suggested order.

## P1 · B2B: company onboarding

- [ ] **Onboarding lessons generated from company docs** (L)
  Companies provide documentation for their internal tools; Topolearn turns it
  into a private tool map in the same format as public tools, and managers
  assign it to employees.
  - **Ingest:** Markdown and PDF uploads first; later connectors for
    Confluence, Notion, Google Drive and Git repositories (READMEs, docs/).
  - **Generate:** lessons in the existing schema (why, body, code, takeaways,
    pitfalls, quiz), prerequisite edges and levels, held to the same clarity
    bar as public lessons. Internal lessons can require public ones (an
    internal deploy tool → Docker, Kubernetes), so new hires only learn the
    public basics they're missing.
  - **Review:** company admins edit and approve before anything is published
    to employees; every lesson links back to its source doc.
  - **Assign and track:** managers assign tools or lessons with due dates and
    see who's done what (absorbs "Teams and classrooms" below).
  - **Keep current:** re-sync when the docs change; flag lessons whose source
    changed.
  - **Foundations:** org accounts and roles (admin, manager, member), strict
    per-org data isolation (a private catalog; company docs never used for
    public content or shared across orgs), SSO (Google Workspace, Microsoft),
    per-seat pricing.

## Content quality (do first)

- [ ] **Lesson clarity review: no unexplained jargon** (L)
  Every lesson must be understandable at its level. Example of the problem:
  Docker → Essentials → "Containers vs virtual machines" opens with
  hypervisor, kernel, drivers, init system and VM, none of them explained.
  1. Automated pass: flag technical terms a lesson uses that its prerequisite
     lessons don't teach and the lesson doesn't define. Essentials first.
  2. Rewrite flagged lessons so Essentials assume no background: define terms
     in plain words on first use, or add a prerequisite lesson.
  3. Per-tool glossary, with tap/hover definitions in the app and on the web,
     adapted to the learner's level.
  4. A `validate_content.py` check that fails on undefined terms in Essentials.
- [ ] **Expand the tool catalog** (L, ongoing)
  Beyond the current 155 tools / ~3,200 lessons. Prioritise by tool-request
  votes, search demand, and prerequisites that point outside the catalog.
  Same clarity bar as above.

## Quick wins (S each)

Help the launch:
- [ ] **"Rate Topolearn" prompt** (app): Google/Apple in-app review dialog at a
  happy moment (7-day streak, finishing a tool's Essentials).
- [ ] **"Was this clear?" on every lesson** (app + web): Clear / Confusing /
  Too easy at the end; "Confusing" asks which word or sentence. Feeds the
  lesson clarity review with real data.
- [ ] **Send feedback** (app + web): Settings item that opens an email to
  support with app version and device filled in.
- [ ] **Links open in the app** (Android App Links, iOS Universal Links):
  `topolearnapp.com/learn/...` opens the lesson in the app when installed.

Conveniences:
- [x] **Collapsible long lesson lists** (web, Oct 2026): Builds on / Unlocks
  show 5 and "Show N more"; app still to do.
- [x] **Copy button on code blocks**: web done (Oct 2026); app still to do.
- [ ] **"Continue where you left off"** card on Home (app + web)
- [ ] **Time-to-finish estimate** per tool (app + web): "About 3 weeks at
  your daily goal."
- [ ] **Private notes per lesson** (app + web), synced.
- [ ] **Keyboard shortcuts on the web**: `→` next lesson, `L` learned, `/`
  search, `?` list.

Make progress feel good:
- [ ] **Activity calendar** on Your learning (app + web): GitHub-style grid.
- [ ] **"Learners of X also learn…"** when a tool is finished (app + web),
  from the existing tool links.
- [ ] **What's new screen** (app): shown once after an update.

Use the map:
- [ ] **"How far is this lesson?"** (app + web): tap a lesson on the map to see
  "4 lessons away" with exactly those lessons highlighted; follow just that
  path instead of the whole level.
- [ ] **Today's plan** on Home (app + web): picks the daily-goal number of
  lessons (next ready lessons + due reviews), so there's nothing to decide.
- [ ] **Save the map as an image or PDF** (web, then app).

Reading comfort:
- [ ] **Read aloud** (app + web): lessons read out with the device's
  built-in text-to-speech.
- [ ] **Text size setting** (app), separate from the system setting.
- [ ] **Takeaways-only view** (app + web) for a quick refresher of a learned
  lesson.

Flexibility:
- [ ] **Rest days** (app + web): chosen days (e.g. weekends) don't break the
  streak.
- [ ] **Pause a tool** (app + web): hides it from Home, keeps progress.

Trust and polish:
- [ ] **"Last updated" date and tool version** on lessons and tool pages
  ("Covers Docker 29 · updated Oct 2026").
- [ ] **"Get the app" banner** on the mobile website (Play Store, later App
  Store).
- [ ] **Download my data** (app + web): progress and notes as a file, from
  Settings.
- [ ] **Invite a friend** link (app + web); pairs with communities.

## Learning experience

- [ ] **Placement test** (M): ~5-minute adaptive quiz per tool; passing a
  lesson's question marks it and everything it builds on as known. Offered in
  onboarding and when adding a tool.
- [ ] **Career roadmaps across tools** (M): goal-based maps (DevOps engineer,
  data analyst, backend developer…) built from the prerequisite graph. Public
  SEO pages on the website, enrollable in app and web.
- [ ] **"Learn X" pulls in only missing prerequisites** (S–M): enrolling in a
  tool includes just the lessons from other tools that it actually requires
  (FastAPI → the needed Python lessons), not whole prerequisite tools.
- [ ] **Runnable code exercises** (L): in-browser/in-app Python (Pyodide), SQL
  (DuckDB-WASM), JavaScript; "fix this / predict the output" per lesson.
- [ ] **AI tutor on lessons** (M): "explain differently / another example /
  why is my answer wrong", grounded in the lesson; extra review questions so
  reviews don't repeat.
- [ ] **Cheat sheet per tool** (S): from lesson takeaways; public web page
  (SEO), print/PDF, linked from the app.

## Community

- [ ] **Community resources per lesson, with voting** (M): a Resources section
  (app + web) where learners submit links (video, article, docs, course) with a
  type and a one-line note, ranked by thumbs up/down. Seed with curated picks.
  Link validation, duplicate detection, rate limits, reporting, auto-hide
  below a score, admin review queue. Top-voted shown on public /learn pages.
- [ ] **Learning communities per tool** (L): communities around a tool or
  topic (app + web). A tool's page lists its active communities; learners join
  one, or create and publish their own. Inside: discussion threads, questions
  with accepted answers, links to lessons, members and their progress on that
  tool. Owners/moderators, community rules, reporting, rate limits, admin
  oversight, reply notifications. Start with one official community per tool;
  user-created ones next. Cohorts are a natural extension ("start Docker
  together in November").
- [ ] **"Suggest an edit" on lessons** (S): builds on reports; admin review.
- [ ] **Version-update lessons** (M): "What changed in Python 3.14 / React
  20" linked into existing maps; notify learners of that tool.

## Engagement and retention

- [ ] **Push notifications** (S–M): FCM streak reminder at a chosen time,
  "N reviews due"; settings toggle.
- [ ] **Streak freeze, achievements, weekly goal** (S)
- [ ] **Transactional emails** (S): welcome, weekly progress digest,
  streak-at-risk; unsubscribe setting (Resend is already wired).
- [ ] **Home-screen widget** (M): streak and next lesson (Android/iOS).

## Growth and sharing

- [ ] **Shareable progress image** (S–M): "I've learned 62% of Docker" as
  the map; share to LinkedIn/X; OG images for public pages.
- [ ] **Public profile page** (S): `topolearnapp.com/u/<name>`, opt-in.
- [ ] **Completion certificates** (M): per tool and depth, verifiable link,
  add to LinkedIn.
- [ ] **Teams and classrooms** (L): a teacher/lead assigns tools and sees
  progress. The company side is part of the P1 B2B project above.
- [ ] **Landing page upgrade** (S–M). Done (Oct 2026): live "Try it" map,
  "Read a lesson now" cards, logo wall with counts. Still to do: stats strip,
  feature sections with screenshots, "Start here" paths, store badges + QR
  code, FAQ, closing call to action, richer footer.

## Website

- [ ] **Web settings** (S–M): change password, change email, link/unlink
  Google/Facebook/Apple.
- [ ] **Cookie consent for EU/UK visitors** (S): banner + Google Consent Mode,
  if European traffic is expected.

## Operations

- [ ] **Admin page** (M): web page gated by `ADMIN_EMAILS`: reports, tool
  requests, community resources queue, user counts and sign-ups.
- [ ] **Monitoring and alerts** (S): Cloud Run uptime check + email alert,
  error log review, nightly `validate_content.py` run.

## Release and platform

- [ ] **Remove the AD_ID permission** (S, next app release):
  `tools:node="remove"` for `com.google.android.gms.permission.AD_ID` and
  `google_analytics_adid_collection_enabled=false`; then answer "No" to
  advertising ID in Play Console.
- [ ] **Enable Facebook and Apple sign-in** (S, after verifications): Facebook
  app to Live after Meta business verification; after the Apple Developer
  (Armapp LLC) enrollment: Services ID + key, Firebase Apple provider, Xcode
  capability. Then remove them from `COMING_SOON_PROVIDERS` (web,
  `src/lib/flags.ts`) and `LoginScreen.comingSoon` (app).
- [ ] **iOS release** (M): Xcode team, icons, launch screen, disable iPad,
  simulator + TestFlight testing, App Store listing, privacy labels,
  screenshots, review notes with a test account.
