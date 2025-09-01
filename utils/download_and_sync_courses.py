#!/usr/bin/env python3

import argparse
import json
import logging
import os
import zipfile
from typing import Dict, Any, List, NamedTuple, cast
from urllib.parse import urlparse, urljoin
import omegaup.api
import shutil
import http.client
import ssl

# Create SSL context that skips certificate verification
context = ssl._create_unverified_context()

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)


# 👇 Add your course aliases here
COURSE_ALIASES = [
    "ResolviendoProblemas2021",
    "omi-public-course"
]

BASE_COURSE_FOLDER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Courses"))


class InputArgs(NamedTuple):
    api_token: str
    base_url: str


def handle_input() -> InputArgs:
    parser = argparse.ArgumentParser(
        description="Download and extract problems from multiple course"
                    " assignments"
    )
    parser.add_argument("--url",
                        default="https://omegaup.com",
                        help="omegaUp base URL")
    parser.add_argument("--api-token",
                        type=str,
                        default=os.environ.get("OMEGAUP_API_TOKEN"),
                        required=("OMEGAUP_API_TOKEN" not in os.environ))
    args = parser.parse_args()
    if args.api_token is None:
        parser.error(
            "API token is required (use --api-token or set OMEGAUP_API_TOKEN)")
    return InputArgs(api_token=str(args.api_token), base_url=str(args.url))


def get_json(
        client: omegaup.api.Client,
        endpoint: str,
        params: Dict[str, str],
        base_url: str
) -> Dict[str, Any]:
    return cast(Dict[str, Any], client.query(endpoint, params))


def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " -_").strip()


def get_course_details(
        client: omegaup.api.Client,
        course_alias: str,
        course_base_folder: str,
        base_url: str
) -> Dict[str, Any]:
    params = {"alias": course_alias}
    details = get_json(client, "/api/course/details/", params, base_url)
    details.pop("assignments", None)
    details.pop("clarifications", None)

    course_folder = os.path.join(course_base_folder, course_alias)
    os.makedirs(course_folder, exist_ok=True)

    # Save course_settings.json
    course_settings_path = os.path.join(course_folder, "course_settings.json")
    with open(course_settings_path, "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2, ensure_ascii=False)

    return details


def get_assignments(
        client: omegaup.api.Client,
        course_alias: str,
        base_url: str
) -> List[Dict[str, Any]]:
    endpoint = "/api/course/listAssignments/"
    params = {"course_alias": course_alias}
    return cast(
        List[Dict[str, Any]],
        get_json(client, endpoint, params, base_url)["assignments"]
    )


def get_assignment_details(
        client: omegaup.api.Client,
        course_alias: str,
        assignment_alias: str,
        base_url: str
) -> Dict[str, Any]:
    endpoint = "/api/course/assignmentDetails/"
    params = {
        "course": course_alias,
        "assignment": assignment_alias
    }
    return get_json(client, endpoint, params, base_url)


def download_and_unzip(client: omegaup.api.Client, problem_alias: str,
                       assignment_folder: str, base_url: str) -> bool:
    try:
        download_url = urljoin(
            base_url,
            f"/api/problem/download/problem_alias/{problem_alias}/"
        )
        parsed_url = urlparse(download_url)
        if parsed_url.hostname is None:
            LOG.error(f"Invalid download URL (missing hostname): "
                      f"{download_url}")
            return False
        conn = http.client.HTTPSConnection(parsed_url.hostname,
                                           context=context)

        headers = {'Authorization': f'token {client.api_token}'}
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


def main() -> None:
    input = handle_input()
    client = omegaup.api.Client(api_token=input.api_token, url=input.base_url)

    if os.path.exists(BASE_COURSE_FOLDER):
        LOG.warning("Delete existing course folder to avoid conflicts")
        shutil.rmtree(BASE_COURSE_FOLDER)

    os.makedirs(BASE_COURSE_FOLDER, exist_ok=True)
    all_problems = []

    for course_alias in COURSE_ALIASES:
        LOG.info(f"📘 Starting course: {course_alias}")
        try:
            assignments = get_assignments(client, course_alias, input.base_url)

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
                    details = get_assignment_details(
                        client,
                        course_alias,
                        assignment_alias,
                        input.base_url
                    )
                    assignment_folder = os.path.join(course_folder,
                                                     assignment_alias)
                    os.makedirs(assignment_folder, exist_ok=True)

                    problems = details.get("problems", [])

                    for problem in problems:
                        try:
                            downloaded = download_and_unzip(client,
                                                            problem["alias"],
                                                            assignment_folder,
                                                            input.base_url)
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
