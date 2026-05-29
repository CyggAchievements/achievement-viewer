import os
import json
import requests
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import subprocess
import re
import asyncio
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
import hashlib

# --- Constants & environment --- #
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")
TRIGGER_SOURCE = os.environ.get("TRIGGER_SOURCE", "")
WARNED_MISSING_STEAM_API_KEY = False


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


FETCH_STEAMHUNTERS = os.environ.get("FETCH_STEAMHUNTERS", "1").lower() not in (
    "0",
    "false",
    "no",
)
STEAMHUNTERS_TIMEOUT_SECONDS = max(env_int("STEAMHUNTERS_TIMEOUT_SECONDS", 8), 1)
STEAMHUNTERS_MAX_PER_RUN = max(env_int("STEAMHUNTERS_MAX_PER_RUN", 35), 0)
STEAMHUNTERS_REFRESH_APPIDS = set()

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/xml,application/xml,text/html;q=0.9,*/*;q=0.8",
}

# NEW: Detect GitHub Pages URL for fallback icon
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # Format: "owner/repo"
if GITHUB_REPOSITORY:
    owner, repo = GITHUB_REPOSITORY.split("/")
    FALLBACK_ICON_URL = f"https://{owner}.github.io/{repo}/default_icon.png"
else:
    # Fallback if not running in GitHub Actions
    FALLBACK_ICON_URL = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='64' height='64'%3E%3Crect width='64' height='64' fill='%233d5a6c'/%3E%3Ctext x='50%25' y='50%25' text-anchor='middle' dy='.3em' fill='%23c7d5e0' font-size='24'%3E%3F%3C/text%3E%3C/svg%3E"

print(f"Using fallback icon URL: {FALLBACK_ICON_URL}")

appid_dir = Path("AppID")
game_data_path = Path("game-data.json")
top_owners_file = Path("top_owners.json")

DEFAULT_OWNERS = [
    76561198028121353,
    76561198017975643,
    76561197979911851,
    76561198355953202,
    76561197993544755,
    76561198001237877,
    76561198355625888,
    76561198217186687,
    76561198152618007,
    76561198237402290,
    76561198213148949,
    76561197973009892,
    76561198037867621,
    76561197969050296,
    76561198019712127,
    76561198094227663,
    76561197965319961,
    76561197976597747,
    76561197963550511,
    76561198044596404,
    76561198134044398,
    76561198367471798,
    76561199492215670,
    76561197962473290,
    76561198842603734,
    76561198119667710,
    76561197969810632,
    76561197995070100,
    76561198017902347,
    76561197996432822,
    76561198082995144,
    76561198027214426,
]

# --- Helper functions --- #


async def fetch_steamhunters_achievements(appid):
    url = f"https://steamhunters.com/apps/{appid}/achievements?group=&sort=name"
    print(f"    → Fetching groups from SteamHunters...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        page.set_default_timeout(STEAMHUNTERS_TIMEOUT_SECONDS * 1000)
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=STEAMHUNTERS_TIMEOUT_SECONDS * 1000,
            )
            try:
                await page.wait_for_function(
                    """() => window.sh?.model || Array.from(document.querySelectorAll('script')).some(s => s.textContent.includes('var sh') || s.textContent.includes('listData'));""",
                    timeout=min(5000, STEAMHUNTERS_TIMEOUT_SECONDS * 1000),
                )
            except PlaywrightTimeoutError:
                print("    ! SteamHunters data model not found, skipping groups")
                return []

            sh_model = await page.evaluate(
                """() => {
                    if (window.sh?.model) return window.sh.model;
                    const scripts = Array.from(document.querySelectorAll('script'));
                    const target = scripts.find(s => s.textContent.includes('var sh') || s.textContent.includes('listData'));
                    if (!target) return {};
                    try {
                        eval(target.textContent);
                        if (window.sh?.model) return window.sh.model;
                        if (typeof sh !== 'undefined' && sh?.model) return sh.model;
                    } catch (e) {
                        return {};
                    }
                    return {};
                }"""
            )
            if not sh_model:
                print("    ! SteamHunters returned no usable achievement model")
                return []
            
            # 1. Build a lookup map for Update IDs -> Group Names
            updates_map = {}
            if "updates" in sh_model:
                for update in sh_model["updates"]:
                    u_id = update.get("updateId")
                    
                    # Robust Naming Logic:
                    # 1. Official DLC Name
                    if update.get("dlcAppName"):
                        name = update.get("dlcAppName")
                    # 2. Steam Event Name (e.g. "Summer Sale")
                    elif update.get("steamEventName"):
                        name = update.get("steamEventName")
                    # 3. Base Game detection (Update #0 and no DLC ID)
                    elif update.get("updateNumber", 0) == 0 and not update.get("dlcAppId"):
                         name = "Base Game"
                    # 4. Numbered Content Update (e.g. "Update 1.5")
                    elif update.get("updateNumber", 0) > 0:
                        name = f"Update {update.get('updateNumber')}"
                    # 5. Fallback
                    else:
                        name = "Base Game" 
                        
                    updates_map[u_id] = name

            # 2. Process the achievements items
            achievements = sh_model.get("listData", {}).get("pagedList", {}).get("items", [])
            
            results = []
            for item in achievements:
                # Use the updateId to find the group name
                update_id = item.get("updateId", 0)
                group_name = updates_map.get(update_id, "Base Game")
                
                results.append({
                    "name": item.get("apiName"),
                    "default_value": 0,
                    "displayName": item.get("name"),
                    "hidden": 1 if item.get("hidden") else 0,
                    "description": item.get("description") or " ",
                    "icon": item.get("icon"),
                    "icongray": item.get("iconGray"),
                    "group": group_name 
                })
            return results
        except Exception as e:
            print(f"Error fetching from SteamHunters: {e}")
            return []
        finally:
            await browser.close()


def fetch_steamhunters_with_timeout(appid):
    try:
        return asyncio.run(
            asyncio.wait_for(
                fetch_steamhunters_achievements(appid),
                timeout=STEAMHUNTERS_TIMEOUT_SECONDS + 2,
            )
        )
    except asyncio.TimeoutError:
        print(
            f"  ! SteamHunters timed out after {STEAMHUNTERS_TIMEOUT_SECONDS}s, preserving existing groups"
        )
    except Exception as e:
        print(f"  ! Could not fetch SteamHunters data: {e}")
    return []


def load_top_owner_ids():
    if top_owners_file.exists():
        try:
            with open(top_owners_file, "r", encoding="utf-8") as f:
                return json.load(f).get("steam_ids", DEFAULT_OWNERS)
        except Exception as e:
            print(f"Error loading top_owners.json: {e}")
            return DEFAULT_OWNERS
    else:
        try:
            with open(top_owners_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "steam_ids": DEFAULT_OWNERS,
                        "updated": "Created from hardcoded backup",
                    },
                    f,
                    indent=2,
                )
            print("✓ Created top_owners.json for future runs")
        except Exception as e:
            print(f"Warning: Could not create top_owners.json: {e}")
        return DEFAULT_OWNERS


TOP_OWNER_IDS = load_top_owner_ids()


def get_changed_appids():
    """Get AppIDs that have changed in the triggering event"""
    try:
        changed_files = []
        
        # Check if we're in a push event by looking at GITHUB_SHA and GITHUB_BEFORE
        github_sha = os.environ.get("GITHUB_SHA", "")
        github_before = os.environ.get("GITHUB_BEFORE", "")
        
        # If we have both SHA values, this is likely a push event
        if github_sha and github_before and github_before != "0000000000000000000000000000000000000000":
            print(f"Comparing push commits: {github_before[:7]}...{github_sha[:7]}")
            result = subprocess.run(
                ["git", "diff", "--name-only", github_before, github_sha],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                changed_files = result.stdout.strip().split("\n")
        
        # If no push-specific changes found, try comparing HEAD~1 to HEAD
        if not changed_files:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                changed_files = result.stdout.strip().split("\n")
        
        # Check for staged changes
        result2 = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
        )
        if result2.returncode == 0 and result2.stdout.strip():
            changed_files.extend(result2.stdout.strip().split("\n"))
        
        # Check for untracked files in AppID directory
        result3 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "AppID/"],
            capture_output=True,
            text=True,
        )
        if result3.returncode == 0 and result3.stdout.strip():
            changed_files.extend(result3.stdout.strip().split("\n"))
        
        # Extract AppIDs from changed files
        found_ids = set()
        for f in changed_files:
            # Match AppID/12345/... pattern
            match = re.match(r"AppID/(\d+)/", f)
            if match:
                found_ids.add(match.group(1))
        
        if found_ids:
            print(f"Detected changes in AppIDs: {', '.join(sorted(found_ids))}")
        else:
            print("No AppID-specific changes detected")
        
        return list(found_ids)
    except Exception as e:
        print(f"Warning: Could not detect changes: {e}")
        return []


def configure_steamhunters_refresh(appids, full_run):
    global STEAMHUNTERS_REFRESH_APPIDS

    if not FETCH_STEAMHUNTERS:
        STEAMHUNTERS_REFRESH_APPIDS = set()
        print("SteamHunters refresh disabled by FETCH_STEAMHUNTERS")
        return

    if not appids:
        STEAMHUNTERS_REFRESH_APPIDS = set()
        return

    if not full_run:
        STEAMHUNTERS_REFRESH_APPIDS = set(appids)
        print(
            f"SteamHunters refresh enabled for {len(STEAMHUNTERS_REFRESH_APPIDS)} changed game(s)"
        )
        return

    sorted_appids = sorted(appids, key=int)
    if STEAMHUNTERS_MAX_PER_RUN == 0:
        STEAMHUNTERS_REFRESH_APPIDS = set(sorted_appids)
        print(
            f"SteamHunters refresh enabled for all {len(STEAMHUNTERS_REFRESH_APPIDS)} games "
            f"(timeout {STEAMHUNTERS_TIMEOUT_SECONDS}s each)"
        )
        return

    window_size = min(STEAMHUNTERS_MAX_PER_RUN, len(sorted_appids))
    # Rotate the refresh window every 6 hours so scheduled runs eventually cover all games.
    window_index = int(time.time() // (6 * 60 * 60))
    start = (window_index * window_size) % len(sorted_appids)
    selected = (sorted_appids + sorted_appids)[start : start + window_size]
    STEAMHUNTERS_REFRESH_APPIDS = set(selected)
    print(
        "SteamHunters refresh enabled for "
        f"{len(STEAMHUNTERS_REFRESH_APPIDS)}/{len(sorted_appids)} games this run "
        f"(timeout {STEAMHUNTERS_TIMEOUT_SECONDS}s each)"
    )


def load_json_file(file_path):
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {file_path}: {e}")
    return None


def save_json_file(file_path, data):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving {file_path}: {e}")


def get_text(elem):
    return elem.text if elem is not None and elem.text else ""


def load_achievements_file(folder):
    appid = folder.name
    json_file = folder / "achievements.json"
    db_file = folder / f"{appid}.db"

    for file_path in [json_file, db_file]:
        if file_path.exists():
            try:
                data = load_json_file(file_path)
                if isinstance(data, dict):
                    return data, file_path.name
                elif isinstance(data, list):
                    converted = {
                        ach["apiname"]: {
                            "earned": ach.get("achieved", 0) == 1,
                            "earned_time": ach.get("unlocktime", 0),
                        }
                        for ach in data
                        if "apiname" in ach
                    }
                    return converted, file_path.name
            except Exception as e:
                print(f"Error processing {file_path}: {e}")
    return None, None


def scrape_hidden_achievements(appid, steam_id, achievement_names_map):
    try:
        url = (
            f"https://steamcommunity.com/profiles/{steam_id}/stats/{appid}/achievements"
        )
        print(f"    → Trying profile {steam_id}...")
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            print(f"    ✗ Profile returned status {response.status_code}")
            return {}

        soup = BeautifulSoup(response.text, "html.parser")
        achievements = {}
        for row in soup.find_all("div", class_="achieveRow"):
            name_elem = row.find("h3")
            desc_elem = row.find("h5")
            if not name_elem or not desc_elem:
                continue
            display_name = name_elem.text.strip()
            description = desc_elem.text.strip()
            api_name = achievement_names_map.get(display_name.lower())
            if api_name and description:
                achievements[api_name] = {
                    "name": display_name,
                    "description": description,
                }

        if achievements:
            print(f"    ✓ Matched {len(achievements)} achievement API names")
        return achievements
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return {}


def fetch_steam_store_info(appid):
    try:
        response = requests.get(
            f"https://store.steampowered.com/api/appdetails?appids={appid}",
            headers=REQUEST_HEADERS,
            timeout=10,
        )
        if response.ok:
            data = response.json().get(appid, {})
            if data.get("success"):
                return {
                    "name": data["data"].get("name", f"Game {appid}"),
                    "icon": data["data"].get("header_image", ""),
                }
    except Exception as e:
        print(f"Error fetching store info for {appid}: {e}")
    return {"name": f"Game {appid}", "icon": ""}


def fetch_steam_schema_achievements(appid):
    global WARNED_MISSING_STEAM_API_KEY

    if not STEAM_API_KEY and not WARNED_MISSING_STEAM_API_KEY:
        print(
            "  ! STEAM_API_KEY is empty; Steam schema may be unavailable. "
            "Add it as a GitHub Actions secret for reliable achievement data."
        )
        WARNED_MISSING_STEAM_API_KEY = True
    if not STEAM_API_KEY:
        return []

    params = {"appid": appid, "l": "english"}
    params["key"] = STEAM_API_KEY

    try:
        response = requests.get(
            "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/",
            params=params,
            headers=REQUEST_HEADERS,
            timeout=10,
        )
        if response.status_code in (401, 403):
            print(f"  ! Steam schema authentication failed for {appid}")
            return []
        if not response.ok:
            print(f"  ! Steam schema returned HTTP {response.status_code} for {appid}")
            return []

        try:
            data = response.json()
        except ValueError:
            print(f"  ! Steam schema returned non-JSON response for {appid}")
            return []

        achievements = (
            data.get("game", {})
            .get("availableGameStats", {})
            .get("achievements", [])
        )
        if not isinstance(achievements, list):
            return []
        return achievements
    except Exception as e:
        print(f"  ! Error fetching Steam schema for {appid}: {e}")
        return []


def existing_info_to_achievements(existing_info):
    achievements = existing_info.get("achievements", {})
    if not isinstance(achievements, dict):
        return []

    converted = []
    for api_name, data in achievements.items():
        if not isinstance(data, dict):
            continue
        converted.append(
            {
                "name": api_name,
                "default_value": 0,
                "displayName": data.get("name", api_name),
                "hidden": 1 if data.get("hidden") else 0,
                "description": data.get("description", ""),
                "icon": data.get("icon", ""),
                "icongray": data.get("icongray", ""),
                "group": data.get("group", "Base Game"),
            }
        )
    return converted


def fetch_community_achievements(appid):
    achievements = {}
    try:
        response = requests.get(
            f"https://steamcommunity.com/stats/{appid}/achievements/?xml=1",
            headers=REQUEST_HEADERS,
            timeout=10,
        )
        if response.ok:
            content = response.content.lstrip()
            content_type = response.headers.get("content-type", "").lower()
            if not content.startswith(b"<") or (
                "html" in content_type and b"<response" not in content[:200].lower()
            ):
                print(f"  ! Steam Community returned non-XML data for {appid}")
                return achievements
            if content[:200].lower().startswith((b"<!doctype html", b"<html")):
                print(f"  ! Steam Community returned HTML instead of XML for {appid}")
                return achievements

            root = ET.fromstring(content)
            for ach in root.findall(".//achievement"):
                api_name_elem = ach.find("apiname")
                if api_name_elem is not None and api_name_elem.text:
                    api_name = api_name_elem.text
                    achievements[api_name] = {
                        "name": get_text(ach.find("name")),
                        "description": get_text(ach.find("description")),
                        "icon": get_text(ach.find("iconOpen")),
                        "icongray": get_text(ach.find("iconClosed")),
                        "hidden": False,
                    }
    except ET.ParseError as e:
        print(f"  ✗ XML parse error for {appid}: {e}")
    except Exception as e:
        print(f"Error fetching community achievements for {appid}: {e}")
    return achievements


def fetch_achievements(appid, existing_info, achievements_from_xml):
    hidden_achievements = []
    achievement_names_map = {}
    achievements_info = {}

    achievements = fetch_steam_schema_achievements(appid)
    if achievements:
        print(f"  OK Got {len(achievements)} achievements from Steam schema")
    else:
        print("  ! Steam schema returned 0 achievements")

    steamhunters_data = {}
    sh_data = []
    if str(appid) in STEAMHUNTERS_REFRESH_APPIDS:
        print("  -> Fetching extra data (groups) from SteamHunters...")
        sh_data = fetch_steamhunters_with_timeout(appid)
        for item in sh_data:
            if item.get("name"):
                steamhunters_data[item["name"]] = item
    else:
        print("  ! SteamHunters group refresh skipped this run; preserving existing groups")

    if not achievements and sh_data:
        achievements = sh_data
        print(f"  OK Using {len(achievements)} achievements from SteamHunters")

    if not achievements:
        achievements = existing_info_to_achievements(existing_info)
        if achievements:
            print(
                f"  ! External sources failed; preserving {len(achievements)} existing achievements"
            )

    try:
        for ach in achievements:
            if not ach.get("name"):
                continue

            if ach.get("icon"):
                if not ach["icon"].startswith("http"):
                    ach["icon"] = f'https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{ach["icon"]}.jpg'
            else:
                ach["icon"] = FALLBACK_ICON_URL

            if ach.get("icongray"):
                if not ach["icongray"].startswith("http"):
                    ach["icongray"] = f'https://cdn.steamstatic.com/steamcommunity/public/images/apps/{appid}/{ach["icongray"]}.jpg'
            else:
                ach["icongray"] = FALLBACK_ICON_URL

        for ach in achievements:
            api_name = ach.get("name")
            if not api_name:
                continue

            hidden_value = ach.get("hidden", 0)
            is_hidden = hidden_value in (1, "1", True)
            display_name = ach.get("displayName") or ach.get("display_name") or api_name
            achievement_names_map[display_name.lower()] = api_name

            achievements_info[api_name] = {
                "name": display_name,
                "description": ach.get("description", ""),
                "icon": ach.get("icon", ""),
                "icongray": ach.get("icongray", ""),
                "hidden": is_hidden,
            }

            if api_name in achievements_from_xml:
                if not achievements_info[api_name]["description"]:
                    achievements_info[api_name]["description"] = achievements_from_xml[
                        api_name
                    ]["description"]
                achievements_info[api_name]["hidden"] = is_hidden

            old_group = (
                existing_info.get("achievements", {})
                .get(api_name, {})
                .get("group")
            )
            if api_name in steamhunters_data:
                achievements_info[api_name]["group"] = steamhunters_data[api_name].get(
                    "group", "Base Game"
                )
            else:
                achievements_info[api_name]["group"] = (
                    ach.get("group") or old_group or "Base Game"
                )

            if not achievements_info[api_name]["description"]:
                old_desc = (
                    existing_info.get("achievements", {})
                    .get(api_name, {})
                    .get("description", "")
                )
                if old_desc:
                    achievements_info[api_name]["description"] = old_desc

            if is_hidden and not achievements_info[api_name]["description"]:
                hidden_achievements.append(api_name)

    except Exception as e:
        print(f"  ! Error building achievements for {appid}: {e}")

    return achievements_info, hidden_achievements, achievement_names_map


# --- Determine AppIDs to process --- #

full_run = False

# FIXED: Process all games ONLY for scheduled runs or explicit manual trigger
if EVENT_NAME == "schedule" or (EVENT_NAME == "workflow_dispatch" and TRIGGER_SOURCE == "manual"):
    full_run = True
    appids = [f.name for f in appid_dir.iterdir() if f.is_dir() and f.name.isdigit()]
    print(f"Processing all {len(appids)} games (Reason: {EVENT_NAME} trigger with source: {TRIGGER_SOURCE or 'scheduled'})")

# Process changed games only for push events or workflow calls triggered by push
else:
    appids = get_changed_appids()
    if appids:
        print(f"Processing {len(appids)} changed game(s): {', '.join(appids)}")
    else:
        print("No game-specific changes detected. Exiting.")
        exit(0)

configure_steamhunters_refresh(appids, full_run)

# --- Load existing game-data.json --- #
existing_game_data = {}
existing_data_file = load_json_file(game_data_path)

if existing_data_file:
    if isinstance(existing_data_file, dict) and "games" in existing_data_file:
        existing_data_list = existing_data_file["games"]
    else:
        existing_data_list = existing_data_file
    
    for game in existing_data_list:
        existing_game_data[game["appid"]] = game
    print(f"Loaded existing data for {len(existing_game_data)} games")

# --- Main AppID Processing Loop --- #
for appid in appids:
    print(f"\nProcessing AppID {appid}...")
    base_path = appid_dir / appid

    platform_files = list(base_path.glob("*.platform"))
    current_platform = platform_files[0].stem if platform_files else None

    blacklist_file = base_path / "blacklist"
    current_blacklist = (
        [
            line.strip()
            for line in open(blacklist_file, "r", encoding="utf-8")
            if line.strip()
        ]
        if blacklist_file.exists()
        else []
    )

    skip_file = base_path / "skip"
    if skip_file.exists():
        print(f"  ! 'skip' file found, skipping data fetch for {appid}")
        existing_info = load_json_file(base_path / "game-info.json")
        achievements_data, file_type = load_achievements_file(base_path)
        
        if existing_info and achievements_data:
            existing_info["platform"] = current_platform
            existing_info["blacklist"] = current_blacklist
            
            existing_game_data[str(appid)] = {
                "appid": str(appid),
                "info": existing_info,
                "achievements": achievements_data,
            }
            print(f"  ✓ Existing data preserved with platform: {current_platform}")
        else:
            print(f"  ✗ Could not load existing info/achievements for skipped game {appid}")
        continue
    
    existing_info = load_json_file(base_path / "game-info.json") or {}

    game_info = {
        "appid": appid,
        "name": f"Game {appid}",
        "icon": "",
        "achievements": {},
        "platform": current_platform,
        "blacklist": current_blacklist,
        "uses_db": (base_path / f"{appid}.db").exists()
    }

    # --- Fetch data --- #
    store_info = fetch_steam_store_info(appid)
    game_info.update(store_info)
    game_info["platform"] = current_platform
    time.sleep(1.5)

    achievements_from_xml = fetch_community_achievements(appid)
    print(f"  ✓ Got {len(achievements_from_xml)} achievements from XML")
    time.sleep(1.5)

    achievements, hidden_achievements, achievement_names_map = fetch_achievements(
        appid, existing_info, achievements_from_xml
    )
    
    # Skip games with 0 achievements (similar to skip file behavior)
    if len(achievements) == 0:
        print(f"  ! Steam returned 0 achievements, skipping data fetch for {appid}")
        existing_info = load_json_file(base_path / "game-info.json")
        achievements_data, file_type = load_achievements_file(base_path)
        
        if existing_info and achievements_data:
            existing_info["platform"] = current_platform
            existing_info["blacklist"] = current_blacklist
            
            existing_game_data[str(appid)] = {
                "appid": str(appid),
                "info": existing_info,
                "achievements": achievements_data,
            }
            print(f"  ✓ Existing data preserved with platform: {current_platform}")
        else:
            print(f"  ✗ Could not load existing info/achievements for game {appid} with 0 achievements")
        continue
    
    game_info["achievements"].update(achievements)
    print(f"  ✓ Merged {len(achievements)} achievements")

    if hidden_achievements:
        print(
            f"  → Found {len(hidden_achievements)} hidden achievements without descriptions"
        )
        descriptions_found = 0
        for steam_id in TOP_OWNER_IDS[:32]:
            scraped = scrape_hidden_achievements(appid, steam_id, achievement_names_map)
            for api_name, data in scraped.items():
                if (
                    api_name in game_info["achievements"]
                    and not game_info["achievements"][api_name]["description"]
                ):
                    game_info["achievements"][api_name]["description"] = data[
                        "description"
                    ]
                    descriptions_found += 1
                    print(f"    ✓ Found: '{data['name']}'")

            missing = sum(
                1
                for api in hidden_achievements
                if not game_info["achievements"][api]["description"]
            )
            print(
                f"    → Progress: {descriptions_found}/{len(hidden_achievements)} found, {missing} missing"
            )
            if missing == 0:
                break
            time.sleep(2)

    # ✅ FIXED: Wrapped percentage fetching in try-except to handle timeouts gracefully
    # Also preserves existing percentages if fetch fails
    try:
        percent_url = f"https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/?gameid={appid}"
        percent_response = requests.get(percent_url, timeout=10)

        if percent_response.ok:
            percent_data = percent_response.json()
            percentages = percent_data.get("achievementpercentages", {}).get("achievements", [])

            for ach_percent in percentages:
                ach_name = ach_percent.get("name")
                if ach_name in game_info["achievements"]:
                    game_info["achievements"][ach_name]["percent"] = (ach_percent.get("percent", 0))

            print(f"  ✓ Got percentages")
        else:
            print(f"  ⚠ Could not fetch percentages (HTTP {percent_response.status_code}), preserving existing data")
            # Preserve existing percentages
            for api_name in game_info["achievements"]:
                old_percent = existing_info.get("achievements", {}).get(api_name, {}).get("percent")
                if old_percent is not None and "percent" not in game_info["achievements"][api_name]:
                    game_info["achievements"][api_name]["percent"] = old_percent
    except requests.exceptions.Timeout:
        print(f"  ⚠ Timeout fetching percentages (Steam API slow), preserving existing data")
        # Preserve existing percentages on timeout
        for api_name in game_info["achievements"]:
            old_percent = existing_info.get("achievements", {}).get(api_name, {}).get("percent")
            if old_percent is not None and "percent" not in game_info["achievements"][api_name]:
                game_info["achievements"][api_name]["percent"] = old_percent
    except Exception as e:
        print(f"  ⚠ Error fetching percentages: {e}, preserving existing data")
        # Preserve existing percentages on error
        for api_name in game_info["achievements"]:
            old_percent = existing_info.get("achievements", {}).get(api_name, {}).get("percent")
            if old_percent is not None and "percent" not in game_info["achievements"][api_name]:
                game_info["achievements"][api_name]["percent"] = old_percent

    missing_file_path = base_path / "missing hidden achievements"
    still_missing_api_names = [
        api
        for api in hidden_achievements
        if not game_info["achievements"][api]["description"]
    ]
    if still_missing_api_names:
        with open(missing_file_path, "w", encoding="utf-8") as f:
            f.writelines(f"{api}\n" for api in still_missing_api_names)
        print(
            f"  ⚠ Created/Updated 'missing hidden achievements' file ({len(still_missing_api_names)} items)"
        )
    elif missing_file_path.exists():
        missing_file_path.unlink()
        print(
            "  ✓ All hidden descriptions found, removed 'missing hidden achievements' file"
        )

    save_json_file(base_path / "game-info.json", game_info)

    achievements_data, file_type = load_achievements_file(base_path)
    if achievements_data is None:
        print(f"  ✗ No achievements file found for {appid}")
        continue
    print(f"  ✓ Loaded achievements from {file_type} format")

    existing_game_data[str(appid)] = {
        "appid": str(appid),
        "info": game_info,
        "achievements": achievements_data,
    }

# --- Rebuild complete game-data.json --- #
all_game_data = []
for folder in appid_dir.iterdir():
    if folder.is_dir() and folder.name.isdigit():
        current_appid = folder.name
        achievements_data, _ = load_achievements_file(folder)
        
        if achievements_data is None:
            print(f"  ⚠ Skipping {current_appid} - no achievements file found")
            continue
        if current_appid in existing_game_data:
            all_game_data.append(existing_game_data[current_appid])
        else:
            info_data = load_json_file(folder / "game-info.json") or {
                "appid": current_appid,
                "name": f"Game {current_appid}",
                "icon": "",
                "achievements": {},
            }
            all_game_data.append(
                {
                    "appid": current_appid,
                    "info": info_data,
                    "achievements": achievements_data,
                }
            )

final_output = {
    "last_updated": int(time.time()),
    "total_games": len(all_game_data),
    "games": all_game_data
}

save_json_file(game_data_path, final_output)
print(f"\n✓ Updated {len(appids)} game(s), total games in data: {len(all_game_data)}")
