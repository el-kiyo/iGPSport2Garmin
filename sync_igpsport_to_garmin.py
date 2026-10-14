#!/usr/bin/env python3
"""
Sync iGPSport cycling activities to Garmin Connect.

This script runs periodically in GitHub Actions to download FIT files from iGPSport
and upload them to Garmin Connect, with filtering to avoid duplicates.
"""

import os
import json
import time
import random
import datetime
import requests
import tempfile
from garminconnect import Garmin
from pathlib import Path
import logging
from dateutil.parser import parse
from typing import Dict, List, Optional, Tuple, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("igpsport-to-garmin")

# Constants
LAST_SYNC_FILE = "last_sync_date.json"
OVERLAP_BUFFER_MINUTES = 5  # Consider activities overlapping if within 5 minutes
GARMIN_SESSION_DIR = "garmin_session"  # Dir to store Garmin session data


class IGPSportClient:
    """Client for the iGPSport API."""

    REGION_CONFIG = {
        "cn": {
            "base_url": "https://prod.zh.igpsport.com/service",
            "origin": "https://login.passport.igpsport.cn",
            "referer": "https://login.passport.igpsport.cn/",
        },
        "global": {
            "base_url": "https://prod.en.igpsport.com/service",
            "origin": "https://login.passport.igpsport.com",
            "referer": "https://login.passport.igpsport.com/",
        },
    }

    def __init__(self, username: str, password: str, region: str = "cn"):
        self.username = username
        self.password = password
        self.token = None
        self.region = (region or "cn").lower()

        if self.region not in self.REGION_CONFIG:
            logger.warning(
                f"Unknown igpsport region '{self.region}', defaulting to 'cn'"
            )
            self.region = "cn"

        region_cfg = self.REGION_CONFIG[self.region]
        self.base_url = region_cfg["base_url"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": region_cfg["origin"],
                "referer": region_cfg["referer"],
            }
        )

    def login(self) -> bool:
        """Login to iGPSport."""
        url = f"{self.base_url}/auth/account/login"
        data = {
            "username": self.username,
            "password": self.password,
            "appId": "igpsport-web",
        }

        try:
            response = self.session.post(url, json=data)
            response.raise_for_status()
            result = response.json()

            if result["code"] == 0 and "data" in result:
                access_token = result["data"]["access_token"]
                self.token = access_token
                self.session.headers.update({"authorization": f"Bearer {access_token}"})
                logger.info("Successfully logged in to iGPSport")
                return True
            else:
                logger.error(f"Login failed: {result.get('message', 'Unknown error')}")
                return False
        except Exception as e:
            logger.error(f"Error during login: {e}")
            return False

    def get_activities(self, page_no: int = 1, page_size: int = 20) -> Dict:
        """Get list of activities."""
        if not self.token:
            logger.error("Not logged in. Call login() first.")
            return {}

        url = f"{self.base_url}/web-gateway/web-analyze/activity/queryMyActivity"
        params = {"pageNo": page_no, "pageSize": page_size, "reqType": 0, "sort": 1}

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            result = response.json()

            if result["code"] == 0 and "data" in result:
                return result["data"]
            else:
                logger.error(
                    f"Failed to get activities: {result.get('message', 'Unknown error')}"
                )
                return {}
        except Exception as e:
            logger.error(f"Error getting activities: {e}")
            return {}

    def get_activity_detail(self, ride_id: int) -> Dict:
        """Get details for a specific activity."""
        if not self.token:
            logger.error("Not logged in. Call login() first.")
            return {}

        url = f"{self.base_url}/web-gateway/web-analyze/activity/queryActivityDetail/{ride_id}"

        try:
            response = self.session.get(url)
            response.raise_for_status()
            result = response.json()

            if result["code"] == 0 and "data" in result:
                return result["data"]
            else:
                logger.error(
                    f"Failed to get activity detail: {result.get('message', 'Unknown error')}"
                )
                return {}
        except Exception as e:
            logger.error(f"Error getting activity detail: {e}")
            return {}

    def download_fit_file(self, fit_url: str) -> Optional[bytes]:
        """Download a FIT file from the given URL."""
        try:
            response = requests.get(fit_url)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"Error downloading FIT file: {e}")
            return None


class GarminClient:
    """Client for Garmin Connect using garminconnect."""

    def __init__(
        self,
        email: str,
        password: str,
        domain: str,
        max_retries: int = 3,
        retry_delay: int = 5,
    ):
        self.email = email
        self.password = password
        self.domain = domain
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.authenticated = False
        self.rate_limited = False
        self.api = Garmin(
    self.email,
    self.password,
)

    def authenticate(self, force: bool = False) -> bool:
        """Load saved tokens or authenticate once with Garmin."""
        if self.authenticated:
            return True

        if self.rate_limited:
            logger.error("Garmin rate limit already reached. Not retrying.")
            return False

        try:
            logger.info("Loading saved Garmin tokens or logging in")
            self.api.login(GARMIN_SESSION_DIR)
            self.authenticated = True
            logger.info("Successfully authenticated with Garmin Connect")
            return True

        except Exception as e:
            self.authenticated = False
            error = str(e).lower()

            if "429" in error or "too many requests" in error:
                self.rate_limited = True
                logger.error(
                    "Garmin rate limit reached. Stopping without retrying login."
                )
            else:
                logger.error(f"Error authenticating with Garmin Connect: {e}")

            return False

    def get_activities(
        self,
        start_date: Optional[datetime.datetime] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """Get recent activities from Garmin Connect."""
        if not self.authenticated and not self.authenticate():
            return []

        try:
            response = self.api.get_activities(start=0, limit=limit)
            return response if isinstance(response, list) else []

        except Exception as e:
            logger.error(f"Error getting Garmin activities: {e}")
            return []

    def upload_fit(
        self,
        fit_data: bytes,
        activity_name: str = None,
    ) -> Optional[Dict]:
        """Upload a FIT file without retrying rate-limit errors."""
        if not self.authenticated and not self.authenticate():
            logger.error("Cannot upload: Garmin authentication failed")
            return None

        temp_file_path = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = self.retry_delay * (2 ** (attempt - 1))
                    delay += random.uniform(0, 2)
                    logger.info(
                        f"Retrying upload ({attempt}/{self.max_retries}) "
                        f"after {delay:.1f}s"
                    )
                    time.sleep(delay)

                with tempfile.NamedTemporaryFile(
                    suffix=".fit",
                    delete=False,
                ) as temp_file:
                    temp_file.write(fit_data)
                    temp_file_path = temp_file.name

                uploaded = self.api.upload_activity(temp_file_path)

                if uploaded:
                    logger.info(
                        f"Successfully uploaded activity: "
                        f"{activity_name or 'Unknown Activity'}"
                    )
                return uploaded

            except Exception as e:
                error = str(e).lower()
                logger.error(
                    f"Upload attempt {attempt + 1} failed: {e}"
                )

                if (
                    "429" in error
                    or "too many requests" in error
                    or "rate limit" in error
                ):
                    self.rate_limited = True
                    logger.error(
                        "Garmin rate limit reached. Stopping uploads."
                    )
                    return None

                if "409" in error or "conflict" in error:
                    logger.warning(
                        f"Activity conflict; skipping "
                        f"{activity_name or 'Unknown Activity'}"
                    )
                    return None

                if attempt >= self.max_retries:
                    logger.error("All upload attempts failed")
                    return None

            finally:
                if temp_file_path and os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                    temp_file_path = None

        return None


def load_last_sync_date() -> datetime.datetime:
    """Load the last sync date from the JSON file."""
    try:
        if os.path.exists(LAST_SYNC_FILE):
            with open(LAST_SYNC_FILE, "r") as f:
                data = json.load(f)

            if not isinstance(data, dict) or "last_sync_date" not in data:
                logger.error(
                    "No valid last sync date found. "
                    "Stopping to avoid syncing old activities."
                )
                raise ValueError("Missing last_sync_date")

            return datetime.datetime.fromisoformat(data["last_sync_date"])

        raise FileNotFoundError(
            f"Sync control file not found: {LAST_SYNC_FILE}"
        )

    except Exception as e:
        logger.error(f"Cannot load last sync date safely: {e}")
        raise


def save_last_sync_date(sync_date: datetime.datetime) -> None:
    """Save the last sync date to the JSON file."""
    try:
        with open(LAST_SYNC_FILE, "w") as f:
            json.dump({"last_sync_date": sync_date.isoformat()}, f)
    except Exception as e:
        logger.error(f"Error saving last sync date: {e}")


def activities_overlap(
    start_time1: datetime.datetime,
    duration1: int,
    start_time2: datetime.datetime,
    duration2: int,
) -> bool:
    """Check if two activities overlap in time."""
    end_time1 = start_time1 + datetime.timedelta(seconds=duration1)
    end_time2 = start_time2 + datetime.timedelta(seconds=duration2)

    buffer = datetime.timedelta(minutes=OVERLAP_BUFFER_MINUTES)

    # Check if either activity is contained within the other (with buffer)
    return (
        (start_time1 - buffer <= start_time2 <= end_time1 + buffer)
        or (start_time1 - buffer <= end_time2 <= end_time1 + buffer)
        or (start_time2 - buffer <= start_time1 <= end_time2 + buffer)
        or (start_time2 - buffer <= end_time1 <= end_time2 + buffer)
    )


def collect_activities_to_sync(
    igpsport_client: IGPSportClient,
    garmin_client: GarminClient,
    last_sync_date: datetime.datetime,
) -> List[Dict]:
    """Collect and filter activities to sync."""
    # Get recent activities from Garmin to check for overlap
    garmin_activities = garmin_client.get_activities(limit=20)
    garmin_activity_times = []
    for activity in garmin_activities:
        try:
            start_time = parse(activity.get("startTimeLocal", ""))
            duration = activity.get("duration", 0)
            garmin_activity_times.append((start_time, duration))
        except Exception as e:
            logger.warning(f"Error parsing Garmin activity time: {e}")

    # Get activities from iGPSport
    page_no = 1
    page_size = 20
    activities_data = igpsport_client.get_activities(page_no, page_size)

    if not activities_data or "rows" not in activities_data:
        logger.error("Failed to get activities from iGPSport")
        return []

    activities = activities_data["rows"]
    activities_to_sync = []

    for activity in activities:
        try:
            # Parse activity start time
            start_time_str = activity.get("startTime", "")
            activity_id = activity.get("rideId")
            # Handle the format like "2024.11.20" which is not ISO format
            # We'll need to convert it to proper datetime
            if "." in start_time_str:
                parts = start_time_str.split(".")
                if len(parts) == 3:
                    year, month, day = parts
                    start_time = datetime.datetime(int(year), int(month), int(day))
                else:
                    logger.warning(f"Invalid date format: {start_time_str}")
                    continue
            else:
                start_time = parse(start_time_str)

            # Skip if older than last sync date
            if start_time.date() < last_sync_date.date():
                logger.info(
                    f"Skipping activity {activity_id} from {start_time} (older than last sync)"
                )
                continue

            # Get activity detail to get the full start time and duration
            activity_detail = igpsport_client.get_activity_detail(activity_id)

            if not activity_detail:
                logger.warning(f"Could not get details for activity {activity_id}")
                continue

            # Parse the detailed start time
            detail_start_time = parse(activity_detail.get("startTime", ""))
            detail_duration = activity_detail.get("totalTime", 0)

            # Check for overlap with existing Garmin activities
            overlaps = False
            for garmin_start, garmin_duration in garmin_activity_times:
                if activities_overlap(
                    detail_start_time, detail_duration, garmin_start, garmin_duration
                ):
                    logger.info(
                        f"Skipping activity {activity_id} due to time overlap with existing Garmin activity"
                    )
                    overlaps = True
                    break

            if overlaps:
                continue

            # Add to list of activities to sync
            # Modified: Get fitUrl from activity_detail instead of activity due to iGPSport API change
            fit_url = activity_detail.get("fitUrl")
            if not fit_url:
                fit_url = activity.get("fitOssPath")

            if not fit_url:
                logger.warning(f"No FIT file URL for activity {activity_id}")
                continue

            activities_to_sync.append(
                {
                    "activity_id": activity_id,
                    "fit_url": fit_url,
                    "start_time": detail_start_time,
                    "duration": detail_duration,
                }
            )

        except Exception as e:
            logger.error(f"Error processing activity: {e}")

    return activities_to_sync


def main():
    """Main execution function."""
    # Get credentials from environment variables
    igpsport_username = os.environ.get("IGPSPORT_USERNAME")
    igpsport_password = os.environ.get("IGPSPORT_PASSWORD")
    igpsport_region = os.environ.get("IGPSPORT_REGION")
    garmin_email = os.environ.get("GARMIN_EMAIL")
    garmin_password = os.environ.get("GARMIN_PASSWORD")
    garmin_domain = os.environ.get("GARMIN_DOMAIN") or "garmin.com"

    # Log the session file location for debugging
    logger.info(
        f"Garmin session directory location: {os.path.abspath(GARMIN_SESSION_DIR)}"
    )
    if os.path.exists(GARMIN_SESSION_DIR):
        logger.info(f"Garmin session directory exists")
    else:
        logger.info("Garmin session directory does not exist yet")

    if not all(
        [
            igpsport_username,
            igpsport_password,
            garmin_email,
            garmin_password,
            garmin_domain,
        ]
    ):
        logger.error("Missing required environment variables")
        return

    # Initialize clients
    igpsport_client = IGPSportClient(
        igpsport_username, igpsport_password, igpsport_region
    )
    garmin_client = GarminClient(garmin_email, garmin_password, garmin_domain)

    # Authenticate with iGPSport
    if not igpsport_client.login():
        logger.error("Failed to authenticate with iGPSport")
        return

    # Load last sync date
    last_sync_date = load_last_sync_date()
    logger.info(f"Last sync date: {last_sync_date}")

    # Collect activities to sync without authenticating with Garmin yet
    activities_to_sync = collect_activities_to_sync(
        igpsport_client, garmin_client, last_sync_date
    )

    if not activities_to_sync:
        logger.info("No new activities to sync")
        return

    logger.info(f"Found {len(activities_to_sync)} activities to sync")

    # Only now authenticate with Garmin since we have activities to upload
    if not garmin_client.authenticate():
        logger.error("Failed to authenticate with Garmin")
        return

        sync_count = 0
    latest_synced_date = None
    had_failures = False

    # Download and upload activities in a batch
    for activity_info in activities_to_sync:
        activity_id = activity_info["activity_id"]
        fit_url = activity_info["fit_url"]
        start_time = activity_info["start_time"]

        # Download the FIT file
        fit_data = igpsport_client.download_fit_file(fit_url)
        if not fit_data:
            logger.warning(
                f"Failed to download FIT file for activity {activity_id}"
            )
            had_failures = True
            continue

        # Upload to Garmin
        result = garmin_client.upload_fit(fit_data)

        if garmin_client.rate_limited:
            logger.error(
                "Garmin rate limit reached. Stopping this sync run."
            )
            had_failures = True
            break

        if result:
            logger.info(
                f"Successfully uploaded activity {activity_id} to Garmin"
            )
            sync_count += 1

            if (
                latest_synced_date is None
                or start_time > latest_synced_date
            ):
                latest_synced_date = start_time

            time.sleep(2)
        else:
            logger.warning(
                f"Failed to upload activity {activity_id} to Garmin"
            )
            had_failures = True

    # Advance the checkpoint only when every activity succeeded
    if (
        not had_failures
        and sync_count == len(activities_to_sync)
        and latest_synced_date is not None
    ):
        save_last_sync_date(latest_synced_date)
        logger.info(
            f"Updated last sync date to: {latest_synced_date}"
        )
    else:
        logger.warning(
            "Some activities were not confirmed as uploaded. "
            "Keeping the previous sync date to avoid losing activities."
        )

    logger.info(
        f"Sync completed: {sync_count} activities uploaded, "
        f"{len(activities_to_sync) - sync_count} activities not confirmed"
    )


if __name__ == "__main__":
    main()
