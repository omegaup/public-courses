#!/usr/bin/env python3

import argparse
import json
import logging
import os
import zipfile
from typing import Dict, Any
from urllib.parse import urlparse, urljoin
import omegaup.api
import shutil
import http.client
import ssl

# Create SSL context that skips certificate verification
context = ssl._create_unverified_context()

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

API_CLIENT = None
BASE_URL = None

# 👇 Add your course aliases here
COURSE_ALIASES = [
    "ResolviendoProblemas2021",
    "omi-public-course"
]

BASE_COURSE_FOLDER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Courses"))


def handle_input():
    global BASE_URL, API_TOKEN
    parser = argparse.ArgumentParser(
        description=f"Download and extract problems from multiple course"
                    f" assignments"
    )
    parser.add_argument("--url",
                        default="https://omegaup.com",
                        help="omegaUp base URL")
    parser.add_argument("--api-token",
                        type=str,
                        default=os.environ.get("OMEGAUP_API_TOKEN"),
                        required=("OMEGAUP_API_TOKEN" not in os.environ))
    args = parser.parse_args()
    BASE_URL = args.url
    return args.api_token


def get_json(endpoint: str, params: Dict[str, str]) -> Dict[str, Any]:
    return API_CLIENT.query(endpoint, params)


def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " -_").strip()


def get_course_details(
        course_alias: str,
        course_base_folder: str
) -> Dict[str, Any]:
    details = get_json("/api/course/details/", {"alias": course_alias})
    details.pop("assignments", None)
    details.pop("clarifications", None)

    course_folder = os.path.join(course_base_folder, course_alias)
    os.makedirs(course_folder, exist_ok=True)

    # Save course_settings.json
    course_settings_path = os.path.join(course_folder, "course_settings.json")
    with open(course_settings_path, "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2, ensure_ascii=False)

    return details


def get_assignments(course_alias: str):
    return get_json("/api/course/listAssignments/",
                    {"course_alias": course_alias})["assignments"]


def get_assignment_details(course_alias: str, assignment_alias: str):
    return get_json("/api/course/assignmentDetails/", {
        "course": course_alias,
        "assignment": assignment_alias
    })


def download_and_unzip(problem_alias: str, assignment_folder: str) -> bool:
    try:
        download_url = urljoin(
            BASE_URL,
            f"/api/problem/download/problem_alias/{problem_alias}/"
        )
        parsed_url = urlparse(download_url)
        conn = http.client.HTTPSConnection(parsed_url.hostname,
                                           context=context)

        headers = {'Authorization': f'token {API_CLIENT.api_token}'}
        path = parsed_url.path

        conn.request("GET", path, headers=headers)
        response = conn.getresponse()

        if response.status == 404:
            response_body = response.read()
            LOG.warning(
                f"⚠️  Problem '{problem_alias}' not found or access denied "
                f"(404). Response body:\n"
                f"{response_body.decode(errors='ignore')}"
            )
            return False
        elif response.status != 200:
            response_body = response.read()
            LOG.error(
                f"❌ Failed to download '{problem_alias}'. HTTP status: "
                f"{response.status}"
            )
            LOG.error(
                f"❌ Response body:\n{response_body.decode(errors='ignore')}"
            )
            return False

        problem_folder = os.path.join(assignment_folder,
                                      sanitize_filename(problem_alias))
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
        except zipfile.BadZipFile:
            LOG.error(f"❌ Failed to unzip: {zip_path}")
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
                LOG.info(
                    f"🛠️  Updated settings.json with alias: {problem_alias}")
            except Exception as e:
                LOG.warning(
                    f"⚠️  Failed to update settings.json for "
                    f"'{problem_alias}': {e}"
                )
        else:
            LOG.warning(f"⚠️  No settings.json found for '{problem_alias}'")

        return True

    except Exception as e:
        LOG.error(f"❌ Failed to download '{problem_alias}': {e}")
        return False


def main():
    global API_CLIENT
    api_token = handle_input()
    API_CLIENT = omegaup.api.Client(api_token=api_token, url=BASE_URL)

    if os.path.exists(BASE_COURSE_FOLDER):
        LOG.warning("Delete existing course folder to avoid conflicts")
        shutil.rmtree(BASE_COURSE_FOLDER)

    os.makedirs(BASE_COURSE_FOLDER, exist_ok=True)
    all_problems = []

    for course_alias in COURSE_ALIASES:
        LOG.info(f"📘 Starting course: {course_alias}")
        try:
            assignments = get_assignments(course_alias)

            if not assignments:
                LOG.warning(f"No assignments found in {course_alias}.")
                continue

            course_folder = os.path.join(BASE_COURSE_FOLDER, course_alias)

            for assignment in assignments:
                assignment_alias = assignment["alias"]
                assignment_name = assignment["name"]
                LOG.info(
                    f"📂 Processing assignment: {assignment_name} "
                    f"({assignment_alias})"
                )

                try:
                    details = get_assignment_details(course_alias,
                                                     assignment_alias)
                    assignment_folder = os.path.join(course_folder,
                                                     assignment_alias)
                    os.makedirs(assignment_folder, exist_ok=True)

                    problems = details.get("problems", [])

                    for problem in problems:
                        try:
                            downloaded = download_and_unzip(problem["alias"],
                                                            assignment_folder)
                            if downloaded:
                                rel_path = os.path.join(
                                    "Courses",
                                    course_alias,
                                    assignment_alias,
                                    sanitize_filename(problem["alias"])
                                )
                                LOG.info(f"📂 Added problem path: {rel_path}")
                                all_problems.append({"path": rel_path})
                            else:
                                LOG.warning(
                                    f"⚠️  Skipped adding '{problem['alias']}' "
                                    f"due to download failure.")
                        except Exception as e:
                            LOG.error(
                                f"❌ Error while processing problem "
                                f"'{problem['alias']}': {e}"
                            )

                except Exception as e:
                    LOG.error(
                        f"❌ Failed to process assignment "
                        f"'{assignment_alias}': {e}"
                    )

        except Exception as e:
            LOG.error(f"❌ Failed to process course '{course_alias}': {e}")

    # ✅ Write problems.json
    problems_json_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "problems.json"))
    with open(problems_json_path, "w", encoding="utf-8") as f:
        LOG.info(f"Writing problems.json to {problems_json_path}")
        json.dump({"problems": all_problems}, f, indent=2, ensure_ascii=False)
    LOG.info("📝 Created problems.json with all problem paths.")


if __name__ == "__main__":
    main()
