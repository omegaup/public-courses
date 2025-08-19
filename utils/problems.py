import logging
import os
import sys
import subprocess
import json

from typing import Any, List, Mapping, NamedTuple, NoReturn, Optional, Sequence


SETTINGS_JSON = 'settings.json'
GITIGNORE = '.gitignore'
OUT_PATTERN = '**/*.out'
PROBLEMS_JSON = 'problems.json'
DEFAULT_COMMIT_RANGE = 'origin/main...HEAD'


class Problem(NamedTuple):
    """Represents a single problem."""
    path: str
    title: str
    config: Mapping[str, Any]

    @staticmethod
    def load(problemPath: str, rootDirectory: str) -> 'Problem':
        """Load a single problem from the path."""
        settings_path = os.path.join(rootDirectory, problemPath, SETTINGS_JSON)
        try:
            with open(settings_path) as f:
                problemConfig = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"{SETTINGS_JSON} not found at: {settings_path}")
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON format in {SETTINGS_JSON} at: {settings_path}. "
                f"Error: {e}"
            )

        return Problem(path=problemPath,
                       title=problemConfig['title'],
                       config=problemConfig)

    def shouldGenerateOutputs(self, *, rootDirectory: str) -> bool:
        """Returns whether the .out files should be generated for this problem.

        .out files are only generated if there is a .gitignore file that
        contains the line `**/*.out` in the problem directory.
        """
        gitignorePath = os.path.join(rootDirectory, self.path, GITIGNORE)
        if not os.path.isfile(gitignorePath):
            return False
        with open(gitignorePath, 'r') as f:
            for line in f:
                if line.strip() == OUT_PATTERN:
                    return True
        return False


def repositoryRoot() -> str:
    """Returns the root directory of the project.

    If this is a submodule, it gets the root of the top-level working tree.
    Raises RuntimeError if it fails to determine the root.
    """
    try:
        output = subprocess.check_output([
            'git', 'rev-parse', '--show-superproject-working-tree',
            '--show-toplevel'
        ], universal_newlines=True)
        return output.strip().split()[0]
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "Failed to find Git repository root: not inside a Git repo.")
    except FileNotFoundError:
        raise RuntimeError("Git is not installed or not found in PATH.")


def enumerateFullPath(path: str) -> List[str]:
    """Returns a list of full paths for the files in `path`."""
    if not os.path.exists(path):
        return []
    return [os.path.join(path, f) for f in os.listdir(path)]


def ci_error(message: str,
             *,
             filename: Optional[str] = None,
             line: Optional[int] = None,
             col: Optional[int] = None) -> None:
    """Show an error message, only on the CI."""
    location = []
    if filename is not None:
        location.append(f'file={filename}')
    if line is not None:
        location.append(f'line={line}')
    if col is not None:
        location.append(f'col={col}')
    print(
        f'::error {",".join(location)}::' +
        message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'),
        file=sys.stderr,
        flush=True)


def error(message: str,
          *,
          filename: Optional[str] = None,
          line: Optional[int] = None,
          col: Optional[int] = None,
          ci: bool = False) -> None:
    """Show an error message."""
    if ci:
        ci_error(message, filename=filename, line=line, col=col)
    else:
        logging.error(message)


def fatal(message: str,
          *,
          filename: Optional[str] = None,
          line: Optional[int] = None,
          col: Optional[int] = None,
          ci: bool = False) -> NoReturn:
    """Show a fatal message and exit."""
    error(message, filename=filename, line=line, col=col, ci=ci)
    sys.exit(1)


def problems(allProblems: bool = False,
             problemPaths: Sequence[str] = (),
             rootDirectory: Optional[str] = None) -> List[Problem]:
    """Gets the list of problems that will be considered.

    If `allProblems` is passed, all the problems that are declared in
    `problems.json` will be returned. Otherwise, only those that have
    differences with `upstream/main`.
    """
    env = os.environ
    if rootDirectory is None:
        rootDirectory = repositoryRoot()

    logging.info('Loading problems...')

    if problemPaths:
        # Generate the Problem objects from just the path. The title is ignored
        # anyways, since it's read from the configuration file in the problem
        # directory for anything important.
        return [
            Problem.load(problemPath=problemPath, rootDirectory=rootDirectory)
            for problemPath in problemPaths
        ]

    with open(os.path.join(rootDirectory, PROBLEMS_JSON), 'r') as p:
        config = json.load(p)

    configProblems: List[Problem] = []
    for problem in config['problems']:
        if problem.get('disabled', False):
            logging.warning('Problem %s disabled. Skipping.', problem['title'])
            continue
        configProblems.append(
            Problem.load(problemPath=problem['path'],
                         rootDirectory=rootDirectory))

    if allProblems:
        logging.info('Loading everything as requested.')
        return configProblems

    logging.info('Loading git diff.')

    if env.get('TRAVIS_COMMIT_RANGE'):
        commitRange = env['TRAVIS_COMMIT_RANGE']
    elif env.get('CIRCLE_COMPARE_URL'):
        commitRange = env['CIRCLE_COMPARE_URL'].split('/')[6]
    elif env.get('GITHUB_BASE_COMMIT'):
        commitRange = env['GITHUB_BASE_COMMIT'] + '...HEAD'
    else:
        commitRange = DEFAULT_COMMIT_RANGE

    changes = subprocess.check_output(
        ['git', 'diff', '--name-only', '--diff-filter=AMDR', commitRange],
        cwd=rootDirectory,
        universal_newlines=True)

    problems: List[Problem] = []
    for problem in configProblems:
        logging.info('Loading %s.', problem.title)

        if problem.path not in changes:
            logging.info('No changes to %s. Skipping.', problem.title)
            continue
        problems.append(problem)

    return problems
