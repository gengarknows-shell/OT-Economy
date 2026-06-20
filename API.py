from ossapi import Ossapi, Scope
import requests
import os
import json
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
)


scopes = [ Scope.PUBLIC, Scope.FORUM_WRITE ]
api    = Ossapi(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, scopes = scopes)



"""
Since ossapi does not support fetching topics by forumID I had to make
a manual implementation
"""

FORUM_ID = 52
TOKEN_CACHE_FILE = "osu_token_cache.json"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_FORUM_URL = "https://osu.ppy.sh/api/v2/forums/{forum_id}"
# === TOKEN HANDLING ===
def get_cached_token():
    """Load cached token if valid"""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    with open(TOKEN_CACHE_FILE, "r") as f:
        cache = json.load(f)
    if cache["expires_at"] > time.time():
        return cache["access_token"]
    return None


def refresh_access_token():
    if not os.path.exists(TOKEN_CACHE_FILE):
        raise RuntimeError("Missing osu_token_cache.json — run getauth.py first.")
    
    with open(TOKEN_CACHE_FILE, "r") as f:
        token_data = json.load(f)

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Missing refresh_token in cache — re-run getauth.py")

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": "http://localhost:8000"
    }

    resp = requests.post(OSU_TOKEN_URL, data=data)
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text}")

    new_data = resp.json()
    new_data["expires_at"] = time.time() + new_data["expires_in"] - 30
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(new_data, f, indent=4)
    
    print("🔁 Token refreshed successfully!")
    return new_data["access_token"]




def save_token(token_data):
    """Save token with expiration time."""
    token_data["expires_at"] = time.time() + token_data["expires_in"] - 30  # refresh 30s early
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(token_data, f)
def request_new_token():
    """Request a new OAuth token from osu!"""
    data = {
        "client_id"     : CLIENT_ID,
        "client_secret" : CLIENT_SECRET,
        "grant_type"    : "client_credentials",
        "scope"         : "public",
    }
    resp = requests.post(OSU_TOKEN_URL, data=data)
    resp.raise_for_status()
    token_data = resp.json()
    save_token(token_data)
    return token_data["access_token"]

def get_access_token():
    token = get_cached_token()
    if token:
        return token
    try:
        return refresh_access_token()
    except Exception as e:
        print("⚠️ Could not refresh token, requesting new one:", e)
        return request_new_token()

def get_forum_data(forum_id, access_token):
    """Fetch a forum and its topics from osu! API using a provided access token."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    url = OSU_FORUM_URL.format(forum_id=forum_id)
    response = requests.get(url, headers=headers)

    # Retry once if token expired
    if response.status_code == 401:
        access_token = request_new_token()
        headers["Authorization"] = f"Bearer {access_token}"
        response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()




from concurrent.futures import ThreadPoolExecutor, as_completed

def get_posts_in_topic(topicid):
    """
    Return the 10 most recent post objects in a topic using OSSAPI.
    Each object contains full post data, e.g., id, content, user_id, etc.
    """
    try:
        topic_data = api.forum_topic(topic_id=topicid, sort="id_desc")
        return topic_data.posts[:10]  # up to 10 most recent posts
    except Exception as e:
        print(f"Error fetching posts for topic {topicid}: {e}")
        return []


def check_new_posts():
    """
    Checks for new posts in all topics of a subforum.
    Returns a list of lists of dicts:
        [
            [{"id": ..., "user_id": ..., "raw": ...}, ...],
        ]
    """
    access_token = get_access_token()
    first_run = not os.path.exists("topic_data.dat")
    previous_data = {}

    if not first_run:
        with open("topic_data.dat", "r") as f:
            try:
                previous_data = json.load(f)
            except json.JSONDecodeError:
                previous_data = {}

    forum_data = get_forum_data(FORUM_ID, access_token)
    topics = forum_data.get("pinned_topics", []) + forum_data.get("topics", [])

    new_posts_per_topic = []
    current_data = {}

    topics_to_fetch = []
    for topic in topics:
        topic_id = str(topic["id"])
        prev_post_ids = previous_data.get(topic_id, [])
        latest_known = prev_post_ids[0] if prev_post_ids else None
        latest_on_forum = str(topic.get("last_post_id", ""))
        if first_run or latest_known != latest_on_forum:
            topics_to_fetch.append(topic_id)
        else:
            current_data[topic_id] = prev_post_ids

    def post_to_dict(p):
        """Convert ForumPost to a dict with id, user_id, and raw text."""
        body = getattr(p, "body", None)
        raw = ""
        if body:
            raw = getattr(body, "raw", None) or getattr(body, "bbcode", None) or ""
        if not raw:
            raw = getattr(p, "raw", None) or getattr(p, "content", None) or ""
        user_id = getattr(p, "user_id", None)
        if user_id is None and hasattr(p, "user"):
            user_id = getattr(p.user, "id", None)
        return {"id": p.id, "user_id": user_id, "raw": raw}

    # Fetch posts in parallel
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_topic = {executor.submit(get_posts_in_topic, tid): tid for tid in topics_to_fetch}
        for future in as_completed(future_to_topic):
            topic_id = future_to_topic[future]
            posts = future.result()
            if not posts:
                # Preserve the last known post IDs so topic_data.dat stays
                # complete. Without this, the topic vanishes from current_data,
                # the next run treats every post as new, and old commands fire again.
                if topic_id in previous_data:
                    current_data[topic_id] = previous_data[topic_id]
                continue

            post_ids = [str(post.id) for post in posts]
            current_data[topic_id] = post_ids

            if not first_run:
                prev_post_ids = previous_data.get(topic_id, [])
                new_posts = [post_to_dict(post) for post in posts if str(post.id) not in prev_post_ids]
                for p in new_posts:
                    p["topic_id"] = topic_id
                if new_posts:
                    new_posts_per_topic.append(new_posts)

    with open("topic_data.dat", "w") as f:
        json.dump(current_data, f)

    if first_run:
        return []

    return new_posts_per_topic


def get_username(userid):
    return api.user(user=userid,key="id").username





