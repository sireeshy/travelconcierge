# Design Concepts — Explored, Not Shipped

Companion to [DESIGN.md](DESIGN.md), which documents what's actually live in `app.py`. Everything
here is **unshipped exploratory work** from Artifact mockups built during a redesign discussion —
none of it exists in the running app. Kept separate on purpose: DESIGN.md is explicitly scoped to
"current state, not aspirational direction," and this is the opposite of that.

Live reference (interactive, both concepts, toggle at the top):
**https://claude.ai/code/artifact/386277e1-6e15-4b3c-b1b1-8afe1991243e**

## Why these exist

The redesign conversation went through several rounds of "no, not that" before landing here:

1. First pass took visual cues from Mindtrip (a well-regarded AI trip planner) — rejected as
   "boring," meaning it borrowed a *look* (soft cinematic travel-brochure palette) without a
   distinct point of view.
2. Second pass built cards + tabs + hover states in a highway-signage palette instead — rejected
   again, because the complaint wasn't about color, it was structural: cards-in-tabs is the same
   shape as every other AI chat product (Perplexity, ChatGPT canvas, Notion AI), regardless of skin.
3. Only then did two genuinely different *structures* get built: **The Strip** (route-native) and
   **The Dossier** (fixed document, not a chat reply). Both deliberately avoid tabs, hover cards, and
   pill badges — the shapes that made the second pass fail.

The lesson worth keeping: reskinning a familiar structure isn't a different design. Changing what
the *object on screen* is, is.

## The Strip — route-native layout

**Core idea:** the road itself is the layout. There's no separate "Food" section and "Restroom"
section pulled out into tabs — you scroll down the actual route, and wherever a real decision
exists, the line branches into a compact inline choice right at that point on the road.

### The visual language

- **A dashed vertical line down the left edge** (`repeating-linear-gradient`, 14px on / 8px off) —
  a literal reference to a road's lane-divider markings, not a generic timeline connector.
- **Circular node markers** on the line for every stop — neutral ink border for a pass-through
  waypoint, accent-colored border (`.node.decision`) for a stop that's an actual decision the user
  needs to make.
- **km labels perched above each node**, not beside it — this was a real bug fix, not just a
  stylistic choice: positioning the label to the *left* of the dot at the same height meant a
  right-aligned short label ("0km") could render its glyphs into the dot's own x-range, since the
  label's bounding box fully contained the dot horizontally. Moving the label to a different
  vertical band (above, not beside) makes the collision impossible regardless of label length,
  rather than fixing it with more precise-but-fragile pixel math.
- **Inline decision clusters**: where a choice exists, a thin accent-colored left border breaks off
  from the node and holds a row of compact chips (name, star rating, one-line "why"). Tapping a chip
  expands its detail inline (accordion-style), replacing the previous one — no modal, no navigating
  away from the road.
- **A thin "ask about this route" bar** at the very bottom — chat is present but deliberately
  secondary, not the primary output surface.

### The proportional-distance mechanism (the part worth reusing verbatim)

Vertical distance between two stops is **not** hand-measured pixels and **not** linear km-to-px
scaling. It's `flex-grow: sqrt(gap_km)` against a fixed track budget:

```css
.strip { display: flex; flex-direction: column; min-height: 820px; }
.node  { flex: 0 0 auto; }              /* natural height, never stretched */
.gap   { flex-shrink: 0; min-height: 20px; }  /* stretches to fill the track, per sqrt(km) */
```

```html
<div class="node">...Depart...</div>
<div class="gap" style="flex-grow: 1;"><span class="gap-label">1 km</span></div>
<div class="node decision">...Dinner decision...</div>
<div class="gap" style="flex-grow: 8.7;"><span class="gap-label">76 km of NH44 — nothing scheduled until here</span></div>
<div class="node decision">...Optional stretch break...</div>
```

This went through two failed versions before landing here:

- **v1 (rejected as "not smart"):** absolute-positioned nodes at hand-computed pixel offsets
  (`top: 304px`, etc.), derived from a manually eyeballed px-per-km scale for this one specific
  route. Two real problems: a very long empty stretch would need the whole track to scale with the
  *longest* trip ever planned, not the current one; and dense clusters near the origin (multiple
  stops within a few km of each other) got crammed into a handful of pixels with no room for their
  content.
- **v2 (this one):** flex-grow driven by `sqrt(gap_km)`, not raw km. Raw linear km was tried and
  broken first — one long empty stretch would swallow the whole flex-grow pool and shove every other
  gap down to its `min-height` floor. Square root keeps a 76km stretch reading as clearly longer
  than a 3km one without either failure mode, and — critically — it's a **formula**, not a
  per-route hand calculation: `flex-grow: Math.sqrt(gapKm)` computed straight from
  `calculate_route_and_etas`'s real distances between consecutive stops would generalize to any
  route, unlike the pixel version.

### Typography and palette (this sketch specifically)

Space Mono (km labels, gap-distance annotations, the `// prompt` lines introducing a decision) +
Karla (place names, body text) — a "technical journal" pairing, data in monospace, prose in a warm
humanist sans. Warm off-white paper (`#f5f4f0`) / near-black ink (`#1a1a1a`), single rust/terracotta
accent (`#b3502c`) reserved for decision points and the active state — not reconciled with any of
the three illustration palettes in DESIGN.md, or with the Karla/Saira Condensed pairing now live in
`app.py`'s own theme.

## The Dossier — fixed document, not a chat reply

Built as the structural alternative to The Strip, for comparison — not the direction that got
picked, but worth keeping on record since the choice between them wasn't obvious going in.

**Core idea:** the plan is a static packet you'd screenshot or print, styled like an actual travel
document (a ticket-header block with a route code, a perforated-edge visual break) rather than a
chat message. You read it once; asking a question ("revise plan") is an explicit, separate action
from reading it, not an in-place edit to a running conversation.

- **Ticket-style header**: origin → destination, a monospace "route code" (`NH44 · ANTP-KNL-0826`,
  evoking a boarding pass), key stats in a stat row.
- **Tearable stubs**: each option is a card styled like a detachable coupon (a dashed vertical rule
  separating a rank/rating column from the body), with a "tear here" edge you click to mark it
  taken — a physical, one-time-use metaphor rather than a persistent selection state.
- **A visible seam between reading and revising**: the "revise plan" control is explicitly labeled
  as opening a separate action, not a chat input sitting right below the content implying it's the
  same continuous surface.

## If either of these gets built for real

Neither is wired into `app.py`. Building The Strip for real would mean:

1. Deriving `_TRAVEL_SPIRIT`-style stop/gap data from the structured plan's `itinerary_timeline` and
   the route's actual leg distances (already available from `calculate_route_and_etas`), not
   hand-authored HTML.
2. Replacing `render_plan_cards`' current card-per-option Streamlit layout with a genuinely
   different DOM structure — this is a bigger lift than a CSS reskin, since Streamlit's own layout
   primitives (`st.container`, `st.columns`) don't natively support the flex-grow-by-sqrt-distance
   mechanism; it would need custom HTML/CSS via `st.markdown(unsafe_allow_html=True)` or a custom
   component, not native widgets.
3. Reconciling typography/palette with whatever `app.py` ends up using as its real theme (currently
   Karla/Saira Condensed via `.streamlit/config.toml` — see DESIGN.md), since none of this sketch's
   choices were made against that constraint.
