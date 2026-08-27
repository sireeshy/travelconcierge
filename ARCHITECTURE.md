# Journey Concierge — Architecture

Written for a senior engineer/architect doing a critical review, not as onboarding material. It
states design decisions plainly, including ones a reviewer would reasonably push back on. Grounded
in `app.py` as of commit `2b5d6f1` (1,743 lines, single file) — line references will drift as the
file changes; re-verify before relying on a specific number.

## 1. What this is, in one paragraph

A Streamlit app that plans road trips (highway or short in-city) by giving Gemini three custom
tools (route calculation, place search along the route, place details/reviews) plus Google's
built-in web-search tool, driven by the SDK's automatic function-calling loop inside a single
`chat.send_message()` call per user turn. The model's final answer is constrained to a JSON schema
(not free Markdown), which the app renders into either interactive Streamlit UI (the active plan) or
plain Markdown (chat history). There is no backend beyond Streamlit itself, no database, and no
persistence layer — every fact the app knows about the current session lives in
`st.session_state` and is gone when that session ends.

## 2. System context

```
Browser
   │  WebSocket (Streamlit's own protocol)
   ▼
Streamlit server — single Python process, app.py re-executed top-to-bottom on every interaction
   │
   ├──▶ Gemini API (google-genai SDK)
   │      chat session + 3 custom tools + built-in google_search;
   │      final turn constrained by response_schema
   │
   ├──▶ Google Maps Platform
   │      Routes API · Places API (New) · Places Autocomplete · Geocoding
   │      (all four behind one GOOGLE_MAPS_API_KEY)
   │
   └──▶ Wikipedia REST API
          public, no key, requires a real User-Agent header
```

Two API keys, both server-side environment variables, no per-user credentials:
`GOOGLE_MAPS_API_KEY` (Routes API, Places API New, Places Autocomplete, Geocoding — one key covers
all four Maps Platform products used) and `GEMINI_API_KEY`. Wikipedia's REST API needs no key but
does require a compliant `User-Agent` header (`app.py:186-189`) — Wikimedia's CDN silently 403s
generic/missing user agents, which is exactly how this was discovered (a plain `curl` test failed
first).

## 3. Runtime model — why Streamlit's execution model matters here

Streamlit re-executes the *entire* `app.py` top to bottom on every user interaction (a button
click, a text input change, a radio selection). There's no persistent request/response cycle in the
traditional sense — "state" only survives across reruns via `st.session_state`, an in-memory dict
scoped to one browser session on one server process. This shapes several decisions documented
below:

- Tool functions can't `return` data to the UI in the normal sense, because the only thing that
  crosses back from `chat.send_message()` to the render code is the model's final text. Data the UI
  actually needs (discovered place coordinates, the route polyline, place photos) is written
  directly into `st.session_state` as a side effect *inside* the tool functions themselves
  (`search_places_along_route` at `app.py:401-407`, `calculate_route_and_etas` at `app.py:324`).
  This is a real coupling smell — a tool function reaching into global UI state — but it's close to
  unavoidable given the framework's execution model without a much bigger architectural change (see
  §10).
- Widget default values can only be set *before* the widget with that key is instantiated in the
  same script run — the quick-select date/time buttons (`app.py:1382-1412`) are ordered above their
  corresponding widgets specifically because of this; writing to a widget's session-state key after
  it's been created raises.
- There's no concurrency primitive available inside a single script run — this is *why* the app
  can't easily run independent tool calls (e.g., a food search and a fuel search) in parallel
  without a genuine architecture change (see §10, "concurrent tool execution").

## 4. Component map

Everything lives in one file. Logically, it separates into five layers, in this order in the file:

| Layer | Functions | Lines (approx) |
|---|---|---|
| **Logging/observability** | `log_usage_event`, `USAGE_LOG_HEADER`, `timed_tool` decorator | 22-81, 227-260 |
| **Standalone data helpers** | `decode_polyline`, `get_place_predictions`, `get_timezone_for_location`, `get_wikipedia_thumbnail` | 88-203 |
| **Gemini tool functions** (the model's only way to get real data) | `calculate_route_and_etas`, `search_places_along_route`, `get_place_details_and_reviews` | 264-522 |
| **Rendering** (illustrations, maps, photos, structured-output → UI) | `render_copy_and_share`, `render_navigate_links`, `render_route_map`, `render_place_photos`, `render_home_illustrations`, `render_region_postcards`, `render_structured_response`, `render_plan_cards`, `render_print_button`, plus the schema constants and `_format_option_*` helpers | 525-1300 |
| **Page script** (Streamlit UI, the system prompt, the AFC loop invocation) | everything from `st.set_page_config` to end of file | 1303-1743 |

There is no `src/` layout, no package, no `__init__.py`, no test directory. `requirements.txt` has
7 pinned dependencies (`streamlit`, `google-genai`, `requests`, `python-dateutil`,
`python-dotenv`, `timezonefinder`, `tzdata`).

## 5. Request lifecycle — "Plan My Trip" end to end

1. User fills the form (origin/destination via Places Autocomplete-backed selectboxes, date/time,
   quick-preference checkboxes + free-text notes) and clicks the button.
2. The click handler (`app.py:1461-1482`) resets *all* session state tied to a previous plan
   (`chat`, `chat_messages`, `discovered_places`, `route_polyline`, `latest_plan`, etc.) and sets
   `need_new_plan = True`, then the script reruns.
3. On the rerun, a new `genai.Client` and a new `chat` session are created (or reused if one exists
   — see §7), with `system_instruction`, all three custom tools + `google_search`,
   `response_schema=CONCIERGE_RESPONSE_SCHEMA`, and `automatic_function_calling.maximum_remote_calls
   = 15`.
4. `chat.send_message(prompt)` is one blocking call. Internally, the SDK's automatic function
   calling loop runs: model requests a tool call → SDK executes the actual Python function
   synchronously → result goes back to the model → repeat, until the model produces a final text
   turn with no more tool calls. This entire loop is **sequential** — even when the model requests
   what could be independent tool calls (e.g., searching for food AND fuel), the SDK executes them
   one at a time. **Measured** during this project's development: total wall-clock for a real plan
   was ~70-80s, of which the actual Maps/Places API calls measured ~6s combined — the remaining
   ~90% was Gemini's own per-turn latency across the multi-turn tool-calling conversation, not the
   tools.
5. Each tool call is wrapped by the `@timed_tool` decorator (`app.py:227-260`), which posts a
   human-readable line to a live `st.status` panel (the only reason the user gets any feedback
   during that 70-80s) and records `{name, detail, duration_s, ok}` into
   `st.session_state._tool_trace` for later logging.
6. Once the model's final turn arrives, `response_to_markdown()` (`app.py:1275-1300`) parses it as
   JSON against the schema shape. On success, it also stashes `data['plan']` into
   `st.session_state.latest_plan` as a side effect — this is what lets the later render step build
   an interactive card UI instead of just displaying text.
7. `log_usage_event()` writes one row to `usage_log.csv` and one line to stdout, covering
   duration, tool call trace, tool error count, and whether structured output actually parsed.
8. The message is appended to `st.session_state.chat_messages`; if it was a valid plan, the index is
   recorded in `latest_plan_message_index`.
9. The render loop (`app.py:1709-1720`) walks `chat_messages`. The message at
   `latest_plan_message_index` renders via `render_plan_cards()` — real Streamlit
   widgets/containers with per-option copy/share buttons, a Google Maps place link, and a
   category-by-category stop-picker. Every other message (older plans, follow-up answers) renders
   as plain `st.markdown()` of pre-formatted text.
10. After the message loop: the route map (pydeck), place photos (fetched server-side to keep the
    Maps key off the client), the multi-stop "Get Directions" builder, and the sidebar's Wikipedia
    region imagery all render, each reading from `st.session_state`.

A follow-up message (`app.py:1727-1743`) repeats steps 4-9 on the *same* `chat` object, so
conversation history is preserved by the SDK's own chat-session mechanism, not re-sent manually.

## 6. State management — `st.session_state` inventory

There is no database and no cache layer beyond Streamlit's own `@st.cache_data` (used for three
pure external lookups: place autocomplete predictions are *not* cached, but timezone resolution and
Wikipedia thumbnails are, at `ttl=3600` and `ttl=86400` respectively). Everything else is
`st.session_state`, which is process-memory, per-browser-session, and gone on server restart or
session expiry. Key entries:

| Key | Set by | Read by | Purpose |
|---|---|---|---|
| `chat` | main script | main script | The `google.genai` chat session object — holds full conversation history server-side (in the SDK's own memory, not Streamlit's) |
| `chat_messages` | main script | render loop | Display-ready `{role, content}` list |
| `latest_plan` | `response_to_markdown` | `render_plan_cards`, `render_navigate_links` | The parsed structured JSON of the *current* plan, not its rendered text |
| `latest_plan_message_index` | main script | render loop | Which `chat_messages` index gets the rich-card treatment vs. plain Markdown |
| `discovered_places` | `search_places_along_route`, `get_place_details_and_reviews` (side effect) | `render_route_map`, `render_place_photos`, `render_navigate_links` | place_id → {name, vicinity, lat, lng, photo_name} — the only source of real coordinates/addresses, since the model's JSON text isn't guaranteed to carry them faithfully |
| `route_polyline` | `calculate_route_and_etas` (side effect) | `render_route_map` | Encoded polyline for the map |
| `_tool_trace` / `_progress_status` | main script, read/written by `@timed_tool` | `log_usage_event`, live status panel | Per-request instrumentation, prefixed `_` as an internal/ephemeral convention |
| `google_maps_api_key` / `gemini_api_key` | main script, from `os.environ` | tool functions, main script | Re-derived from env vars on every rerun (not user input anymore — see §9) |

## 7. The structured-output design

This is the most consequential design decision in the app, and the one most worth an architect's
scrutiny.

**The problem it solves:** two runs of the same route/preferences used to produce visibly different
Markdown layouts (a table one time, a numbered list the next) — the underlying content was fine,
but there was no stable *shape* the app could build any UI around.

**The mechanism:** Gemini 3 models support combining function calling with `response_schema` +
`response_mime_type="application/json"` in the same `GenerateContentConfig` — the model still calls
tools freely mid-conversation, but its final (non-tool-call) turn must conform to
`CONCIERGE_RESPONSE_SCHEMA`. `response_type` (`"plan"` vs `"answer"`) lets the same schema cover
both a full itinerary and a plain conversational reply, so a follow-up like "why did you suggest
that one?" doesn't get forced into the full itinerary shape.

**The reliability problem, and how it's handled:** Google documents this feature combination as
*preview*, not guaranteed, as of this writing. Two concrete failure modes were hit and designed
around during development:

1. **The model can simply not return valid JSON.** `parse_structured_response()`
   (`app.py:1068-1078`) tolerates a fenced ` ```json ` block and falls back to `None` on any parse
   failure; `response_to_markdown()` falls back to showing the raw text untouched. `structured_ok`
   is logged every time specifically to make silent degradation of this preview feature visible
   over time (`log_usage_event`'s docstring is explicit about this).
2. **The model can silently omit an optional field even when real data exists for it.** This was
   observed directly, not theorized: `review_recency` and `critical_review_snippet` were added as
   `nullable: true` fields with descriptive prompt instructions, and the model omitted both for
   every option across multiple real runs — including for a restaurant with a verified, real 1-star
   review sitting in the tool's own response data. The fix was moving both into the schema's
   `required` array. `required` + `nullable: true` is a real, load-bearing pattern here: it forces
   the model to make an explicit decision (a real value or an explicit `null`) rather than silently
   skipping the field. This is a strong signal that **prompt instructions alone are not reliable
   for structured-output field population; schema constraints are.** Worth generalizing that lesson
   to any future field added to this schema.

**What this buys the app:** `render_plan_cards()` can put a real Copy/Share button and a real
"View on Google Maps" link (built from a `place_id` the model is now schema-required to carry
through) on *every individual option*, and a stop-picker can offer one radio choice per category
sourced directly from what was actually presented — none of that is possible against unstructured
Markdown text.

**What it costs:** the system prompt (`app.py:1536-1641`) is ~1,100 words of accumulated,
failure-driven instructions — not written speculatively, each numbered point in the comment above
it maps to a real bug that was observed and fixed. This is a maintenance surface: it's dense,
coupled to the schema's exact field names, and every future schema change likely needs a
corresponding prompt change to stay in sync (there's no automated check that they agree).

## 8. Key architectural decisions (with tradeoffs)

| Decision | Rationale | Tradeoff / risk |
|---|---|---|
| Single-file, ~1,750-line `app.py` | Started as a workshop demo; never refactored as scope grew | No module boundaries between tool functions, rendering, and page script. Everything is globally importable/mutable within the one namespace. A senior reviewer would likely ask for at minimum a `tools.py` / `render.py` / `app.py` split. |
| Session-state side-channeling from tool functions | Streamlit's execution model gives no other way to get non-text data from a blocking `chat.send_message()` call back to the UI | Tool functions have a hidden dependency on Streamlit's runtime (`st.session_state`) — they are not pure functions and can't be unit-tested without mocking Streamlit. |
| Automatic (sequential) function calling, not a manual loop | Simpler code, fewer moving parts | Verified as the actual latency bottleneck — no concurrency possible without disabling AFC and hand-rolling a loop with a thread pool, a real architecture change that hasn't been done (see §10). |
| `response_schema` used to force output shape | Fixed a real, demonstrated UI-consistency problem (see §7) | Depends on an explicitly preview-labeled Google feature. Has a designed fallback path, but that fallback (`response_text` shown raw) is materially worse UX and was hit in testing (a genuine Google Routes API 500 once caused two consecutive `"answer"`-typed error explanations instead of a plan — not a structured-output failure that time, but the same code path). |
| No fallback to model's own "knowledge" when a live search tool fails | Verified real risk: recalled restaurant names from the model's training data were checked against live data and found permanently closed or in the wrong city in 3 of 5 cases tested | The user gets no recommendation at all on a tool failure, only a message to retry — a deliberate quality-over-availability tradeoff. |
| API keys always from server env vars, no user-supplied key UI | The app is no longer a shared workshop deployment where attendees needed their own key/quota | Every request against the deployed app spends the *operator's* API budget. There is no per-user rate limiting, no auth, and the "Plan My Trip" button is unauthenticated and unthrottled — see §9. |
| `usage_log.csv` on local disk, plain `logging` to stdout | Zero-infrastructure observability, immediate (Streamlit Cloud's log viewer shows stdout directly) | Not durable: Streamlit Community Cloud's filesystem is ephemeral, the CSV resets on every restart/redeploy. Explicitly documented as "a local-dev tool, not a durable analytics store" in the code itself — i.e., there is no real production analytics/observability story yet. |
| Photos fetched server-side, never client `<img src>` | Keeps `GOOGLE_MAPS_API_KEY` out of the browser's page source | One more server-side HTTP round trip per photo, and it's synchronous within the render pass (not parallelized across up to 4 photos). |
| Hand-rolled polyline decoding, no Static Maps API | Static Maps API needs separate Cloud Console enablement most keys won't have by default (this project hit that wall repeatedly with other Maps products) | pydeck + a ~20-line decode function instead of an image URL — more code, but zero extra API dependency and no enablement risk. |

## 9. Known limitations and risks (explicit, for critique)

- **No authentication, no rate limiting, no per-user cost cap.** The deployed app is public, uses
  server-side API keys for both Gemini and Google Maps Platform, and nothing stops repeated or
  automated use from consuming the operator's quota/budget. This is the single biggest operational
  risk in the current design and the most obvious thing a reviewer would flag.
- **Zero automated tests.** Every piece of functionality added or changed during this project's
  development was verified by manually driving a live browser session against the running app
  (or, for a couple of narrow cases, by hitting the real Google/Wikipedia APIs directly from a
  Python shell). There is no unit test, integration test, or CI pipeline of any kind in this repo.
- **Ephemeral everything.** No database, no durable log store, no session persistence across a
  server restart. A user's in-progress conversation is gone if the underlying Streamlit process
  restarts (Streamlit Community Cloud does this on redeploy, and can idle-sleep the app after
  inactivity).
- **The name-matching-vs-place_id issue class.** One concrete instance was found and fixed (stop
  selection silently dropping a choice because the model's descriptive name didn't exactly match
  the raw Places API name — fixed by matching on `place_id` instead). The fallback path for when
  neither `place_id` nor an exact name match resolves (`app.py:619-628`, using the raw name as
  free-text Maps waypoint text) is a heuristic, not a guarantee — it wasn't a bug fix so much as a
  "fail more gracefully" measure, and it's plausible similar name-based assumptions exist elsewhere
  that haven't been exercised yet.
- **Reliability of structured output is inherently probabilistic, not deterministic.** Even with
  the `required`-field workaround in §7, this is fundamentally a language model choosing to comply
  with a schema, not a type system enforcing it at the language level. The `structured_ok` logging
  exists because of this, but nothing currently *alerts* on a degradation trend — someone has to
  read the logs.
- **The system prompt is a single ~1,100-word string with no structure-checking.** It references
  exact schema field names in prose (e.g., "set `review_recency`..."). If the schema changes without
  a corresponding prompt update, nothing catches the drift automatically.
- **`get_place_details_and_reviews` makes one HTTP request per place_id in a Python `for` loop**
  (`app.py:442-520`) — sequential, not batched or parallelized, inside a function that's already
  inside the sequential AFC loop. For the typical 5-8 candidate places this is a minor contributor
  to total latency next to Gemini's own per-turn time, but it compounds with everything else being
  sequential.
- **`google.genai.Client(api_key=...)` and the chat session are re-created on every "Plan My Trip"
  click** (not cached/reused across trips within a session, only across follow-ups within one
  trip) — cheap in practice, but worth noting as a pattern.

## 10. Open questions for review

Framed as questions a senior reviewer might reasonably raise, not settled conclusions:

1. **Should tool orchestration move off automatic function calling to a manual loop?** This would
   be the single highest-leverage change for actual latency (not just perceived latency, which the
   live progress panel already addresses) — it would allow running independent tool calls (e.g.,
   food search + fuel search) concurrently via a thread pool. It's a genuine architecture change,
   not a tweak, and was explicitly scoped as future work rather than done in this pass.
2. **Should this be split into multiple modules?** At ~1,750 lines with five identifiable layers
   (data helpers, Gemini tools, rendering, schema, page script), a `tools.py` / `rendering.py` /
   `schema.py` / `app.py` split seems like a reasonable next step for maintainability, but hasn't
   been done — this remains a single file by historical accident (workshop-demo origin) more than
   deliberate choice.
3. **What's the actual cost-control story for a public, unauthenticated, server-keyed deployment?**
   Currently none. Options worth evaluating: Streamlit-level auth, a request quota, a CAPTCHA on the
   planning action, or moving to user-supplied keys again (which was the old behavior, removed this
   session specifically because it was judged unnecessary friction for what's currently a
   single-operator use case — that judgment call should be revisited if traffic/cost patterns
   change).
4. **Is `st.session_state` side-channeling (tool functions writing directly into global UI state)
   an acceptable long-term pattern, or does it need a cleaner data-flow boundary** (e.g., tool
   functions returning everything, with the render layer reading only from the model's structured
   output plus a single well-defined "tool results" accumulator, rather than tools reaching into
   `st.session_state` directly)?
5. **Does the structured-output reliability pattern (nullable-but-required fields) generalize
   safely as the schema grows**, or does it need a more systematic approach — e.g., a schema
   validation/linting step that checks every prompt reference to a field name against the actual
   schema, to catch the two from drifting apart?
6. **Is there a real need for persistence** (saved trips, history across sessions, a lightweight
   database) now that the app has grown well past its original single-shot-demo scope, or does the
   ephemeral, single-session model still match actual usage?

## Related documents

- [DESIGN.md](DESIGN.md) — current visual identity (colors, fonts, iconography), grounded in live code
- [DESIGN_CONCEPTS.md](DESIGN_CONCEPTS.md) — unshipped UI redesign explorations
- [HANDOFF.md](HANDOFF.md) — session-to-session narrative handoff notes, non-obvious gotchas
