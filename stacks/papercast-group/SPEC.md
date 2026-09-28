# papercast-group — the contract every part is built against

Version 0.1, 2026-09-28. Leo's papercast (stacks/papercast: NAS page + his runner + Breeze voice)
made multi-person for his research group. **Leo's own papercast is not touched**: nothing here
edits `stacks/papercast/`; code from it is copied, not imported.

## 0. Shape

- **Hub** (`stacks/papercast-group/hub/`, runs on perov = `val-perovskite`, tailnet 100.97.205.90,
  Ubuntu, Python **3.10**, 96 cores, one idle RTX A4000, shared with other users, **no docker**:
  everything runs as `leo`, `systemd --user`, in a venv). Serves the web page, stores everything,
  queues voice. Uses **no Claude at all**.
- **Web** (browser, phone or laptop): browse, listen, tick Listened, see who made what, edit and
  create graphs. No chat, no Q&A (dropped for the group version).
- **CLI** (`packages/papercast-cli/`, pip package `papercast`, command `papercast`, Python >= 3.10,
  **stdlib only**): `papercast login`, `papercast add <path-or-url>...`, `papercast status`,
  `papercast prefs`, `papercast logout`. Runs Claude Code **on the contributor's own machine,
  under their own login** (the unmodified `claude` binary, `claude -p`); the hub never sees any
  Claude credential. Uploads a finished bundle.
- **Voice worker** (on perov's A4000): takes finished scripts from the hub in a fair order and
  voices them with Breeze (the existing `papercast-voice`, installed on perov). No Claude.
- **Access**: over the internet through a Cloudflare tunnel (papercast.virtualatoms.org →
  127.0.0.1:8480), which only carries the traffic. Production sign-in is the hub's own
  **password** auth (Leo, 2026-09-28): a ledger of allowed Imperial emails, email only for
  "forgot password", no Google, no Cloudflare Access. Tonight's test: a free quick tunnel
  (`cloudflared tunnel --url`, `*.trycloudflare.com`) with the hub's own **local** auth
  (invite links), fake data only, taken down after the test.

Anthropic's terms (code.claude.com/docs/en/legal-and-compliance, read 2026-09-28): nobody's
subscription login may be collected, stored or routed through by a service, and one person's plan
never serves another person's request. So: Claude runs only inside the CLI, on the uploader's own
machine, for the uploader's own paper.

## 1. Layout and ownership (one owner per file; others only read)

```
stacks/papercast-group/
  SPEC.md                       this file (coordinator)
  hub/__init__.py
  hub/app.py                    server, routing, request helpers        (coordinator; owners may add a route line to ROUTE_MODULES only)
  hub/config.py                 env -> Config                           (coordinator)
  hub/events.py                 in-process pub/sub for SSE              (coordinator)
  hub/db.py                     SQLite schema, migrations, data helpers (A1)
  hub/auth.py                   identity, roles, sessions, CLI login, tokens (A2)
  hub/accounts.py               PCG_AUTH=password: the ledger, passwords, reset links, limits, email
  hub/static/signin.* setpw.*   the sign-in and set-password pages
  hub/contrib.py                CLI API: lookup, claims, bundle upload, server-side checks (A3)
  hub/voiceq.py                 voice queue API + fairness              (A3)
  hub/web.py                    browser API: library, listened, positions, prefs, admin, audio, explainer, SSE (A4)
  hub/static/index.html app.js app.css icon.svg    the page             (A4)
  hub/graph.py                  links, graphs, edit log, revert         (A5)
  hub/layout.py                 settled positions (ground state)        (A5)
  hub/static/map.js map.css     the map with editing                    (A6)
  hub/tests/test_<module>.py    each owner's tests
  prompts/base-guideline.md     base prompt v1 (group)                  (A9)
  deploy/                       perov install, systemd units, tunnel, backup, voice worker (A10)
  deploy/voice_worker.py        pulls voice jobs, runs papercast-voice  (A10)
  tests/e2e_test.py             CLI -> hub -> voice -> web, fake claude (A10)
  tools/import_papercast.py     import Leo's existing episodes (dry-run by default) (A10)
packages/papercast-cli/
  pyproject.toml                                                         (A7)
  papercast_cli/cli.py config.py api.py jobs.py login.py                 (A7)
  papercast_cli/common/         shared by CLI and hub (hub imports it; deploy pip-installs the package)
    prefs.py wording.py wording.json checks.py bundle.py base_guideline.md  (A9)
  papercast_cli/pipeline/       the local episode pipeline               (A8)
    run.py prompts.py claude.py explainer.py pdf.py links.py cut.py title.py tags.py
  tests/                                                                  (A7, A8, A9: own files)
```

## 2. Identity and auth (A2)

- `PCG_AUTH`: `password` (production), `cf-access`, `local` (tonight's tunnel test), `header`
  (tests only: trusts `X-Test-User: <email>` and only from 127.0.0.1).
  - `password` (`hub/accounts.py`): only emails on the ledger (`allowed_emails`, `@ic.ac.uk` or
    `@imperial.ac.uk`) have accounts; adding one makes it (username = the local part, fixed; role
    contributor; first password = the username, which must be replaced before any other request
    works: 403 `must_change_password`). Sign-in by username or email; scrypt; cookie `pcg_s` v2
    carries `users.session_v`, so a new password, a reset, "sign out everywhere", disabling or
    removal ends every session; removal also revokes the device tokens. Reset links (one use, 1 h
    by email, 24 h from an admin; sha256 only) by `POST /api/auth/forgot`, which answers the same
    for anyone. Rate limits per account and per client address (`CF-Connecting-IP` from
    127.0.0.1). A signed-out browser opening the page gets `/signin`. Commands:
    `python3 -m hub.auth bootstrap EMAIL`, `allow list|add|remove|import`, `email-test`.
  - `cf-access`: verify header `Cf-Access-Jwt-Assertion` (RS256; keys from
    `https://<PCG_CF_TEAM>.cloudflareaccess.com/cdn-cgi/access/certs`, cached 1 h; `aud` must
    contain `PCG_CF_AUD`; `exp` checked). Identity = the `email` claim (lowercased). Stdlib RSA
    verification (PKCS#1 v1.5 over SHA-256: pow(sig, e, n) and compare the DER DigestInfo).
  - `local`: an admin makes an invite link `/join/<token>` (one use, 7 days); opening it asks for
    a name, creates the user and sets a session cookie `pcg_s` (HMAC-SHA256 with `PCG_SECRET`,
    HttpOnly, Secure when `PCG_PUBLIC_URL` is https, SameSite=Lax, 30 days). The first admin comes
    from `PCG_ADMIN_EMAILS` via `python -m hub.auth bootstrap <email>` printing a one-time link.
- A user seen for the first time under `cf-access` is created as `viewer` (name = the email's
  local part until they set one); emails in `PCG_ADMIN_EMAILS` are `admin`.
- Roles: `viewer` (browse, listen, tick, edit graphs), `contributor` (+ upload through the CLI),
  `admin` (+ users and roles, base prompt, delete anything, lock a graph). Disabled users get 403.
- **CLI login** (device flow): `POST /api/cli/login/start` → `{"code": "ABCD-EFGH", "url":
  "<public>/cli?code=ABCD-EFGH", "poll": "<secret>", "interval": 2, "expires_in": 600}`; the
  person opens the URL, is logged in by the web's normal auth, sees "Approve papercast on <device
  name>?" and approves; `POST /api/cli/login/poll {"poll"}` → 428 `pending` | 200 `{"token":
  "pcg_<32 base32>", "user": {...}}` | 410 `expired` | 403 `denied`. Tokens are stored as sha256
  only (`tokens` table), listed and revocable on the web ("Devices").
- CLI requests: `Authorization: Bearer pcg_...`, paths under `/api/cli/`. In production those
  paths are **excluded from Cloudflare Access** (documented in deploy/README) and protected by the
  token alone. Upload needs role contributor or admin; `lookup`/`prompt`/`prefs`/`library` any user.
- Browser mutations: same-origin only, and header `X-PCG: 1` (CSRF).
- Voice worker: `Authorization: Bearer <worker token>`, sha256 in `PCG_WORKER_TOKEN_SHA256`.
- `auth.authenticate(req, level)` → user dict or raises `HTTPError(401|403)`; levels
  `public`, `viewer`, `contributor`, `admin`, `cli`, `cli-contributor`, `worker`.

## 3. Data (A1). SQLite at `$PCG_DATA/hub.db`, WAL, `foreign_keys=ON`

Schema is in `hub/db.py` (`SCHEMA`); A1 may add indexes and columns but not rename. Files under
`$PCG_DATA/episodes/<episode_id>/`: `script.md`, `explainer.json`, `explainer.html`,
`claims.md`, `meta.json`, `bundle-manifest.json`, `audio.mp3`, `voice/` (the voice job dir).
IDs: papers `p_` + 12 lowercase base32, episodes `e_` + 12, graphs `g_` + 10.

Paper identity (dedupe), in this order: arXiv id without version (`2210.02747`), DOI lowercased,
sha256 of the PDF, normalised title (lowercase, non-alphanumerics to one space, trimmed). A paper
has one or more **episodes** (versions), each made by one person with their preferences.

Helpers A1 provides (others call these; if one is missing, write the SQL in your own module
against the schema and tell the coordinator): `conn()` (thread-local), `migrate()`,
`get_user(id)`, `user_by_email(email)`, `create_user(email, name, role)`, `set_user(id, **f)`,
`list_users()`, `create_token(user_id, name) -> plaintext`, `user_by_token(plaintext)`,
`list_tokens(user_id)`, `revoke_token(user_id, token_id)`, `find_paper(keys) -> paper|None`
(keys: dict with any of arxiv_id, doi, source_sha256, title), `create_paper(fields, by)`,
`get_paper(id)`, `create_episode(paper_id, by, fields)`, `get_episode(id)`,
`set_episode(id, **f)`, `list_library(user_id, q=None)` (papers with their episodes, makers' names,
this user's listened flag and positions), `set_listened(user_id, paper_id, bool)`,
`set_position(user_id, episode_id, seconds)`, `get_prefs(user_id)`, `set_prefs(user_id, settings,
note)`, `base_prompt(version=None)`, `add_base_prompt(text, wording_json, by)`.

## 4. CLI API (A3; auth `cli` unless noted)

- `GET /api/cli/me` → user.
- `GET /api/cli/prompt` → `{"version": N, "guideline": "<markdown>", "wording": {...}}` (latest base).
- `GET /api/cli/prefs` → `{"settings": {...}, "note": "...", "version": N}`.
- `GET /api/cli/library` → `{"papers": [{"id","title","year","arxiv_id","doi","s2_id"}]}` (for links).
- `GET /api/cli/lookup?arxiv_id=&doi=&sha256=&title=` → `{"paper": null | {"id","title",
  "episodes": [{"id","made_by":{"id","name"},"prefs_summary","state"}]}, "claim": null | {"by",
  "since"}}`.
- `POST /api/cli/claims` `{"keys": {...}, "device": "..."}` (cli-contributor) → 201 `{"claim_id",
  "expires_at"}` or 409 `{"error":"in_progress","by":{"name"},"since"}`. A claim holds a paper
  identity for 6 h (renewed by `PUT /api/cli/claims/<id>`), so two people never make the same new
  paper at once; "make my own version" of an existing paper needs no claim.
- `POST /api/cli/episodes` (cli-contributor), body `application/gzip`: a tar.gz of a bundle
  (section 5), at most 50 MB → 201 `{"episode_id","paper_id","state":"checking"}`; the hub then
  checks it (section 6) and moves it to `waiting-for-gpu` or `rejected` (with `check_report`).
- `GET /api/cli/episodes?mine=1` → the caller's episodes with state; `GET /api/cli/episodes/<id>`.

## 5. The bundle (A9 defines `common/bundle.py`: `MANIFEST_VERSION = 1`, `validate(manifest,
names) -> list[str]` of problems)

tar.gz with `manifest.json` at the root plus the files it names:
```json
{"manifest_version": 1, "client_version": "0.1.0", "base_version": 1,
 "prefs": {"settings": {...}, "note": "...", "version": 3}, "model": "claude-opus-5-5",
 "claim_id": "c_..." | null, "paper_id": "p_..." | null,
 "paper": {"title": "...", "authors": ["..."], "year": 2022, "arxiv_id": "2210.02747" | null,
           "doi": null, "url": "https://...", "source_sha256": "<hex>" | null,
           "tags": ["diffusion", "generative models"]},
 "files": {"script": "script.md", "explainer_json": "explainer.json",
           "explainer_html": "explainer.html", "claims": "claims.md"},
 "links": [{"other": {"paper_id": "p_..."} | {"arxiv_id": "..."} | {"doi": "..."} | {"title": "..."},
            "direction": "builds_on" | "built_on_by", "grade": "e" | "s" | "w",
            "source": "s2" | "text"}],
 "stats": {"words": 3100, "est_minutes": 21.0, "wall_s": 1400}}
```
`paper_id` set = a new version of an existing paper; else `claim_id` set = a new paper.
`explainer.html` is self-contained (the page built from explainer.json with its crops inline, as
Leo's runner builds it today). The hub serves it only inside the sandbox (section 7).

## 6. Checks on the hub (A3, using `common/checks.py` from A9)

Every uploaded episode, whatever the client says: manifest valid; script is UTF-8 markdown, at
most 60 kB; estimated minutes (words / 150) within the base prompt's range (15-25 by default);
`common.wording.find(script)` finds no never-wanted phrase (the same list the CLI checks);
explainer.html at most 8 MB, starts with `<!doctype html`; explainer.json valid per
`common.checks.explainer_problems`. Failing → state `rejected`, `check_report` lists why, the
uploader sees it in `papercast status`. The client version and base version are stored.

## 7. Browser API (A4; auth `viewer` unless noted)

`GET /api/me`; `GET /api/library?q=` (as db.list_library); `GET /api/papers/<id>`;
`PUT /api/papers/<id>/listened {"listened": bool}`; `PUT /api/episodes/<id>/position {"s": 12.3}`;
`GET /audio/<episode_id>.mp3` (Range support); `GET /x/<episode_id>/explainer.html` with exactly
Leo's current explainer CSP (copy from `stacks/papercast/web/app.py` EXPLAINER_CSP) and the page
embeds it in `<iframe sandbox="allow-scripts allow-popups">`; `DELETE /api/episodes/<id>` (maker or
admin, soft, 30 days) and `POST /api/episodes/<id>/undelete`; `GET/PUT /api/prefs`;
`GET /api/tokens`, `DELETE /api/tokens/<id>` (own devices); `GET /cli?code=` (the approve page)
and `POST /api/cli/login/approve {"code","approve": bool}` (A2 owns these two);
admin: `GET /api/admin/users`, `PUT /api/admin/users/<id> {"role"|"disabled"}`,
`POST /api/admin/invites` (local auth), `GET/POST /api/admin/base` (base prompt versions);
`GET /api/events` SSE: `paper`, `episode`, `graph`, `log` events (events.py).
Page CSP as Leo's (`PAGE_CSP` in stacks/papercast/web/app.py): no inline script or style.

## 8. Graphs (A5 backend, A6 map UI)

- **Links** are global: `src` = the earlier paper, `dst` = the paper built on it, `grade` e/s/w.
  `origin` agent|human. A link a person removed stays `removed`, and an agent never re-adds it; a
  link a person added is never removed by an agent.
- **Graphs** are named sets of papers: `rule_tags` (papers with any of these tags) plus
  `added` minus `removed`. Seed graphs from Leo's five topics (reinforcement learning, diffusion
  and generative models, materials and molecules, language models, robotics and agents) by tag.
  A graph shows the links among its members.
- **Edit log**: every change is one row in `graph_log` (who, when, op, before, after). Ops:
  `link.add`, `link.remove`, `link.grade`, `graph.create`, `graph.rename`, `graph.delete`,
  `graph.add_paper`, `graph.remove_paper`, `graph.set_tags`, `graph.lock`. Agent link batches
  from an upload are one row each with `actor = 'agent'` and `user_id` = the uploader.
- **Revert**: `POST /api/graph-log/revert {"scope": "mine"|"any", "expect": <log id>}` reverts the
  newest un-reverted op in scope, among the newest 100 ops; `expect` must be that op's id (the
  page shows it first: "Undo: Bob removed PPO → DPO, 3 min ago"), else 409 `moved`. The inverse is
  applied against the current state: if the thing changed since (current != `after`), 409
  `conflict` with detail and nothing changes. A revert is itself logged (`revert_of`), so it can
  be undone. With `"redo": true` it redoes the undo that `GET /api/graph-log`'s `redo[scope]`
  names (`undo[scope]` names the op to undo); after a new change in a scope there is nothing to
  redo there, as in an editor.
- **Revisions**: every graph has a revision, one up with every change touching it (members, links
  among them, name, tags, lock, a member's label, delete, undo, redo; members that come or go by
  their tags or episodes count on the next read). `GET /api/graphs/<id>` gives it (`rev`,
  `graph.rev`). Any edit may send `base_rev` and `graph_id` (the JSON body; the query for a
  DELETE): when that graph is at another revision, 409 `stale` `{rev, base_rev, by, actor, at}`
  and nothing changes; an edit whose result is there already answers 200 with `"already": true`.
  Edit answers carry `revs: {graph id: rev}`; graph events are `{id, change: "edit", graph_rev,
  by, actor, log_op, deleted}` (also `change: "layout"`, `"suggestions"`, `"settings"`).
- **Links from uploads**: `GET /api/graph-settings` → `{"agent_links": "auto"|"suggest",
  "suggestions": n}`, `PUT` (admin) `{"agent_links"}`; automatic by default. While "suggest",
  `apply_agent_links` keeps the links it would add (same rules) as suggestions, listed by
  `GET /api/graphs/<id>` as `suggestions: [{id, src, dst, grade, by, created_at}]`;
  `POST /api/link-suggestions/<id>/accept` makes one the person's `link.add` (base_rev as any
  edit), `/dismiss` drops it for good (that pair is never suggested again), and
  `POST /api/link-suggestions/accept-all {"graph_id"?}` (admin) accepts every open one.
- API: `GET /api/graphs` → list; `POST /api/graphs {"name","tags"?}`; `GET /api/graphs/<id>` →
  `{"graph", "nodes": [{"id","label","title","year","made_by","x","y","deg"}], "links":
  [{"id","src","dst","grade","origin"}], "roots", "start", "path", "descendants"}`;
  `PUT /api/graphs/<id> {"name"|"tags"|"locked"}`; `DELETE /api/graphs/<id>`;
  `POST /api/graphs/<id>/papers {"paper_id"}`, `DELETE /api/graphs/<id>/papers/<pid>`;
  `POST /api/links {"src","dst","grade"}`, `PUT /api/links/<id> {"grade"}`,
  `DELETE /api/links/<id>`; `GET /api/graph-log?limit=100`; the revert above.
  Links from a bundle: `graph.apply_agent_links(episode, links)` (A3 calls it after checks pass).
- **Layout**: `layout.py` ports `/home/leo/papercast-itest/lineage/ground_state.py` (map.js's
  force equations run to rest; numpy allowed on the hub): after changes to a graph, debounced
  30 s, in a background thread: warm start from current positions, a new paper placed at its
  neighbours' centroid, 400 steps; nightly a full run from 8 starts. Positions in `layout`.
- **Map UI** (A6): start from `stacks/papercast/web/static/map.js` + `map.css` (Leo's current map:
  WebGL + 2D fallback, settled positions, hover card, Start here, listening order); add editing:
  select two papers → "Add link" (grade), a selected link → change grade / remove, "New graph",
  add or remove papers from a graph, "Undo" and "Redo" (each shows what it will do, both scopes;
  Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z, Ctrl+Y), a History panel (last 100, who, when), locked graphs
  read-only for non-admins, deleting a graph after a dialog (its maker or an admin). A new link is
  aimed: an arrow from the first paper to the pointer that snaps to a paper. Suggested links show
  on request, dashed. Every edit sends `base_rev`; a stale one brings the map up to date at once;
  the page's live events keep every open map current. Colours as now plus Listened per person.

## 9. Voice (A3 queue, A10 worker)

`POST /api/voice/claim` (worker) → 200 `{"episode_id","script_url","title","first_author","year"}`
or 204. Fair order: round-robin over uploaders by who was served longest ago, oldest first
within one uploader. `PUT /api/voice/<episode_id>/status {"phase","progress"}`;
`PUT /api/voice/<episode_id>/audio` (audio/mpeg body, with `X-Duration-S`) → episode `ready`;
`POST /api/voice/<episode_id>/failed {"error"}`. A claim not updated for 30 min goes back to the
queue. The worker runs `papercast-voice run <job dir>` as Leo's runner does (see
`stacks/papercast/voice/README.md`), one episode at a time on perov's A4000, niced, and waits
if another user takes the GPU (papercast-voice already does).

Voices and timings (hub/voices.py). The claim also carries `"voice"`: null (papercast-voice's own
default narrator) or `{"id","name","cpu","spec"}`, where `spec` is job.json's `voice` for
papercast-voice (`{"engine","voice": key, "instruction","seed"}` for a Breeze narrator, `{"engine":
"kokoro","voice"}` with job engine `cpu`). The worker sends `PUT /api/voice/<id>/timings` (JSON,
`{"version": 1, "duration_s", "segments": [{"start","end","text"}]}`: one segment per sentence
of script.md in order, seconds of the MP3) before the audio, whose upload names the voice it is in
(`X-Voice: <key>`); the hub keeps the timings as `timings.json` next to `audio.mp3` from the moment
the MP3 lands. The same PUT for an episode not being voiced stores timings for the audio it has.
A new episode is voiced in its maker's own voice (Settings, Voice). Its maker or an admin can have
an episode that has audio voiced again in another preset: the job goes back into this fair queue
under whoever asked (at most two waiting per person), the episode stays `ready` with its old
audio meanwhile, and the new MP3 replaces it with its timings; `voice.rev` (each episode's in the
library) goes up, and the page asks `/audio/<id>.mp3?v=<rev>`. Browser routes: `GET /api/voices`,
`PUT /api/voices/mine`, `GET /api/voices/<id>/sample.mp3`, `GET|PUT|DELETE /api/episodes/<id>/voice`,
`GET /api/episodes/<id>/timings` (timings.json plus `rev`, the audio revision they are for).

## 10. Prompt (A9) and preferences

- Base guideline v1 = `stacks/papercast/runner/seed-guideline.md` made general: "the listener"
  instead of Leo; no vault (the group has none yet): "what the listener knows" comes from the
  listener's `background` setting; Leo's binding rules kept (only the script and the explainer;
  no caveats; the silent run; the 15-25 minute range; the key idea stated once, early, without
  announcing it; never address the listener; no episode talk). The wording list is Leo's
  `wording.json` with the listener-name class taken from the user's name.
- Preferences (`common/prefs.py`: `SCHEMA`, `DEFAULTS`, `validate(settings, note)`,
  `render(settings, note) -> str`, `summary(settings) -> str` like "derivations · practical"):
  `maths`: `words` | `key-steps` | `full`; `emphasis`: `balanced` | `theory` | `method` |
  `practice`; `background`: `newcomer` | `field` | `specialist`; `note`: at most 500 characters.
  Rendered after the base under "This listener's preferences", opening with: "The rules above
  decide how the episode sounds and what never goes in; these preferences decide what gets more
  time." Maths `full` means the derivation is walked through in words and the equations go on the
  explainer page.

## 11. The CLI (A7 shell, A8 pipeline)

- Config `~/.config/papercast/config.json` (`server`, `token`, `device`), mode 600; state
  `~/.local/state/papercast/jobs/<job_id>/`. `papercast login [--server URL]`, `logout`,
  `status [--all]`, `prefs [--maths ...] [--emphasis ...] [--background ...] [--note ...]`
  (stored on the hub), `add <path-or-url>... [--version-of <paper_id>] [--model M]`.
- `add`: for each input: a job dir, then a **detached** worker (`setsid`, `nice -n 10`) that runs
  the pipeline and survives the terminal closing; `papercast status` shows jobs; a job
  interrupted by sleep, reboot or Claude's usage limit resumes on the next `papercast` command
  (or `papercast worker`, which a user may put in their login items), at most 2 jobs at once.
- The pipeline (A8), all local: (1) hand the path or URL to the agent, which reads it (Read for a
  PDF, WebFetch for a link, restricted to the named research sites Leo's runner allows); the agent
  writes `paper.json` (title, authors, year, arxiv_id, doi) first; (2) `lookup` + `claim` (or ask
  "made by Alice, make your own version? [y/N]"); (3) the episode run: base guideline + prefs,
  the same locked-down `claude -p` flags as Leo's runner (no MCP, named-site WebFetch, writes
  only in the job dir, `--permission-mode` as Leo's), `script.md`, `explainer.json`, `claims.md`;
  (4) the local checks and the cut pass as Leo's runner does; (5) `explainer.html` built from
  explainer.json (crops need `pdftoppm` if the paper has figures: when missing, SVG and text only);
  (6) links: Semantic Scholar references and citations of the paper, matched against
  `GET /api/cli/library`, graded by `claude -p --model claude-haiku-4-5` with no tools;
  (7) bundle and `POST /api/cli/episodes`. Claude usage limit: detect as Leo's runner's
  `limits.py` does, pause until the reset time, resume.
- Tests use a fake `claude` on PATH (a script that writes the expected files), never the real one.

## 12. Rules for every agent

- Python 3.10 compatible (perov). Hub: stdlib + numpy (layout only). CLI: stdlib only.
- Copy what you need from `stacks/papercast/` (runner, web, voice); never edit anything there.
- Match the style of Leo's papercast code: plain names, short comments that say why.
- Your own tests must pass: `python3 -m pytest` is not installed everywhere; use `unittest`
  (`python3 -m unittest discover -s <dir>`).
- stibnite and perov are shared with other users: nice long jobs, never touch other users'
  processes or files, no system-wide installs (`sudo` only if the coordinator says so).
- Do not message other Claude sessions, do not deploy, do not push. Commit on your worktree
  branch; the coordinator merges. Final message: what you built, what is tested, what is left.
