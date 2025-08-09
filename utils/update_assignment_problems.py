#!/usr/bin/env python3

import argparse
import json
import logging
import os
import datetime
from typing import Dict, Any, List, NamedTuple, Tuple
import omegaup.api
import re
from urllib.parse import urlparse, urljoin
import shutil
import http.client
import ssl
import zipfile

context = ssl._create_unverified_context()

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

# ✅ Allowed course aliases
COURSE_ALIASES = [
    "curso-publico",
    "omi-public-course"
]

DOWNLOAD_BASE_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Courses"))
PROBLEMS_JSON_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "problems.json"))

class ProblemEntry(NamedTuple):
    path: str

def sanitize_filename(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)


def handle_input() -> Tuple[str, str, str]:
    parser = argparse.ArgumentParser(description="Add or remove problems from course assignments.")
    parser.add_argument("--url", default="https://omegaup.com", help="omegaUp base URL")
    parser.add_argument("--api-token", type=str, default=os.environ.get("OMEGAUP_API_TOKEN"), 
                      required=("OMEGAUP_API_TOKEN" not in os.environ))

    parser.add_argument("--input", type=str, default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "adding_removing_problems.json")), help="Path to JSON file")

    args = parser.parse_args()
    return args.api_token, args.url, args.input


def assignment_exists(assignments: List[Dict[str, Any]], alias: str) -> bool:
    return any(a["alias"] == alias for a in assignments)


def create_assignment(client: omegaup.api.Client, course_alias: str, assignment_alias: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    finish = now + datetime.timedelta(days=30)

    LOG.info(f"📅 Creating assignment '{assignment_alias}' in course '{course_alias}'")

    try:
        client.course.createAssignment(
            course_alias=course_alias,
            alias=assignment_alias,
            assignment_type="homework",
            name=assignment_alias,
            description=f"Auto-created assignment {assignment_alias}",
            start_time=int(now.timestamp()),
            finish_time=int(finish.timestamp()),
            unlimited_duration=True
        )
        LOG.info(f"✅ Created assignment '{assignment_alias}'")
    except Exception as e:
        LOG.error(f"❌ Failed to create assignment '{assignment_alias}': {e}")


def download_and_unzip(problem_alias: str, assignment_folder: str, base_url: str, api_token: str) -> bool:
    try:
        download_url = urljoin(base_url, f"/api/problem/download/problem_alias/{problem_alias}/")
        parsed_url = urlparse(download_url)
        conn = http.client.HTTPSConnection(parsed_url.hostname, context=context)

        headers = {'Authorization': f'token {api_token}'}
        path = parsed_url.path

        conn.request("GET", path, headers=headers)
        response = conn.getresponse()

        if response.status == 404:
            response_body = response.read()
            LOG.warning(
                f"⚠️  Problem '{problem_alias}' not found or access denied (404). "
                f"Response body:\n{response_body.decode(errors='ignore')}"
            )
            return False
        elif response.status != 200:
            response_body = response.read()
            LOG.error(f"❌ Failed to download '{problem_alias}'. HTTP status: {response.status}")
            LOG.error(f"❌ Response body:\n{response_body.decode(errors='ignore')}")
            return False

        problem_folder = os.path.join(assignment_folder, sanitize_filename(problem_alias))
        os.makedirs(problem_folder, exist_ok=True)

        zip_path = os.path.join(problem_folder, f"{problem_alias}.zip")
        with open(zip_path, "wb") as f:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)

        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(problem_folder)
            os.remove(zip_path)
            LOG.info(f"✅ Extracted: {problem_alias} → {problem_folder}")
        except zipfile.BadZipFile as e:
            LOG.error(f"❌ Failed to unzip: {zip_path}: {e}")
            return False

        settings_path = os.path.join(problem_folder, "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r+", encoding="utf-8") as f:
                    settings = json.load(f)
                    settings["alias"] = problem_alias
                    settings["title"] = problem_alias
                    f.seek(0)
                    json.dump(settings, f, indent=2, ensure_ascii=False)
                    f.truncate()
                LOG.info(f"🛠️  Updated settings.json with alias: {problem_alias}")
            except json.JSONDecodeError as e:
                LOG.warning(f"⚠️  Failed to update settings.json for '{problem_alias}': {e}")
        else:
            LOG.warning(f"⚠️  No settings.json found for '{problem_alias}'")

        return True

    except Exception as e:
        LOG.error(f"❌ Failed to download '{problem_alias}': {e}")
        return False


def process_add(data: Dict[str, Any], problems_data: Dict[str, List[Dict[str, str]]], 
               client: omegaup.api.Client, base_url: str):
    for item in data.get("add_problem", []):
        course = item["course_alias"]
        assignment = item["assignment_alias"]
        problem = item["problem_alias"]
        points = item["points"]

        if course not in COURSE_ALIASES:
            LOG.error(f"❌ Course '{course}' not allowed.")
            continue

        LOG.info(f"➕ Adding problem '{problem}' to assignment '{assignment}' in course '{course}'")

        try:
            assignments = client.course.listAssignments(course_alias=course).get("assignments", [])
            if not assignment_exists(assignments, assignment):
                LOG.warning(f"📂 Assignment '{assignment}' not found in course '{course}', creating it...")
                create_assignment(client, course, assignment)

            client.course.addProblem(
                course_alias=course,
                assignment_alias=assignment,
                problem_alias=problem,
                points=points
            )
            LOG.info(f"✅ Added problem '{problem}' to assignment '{assignment}'")

            assignment_folder = os.path.join(DOWNLOAD_BASE_FOLDER, sanitize_filename(course), 
                                            sanitize_filename(assignment))
            os.makedirs(assignment_folder, exist_ok=True)

            LOG.info(f"📥 Downloading and unzipping problem '{problem}'")
            success = download_and_unzip(
                problem_alias=problem,
                assignment_folder=assignment_folder,
                base_url=base_url,
                api_token=client.api_token
            )

            if success:
                add_problem_to_json(course, assignment, problem, problems_data)
                LOG.info(f"📘 problems.json updated with: Courses/{course}/{assignment}/{problem}")
            else:
                LOG.warning(f"⚠️  Skipping problems.json update due to failed download for '{problem}'")

        except Exception as e:
            LOG.error(f"❌ Failed to add problem '{problem}': {e}")


def process_remove(data: Dict[str, Any], problems_data: Dict[str, List[Dict[str, str]]], 
                  client: omegaup.api.Client):
    for item in data.get("remove_problem", []):
        course = item["course_alias"]
        assignment = item["assignment_alias"]
        problem = item["problem_alias"]

        if course not in COURSE_ALIASES:
            LOG.error(f"❌ Course '{course}' not allowed.")
            continue

        LOG.info(f"➖ Removing problem '{problem}' from assignment '{assignment}' in course '{course}'")

        try:
            assignments = client.course.listAssignments(course_alias=course).get("assignments", [])
            if not assignment_exists(assignments, assignment):
                LOG.warning(f"⚠️ Assignment '{assignment}' not found in course '{course}', skipping removal.")
                continue

            client.course.removeProblem(
                course_alias=course,
                assignment_alias=assignment,
                problem_alias=problem
            )
            LOG.info(f"✅ Removed problem '{problem}' from assignment '{assignment}'")

            problem_folder = os.path.join(
                DOWNLOAD_BASE_FOLDER, 
                sanitize_filename(course), 
                sanitize_filename(assignment), 
                sanitize_filename(problem)
            )
            if os.path.exists(problem_folder):
                try:
                    shutil.rmtree(problem_folder)
                    LOG.info(f"🗑️  Deleted folder for problem '{problem}' at {problem_folder}")
                except OSError as e:
                    LOG.warning(f"⚠️  Failed to delete folder '{problem_folder}': {e}")
            else:
                LOG.warning(f"⚠️  Folder '{problem_folder}' not found, skipping deletion.")

            remove_problem_from_json(course, assignment, problem, problems_data)
            LOG.info(f"📘 problems.json entry removed: Courses/{course}/{assignment}/{problem}")

        except Exception as e:
            LOG.error(f"❌ Failed to remove problem '{problem}': {e}")


def load_problems_json() -> Dict[str, List[ProblemEntry]]:
    if os.path.exists(PROBLEMS_JSON_PATH):
        with open(PROBLEMS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {"problems": [ProblemEntry(**p) for p in data.get("problems", [])]}
    return {"problems": []}


def save_problems_json(data: Dict[str, List[ProblemEntry]]):
    with open(PROBLEMS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"problems": [p._asdict() for p in data["problems"]]}, f, indent=2, ensure_ascii=False)


def add_problem_to_json(course: str, assignment: str, problem: str, 
                       problems_data: Dict[str, List[ProblemEntry]]):
    path = f"Courses/{course}/{assignment}/{problem}"
    if not any(p.path == path for p in problems_data["problems"]):
        problems_data["problems"].append(ProblemEntry(path=path))
        LOG.info(f"📝 Added '{path}' to problems.json")


def remove_problem_from_json(course: str, assignment: str, problem: str, 
                            problems_data: Dict[str, List[ProblemEntry]]):
    path = f"Courses/{course}/{assignment}/{problem}"
    before = len(problems_data["problems"])
    problems_data["problems"] = [p for p in problems_data["problems"] if p.path != path]
    after = len(problems_data["problems"])
    if before != after:
        LOG.info(f"🗑️  Removed '{path}' from problems.json")


def main():
    api_token, base_url, input_path = handle_input()
    client = omegaup.api.Client(api_token=api_token, url=base_url)

    if not os.path.exists(input_path):
        LOG.error(f"❌ JSON file not found: {input_path}")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    problems_data = load_problems_json()
    process_add(data, problems_data, client, base_url)
    process_remove(data, problems_data, client)
    save_problems_json(problems_data)

    try:
        with open(input_path, "w", encoding="utf-8") as f:
            json.dump({"add_problem": [], "remove_problem": []}, f, indent=2, ensure_ascii=False)
        LOG.info(f"🧹 Cleared 'add_problem' and 'remove_problem' arrays in {input_path}")
    except (IOError, json.JSONDecodeError) as e:
        LOG.error(f"❌ Failed to reset {input_path}: {e}")


if __name__ == "__main__":
    main()