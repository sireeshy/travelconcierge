# Journey Concierge — Current Visual Identity

Documents what's actually styled in `app.py` today, not aspirational direction. Everything below
is grounded in the live code as of this writing — see the line-anchored notes if it drifts.

## The honest summary

There is **no Streamlit theme file** (`.streamlit/config.toml` doesn't exist) — the app runs on
Streamlit's stock default theme for every native widget: headers, buttons, inputs, the sidebar,
`st.status`, `st.radio`, `st.container`. Default light/dark switching follows the visitor's system
setting; nothing overrides Streamlit's own palette or font stack.

On top of that default chrome, a small number of specific elements carry **hand-set inline colors**
(mostly matching third-party brand colors, since they link out to those products), plus **two SVG
illustrations** with their own bespoke, self-contained palettes. Those are the only two places any
deliberate "branding" exists right now — everything else is default Streamlit.

## Inline-styled UI elements

These are hard-coded hex values in `app.py`, not theme tokens — changing them means editing each
call site individually.

| Element | Colors | Where |
|---|---|---|
| Copy button | bg `#f0f2f6`, border `#999`, text `#31333F` | `render_copy_and_share` |
| WhatsApp share button | bg/border `#25D366` (WhatsApp's own brand green), text white | `render_copy_and_share` |
| Print button | bg `#f0f2f6`, border `#999`, text `#31333F` (matches Copy) | `render_print_button` |
| "Get Directions" button | bg/border `#4285F4` (Google's brand blue), text white | `render_navigate_links` |
| "View on Google Maps" link | text `#4285F4` (same Google blue, no background) | `render_plan_cards` |

All of these use `border-radius: 6px` and `padding: 6px 14px` (buttons) — the closest thing to a
consistent "component style" in the app, though it was arrived at per-element, not from a shared
constant.

## Typography

**No custom font is loaded anywhere in `app.py`.** Every native Streamlit element (headings, body
text, buttons, inputs) renders in Streamlit's default font stack (`"Source Sans Pro", sans-serif`
in the current Streamlit theme). The inline-HTML buttons above don't set `font-family` either, so
they inherit the same default.

The Google Fonts pairings explored this session (Saira Condensed / Karla / JetBrains Mono for the
highway-dashboard concept; Space Mono / Karla for the "Two Rough Directions" sketches) live **only**
in throwaway design-exploration Artifacts, never in `app.py`. If a real typographic identity gets
adopted, it hasn't shipped yet.

## Iconography — the emoji vocabulary

Emoji are the only consistent "icon system" in the app, used two ways:

**Section markers** (in both the system prompt's instructions and the Python rendering code):

| Emoji | Meaning |
|---|---|
| 🚗 | Trip overview |
| ⏱️ | Itinerary timeline |
| 💡 | Proactive notes (things volunteered, not asked for) |
| 🍱 / 🥗 | Food-related sections |
| ⛽ | Fuel |
| 🚻 | Restroom |
| 🗺️ | Route map / navigation / "build your route" |
| 📸 / 🖼️ | Photos (Places photos vs. Wikipedia region imagery, respectively) |
| 📤 | Share/print |
| 🔗 | External link (View on Google Maps) |

**Semantic status flags** (defined explicitly in the system prompt, `app.py:1530`, meant to appear
*inside* the model's own field text, not as section headers):

| Emoji | Meaning |
|---|---|
| 🟢 | Good |
| 🟡 | Moderate |
| ⚠️ | Red flag / concern |

This is a real, if informal, "semantic color" system (see `dataviz`-style design thinking) — 🟢/🟡/⚠️
carry meaning independent of the accent colors above, the same way a status pill would in a more
visual design system.

## The two SVG illustrations (landing page banners)

`_HOME_ILLUSTRATIONS` (`app.py:~709`) — two wide (1200×220 / 1400×320) flat-vector banners shown
full-width, uncaptioned, before a trip is planned. Each has its own self-contained palette; they were
not designed against a shared token set, so treat these as two independent one-offs rather than a
system:

**Halebeedu (Hoysaleswara temple)** — golden-hour palette:
- Sky gradient: `#f7e2b8 → #eab06a → #c97a4f`
- Stone/ink: `#171814`, `#0f100d`, `#20211d`
- Carved-frieze bands: `#3a3b36`, `#2a2b28`
- Sun: `#fbe7c2`

**Lothal (ancient dockyard)** — bright daytime palette:
- Sky gradient: `#bfe0ee → #e7f0e0 → #e7c98a`
- Fields: `#bcd9a0` / `#9fc084`
- Brick (running-bond pattern fill): `#a15a34` / `#7a3f24`
- Water: `#4c8fa6` / `#5ea3bd`
- Ground: `#d9b483`, boats/figures `#3a2416`

Both use SVG `<pattern>` fills (brick coursing, carved-frieze bands) for texture rather than flat
color blocks or individual shapes — that's what makes them read as "stonework"/"brickwork" rather
than a few dots on a line.

The **sidebar's** region-imagery fallback illustrations (`_TRAVEL_SPIRIT_SVGS`, shown only when a
place has no Wikipedia photo) use a *third*, still-different palette (`#f2a25c`/`#d9714a` dawn-road
sky; `#eef1ea`/`#c9603f`/`#123f30` for the compass sketch) — a third one-off, not reconciled with
the other two.

## What this means for future work

There's no single accent color, no defined neutral scale, no shared type ramp — three different
illustration palettes and five different inline-styled UI elements, each chosen independently. If a
real design system gets adopted (the earlier "highway dashboard" and route-native/dossier concepts
explored in Artifacts this session are candidates), it should replace all of the above with a single
token set — pulling every hard-coded hex in this file into shared constants — rather than adding a
fourth palette alongside the existing three.
