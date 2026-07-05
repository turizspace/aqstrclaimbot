#!/usr/bin/env python3
"""
AQSTR Task Automation Bot – Full Loop (Educational)
====================================================
This script demonstrates:
- Fetching the user dashboard
- Parsing available tasks and eligibility
- Processing all eligible tasks in a loop
- Handling pagination / refreshing the list

WARNING: This is for EDUCATIONAL use only.
         Running this against the live service violates ToS.
         You WILL be banned and forfeit any earnings.
         Use a throwaway key and a local test environment.
"""

import os
import time
import json
import requests
import websocket
from datetime import datetime, timezone
from nostr.key import PrivateKey
from nostr.event import Event, EventKind
import hashlib
try:
    from coincurve import PrivateKey as CPrivateKey
except Exception:
    CPrivateKey = None

# ============ CONFIGURATION ============
NSEC = os.getenv("NOSTR_NSEC", "nsec1...")
TERMINAL_TASK_STATUSES = {"paused", "ended", "completed", "complete", "done", "closed", "expired", "canceled", "cancelled", "inactive", "archived", "failed"}

# Use a requests.Session so we can capture Set-Cookie from the auth flow
SES = requests.Session()

# Optional pre-seeded session cookie (keeps compatibility with existing runs)
PRESET_SESSION_COOKIE = os.getenv("AQSTR_SESSION_COOKIE")
if PRESET_SESSION_COOKIE:
    SES.headers.update({"Cookie": PRESET_SESSION_COOKIE})

AQSTR_BASE = "https://aqstr.com"
REQUEST_TIMEOUT = 3
MAX_REPLY_LENGTH = int(os.getenv("AQSTR_MAX_REPLY_LENGTH", "280"))
MAX_QUOTE_LENGTH = int(os.getenv("AQSTR_MAX_QUOTE_LENGTH", "280"))
REPLY_TEMPLATE = os.getenv("AQSTR_REPLY_TEMPLATE", "This stood out to me — {excerpt}")
QUOTE_TEMPLATE = os.getenv("AQSTR_QUOTE_TEMPLATE", "Worth a look — {excerpt} nostr:{event_id}")
RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.nostr.band",
    "wss://relay.bullishbounty.com",
    "wss://relay.snort.social",
    # ... add more from your captured POST
]

ACTIONS = [
    "like",
    "repost",
    "reply",
    "repost_with_quote",
    "follow",
]

# ============ HELPERS ============
def get_private_key():
    if not NSEC or NSEC == "nsec1...":
        raise ValueError("Set your NSEC in the environment variable NOSTR_NSEC")
    return PrivateKey.from_nsec(NSEC)

def sign_event(private_key, kind, content, tags, created_at=None):
    if created_at is None:
        created_at = int(time.time())
    event = Event(
        private_key.public_key.hex(),
        content,
        created_at,
        kind,
        tags,
    )
    private_key.sign_event(event)
    return event

def publish_event(event, relays=RELAYS):
    payload = {
        "event": {
            "id": event.id,
            "pubkey": event.public_key,
            "created_at": event.created_at,
            "kind": event.kind,
            "tags": event.tags,
            "content": event.content,
            "sig": event.signature,
        },
        "relays": relays,
    }
    headers = {"Content-Type": "application/json"}
    resp = SES.post(f"{AQSTR_BASE}/api/publish-nostr", json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    return resp.status_code == 200

def complete_task(task_id, task_type, nostr_event_id, reply_content=None):
    payload = {
        "taskId": task_id,
        "taskType": task_type,
        "nostrEventId": nostr_event_id,
    }
    if reply_content:
        payload["replyContent"] = reply_content
    headers = {"Content-Type": "application/json"}
    resp = SES.post(f"{AQSTR_BASE}/api/task/complete", json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        print(f"    completion endpoint returned {resp.status_code}: {detail}")
    return resp.status_code == 200

# ============ FETCH DASHBOARD ============
def _extract_first_list(data, keys):
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for nested_key in ("data", "dashboard", "user", "result", "payload"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                value = _extract_first_list(nested, keys)
                if value is not None:
                    return value
    return None


def get_task_id(task):
    if not isinstance(task, dict):
        return None
    return task.get("id") or task.get("taskId")


def get_task_completion_map(task, task_completions):
    task_id = get_task_id(task)
    completions = {}
    if isinstance(task_completions, dict) and task_id in task_completions:
        entry = task_completions.get(task_id)
        if isinstance(entry, dict):
            completions.update(entry)
    if not completions and isinstance(task, dict):
        for key in ("completedActions", "completed_actions"):
            entry = task.get(key)
            if isinstance(entry, dict):
                completions.update(entry)
                break
    if not completions and isinstance(task.get("userTasks"), list):
        for item in task.get("userTasks", []):
            if not isinstance(item, dict):
                continue
            if item.get("completed"):
                task_type = item.get("taskType")
                if task_type:
                    completions[task_type] = True
    return completions


def has_reward_for_action(task, action):
    reward_keys = []
    if action == "repost_with_quote":
        reward_keys = ["repostWithQuoteReward", "repost_with_quoteReward", f"{action}Reward"]
    elif action == "reply":
        reward_keys = ["replyReward", f"{action}Reward"]
    else:
        reward_keys = [f"{action}Reward"]
    for reward_key in reward_keys:
        reward = task.get(reward_key, 0)
        if reward in (None, ""):
            continue
        try:
            return float(reward) > 0
        except (TypeError, ValueError):
            continue
    return False


def count_pending_actions(task, task_completions):
    completions = get_task_completion_map(task, task_completions)
    return sum(
        1
        for action in ACTIONS
        if not bool(completions.get(action, False)) and has_reward_for_action(task, action)
    )


def parse_task_timestamp(task):
    if not isinstance(task, dict):
        return None
    for key in ("createdAt", "created_at", "created", "timestamp", "startAt", "start_at", "startTime", "date", "publishedAt", "published_at", "updatedAt", "updated_at"):
        value = task.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            if text.isdigit():
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            normalized = text.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None


def task_sort_key(task, task_completions=None):
    timestamp = parse_task_timestamp(task)
    pending_actions = count_pending_actions(task, task_completions) if task_completions is not None else 0
    task_id = get_task_id(task) or ""
    return (
        timestamp is None,
        timestamp or datetime.min.replace(tzinfo=timezone.utc),
        pending_actions,
        task_id,
    )


def sort_tasks_for_processing(tasks, task_completions=None):
    if not isinstance(tasks, list):
        return []
    actionable_tasks = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or task.get("taskStatus") or "").strip().lower()
        if status in TERMINAL_TASK_STATUSES:
            continue
        actionable_tasks.append(task)
    return sorted(actionable_tasks, key=lambda task: task_sort_key(task, task_completions))


def _parse_json_object(text):
    if not isinstance(text, str):
        return None
    start = None
    stack = []
    escape = False
    in_string = False
    for idx, ch in enumerate(text):
        if start is None:
            if ch == '{':
                start = idx
                stack.append('{')
                continue
            else:
                continue
        if ch == '"' and not escape:
            in_string = not in_string
        if in_string and ch == '\\' and not escape:
            escape = True
            continue
        if escape:
            escape = False
            continue
        if not in_string:
            if ch == '{':
                stack.append('{')
            elif ch == '}':
                if stack:
                    stack.pop()
                    if not stack:
                        candidate = text[start:idx + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            return None
    return None


def fetch_task_details(task_id):
    if not task_id:
        return None
    url = f"{AQSTR_BASE}/task/{task_id}?_data=routes/task.$id"
    try:
        resp = SES.get(url, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    payload = None
    try:
        payload = resp.json()
    except Exception:
        payload = _parse_json_object(resp.text)
    if not isinstance(payload, dict):
        return None
    for key in ("task", "data", "result", "payload"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return None


def enrich_task_with_details(task):
    if not isinstance(task, dict):
        return task
    task_id = get_task_id(task)
    if not task_id:
        return task
    if task.get("eventId") and task.get("eventAuthor"):
        return task
    details = fetch_task_details(task_id)
    if not details:
        return task
    enriched = dict(task)
    for key, value in details.items():
        if key not in enriched or enriched.get(key) in (None, "", []):
            enriched[key] = value
    return enriched


def extract_available_tasks(data):
    fallback = _extract_first_list(data, ("availableTasks", "tasks", "items", "taskList", "dashboardTasks"))
    if fallback:
        return fallback

    task_completions = extract_task_completions(data) or {}
    task_eligibility = extract_task_eligibility(data) or {}
    task_ids = set(task_completions.keys()) | set(task_eligibility.keys())
    tasks = []
    for task_id in sorted(task_ids):
        task = {"id": task_id, "taskId": task_id}
        if task_id in task_completions:
            task["completions"] = task_completions[task_id]
        if task_id in task_eligibility:
            task["eligibility"] = task_eligibility[task_id]
            if isinstance(task_eligibility[task_id], dict):
                task["isEligible"] = bool(task_eligibility[task_id].get("isEligible", True))
                task["failedRequirements"] = task_eligibility[task_id].get("failedRequirements", [])
        tasks.append(task)
    return tasks


def extract_task_completions(data):
    if isinstance(data, dict):
        for key in ("taskCompletions", "completions", "completedTasks"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        for nested_key in ("data", "dashboard", "user", "result", "payload"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                value = extract_task_completions(nested)
                if value:
                    return value
    return {}


def extract_task_eligibility(data):
    if isinstance(data, dict):
        for key in ("taskEligibility", "eligibility", "eligibilities"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        for nested_key in ("data", "dashboard", "user", "result", "payload"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                value = extract_task_eligibility(nested)
                if value:
                    return value
    return {}


def task_is_eligible(task, task_eligibility):
    if not isinstance(task, dict):
        return False

    status = str(task.get("status") or task.get("taskStatus") or "").strip().lower()
    if status in TERMINAL_TASK_STATUSES:
        return False

    task_id = task.get("id") or task.get("taskId")
    if task_id is None:
        return True

    elig = task_eligibility.get(task_id, {}) if isinstance(task_eligibility, dict) else {}
    if isinstance(elig, bool):
        return elig
    if isinstance(elig, dict):
        if "isEligible" in elig:
            return bool(elig.get("isEligible"))
        if "eligible" in elig:
            return bool(elig.get("eligible"))
        if "failedRequirements" in elig:
            return not bool(elig.get("failedRequirements"))
        if "status" in elig:
            return str(elig.get("status")).lower() not in {"ineligible", "not_eligible", "failed"}
    if isinstance(task, dict) and "isEligible" in task:
        return bool(task.get("isEligible"))
    return True


def task_has_work(task, task_completions):
    completions = get_task_completion_map(task, task_completions)
    if completions:
        for action in ACTIONS:
            if action in completions and not bool(completions.get(action, False)):
                return True

    for action in ACTIONS:
        if has_reward_for_action(task, action) and not bool(completions.get(action, False)):
            return True
    return False


def fetch_dashboard():
    """Fetch user dashboard data including available tasks and eligibility."""
    url = f"{AQSTR_BASE}/dashboard/user?_data=routes/dashboard.user"
    resp = SES.get(url)
    if resp.status_code != 200:
        raise Exception(f"Failed to fetch dashboard: {resp.status_code}")
    data = resp.json()
    return data


def fetch_auth_challenge():
    """Query the server auth challenge endpoint used during login.

    Returns a tuple `(status_code, payload)` where `payload` is the parsed
    JSON body when available or the raw text otherwise.
    """
    url = f"{AQSTR_BASE}/api/auth/challenge"
    try:
        resp = SES.get(url, headers={"Accept": "application/json"})
    except Exception as e:
        return (None, f"request-error: {e}")

    if resp.status_code == 200:
        try:
            return (200, resp.json())
        except Exception:
            return (200, resp.text)
    elif resp.status_code == 204:
        # 204 No Content is returned by the site in some cases when no
        # interactive challenge is required. Caller can decide how to react.
        return (204, None)
    else:
        try:
            return (resp.status_code, resp.json())
        except Exception:
            return (resp.status_code, resp.text)


def login_with_challenge(session, private_key):
    """Complete the auth challenge using the multipart form flow that the site expects."""
    status, payload = fetch_auth_challenge()
    if status is None or status == 204 or status != 200:
        return False

    challenge = None
    if isinstance(payload, dict):
        for key in ("challenge", "message", "text", "data"):
            if key in payload and isinstance(payload[key], str):
                challenge = payload[key]
                break
        if not challenge:
            for v in payload.values():
                if isinstance(v, str) and len(v) > 8:
                    challenge = v
                    break
    elif isinstance(payload, str):
        challenge = payload

    if not challenge:
        return False

    pubkey = private_key.public_key.hex()

    # Build the same multipart form fields the browser POST used.
    event = Event(pubkey, "", int(time.time()), 22242, [["challenge", challenge], ["domain", "aqstr.com"]])
    private_key.sign_event(event)

    form_data = {
        "pubkey": pubkey,
        "signature": event.signature,
        "event": json.dumps({
            "kind": event.kind,
            "created_at": event.created_at,
            "tags": event.tags,
            "content": event.content,
            "pubkey": event.public_key,
            "id": event.id,
            "sig": event.signature,
        }),
        "contentSign": "",
        "challenge": challenge,
        "authMethod": "nip07",
    }

    url = f"{AQSTR_BASE}/nostr-auth"
    try:
        resp = session.post(url, data=form_data, headers={"Accept": "application/json"})
    except Exception as e:
        print(f"  auth POST to {url} failed: {e}")
        return False

    if 200 <= resp.status_code < 300:
        # requests.Session stores Set-Cookie automatically.
        return True

    snippet = resp.text[:300].replace("\n", " ")
    print(f"  auth POST to {url} returned {resp.status_code}: {snippet}")
    return False

# ============ PROCESS A SINGLE TASK ============
def resolve_event_kind(action_name):
    mapping = {
        "like": (("REACTION",), 7),
        "repost": (("REPOST",), 6),
        "reply": (("TEXT_NOTE",), 1),
        "repost_with_quote": (("TEXT_NOTE",), 1),
        "follow": (("CONTACTS",), 3),
    }
    names, fallback = mapping.get(action_name, (("TEXT_NOTE",), 1))
    for name in names:
        kind = getattr(EventKind, name, None)
        if kind is not None:
            return kind
    return fallback


def fetch_event_by_id(event_id, timeout=5):
    if not event_id:
        return None

    relay_urls = [
        "wss://relay.damus.io",
        "wss://relay.primal.net",
        "wss://nos.lol",
        "wss://relay.nostr.band",
    ]

    for relay_url in relay_urls:
        try:
            ws = websocket.create_connection(relay_url, timeout=min(timeout, REQUEST_TIMEOUT))
            sub_id = f"aqstr-{int(time.time() * 1000)}"
            ws.send(json.dumps(["REQ", sub_id, {"ids": [event_id], "kinds": [1], "limit": 1}]))
            deadline = time.time() + min(timeout, REQUEST_TIMEOUT)
            while time.time() < deadline:
                try:
                    raw_message = ws.recv()
                except Exception:
                    break
                if not raw_message:
                    break
                try:
                    payload = json.loads(raw_message)
                except Exception:
                    continue
                if isinstance(payload, list) and len(payload) >= 3 and payload[0] == "EVENT":
                    event = payload[2]
                    if isinstance(event, dict) and event.get("id") == event_id:
                        ws.close()
                        return event
                if isinstance(payload, list) and payload[0] == "EOSE":
                    break
            try:
                ws.close()
            except Exception:
                pass
        except Exception:
            continue

    return None


def _extract_excerpt(content, max_length=140):
    if not content:
        return "a thoughtful post"
    cleaned = " ".join(str(content).split())
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 3].rstrip() + "..."


def _clip_content(content, max_length):
    if not content:
        return ""
    cleaned = " ".join(str(content).split())
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 3].rstrip() + "..."


def should_fetch_target_event_content(task):
    if not isinstance(task, dict):
        return False
    return any(
        has_reward_for_action(task, action)
        for action in ("reply", "repost_with_quote")
    )


def build_reply_content(target_event, event_id):
    if isinstance(target_event, dict):
        content = str(target_event.get("content") or "").strip()
    else:
        content = str(target_event or "").strip()
    excerpt = _extract_excerpt(content, max_length=120)
    rendered = REPLY_TEMPLATE.format(excerpt=excerpt, event_id=event_id or "")
    return _clip_content(rendered, MAX_REPLY_LENGTH)


def build_quote_content(target_event, event_id):
    if isinstance(target_event, dict):
        content = str(target_event.get("content") or "").strip()
    else:
        content = str(target_event or "").strip()
    excerpt = _extract_excerpt(content, max_length=140)
    rendered = QUOTE_TEMPLATE.format(excerpt=excerpt, event_id=event_id or "")
    return _clip_content(rendered, MAX_QUOTE_LENGTH)


def process_task(task, completions, private_key):
    """
    Process one task: for each action not yet completed, perform it.
    Returns True if any action was completed, False otherwise.
    """
    task_id = task["id"]
    target_event_id = task.get("eventId")
    target_pubkey = task.get("eventAuthor")
    if not target_event_id or not target_pubkey:
        print(f"  ⚠️ Task {task_id} has no eventId/eventAuthor in dashboard payload; skipping")
        return False

    # Build a map of which actions are already done for THIS user
    # completions is a dict: { taskId: { "like": true, "repost": false, ... } }
    user_completions = completions.get(task_id, {})
    # If the task has a `userTasks` list, we could also parse that, but the
    # `taskCompletions` map is more reliable.

    target_event = None
    if target_event_id and should_fetch_target_event_content(task):
        target_event = fetch_event_by_id(target_event_id)

    # Define actions with their configuration
    actions = {
        "like": {
            "kind": resolve_event_kind("like"),
            "reward": task.get("likeReward", 0),
            "completed": user_completions.get("like", False),
            "content": "+",
            "tags": [["e", target_event_id], ["p", target_pubkey]],
            "task_type": "like",
        },
        "repost": {
            "kind": resolve_event_kind("repost"),
            "reward": task.get("repostReward", 0),
            "completed": user_completions.get("repost", False),
            "content": "",  # Some clients use the target event ID; test both
            "tags": [["e", target_event_id], ["p", target_pubkey]],
            "task_type": "repost",
        },
        "reply": {
            "kind": resolve_event_kind("reply"),
            "reward": task.get("replyReward", 0),
            "completed": user_completions.get("reply", False),
            "content": build_reply_content(target_event, target_event_id),
            "tags": [["e", target_event_id, "", "reply"], ["p", target_pubkey]],
            "task_type": "reply",
        },
        "repost_with_quote": {
            "kind": resolve_event_kind("repost_with_quote"),
            "reward": task.get("repostWithQuoteReward", 0),
            "completed": user_completions.get("repost_with_quote", False),
            "content": build_quote_content(target_event, target_event_id),
            "tags": [["e", target_event_id, "", "quote"], ["p", target_pubkey]],
            "task_type": "repost_with_quote",
        },
        "follow": {
            "kind": resolve_event_kind("follow"),
            "reward": task.get("followReward", 0),
            "completed": user_completions.get("follow", False),
            "content": "",
            "tags": [["p", target_pubkey]],
            "task_type": "follow",
        },
    }

    any_completed = False
    for action_name, cfg in actions.items():
        if cfg["completed"]:
            print(f"  ⏭️ {action_name} already completed")
            continue
        if cfg["reward"] == 0:
            print(f"  ⏭️ {action_name} has zero reward, skipping")
            continue

        print(f"  ▶️ Processing {action_name} (reward: {cfg['reward']} sats)...")

        # Build and sign the event
        event = sign_event(
            private_key,
            cfg["kind"],
            cfg["content"],
            cfg["tags"],
            created_at=int(time.time()),
        )

        # Publish
        if not publish_event(event):
            print(f"  ❌ Failed to publish {action_name} event")
            continue

        # Mark complete
        reply_content = cfg["content"] if action_name in {"reply", "repost_with_quote"} else None
        if complete_task(task_id, cfg["task_type"], event.id, reply_content):
            print(f"  ✅ Completed {action_name} earned {cfg['reward']} sats")
            any_completed = True
        else:
            print(f"  ❌ Failed to mark {action_name} as complete")

        # Short delay to avoid hammering the APIs while keeping the loop responsive.
        time.sleep(0.5)

    return any_completed

# ============ MAIN LOOP ============
def main():
    private_key = get_private_key()
    authenticated = False

    while True:
        if not authenticated:
            print("\n🔐 Authenticating with the Nostr challenge...")
            try:
                authenticated = login_with_challenge(SES, private_key)
                if authenticated:
                    print("🔐 Authenticated successfully")
                else:
                    print("⚠️ Authentication failed; waiting before retry")
                    time.sleep(30)
                    continue
            except Exception as e:
                print(f"❌ Login attempt failed: {e}")
                time.sleep(30)
                continue

        print("\n🔄 Fetching dashboard...")
        try:
            data = fetch_dashboard()
        except Exception as e:
            print(f"❌ Failed to fetch dashboard: {e}")
            authenticated = False
            time.sleep(60)
            continue

        available_tasks = extract_available_tasks(data)
        task_completions = extract_task_completions(data)
        task_eligibility = extract_task_eligibility(data)
        available_tasks = sort_tasks_for_processing(available_tasks, task_completions)

        print(f"📋 Found {len(available_tasks)} available tasks")

        eligible_tasks = []
        for task in available_tasks:
            if not isinstance(task, dict):
                continue

            task = enrich_task_with_details(task)
            task_id = get_task_id(task)
            if task_id is None:
                continue

            if not task_has_work(task, task_completions):
                continue

            if task_is_eligible(task, task_eligibility) is False:
                print(f"  ⏭️ Task {task_id} not eligible")
                continue

            if not task.get("eventId") or not task.get("eventAuthor"):
                print(f"  ⚠️ Task {task_id} has no eventId/eventAuthor after detail enrichment; skipping")
                continue

            eligible_tasks.append(task)

        eligible_tasks = sort_tasks_for_processing(eligible_tasks, task_completions)
        print(f"✅ Found {len(eligible_tasks)} eligible tasks to process")

        processed_any = False

        # Process each eligible task
        for task in eligible_tasks:
            task_id = get_task_id(task)
            completions = get_task_completion_map(task, task_completions)
            pending_actions = [
                action for action in ACTIONS
                if not bool(completions.get(action, False)) and has_reward_for_action(task, action)
            ]
            print(f"\n📝 Task {task_id} has pending actions: {pending_actions or ['none']}")
            if not pending_actions:
                print(f"  ⚠️ Task {task_id} had no actions to perform")
                continue
            try:
                any_done = process_task(task, task_completions, private_key)
                if any_done:
                    processed_any = True
                    print(f"  ✅ Task {task_id} completed at least one action")
                else:
                    print(f"  ⚠️ Task {task_id} had no actions to perform")
            except Exception as e:
                print(f"  ❌ Error processing task {task_id}: {e}")

            # Keep the loop moving quickly, but still avoid rate-limiting bursts.
            time.sleep(1)

        if not processed_any:
            print("\n🛑 No new actions were completed in this pass. Exiting loop to avoid repeating work.")
            break

        # After processing all eligible tasks, wait before refreshing the list
        # This gives the system time to update completions and for new tasks to appear.
        print("\n⏳ All eligible tasks processed. Waiting 10 seconds before refresh...")
        time.sleep(10)

if __name__ == "__main__":
    main()