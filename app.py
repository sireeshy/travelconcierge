import streamlit as st
import google.genai as genai
from google.genai import types
import requests
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from dateutil import parser
import json
import os
import dotenv

dotenv.load_dotenv()

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


def search_places_along_route(encoded_polyline: str, category: str = "restaurant") -> dict:
    """
    Searches for places of a specific category along an encoded polyline route.
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

departure_time_str = st.text_input("Departure Time (e.g., '11:30 AM' or '2026-08-27T11:30:00Z')", "11:30 AM")
preferences = st.text_area(
    "Preferences / Notes",
    "Traveling with elderly parents, need pure veg and clean restrooms"
)

try:
    if 'T' in departure_time_str or '-' in departure_time_str:
        departure_datetime = parser.parse(departure_time_str)
    else:
        today = datetime.now().date()
        time_obj = parser.parse(departure_time_str).time()
        departure_datetime = datetime.combine(today, time_obj)
    if departure_datetime.tzinfo is not None:
        departure_time_iso = departure_datetime.isoformat(timespec='seconds')
    else:
        departure_time_iso = departure_datetime.isoformat(timespec='seconds') + 'Z'
except Exception as e:
    st.error(f"Invalid Departure Time format: {e}")
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
        "for the user, focusing on comfortable and safe pitstops for food and restrooms. "
        "Prioritize clean restrooms and appropriate food options based on user preferences. "
        "Evaluate each place against the Highway Parameter Rubric. Provide structured, scannable Markdown output. "
        "Be extremely helpful and empathetic. Speak in a friendly, conversational tone, like a knowledgeable local guide. "
        "Do not make up information. Only use the tools provided to gather information. "
        "If a tool call returns an error, do not retry it with guessed or reformatted inputs and do not invent "
        "place IDs or details — report the limitation to the user instead. "
        "When calculating ETAs, consider the 'departure_time_iso' for traffic. "
        "Always try to find multiple suitable stops, but call search_places_along_route at most once per category. "
        "Call get_place_details_and_reviews exactly once, passing the place_ids of every candidate place "
        "you want details for together in one list, instead of calling it separately per place. "
        "When outputting the final plan, include a summary itinerary timeline at the top with departure and stop arrival times. "
        "Use emojis (🟢 Good, 🟡 Moderate, ⚠️ Red Flag) for rubrics. Provide actual ratings and review snippets. "
        "For food, explicitly state if it's 'Pure Veg', 'Veg & Non-Veg', or 'Fast Food/Chains'. "
        "If traveling with elders, flag places with no traditional Indian meals (Roti/Dal/Thali) as ⚠️. "
        "Verify if the kitchen is open and serving the appropriate meal (Breakfast/Lunch/Snacks/Dinner) at the calculated ETA for the stop."
    )

    # Set up configuration with tools and system instruction
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=gemini_tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=12
        )
    )

    # Create chat session using the new SDK
    chat = client.chats.create(
        model='gemini-3.6-flash',
        config=config
    )

    with st.spinner("Planning your highway trip and evaluating live stops..."):
        prompt = (
            f"Plan a highway trip from {st.session_state.origin} to {st.session_state.destination}. "
            f"My departure time is {st.session_state.departure_time_iso}. "
            f"Here are my preferences/notes: {st.session_state.preferences}. "
            "First, calculate the route and ETAs. Next, search for places along the route polyline. "
            "Finally, fetch place details/reviews for the best options and evaluate them against the rubric."
        )

        final_response = chat.send_message(prompt)

    st.subheader("Your Personalized Highway Itinerary")
    st.markdown(final_response.text)