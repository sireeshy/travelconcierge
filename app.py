import streamlit as st
import streamlit.components.v1 as components
import google.genai as genai
from google.genai import types
import requests
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
from dateutil import parser
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo
import base64
import os
import re
import dotenv

dotenv.load_dotenv()

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

# --- Pydantic Models for Tool Outputs ---

class RouteLeg(BaseModel):
    duration_seconds: int
    distance_meters: int
    end_address: str
    start_address: str
    encoded_polyline: str

class RouteResponse(BaseModel):
    total_duration_seconds: int
    total_distance_meters: int
    legs: list[RouteLeg]
    encoded_overall_polyline: str

class Place(BaseModel):
    place_id: str
    name: str
    rating: float | None = None
    user_ratings_total: int | None = None
    vicinity: str
    types: list[str]

class SearchPlacesResponse(BaseModel):
    places: list[Place]

class Review(BaseModel):
    author_name: str
    rating: int
    text: str | None = None

class PlaceDetails(BaseModel):
    place_id: str
    opening_hours: dict | None = None
    reviews: list[Review]
    current_opening_status: str | None = None
    price_level: str | None = None
    serves_breakfast: bool | None = None
    serves_lunch: bool | None = None
    serves_dinner: bool | None = None
    serves_vegetarian_food: bool | None = None
    error: str | None = None

class PlaceDetailsBatchResponse(BaseModel):
    details: list[PlaceDetails]

# --- Gemini Tool Definitions ---

def calculate_route_and_etas(origin: str, destination: str, departure_time_iso: str) -> dict:
    """
    Calculates the route between an origin and destination, providing total duration, distance,
    and estimated arrival times (ETAs) for major milestones along the route, considering traffic.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "Google Maps API Key is not set in the sidebar."}

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters,routes.legs.polyline.encodedPolyline,routes.polyline.encodedPolyline"
    }
    data = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "departureTime": departure_time_iso,
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "en-US",
        "units": "METRIC"
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
            "end_address": destination,
            "start_address": origin,
            "encoded_polyline": leg_data['polyline']['encodedPolyline']
        })

    return {
        "total_duration_seconds": int(route['duration'].replace('s', '')),
        "total_distance_meters": route['distanceMeters'],
        "legs": legs,
        "encoded_overall_polyline": route['polyline']['encodedPolyline']
    }


def search_places_along_route(encoded_polyline: str, category: str) -> dict:
    """
    Searches for places along an encoded polyline route matching a free-text query.

    'category' is a natural-language search query for whatever the user actually needs along the
    route -- not limited to food. Pick a query that matches their request, e.g. "vegetarian
    restaurant", "clean public restroom", "grocery store", "liquor store", "convenience store
    selling snacks and drinks", "petrol pump", "pharmacy", "ATM". Call this once per distinct kind
    of stop the user needs.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "Google Maps API Key is not set in the sidebar."}

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
        return {"error": f"Places API error {response.status_code}: {response.text}"}
    places_data = response.json()

    places = []
    for p_data in places_data.get('places', [])[:5]:
        places.append({
            "place_id": p_data['id'],
            "name": p_data['displayName']['text'],
            "rating": p_data.get('rating'),
            "user_ratings_total": p_data.get('userRatingCount'),
            "vicinity": p_data.get('formattedAddress', ''),
            "types": p_data.get('types', [])[:4]
        })
    return {"places": places}


def get_place_details_and_reviews(place_ids: list[str]) -> dict:
    """
    Fetches detailed information, operating hours, and top user reviews for MULTIPLE places at once.
    Pass every candidate place_id from search_places_along_route in a single call (do not call this
    once per place) so all pitstop options are evaluated together.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "Google Maps API Key is not set in the sidebar."}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "id,displayName.text,rating,userRatingCount,formattedAddress,nationalPhoneNumber,websiteUri,currentOpeningHours,priceLevel,regularOpeningHours,reviews,servesBreakfast,servesLunch,servesDinner,servesVegetarianFood"
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
            "serves_vegetarian_food": details_data.get('servesVegetarianFood')
        })

    return {"details": results}


def render_copy_and_share(text: str):
    """Renders a Copy button and a Share-on-WhatsApp button for a block of text.

    WhatsApp's pre-filled share links (wa.me / api.whatsapp.com) can silently fail or get
    truncated for long text, so the share button also copies the full text to the clipboard
    as a guaranteed fallback the user can paste in if the pre-fill doesn't come through.
    """
    b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
    # st.markdown(unsafe_allow_html=True) sanitizes out inline event handlers (onclick etc.),
    # so this needs an actual iframe via components.html instead, which allows real JS.
    components.html(
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


# --- Streamlit UI ---
st.set_page_config(page_title="🛣️ Highway Pitstop Concierge", layout="wide")

st.title("🛣️ Highway Pitstop Concierge")

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
date_col1, date_col2, date_col3 = st.columns(3)
with date_col1:
    if st.button("Today", use_container_width=True):
        st.session_state.departure_date = now_local.date()
with date_col2:
    if st.button("Tomorrow", use_container_width=True):
        st.session_state.departure_date = now_local.date() + timedelta(days=1)
with date_col3:
    if st.button("Day after", use_container_width=True):
        st.session_state.departure_date = now_local.date() + timedelta(days=2)
departure_date = st.date_input(
    "Departure date", key="departure_date", value=now_local.date(), label_visibility="collapsed"
)

def _format_time_12h(t):
    return t.strftime("%I:%M %p").lstrip("0")

st.markdown("**Departure Time**")
time_col1, time_col2, time_col3 = st.columns(3)
with time_col1:
    if st.button("Now", use_container_width=True):
        st.session_state.departure_time_text = _format_time_12h(now_local)
with time_col2:
    if st.button("1 hr from now", use_container_width=True):
        st.session_state.departure_time_text = _format_time_12h(now_local + timedelta(hours=1))
with time_col3:
    st.button("Custom", use_container_width=True, disabled=True, help="Type any time below, e.g. '630pm' or '6:30 PM'")
departure_time_str = st.text_input(
    "Departure time", key="departure_time_text", value=_format_time_12h(now_local),
    label_visibility="collapsed"
)

# Accept compact times like "630pm" or "630 pm" by inserting the colon dateutil expects.
_compact_time_match = re.fullmatch(r'(\d{1,2})(\d{2})\s*([AaPp][Mm])', departure_time_str.strip())
if _compact_time_match:
    hour, minute, meridiem = _compact_time_match.groups()
    departure_time_str = f"{hour}:{minute} {meridiem}"

preferences = st.text_area(
    "Preferences / Notes",
    "Traveling with elderly parents, need pure veg and clean restrooms"
)

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

if st.button("Plan My Trip", use_container_width=True):
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
        # Starting a new plan resets any prior conversation.
        st.session_state.chat = None
        st.session_state.chat_messages = []
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
        get_place_details_and_reviews
    ]

    system_instruction = (
        "You are a Thoughtful Indian Highway Concierge. Your goal is to plan an optimal road trip "
        "for the user and help with whatever stops they actually need along the way — this can include "
        "food, restrooms, fuel, pharmacies, ATMs, or errands like picking up snacks, drinks, or groceries. "
        "Read the user's request carefully and search for the specific kind of place that matches it "
        "(e.g. a request for snacks and drinks means a grocery/convenience/liquor store, not a restaurant). "
        "Do not default to restaurants unless the user is actually asking about a meal. "
        "Provide structured, scannable Markdown output. "
        "Be extremely helpful and empathetic. Speak in a friendly, conversational tone, like a knowledgeable local guide. "
        "Do not make up information. Only use the tools provided to gather information. "
        "If a tool call returns an error, do not retry it with guessed or reformatted inputs and do not invent "
        "place IDs or details — report the limitation to the user instead. "
        "When calculating ETAs, consider the 'departure_time_iso' for traffic. "
        "Always try to find multiple suitable options, but call search_places_along_route at most once per kind of stop needed. "
        "Call get_place_details_and_reviews exactly once, passing the place_ids of every candidate place "
        "you want details for together in one list, instead of calling it separately per place. "
        "When outputting the final plan, include a summary itinerary timeline at the top with departure and stop arrival times. "
        "Use emojis (🟢 Good, 🟡 Moderate, ⚠️ Red Flag) for quick-scan ratings. Provide actual ratings and review snippets. "
        "The following Highway Parameter Rubric applies specifically when evaluating FOOD stops (skip it for "
        "non-food stops like shops, fuel, or pharmacies, and instead just note hours, ratings, and anything "
        "relevant from reviews): explicitly state if it's 'Pure Veg', 'Veg & Non-Veg', or 'Fast Food/Chains'; "
        "if traveling with elders, flag places with no traditional Indian meals (Roti/Dal/Thali) as ⚠️; "
        "verify the kitchen is open and serving the appropriate meal (Breakfast/Lunch/Snacks/Dinner) at the calculated ETA."
    )

    # Set up configuration with tools and system instruction
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=gemini_tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=12
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
            f"Plan a highway trip from {st.session_state.origin} to {st.session_state.destination}. "
            f"My departure time is {st.session_state.departure_time_iso}. "
            f"Here are my preferences/notes: {st.session_state.preferences}. "
            "First, calculate the route and ETAs. Next, based on what I actually need (see my preferences/notes "
            "above), search along the route polyline for the appropriate kind of stop -- this might be food, "
            "restrooms, fuel, or an errand like buying snacks/drinks/groceries; don't assume it's a restaurant "
            "unless my notes actually ask for one. Finally, fetch place details/reviews for the best options and "
            "evaluate them appropriately for what I asked for."
        )
        with st.spinner("Planning your highway trip and evaluating live stops..."):
            response = chat.send_message(prompt)
        st.session_state.chat_messages.append({"role": "assistant", "content": response.text})

    st.subheader("Your Personalized Highway Itinerary")
    for message in st.session_state.chat_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant":
                render_copy_and_share(message["content"])

    followup = st.chat_input("Ask a follow-up — e.g. 'suggest a different restaurant' or 'what about the return trip?'")
    if followup:
        st.session_state.chat_messages.append({"role": "user", "content": followup})
        with st.spinner("Thinking..."):
            response = chat.send_message(followup)
        st.session_state.chat_messages.append({"role": "assistant", "content": response.text})
        st.rerun()