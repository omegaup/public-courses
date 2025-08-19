#!/usr/bin/python3
import argparse
import json
import logging
import os
import subprocess
import tempfile
import zipfile
import re

from typing import Any, Mapping, Set

import omegaup.api
import problems


# === Constants ===
SETTINGS_JSON = 'settings.json'
TESTPLAN_FILE = 'testplan'
STATEMENTS_DIR = 'statements'
SOLUTIONS_DIR = 'solutions'
CASES_DIR = 'cases'
EXAMPLES_DIR = 'examples'
INTERACTIVE_DIR = 'interactive'
VALIDATOR_PREFIX = 'validator'

API_PROBLEM_DETAILS = '/api/problem/details/'
API_PROBLEM_CREATE = '/api/problem/create/'
API_PROBLEM_UPDATE = '/api/problem/update/'

LANGUAGES_ALL = ','.join((
    'c11-gcc',
    'c11-clang',
    'cpp11-gcc',
    'cpp11-clang',
    'cpp17-gcc',
    'cpp17-clang',
    'cs',
    'hs',
    'java',
    'lua',
    'pas',
    'py2',
    'py3',
    'rb',
))
LANGUAGES_KAREL = 'kj,kp'
LANGUAGES_NONE = ''


def createProblemZip(problemConfig: Mapping[str, Any], problemPath: str,
                     zipPath: str) -> None:
    """Creates a problem .zip on the provided path."""
    with zipfile.ZipFile(zipPath, 'w',
                         compression=zipfile.ZIP_DEFLATED) as archive:

        def _addFile(f: str) -> None:
            logging.debug('writing %s', f)
            archive.write(f, os.path.relpath(f, problemPath))

        def _recursiveAdd(directory: str) -> None:
            for (root, _,
                 filenames) in os.walk(os.path.join(problemPath, directory)):
                for f in filenames:
                    _addFile(os.path.join(root, f))

        testplan = os.path.join(problemPath, TESTPLAN_FILE)

        if os.path.isfile(testplan):
            _addFile(testplan)

        if problemConfig['Validator']['Name'] == 'custom':
            validators = [
                x
                for x in os.listdir(problemPath)
                if x.startswith(VALIDATOR_PREFIX)
            ]

            if not validators:
                raise Exception('Custom validator missing!')
            if len(validators) != 1:
                raise Exception('More than one validator found!')

            validator = os.path.join(problemPath, validators[0])

            _addFile(validator)

        for directory in (STATEMENTS_DIR, SOLUTIONS_DIR, CASES_DIR):
            _recursiveAdd(directory)

        for directory in (EXAMPLES_DIR, INTERACTIVE_DIR):
            if not os.path.isdir(os.path.join(problemPath, directory)):
                continue
            _recursiveAdd(directory)


def uploadProblemZip(client: omegaup.api.Client,
                     problemConfig: Mapping[str, Any], canCreate: bool,
                     zipPath: str, commitMessage: str) -> None:
    """Uploads a problem with the given .zip and configuration."""
    misc = problemConfig.get('misc', {})
    alias = problemConfig.get('alias', "")
    limits = problemConfig.get('Limits', {})
    validator = problemConfig.get('Validator', {})

    payload = {
        'message': commitMessage,
        'problem_alias': alias,
    }

    if misc:
        if misc.get('visibility') is not None:
            payload['visibility'] = misc['visibility']
        if misc.get('languages') is not None:
            payload['languages'] = misc['languages']
        if misc.get('email_clarifications') is not None:
            payload['email_clarifications'] = misc.get('email_clarifications',
                                                       0)
        if misc.get('group_score_policy') is not None:
            payload['group_score_policy'] = misc.get('group_score_policy',
                                                     'sum-if-not-zero'),

    if limits:
        time_limit = limits.get('TimeLimit')
        if time_limit is not None:
            payload['time_limit'] = parse_limit_value(time_limit)
        memory_limit = limits.get('MemoryLimit')
        if memory_limit is not None:
            payload['memory_limit'] = parse_limit_value(memory_limit) // 1024
        input_limit = limits.get('InputLimit')
        if input_limit is not None:
            payload['input_limit'] = parse_limit_value(input_limit)
        output_limit = limits.get('OutputLimit')
        if output_limit is not None:
            payload['output_limit'] = parse_limit_value(output_limit)
        extra_wall_time = limits.get('ExtraWallTime')
        if extra_wall_time is not None:
            payload['extra_wall_time'] = parse_limit_value(extra_wall_time)
        overall_wall_time = limits.get('OverallWallTimeLimit')
        payload['overall_wall_time_limit'] = 0
        if overall_wall_time is not None:
            payload['overall_wall_time_limit'] = parse_limit_value(
                overall_wall_time)

    if validator:
        if validator.get('validator') is None:
            payload['validator'] = validator.get('Name', 'default')

    exists = client.query(
        API_PROBLEM_DETAILS,
        {'problem_alias': alias}
    )['status'] == 'ok'

    if not exists:
        if not canCreate:
            raise Exception("Problem doesn't exist!")
        logging.info("Problem doesn't exist. Creating problem.")
        endpoint = API_PROBLEM_CREATE
    else:
        endpoint = API_PROBLEM_UPDATE

    languages = payload.get('languages', '')

    if languages == 'all':
        payload['languages'] = LANGUAGES_ALL
    elif languages == 'karel':
        payload['languages'] = LANGUAGES_KAREL
    elif languages == 'none':
        payload['languages'] = LANGUAGES_NONE

    with open(zipPath, 'rb') as f:
        files = {'problem_contents': f}
        client.query(endpoint, payload, files)

    if exists:
        course_alias = misc.get('course_alias', '')
        assignment_alias = misc.get('assignment_alias', '')

        if course_alias and assignment_alias:
            try:
                details = client.course.assignmentDetails(
                    course=course_alias,
                    assignment=assignment_alias
                )

                versions = client.problem.versions(problem_alias=alias,
                                                   check_=False)
                commit = getattr(versions, 'published', '')

                if not commit:
                    logging.warning(
                        "No commit found in versions: %s", versions)
                    commit = ''

                client.course.addProblem(
                    course_alias=course_alias,
                    assignment_alias=assignment_alias,
                    problem_alias=alias,
                    points=getattr(details, 'points', 100.0),
                    check_=False
                )
                logging.info(
                    "Successfully added problem %s to course %s, "
                    "assignment %s", alias, course_alias, assignment_alias
                )

            except Exception as e:
                logging.warning("Could not add problem to assignment: %s", e)
        else:
            logging.info(
                "No course information found, "
                "problem %s uploaded successfully",
                alias
            )

    targetAdmins = misc.get('admins', [])
    targetAdminGroups = misc.get('admin-groups', [])
    allAdmins = None

    if targetAdmins or targetAdminGroups:
        allAdmins = client.problem.admins(problem_alias=alias)

    if targetAdmins and allAdmins:
        admins = {
            a['username'].lower()
            for a in allAdmins['admins'] if a['role'] == 'admin'
        }

        desiredAdmins = {admin.lower() for admin in targetAdmins}

        clientAdmin: Set[str] = set()
        if client.username:
            clientAdmin.add(client.username.lower())
        adminsToRemove = admins - desiredAdmins - clientAdmin
        adminsToAdd = desiredAdmins - admins - clientAdmin

        for admin in adminsToAdd:
            logging.info('Adding problem admin: %s', admin)
            client.problem.addAdmin(problem_alias=alias, usernameOrEmail=admin)

        for admin in adminsToRemove:
            logging.info('Removing problem admin: %s', admin)
            client.problem.removeAdmin(problem_alias=alias,
                                       usernameOrEmail=admin)

    if targetAdminGroups and allAdmins:
        adminGroups = {
            a['alias'].lower()
            for a in allAdmins['group_admins'] if a['role'] == 'admin'
        }

        desiredGroups = {group.lower() for group in targetAdminGroups}

        groupsToRemove = adminGroups - desiredGroups
        groupsToAdd = desiredGroups - adminGroups

        for group in groupsToAdd:
            logging.info('Adding problem admin group: %s', group)
            client.problem.addGroupAdmin(problem_alias=alias, group=group)

        for group in groupsToRemove:
            logging.info('Removing problem admin group: %s', group)
            client.problem.removeGroupAdmin(problem_alias=alias, group=group)

    if 'tags' in misc:
        tags = {
            t['name'].lower()
            for t in client.problem.tags(problem_alias=alias)['tags']
        }

        desiredTags = {t.lower() for t in misc['tags']}

        tagsToRemove = tags - desiredTags
        tagsToAdd = desiredTags - tags

        for tag in tagsToRemove:
            if tag.startswith('problemRestrictedTag'):
                logging.info('Skipping restricted tag: %s', tag)
                continue
            client.problem.removeTag(problem_alias=alias, name=tag)

        for tag in tagsToAdd:
            logging.info('Adding problem tag: %s', tag)
            client.problem.addTag(problem_alias=alias,
                                  name=tag,
                                  public=payload.get('public', False))


def parse_limit_value(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value.endswith("ms"):
            return int(float(value[:-2]))
        if value.endswith("s"):
            return int(float(value[:-1]) * 1000)
        if re.match(r"^\d+(\.\d+)?$", value):
            # Assume milliseconds if no suffix
            return int(float(value))
        raise ValueError(f"Invalid limit value format: {value}")
    raise TypeError(f"Unsupported type for limit value: {type(value)}")


def uploadProblem(client: omegaup.api.Client, problemPath: str,
                  commitMessage: str, canCreate: bool) -> None:
    with open(os.path.join(problemPath, SETTINGS_JSON), 'r') as f:
        problemConfig = json.load(f)

    logging.info('Uploading problem: %s', problemConfig['alias'])
    path_parts = problemPath.split(os.sep)
    course_alias = ''
    assignment_alias = ''

    if len(path_parts) >= 3:
        assignment_alias = path_parts[-2]
        course_alias = path_parts[-3]

    if 'misc' not in problemConfig:
        problemConfig['misc'] = {}

    problemConfig['misc']['course_alias'] = course_alias
    problemConfig['misc']['assignment_alias'] = assignment_alias

    with tempfile.NamedTemporaryFile() as tempFile:
        createProblemZip(problemConfig, problemPath, tempFile.name)

        uploadProblemZip(client,
                         problemConfig,
                         canCreate,
                         tempFile.name,
                         commitMessage=commitMessage)

        logging.info('Success uploading %s', problemConfig['alias'])


def _main() -> None:
    env = os.environ

    parser = argparse.ArgumentParser(
        description='Deploy a problem to omegaUp.')
    parser.add_argument('--ci',
                        action='store_true',
                        help='Signal that this is being run from the CI.')
    parser.add_argument(
        '--all',
        action='store_true',
        help='Consider all problems, instead of only those that have changed')
    parser.add_argument('--verbose',
                        action='store_true',
                        help='Verbose logging')
    parser.add_argument('--url',
                        default='https://omegaup.com',
                        help='URL of the omegaUp host.')
    parser.add_argument('--api-token',
                        type=str,
                        default=env.get('OMEGAUP_API_TOKEN'))
    parser.add_argument('-u',
                        '--username',
                        type=str,
                        default=env.get('OMEGAUPUSER'),
                        required=('OMEGAUPUSER' not in env
                                  and 'OMEGAUP_API_TOKEN' not in env))
    parser.add_argument('-p',
                        '--password',
                        type=str,
                        default=env.get('OMEGAUPPASS'),
                        required=('OMEGAUPPASS' not in env
                                  and 'OMEGAUP_API_TOKEN' not in env))
    parser.add_argument('--can-create',
                        action='store_true',
                        help=("Whether it's allowable to create the "
                              "problem if it does not exist."))
    parser.add_argument('problem_paths',
                        metavar='PROBLEM',
                        type=str,
                        nargs='*')
    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s: %(message)s',
                        level=logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)

    client = omegaup.api.Client(api_token=args.api_token,
                                url=args.url)

    if env.get('GITHUB_ACTIONS'):
        commit = env['GITHUB_SHA']
    else:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                         universal_newlines=True).strip()

    rootDirectory = problems.repositoryRoot()

    for problem in problems.problems(allProblems=args.all,
                                     rootDirectory=rootDirectory,
                                     problemPaths=args.problem_paths):
        uploadProblem(
            client,
            os.path.join(rootDirectory, problem.path),
            commitMessage=f'Deployed automatically from commit {commit}',
            canCreate=args.can_create)


if __name__ == '__main__':
    _main()
