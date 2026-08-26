import streamlit as st
import pydeck as pdk
import google.genai as genai
from google.genai import types
import requests
from datetime import datetime, timedelta, timezone
from dateutil import parser
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo
import base64
import csv
import functools
import logging
import os
import re
import time
import dotenv

dotenv.load_dotenv()

# Plain stdout logging -- Streamlit Community Cloud's "Manage app" log viewer shows this directly,
# no extra setup needed. Kept separate from the usage_log.csv below: this is for "what happened,
# skim server logs" visibility, the CSV is for "how long is this actually taking, per request".
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("journey_concierge")

# CSV lives next to app.py, not in the repo (gitignored) -- structured per-request timing data for
# local analysis. Note this resets on every Streamlit Community Cloud restart/redeploy since its
# filesystem is ephemeral; treat it as a local-dev tool, not a durable analytics store.
USAGE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_log.csv")


def log_usage_event(event_type: str, origin: str, destination: str, preferences: str, duration_s: float, tool_trace: list[dict]):
    """Appends one row per user-facing request (initial plan or follow-up) to usage_log.csv, and
    mirrors a summary line to stdout. tool_trace is the list of {name, detail, duration_s, ok}
    dicts collected by @timed_tool during this request -- this is what actually answers "what are
    people using this for and how long does each part take," not just the total."""
    tool_summary = "; ".join(f"{t['name']}{t['detail']}({t['duration_s']:.1f}s)" for t in tool_trace)
    logger.info(
        "usage event=%s origin=%r destination=%r duration_s=%.2f tool_calls=%d [%s]",
        event_type, origin, destination, duration_s, len(tool_trace), tool_summary,
    )
    try:
        file_exists = os.path.exists(USAGE_LOG_PATH)
        with open(USAGE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp_utc", "event", "origin", "destination", "preferences", "duration_s", "tool_calls"])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                event_type,
                origin,
                destination,
                preferences.replace("\n", " ")[:200],
                f"{duration_s:.2f}",
                tool_summary,
            ])
    except Exception:
        logger.exception("failed to write usage_log.csv")


# Hand-rolled instead of using the `polyline` pip package or Google's Static Maps API (which would
# draw the map for us) -- Static Maps API needs a separate Cloud Console enablement most keys won't
# have by default (this app hit that wall repeatedly with other "one more API" surprises), and the
# decode algorithm itself is short, stable, and dependency-free. See render_route_map() below.
def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decodes a Google-encoded polyline string into a list of (lat, lng) points."""
    points = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append((lat / 1e5, lng / 1e5))
    return points

_timezone_finder = TimezoneFinder()

# --- Helper Function for Google Maps Autocomplete ---

def get_place_predictions(query_text: str, api_key: str) -> list[str]:
    """Fetches autocomplete predictions from the Places API (New) for guaranteed place selections."""
    if not query_text or not api_key:
        return [query_text] if query_text else []

    url = "https://places.googleapis.com/v1/places:autocomplete"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
    }
    data = {"input": query_text, "languageCode": "en"}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=5)
        if response.ok:
            suggestions = response.json().get('suggestions', [])
            predictions = [
                s['placePrediction']['text']['text']
                for s in suggestions
                if 'placePrediction' in s
            ]
            if predictions:
                return predictions
    except Exception:
        pass
    return [query_text]


@st.cache_data(ttl=3600, show_spinner=False)
def get_timezone_for_location(location: str, api_key: str) -> str:
    """Resolves a place name to its IANA timezone (e.g. 'Asia/Kolkata') by geocoding it and
    then looking up the timezone offline for those coordinates -- no separate Time Zone API
    call or extra enablement needed. Falls back to Asia/Kolkata (this app's home turf) if the
    location can't be geocoded."""
    if location and api_key:
        try:
            response = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": location, "key": api_key},
                timeout=5,
            )
            results = response.json().get('results', [])
            if results:
                loc = results[0]['geometry']['location']
                tz_name = _timezone_finder.timezone_at(lat=loc['lat'], lng=loc['lng'])
                if tz_name:
                    return tz_name
        except Exception:
            pass
    return "Asia/Kolkata"

# --- Gemini Tool Definitions ---

_TOOL_LABELS = {
    "calculate_route_and_etas": "🚗 Calculating route and ETAs",
    "search_places_along_route": "🔍 Searching along the route",
    "get_place_details_and_reviews": "📋 Checking reviews, hours, and parking",
}


def _tool_call_detail(name: str, kwargs: dict) -> str:
    """Short human-readable suffix for a tool call, used both in the live progress status and the
    usage log -- e.g. what category was searched, or how many places were looked up."""
    if name == "search_places_along_route":
        return f" for \"{kwargs.get('category', '')}\""
    if name == "get_place_details_and_reviews":
        n = len(kwargs.get('place_ids') or [])
        return f" ({n} candidate place{'s' if n != 1 else ''})"
    if name == "calculate_route_and_etas":
        return f" ({kwargs.get('origin', '')} → {kwargs.get('destination', '')})"
    return ""


def timed_tool(func):
    """Wraps a Gemini tool function to (1) post a line to the live st.status progress panel as the
    call starts, and (2) record its name/detail/duration/outcome into st.session_state['_tool_trace']
    for the usage log. Automatic function calling drives these tool functions synchronously inside
    one blocking chat.send_message() call, so a live progress panel is the only way to show the user
    what's happening without a bigger architecture change (see handoff notes on concurrent tool
    execution) -- this decorator is how each tool reports in.

    functools.wraps preserves __name__/__doc__/__wrapped__ so inspect.signature(wrapper) still
    resolves to the original function's signature -- required for the genai SDK to build the tool's
    schema correctly from the wrapped function.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        detail = _tool_call_detail(func.__name__, kwargs)
        status = st.session_state.get("_progress_status")
        if status is not None:
            status.write(f"{_TOOL_LABELS.get(func.__name__, func.__name__)}{detail}...")

        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            ok = not (isinstance(result, dict) and "error" in result)
            return result
        except Exception:
            ok = False
            raise
        finally:
            duration_s = time.monotonic() - start
            trace = st.session_state.get("_tool_trace")
            if trace is not None:
                trace.append({"name": func.__name__, "detail": detail, "duration_s": duration_s, "ok": ok})
            logger.info("tool=%s%s duration_s=%.2f ok=%s", func.__name__, detail, duration_s, ok)
    return wrapper


@timed_tool
def calculate_route_and_etas(origin: str, destination: str, departure_time_iso: str) -> dict:
    """
    Calculates the route between an origin and destination, providing total duration, distance,
    estimated toll cost, and estimated arrival times (ETAs) for major milestones, considering traffic.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "Google Maps API Key is not set in the sidebar."}

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters,routes.legs.polyline.encodedPolyline,routes.polyline.encodedPolyline,routes.travelAdvisory.tollInfo"
    }
    data = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "departureTime": departure_time_iso,
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "en-US",
        "units": "METRIC",
        # Toll estimates are opt-in: extraComputations must list "TOLLS", and the API requires
        # routeModifiers.vehicleInfo to be present (any value) before it will compute a price.
        "extraComputations": ["TOLLS"],
        "routeModifiers": {"vehicleInfo": {"emissionType": "GASOLINE"}}
    }

    response = requests.post(url, headers=headers, json=data)
    if not response.ok:
        return {"error": f"Routes API error {response.status_code}: {response.text}"}
    routes_data = response.json()

    if not routes_data.get('routes'):
        return {"total_duration_seconds": 0, "total_distance_meters": 0, "legs": [], "encoded_overall_polyline": ""}

    route = routes_data['routes'][0]
    legs = []
    for leg_data in route['legs']:
        legs.append({
            "duration_seconds": int(leg_data['duration'].replace('s', '')),
            "distance_meters": leg_data['distanceMeters'],
            # There is no legs.startAddress/endAddress field in Routes API v2 (that's a legacy
            # Directions API field name that silently 400s here) -- since this app never passes
            # waypoints, computeRoutes always returns exactly one leg spanning origin->destination,
            # so the function's own params are a safe stand-in for the address strings.
            "end_address": destination,
            "start_address": origin,
            "encoded_polyline": leg_data['polyline']['encodedPolyline']
        })

    toll_prices = route.get('travelAdvisory', {}).get('tollInfo', {}).get('estimatedPrice', [])
    estimated_toll = None
    if toll_prices:
        price = toll_prices[0]
        estimated_toll = f"{price.get('units', '0')}.{price.get('nanos', 0) // 10_000_000:02d} {price.get('currencyCode', '')}".strip()

    # Not sent to the model -- stashed for the UI to draw the route on a map.
    st.session_state.route_polyline = route['polyline']['encodedPolyline']

    return {
        "total_duration_seconds": int(route['duration'].replace('s', '')),
        "total_distance_meters": route['distanceMeters'],
        "legs": legs,
        "encoded_overall_polyline": route['polyline']['encodedPolyline'],
        "estimated_toll_cost": estimated_toll
    }


@timed_tool
def search_places_along_route(encoded_polyline: str, category: str) -> dict:
    """
    Searches for places along an encoded polyline route matching a free-text query.

    'category' has no default value on purpose: it used to default to "restaurant", which meant
    the model would silently fall back to restaurant searches for any request it didn't reason
    carefully about (e.g. "pick up snacks and drinks" still returned restaurants). Making it
    required, plus the system prompt's explicit "don't default to restaurants" instruction, forces
    the model to actually decide what kind of place fits the request.

    'category' is a natural-language search query for whatever the user actually needs along the
    route -- not limited to food. Pick a query that matches their request, e.g. "vegetarian
    restaurant", "clean public restroom", "grocery store", "liquor store", "convenience store
    selling snacks and drinks", "petrol pump", "pharmacy", "ATM". Call this once per distinct kind
    of stop the user needs.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "Google Maps API Key is not set in the sidebar."}

    # There is no dedicated "search along route" endpoint in the Places API (New) -- that's a
    # parameter (searchAlongRouteParameters) on ordinary Text Search, not its own URL. An earlier
    # version of this app called a nonexistent places:searchAlongRoute endpoint, which 404'd every
    # time and drove the model to hallucinate a placeholder place_id to keep going.
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName.text,places.rating,places.userRatingCount,places.location,places.types,places.formattedAddress"
    }
    data = {
        "textQuery": category,
        "searchAlongRouteParameters": {"polyline": {"encodedPolyline": encoded_polyline}},
        "pageSize": 20,
        "languageCode": "en-US",
        "minRating": 3.5,
    }

    response = requests.post(url, headers=headers, json=data)
    if not response.ok:
        # This call fails occasionally and transiently even with a valid, well-formed request --
        # observed once with a real 360km route that a direct retry of the identical request
        # immediately succeeded on. This print is deliberately kept (error path only, not per-call)
        # so a recurrence shows up in server logs with enough detail to tell transient flakiness
        # apart from a real, reproducible request problem.
        print(f"[search_places_along_route] category={category!r} polyline_len={len(encoded_polyline)} HTTP {response.status_code}: {response.text}", flush=True)
        return {"error": f"Places API error {response.status_code}: {response.text}"}
    places_data = response.json()

    places = []
    if 'discovered_places' not in st.session_state:
        st.session_state.discovered_places = {}
    for p_data in places_data.get('places', [])[:5]:
        places.append({
            "place_id": p_data['id'],
            "name": p_data['displayName']['text'],
            "rating": p_data.get('rating'),
            "user_ratings_total": p_data.get('userRatingCount'),
            "vicinity": p_data.get('formattedAddress', ''),
            "types": p_data.get('types', [])[:4]
        })

        # Track every discovered place so the UI can offer a "Navigate" link and a map marker for
        # it later -- the chat response is free-form text, so this is the only reliable source of
        # real place_ids and coordinates. Lat/lng isn't sent to the model, just stashed for the UI.
        location = p_data.get('location', {})
        st.session_state.discovered_places[p_data['id']] = {
            "name": p_data['displayName']['text'],
            "vicinity": p_data.get('formattedAddress', ''),
            "lat": location.get('latitude'),
            "lng": location.get('longitude'),
        }

    return {"places": places}


@timed_tool
def get_place_details_and_reviews(place_ids: list[str]) -> dict:
    """
    Fetches detailed information, operating hours, and top user reviews for MULTIPLE places at once.
    Pass every candidate place_id from search_places_along_route in a single call (do not call this
    once per place) so all pitstop options are evaluated together.

    This is intentionally batched (one call per plan, not one per place) because the automatic
    function-calling loop that drives this app has a capped number of round trips
    (see maximum_remote_calls below) -- evaluating 5 candidate places used to cost 5 of that budget
    on its own, which was the dominant reason plans ran out of calls before producing a final answer.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "Google Maps API Key is not set in the sidebar."}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # currentOpeningStatus is NOT a real field on the Place resource (it 400s) -- the actual
        # field for "is it open right now" is currentOpeningHours.openNow, mapped below.
        "X-Goog-FieldMask": "id,displayName.text,rating,userRatingCount,formattedAddress,nationalPhoneNumber,websiteUri,currentOpeningHours,priceLevel,regularOpeningHours,reviews,servesBreakfast,servesLunch,servesDinner,servesVegetarianFood,parkingOptions,photos"
    }

    results = []
    for place_id in place_ids:
        url = f"https://places.googleapis.com/v1/places/{place_id}"
        response = requests.get(url, headers=headers)
        if not response.ok:
            results.append({"place_id": place_id, "error": f"Places API error {response.status_code}: {response.text}"})
            continue
        details_data = response.json()

        reviews = []
        for r_data in details_data.get('reviews', [])[:3]:
            review_text = r_data.get('text', {}).get('text', '')
            reviews.append({
                "author_name": r_data.get('authorAttribution', {}).get('displayName', 'Anonymous'),
                "rating": r_data.get('rating', 0),
                "text": review_text[:280]
            })

        parking_options = details_data.get('parkingOptions', {})
        available_parking = [
            label for flag, label in [
                ('freeParkingLot', 'free parking lot'),
                ('paidParkingLot', 'paid parking lot'),
                ('freeStreetParking', 'free street parking'),
                ('paidStreetParking', 'paid street parking'),
                ('valetParking', 'valet parking'),
                ('freeGarageParking', 'free garage parking'),
                ('paidGarageParking', 'paid garage parking'),
            ] if parking_options.get(flag)
        ]

        results.append({
            "place_id": place_id,
            "opening_hours": details_data.get('regularOpeningHours'),
            "reviews": reviews,
            "current_opening_status": (
                "Open now" if details_data.get('currentOpeningHours', {}).get('openNow')
                else "Closed now" if 'currentOpeningHours' in details_data
                else None
            ),
            "price_level": details_data.get('priceLevel'),
            "serves_breakfast": details_data.get('servesBreakfast'),
            "serves_lunch": details_data.get('servesLunch'),
            "serves_dinner": details_data.get('servesDinner'),
            "serves_vegetarian_food": details_data.get('servesVegetarianFood'),
            "parking_available": available_parking if available_parking else "Not listed by Google -- mention this is unverified if parking matters for this trip"
        })

        # Not sent to the model (not useful context, just tokens) -- stashed for the UI to render a photo.
        photos = details_data.get('photos', [])
        if place_id in st.session_state.get('discovered_places', {}) and photos:
            st.session_state.discovered_places[place_id]['photo_name'] = photos[0]['name']

    return {"details": results}


def render_copy_and_share(text: str):
    """Renders a Copy button and a Share-on-WhatsApp button for a block of text.

    WhatsApp's pre-filled share links (wa.me / api.whatsapp.com) can silently fail or get
    truncated for long text, so the share button also copies the full text to the clipboard
    as a guaranteed fallback the user can paste in if the pre-fill doesn't come through.
    """
    b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
    # st.markdown(unsafe_allow_html=True) sanitizes out inline event handlers (onclick etc.),
    # so this needs an actual iframe via st.iframe instead, which allows real JS.
    st.iframe(
        f"""
        <div style="display:flex; gap:8px; font-family:sans-serif;">
          <button onclick="
            (function(btn){{
              const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
              const decoded = new TextDecoder('utf-8').decode(bytes);
              navigator.clipboard.writeText(decoded).then(() => {{
                const orig = btn.innerText;
                btn.innerText = '✅ Copied!';
                setTimeout(() => {{ btn.innerText = orig; }}, 1500);
              }});
            }})(this)
          " style="padding:6px 14px; border-radius:6px; border:1px solid #999; background:#f0f2f6; color:#31333F; cursor:pointer; font-size:14px;">📋 Copy</button>
          <button onclick="
            (function(btn){{
              const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
              const decoded = new TextDecoder('utf-8').decode(bytes);
              navigator.clipboard.writeText(decoded);
              window.open('https://api.whatsapp.com/send?text=' + encodeURIComponent(decoded), '_blank');
            }})(this)
          " style="padding:6px 14px; border-radius:6px; border:1px solid #25D366; background:#25D366; color:white; cursor:pointer; font-size:14px;">📤 Share on WhatsApp</button>
        </div>
        """,
        height=45,
    )


def render_navigate_links():
    """Lets the user pick which discovered places to actually use as stops, in visit order, then
    renders one Google Maps link with the whole route: current location -> stop(s) -> trip
    destination, using Maps' waypoints so every selected stop is set automatically in one tap
    instead of navigating to each place separately."""
    places = st.session_state.get('discovered_places', {})
    if not places:
        return
    st.caption("🗺️ Build your route:")
    labels = [info['name'] for info in places.values()]
    label_to_id = {info['name']: place_id for place_id, info in places.items()}
    selected = st.multiselect(
        "Pick stops to include, in the order you'll visit them",
        options=labels,
        key="route_stops_selected",
    )
    if not selected:
        st.caption("Select one or more stops above, then get one link with your whole route set up in Maps.")
        return

    waypoints = "|".join(
        requests.utils.quote(f"{places[label_to_id[name]]['name']}, {places[label_to_id[name]]['vicinity']}")
        for name in selected
    )
    destination = requests.utils.quote(st.session_state.get('destination', ''))
    maps_url = (
        f"https://www.google.com/maps/dir/?api=1&destination={destination}"
        f"&waypoints={waypoints}&travelmode=driving"
    )
    st.markdown(
        f'<a href="{maps_url}" target="_blank" style="display:inline-block; margin-top:4px; padding:8px 16px; '
        'border-radius:6px; border:1px solid #4285F4; background:#4285F4; color:white; text-decoration:none; '
        'font-weight:600; font-size:14px;">🗺️ Get Directions with Selected Stops</a>',
        unsafe_allow_html=True,
    )


def render_route_map():
    """Draws the calculated route (decoded from its polyline) with a marker for every discovered
    place, so the user can see the trip and stops at a glance instead of only reading about them."""
    polyline = st.session_state.get('route_polyline')
    if not polyline:
        return
    path = decode_polyline(polyline)
    if not path:
        return

    layers = [pdk.Layer(
        "PathLayer",
        data=[{"path": [[lng, lat] for lat, lng in path]}],
        get_path="path",
        get_width=5,
        get_color=[66, 133, 244],
        width_min_pixels=3,
    )]

    places = st.session_state.get('discovered_places', {})
    marker_data = [
        {"lat": info['lat'], "lng": info['lng'], "name": info['name']}
        for info in places.values() if info.get('lat') is not None
    ]
    if marker_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=marker_data,
            get_position=["lng", "lat"],
            get_fill_color=[234, 67, 53],
            get_radius=300,
            pickable=True,
        ))

    mid_lat, mid_lng = path[len(path) // 2]
    st.caption("🗺️ Route Map:")
    st.pydeck_chart(pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lng, zoom=8),
        layers=layers,
        tooltip={"text": "{name}"} if marker_data else None,
    ))


def render_place_photos():
    """Shows a photo for each discovered place that has one. Fetched server-side (not linked
    directly as an <img src>) so the Maps API key is never exposed to the browser."""
    places = st.session_state.get('discovered_places', {})
    photo_entries = [(info['name'], info['photo_name']) for info in places.values() if info.get('photo_name')]
    api_key = st.session_state.get('google_maps_api_key')
    if not photo_entries or not api_key:
        return

    st.caption("📸 Photos:")
    cols = st.columns(min(len(photo_entries), 4))
    for i, (name, photo_name) in enumerate(photo_entries[:4]):
        try:
            resp = requests.get(
                f"https://places.googleapis.com/v1/{photo_name}/media",
                params={"maxWidthPx": 400, "key": api_key},
                timeout=5,
            )
            if resp.ok:
                with cols[i % len(cols)]:
                    st.image(resp.content, caption=name, width='stretch')
        except Exception:
            pass


# --- Streamlit UI ---
st.set_page_config(page_title="🧭 Journey Concierge", layout="wide")

st.title("🧭 Journey Concierge")

# Sidebar for API keys
with st.sidebar:
    st.header("API Keys Configuration")
    has_env_maps_key = bool(os.environ.get("GOOGLE_MAPS_API_KEY"))
    has_env_gemini_key = bool(os.environ.get("GEMINI_API_KEY"))
    google_maps_key_input = st.text_input(
        "Google Maps API Key" + (" (server key configured)" if has_env_maps_key else ""),
        type="password",
        placeholder="Using server-configured key" if has_env_maps_key else None,
    )
    gemini_key_input = st.text_input(
        "GEMINI API Key" + (" (server key configured)" if has_env_gemini_key else ""),
        type="password",
        placeholder="Using server-configured key" if has_env_gemini_key else None,
    )
    # Server-side env vars are used as a fallback without ever being sent to the
    # browser as a widget value, so a public deployment doesn't leak its own keys.
    st.session_state.google_maps_api_key = google_maps_key_input or os.environ.get("GOOGLE_MAPS_API_KEY", "")
    st.session_state.gemini_api_key = gemini_key_input or os.environ.get("GEMINI_API_KEY", "")
    st.markdown("---")
    st.info("Get your Google Maps API Key from Google Cloud Console. Enable Routes API, Places API, Places API (New), and Places Autocomplete API.")
    st.info("Get your Gemini API Key from Google AI Studio.")

# Main Page Inputs
st.header("Trip Details")

col1, col2 = st.columns(2)

with col1:
    origin_search = st.text_input("Search Origin", value="Anantapur")
    if st.session_state.get("google_maps_api_key") and origin_search:
        origin_options = get_place_predictions(origin_search, st.session_state.google_maps_api_key)
    else:
        origin_options = [origin_search]
    origin = st.selectbox("📍 Confirmed Origin (Google Maps)", options=origin_options)

with col2:
    dest_search = st.text_input("Search Destination", value="Kurnool")
    if st.session_state.get("google_maps_api_key") and dest_search:
        dest_options = get_place_predictions(dest_search, st.session_state.google_maps_api_key)
    else:
        dest_options = [dest_search]
    destination = st.selectbox("🎯 Confirmed Destination (Google Maps)", options=dest_options)

origin_tz_name = get_timezone_for_location(origin, st.session_state.get("google_maps_api_key", ""))
origin_tz = ZoneInfo(origin_tz_name)
now_local = datetime.now(origin_tz)
st.caption(f"🕐 Times below are local time at your origin ({origin_tz_name.replace('_', ' ')}).")

st.markdown("**Departure Date**")
# Pattern for all three quick-select button rows in this file (date, then time below): writing to
# st.session_state[key] BEFORE the widget with that key is instantiated overrides its value for
# this rerun. Doing it the other way around (setting state after the widget call) raises, since
# Streamlit already owns that key by then -- the buttons must run first in the script.
date_col1, date_col2, date_col3 = st.columns(3)
with date_col1:
    if st.button("Today", width='stretch'):
        st.session_state.departure_date = now_local.date()
with date_col2:
    if st.button("Tomorrow", width='stretch'):
        st.session_state.departure_date = now_local.date() + timedelta(days=1)
with date_col3:
    if st.button("Day after", width='stretch'):
        st.session_state.departure_date = now_local.date() + timedelta(days=2)
departure_date = st.date_input(
    "Departure date", key="departure_date", value=now_local.date(), label_visibility="collapsed"
)

def _format_time_12h(t):
    return t.strftime("%I:%M %p").lstrip("0")

st.markdown("**Departure Time**")
time_col1, time_col2, time_col3 = st.columns(3)
with time_col1:
    if st.button("Now", width='stretch'):
        st.session_state.departure_time_text = _format_time_12h(now_local)
with time_col2:
    if st.button("1 hr from now", width='stretch'):
        st.session_state.departure_time_text = _format_time_12h(now_local + timedelta(hours=1))
with time_col3:
    st.button("Custom", width='stretch', disabled=True, help="Type any time below, e.g. '630pm' or '6:30 PM'")
departure_time_str = st.text_input(
    "Departure time", key="departure_time_text", value=_format_time_12h(now_local),
    label_visibility="collapsed"
)

# Accept compact times like "630pm" or "630 pm" by inserting the colon dateutil expects.
_compact_time_match = re.fullmatch(r'(\d{1,2})(\d{2})\s*([AaPp][Mm])', departure_time_str.strip())
if _compact_time_match:
    hour, minute, meridiem = _compact_time_match.groups()
    departure_time_str = f"{hour}:{minute} {meridiem}"

st.markdown("**Quick Preferences** (optional — combined with the notes below)")
qp_col1, qp_col2, qp_col3, qp_col4 = st.columns(4)
with qp_col1:
    want_veg = st.checkbox("🥗 Pure Veg")
with qp_col2:
    want_fuel = st.checkbox("⛽ Fuel Stop")
with qp_col3:
    want_restroom = st.checkbox("🚻 Restroom Break")
with qp_col4:
    want_snacks = st.checkbox("🍿 Snacks/Drinks")

preferences_notes = st.text_area(
    "Preferences / Notes",
    "Traveling with elderly parents, need pure veg and clean restrooms"
)

_quick_prefs = []
if want_veg:
    _quick_prefs.append("Pure vegetarian food only.")
if want_fuel:
    _quick_prefs.append("Need a fuel/petrol stop along the way.")
if want_restroom:
    _quick_prefs.append("Need a restroom break stop.")
if want_snacks:
    _quick_prefs.append("Need to pick up snacks and drinks.")
preferences = (" ".join(_quick_prefs) + " " + preferences_notes).strip() if _quick_prefs else preferences_notes

try:
    departure_time_val = parser.parse(departure_time_str).time()
    departure_datetime = datetime.combine(departure_date, departure_time_val, tzinfo=origin_tz)
    # The "Now" default is only fresh at page load -- if the page has been open a while and the
    # picked date/time has quietly drifted into the past, treat it as "as soon as possible" instead
    # of sending an invalid past timestamp to the Routes API.
    if departure_datetime < now_local:
        departure_datetime = now_local + timedelta(minutes=1)
        st.caption(f"⏱️ That time has passed — using {_format_time_12h(departure_datetime.time())} instead.")
    departure_time_iso = departure_datetime.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
except Exception as e:
    st.error(f"Invalid time format: {e}")
    departure_time_iso = None

if st.button("Plan My Trip", width='stretch'):
    if not st.session_state.get("google_maps_api_key") or not st.session_state.get("gemini_api_key"):
        st.warning("Please enter both Google Maps API Key and Gemini API Key in the sidebar.")
    elif not departure_time_iso:
        st.warning("Please fix the departure time format.")
    else:
        st.session_state.planning_triggered = True
        st.session_state.origin = origin
        st.session_state.destination = destination
        st.session_state.departure_time_iso = departure_time_iso
        st.session_state.preferences = preferences
        # Starting a new plan resets any prior conversation and everything the UI derived from it
        # (map, photos, navigate links all key off discovered_places/route_polyline) -- without this,
        # stale places/route from a previous trip would linger into the new one's map and stop list.
        st.session_state.chat = None
        st.session_state.chat_messages = []
        st.session_state.discovered_places = {}
        st.session_state.route_stops_selected = []
        st.session_state.route_polyline = None
        st.session_state.need_new_plan = True

if st.session_state.get('planning_triggered', False):
    if not st.session_state.get('gemini_api_key'):
        st.error("Gemini API Key is missing. Please set it in the sidebar.")
        st.stop()

    # Initialize the new Google GenAI Client
    client = genai.Client(api_key=st.session_state.gemini_api_key)

    # Function list for tools
    gemini_tools = [
        calculate_route_and_etas,
        search_places_along_route,
        get_place_details_and_reviews,
        types.Tool(google_search=types.GoogleSearch())
    ]

    # This prompt has been hardened against specific failure modes actually observed while building
    # this app, not written speculatively -- each numbered point below maps to a paragraph/bullet in
    # the text and is worth understanding before editing it:
    #   1. Early versions were framed entirely around "highway food/restroom pitstops," so the model
    #      defaulted every request (even "buy snacks for a friend visit") into a restaurant-shaped
    #      answer. The "don't default to restaurants" + "think like a concierge, not a search tool"
    #      framing exists specifically to break that bias -- see also search_places_along_route's
    #      required (no-default) `category` param above, which was the other half of that fix.
    #   2. When search_places_along_route failed, the model would quietly substitute restaurant names
    #      recalled from its own training data or a raw google_search instead of admitting the live
    #      search failed. Verified against live Places data: of 5 such recalled names, 3 were
    #      permanently closed and 1 was in the wrong city/state entirely -- i.e. this is a real,
    #      demonstrated risk for someone about to drive there with elderly parents, not a
    #      theoretical one. The explicit "do not fall back to naming places from your own knowledge"
    #      instruction exists specifically to prevent that.
    #   3. google_search is scoped narrowly (only to double-check a place search already found with
    #      few reviews) for the same reason -- without that scoping, the model would reach for it as
    #      a substitute discovery mechanism whenever the primary tool had a rough result.
    #   4. The proactive-timing paragraph (meal windows / biobreaks / late-night driving) was added
    #      because the app only reacted to what was explicitly asked for, missing the kind of
    #      forward-thinking a real concierge would offer unprompted (e.g. noting a very late arrival
    #      time, or that dinner-hour timing means suggesting food even if only fuel was requested).
    system_instruction = (
        "You are a Thoughtful Indian Journey Concierge. Your goal is to plan an optimal trip for the user -- "
        "whether it's a long highway drive between cities or a short trip across town -- and help with "
        "whatever stops they actually need along the way — this can include "
        "food, restrooms, fuel, pharmacies, ATMs, or errands like picking up snacks, drinks, or groceries. "
        "You are not just a search tool stringing together API results -- think like an actual concierge who "
        "knows the route and anticipates needs before being asked. Consider the trip holistically, not just as "
        "a list of isolated stops: Is the arrival time reasonable for who's traveling -- would you personally "
        "flag a very late-night arrival with elderly parents, or suggest an earlier departure or an overnight "
        "stop for a very long drive? Are the stops you're suggesting sensibly spaced along the actual route "
        "and ordered the way you'd actually hit them while driving, not just listed in search-result order? "
        "Give real opinions and specific advice the way a knowledgeable local friend would -- 'this one's worth "
        "it because X' or 'I'd skip Y and go with Z instead' -- rather than a mechanical, uniform list. "
        "Read the user's request carefully and search for the specific kind of place that matches it "
        "(e.g. a request for snacks and drinks means a grocery/convenience/liquor store, not a restaurant). "
        "Do not default to restaurants unless the user is actually asking about a meal. "
        "Provide structured, scannable Markdown output. "
        "Be extremely helpful and empathetic. Speak in a friendly, conversational tone, like a knowledgeable local guide. "
        "Do not make up information. Only use the tools provided to gather information. "
        "If a tool call returns an error, do not retry it with guessed or reformatted inputs and do not invent "
        "place IDs or details — report the limitation to the user instead. "
        "This applies to search_places_along_route specifically: if it errors, do not fall back to naming "
        "specific restaurants/places from your own knowledge or a general google_search, even with a caveat -- "
        "unverified place names routinely turn out to be permanently closed, in the wrong city, or simply "
        "nonexistent, which is worse than no recommendation for someone actually about to drive there. Instead, "
        "tell the user the live place search hit a temporary issue and suggest they try again. "
        "google_search is only for double-checking a place get_place_details_and_reviews already returned with "
        "very few reviews, not for discovering new candidate places when search_places_along_route fails. "
        "When calculating ETAs, consider the 'departure_time_iso' for traffic. "
        "Always try to find multiple suitable options, but call search_places_along_route at most once per kind of stop needed. "
        "Call get_place_details_and_reviews exactly once, passing the place_ids of every candidate place "
        "you want details for together in one list, instead of calling it separately per place. "
        "When outputting the final plan, include a summary itinerary timeline at the top with departure and stop "
        "arrival times, and mention the estimated toll cost for the route if calculate_route_and_etas returned one. "
        "Use emojis (🟢 Good, 🟡 Moderate, ⚠️ Red Flag) for quick-scan ratings. Provide actual ratings and review snippets. "
        "The following Trip Stop Rubric applies specifically when evaluating FOOD stops (skip it for "
        "non-food stops like shops, fuel, or pharmacies, and instead just note hours, ratings, and anything "
        "relevant from reviews): explicitly state if it's 'Pure Veg', 'Veg & Non-Veg', or 'Fast Food/Chains'; "
        "if traveling with elders, flag places with no traditional Indian meals (Roti/Dal/Thali) as ⚠️; "
        "verify the kitchen is open and serving the appropriate meal (Breakfast/Lunch/Snacks/Dinner) at the calculated ETA. "
        "For every recommended stop, mention parking availability using the 'parking_available' field from "
        "get_place_details_and_reviews. "
        "Think proactively about the journey's timing using the duration and ETAs from calculate_route_and_etas, "
        "and volunteer relevant suggestions even when the user didn't explicitly ask for them -- clearly flagged "
        "as proactive (e.g. under a '💡 Since your trip...' note) so they don't crowd out what was actually asked: "
        "- Compare the departure time and ETAs against typical Indian meal windows (breakfast ~7-10am, lunch "
        "~12:30-3pm, dinner ~7:30-10:30pm). If the journey overlaps one, proactively suggest a food stop timed to "
        "that point even if the user only asked for something else like fuel or snacks -- people traveling around "
        "mealtimes usually want to eat too. "
        "- Call search_places_along_route with a query like 'clean public restroom' or 'rest area' -- and include "
        "a distinct '🚻 Restroom Stops' section with real results from it -- only when the total drive duration "
        "exceeds 2 hours, or when the user specifically asked for restrooms regardless of trip length. Don't run "
        "this search for short trips unless it was actually requested. When it does apply, don't consider a food "
        "stop's restroom sufficient on its own, since it may not land at a convenient point in the drive; for 4+ "
        "hour trips, look for more than one restroom option spaced through the journey rather than one near the start. "
        "- If departure or a significant part of the drive falls late at night (roughly 10pm-5am), note that fewer "
        "places will be open and consider suggesting a tea/coffee stop for driver alertness. "
        "If a promising place has very few reviews (roughly under 10) and you're unsure it's reliable -- e.g. it "
        "might be new or low-quality -- use the google_search tool to check for other information about it (news, "
        "blog mentions, its own website) before recommending it, and say in the plan that you double-checked it "
        "this way since Google reviews were sparse."
    )

    # Set up configuration with tools and system instruction
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=gemini_tools,
        # Required to mix our custom function-calling tools with the built-in Google Search tool --
        # omitting this gives a 400 telling you to set exactly this flag.
        tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
        # The SDK default is 10. A typical plan now needs ~3-6 calls (route + 1-2 category searches
        # + one batched details call), well under that -- this is headroom for multi-need requests
        # (e.g. food + fuel + restrooms all in one trip) plus the occasional google_search
        # verification, not a response to normal usage running out. If the AFC loop exhausts this
        # budget mid-plan, the model's last turn is left holding an unresolved function_call with no
        # text response, which surfaces in the UI as a literal "None" -- if that recurs, the fix is
        # more budget here or fewer categories per plan, not a UI-side workaround.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=15
        )
    )

    # Reuse the same chat session across reruns so follow-up questions share context.
    if st.session_state.get('chat') is None:
        st.session_state.chat = client.chats.create(
            model='gemini-3.6-flash',
            config=config
        )
    chat = st.session_state.chat

    if 'chat_messages' not in st.session_state:
        st.session_state.chat_messages = []

    if st.session_state.get('need_new_plan', False):
        st.session_state.need_new_plan = False
        prompt = (
            f"Plan a trip from {st.session_state.origin} to {st.session_state.destination}. "
            f"My departure time is {st.session_state.departure_time_iso}. "
            f"Here are my preferences/notes: {st.session_state.preferences}. "
            "First, calculate the route and ETAs. Next, based on what I actually need (see my preferences/notes "
            "above), search along the route polyline for the appropriate kind of stop -- this might be food, "
            "restrooms, fuel, or an errand like buying snacks/drinks/groceries; don't assume it's a restaurant "
            "unless my notes actually ask for one. Finally, fetch place details/reviews for the best options and "
            "evaluate them appropriately for what I asked for."
        )
        status = st.status("Planning your trip and evaluating live stops...", expanded=True)
        st.session_state._progress_status = status
        st.session_state._tool_trace = []
        start = time.monotonic()
        response = chat.send_message(prompt)
        duration_s = time.monotonic() - start
        status.update(label=f"✅ Plan ready in {duration_s:.1f}s", state="complete")
        log_usage_event("plan", st.session_state.origin, st.session_state.destination,
                         st.session_state.preferences, duration_s, st.session_state._tool_trace)
        st.session_state._progress_status = None
        st.session_state.chat_messages.append({"role": "assistant", "content": response.text})

    st.subheader("Your Personalized Journey Plan")
    for message in st.session_state.chat_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant":
                render_copy_and_share(message["content"])

    render_route_map()
    render_place_photos()
    render_navigate_links()

    followup = st.chat_input("Ask a follow-up — e.g. 'suggest a different restaurant' or 'what about the return trip?'")
    if followup:
        st.session_state.chat_messages.append({"role": "user", "content": followup})
        status = st.status("Thinking...", expanded=True)
        st.session_state._progress_status = status
        st.session_state._tool_trace = []
        start = time.monotonic()
        response = chat.send_message(followup)
        duration_s = time.monotonic() - start
        status.update(label=f"✅ Answered in {duration_s:.1f}s", state="complete")
        log_usage_event("followup", st.session_state.origin, st.session_state.destination,
                         followup, duration_s, st.session_state._tool_trace)
        st.session_state._progress_status = None
        st.session_state.chat_messages.append({"role": "assistant", "content": response.text})
        st.rerun()