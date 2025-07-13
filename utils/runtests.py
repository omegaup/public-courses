#!/usr/bin/python3
import argparse
import collections
import concurrent.futures
import decimal
import json
import logging
import os
import os.path
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading

from typing import Any, DefaultDict, Dict, List, Mapping, Optional, Tuple

import container
import problems

TestResult = Tuple[problems.Problem, Mapping[str, Any]]

_SANDBOX_DISABLED_WARNING = 'WARNING: Running with --disable-sandboxing'


def _availableProcessors() -> int:
    """Returns the number of available processors."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        # os.sched_setaffinity() is not available in all OSs. Since we don't
        # want to speculate how many cores there are, let's be paranoid and
        # return 1.
        return 1


def _threadInitializer(threadAffinityMapping: Dict[int, int],
                       lock: threading.Lock) -> None:
    """Set the thread affinity mapping for the current thread."""
    with lock:
        threadAffinityMapping[threading.get_ident()] = len(
            threadAffinityMapping)


def _testProblem(p: problems.Problem, *, threadAffinityMapping: Dict[int, int],
                 resultsDirectory: str, rootDirectory: str,
                 ci: bool) -> Optional[TestResult]:
    """Run the CI on a single problem."""
    logging.info('[%2d] %-30s: Testing problem...',
                 threadAffinityMapping[threading.get_ident()], p.title)

    problemResultsDirectory = os.path.join(resultsDirectory, p.path)
    problemOutputsDirectory = os.path.join(resultsDirectory, p.path, 'outputs')
    os.makedirs(problemOutputsDirectory)
    # The results are written with the container's UID, which does not
    # necessarily match the caller's UID. To avoid that problem, we create
    # the results directory with very lax permissions so that the container
    # can write it.
    os.chmod(problemResultsDirectory, 0o777)
    os.chmod(problemOutputsDirectory, 0o777)
    with open(os.path.join(problemResultsDirectory, 'ci.log'), 'w') as f:
        pass
    # Also make the ci log's permissions very lax.
    os.chmod(os.path.join(problemResultsDirectory, 'ci.log'), 0o666)

    if p.shouldGenerateOutputs(rootDirectory=rootDirectory):
        outputsArgs = [
            '-outputs',
            os.path.relpath(problemOutputsDirectory, rootDirectory),
        ]
    else:
        outputsArgs = []

    if len(threadAffinityMapping) == 1:
        # No need to involve taskset. Just run the container normally.
        tasksetArgs = [
            container.getImageName(ci),
        ]
    else:
        # Mark the entrypoint as only being able to run in a single core.
        tasksetArgs = [
            '--entrypoint',
            '/usr/bin/taskset',
            container.getImageName(ci),
            f'0x{2**threadAffinityMapping[threading.get_ident()]:x}',
            '/usr/bin/omegaup-runner',
        ]

    args = [
        'docker',
        'run',
        '--rm',
        '--volume',
        f'{rootDirectory}:/src',
    ] + tasksetArgs + [
        '-oneshot=ci',
        '-input',
        p.path,
        '-results',
        os.path.relpath(problemResultsDirectory, rootDirectory),
    ] + outputsArgs

    logging.debug('[%2d] %-30s: Running `%s`...',
                  threadAffinityMapping[threading.get_ident()], p.title,
                  shlex.join(args))
    processResult = subprocess.run(args,
                                   universal_newlines=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   cwd=rootDirectory)

    if processResult.returncode != 0:
        problems.error(f'Failed to run {p.title}:\n{processResult.stderr}',
                       filename=os.path.join(p.path, 'settings.json'),
                       ci=ci)
        return None

    # The CI might have written a log, but the stderr contents have a few
    # more things in it.
    with open(os.path.join(problemResultsDirectory, 'ci.log'), 'w') as f:
        f.write(processResult.stderr)

    for root, _, filenames in os.walk(problemOutputsDirectory):
        for filename in filenames:
            shutil.copy(
                os.path.join(root, filename),
                os.path.join(
                    rootDirectory, p.path,
                    os.path.relpath(os.path.join(root, filename),
                                    problemOutputsDirectory)))

    report = json.loads(processResult.stdout)
    logging.info('[%2d] %-30s: %s',
                 threadAffinityMapping[threading.get_ident()], p.title,
                 report['state'])
    return p, report


def _main() -> None:
    rootDirectory = problems.repositoryRoot()

    parser = argparse.ArgumentParser('Run tests')
    parser.add_argument('--ci',
                        action='store_true',
                        help='Signal that this is being run from the CI.')
    parser.add_argument('--all',
                        action='store_true',
                        help=('Consider all problems, instead of '
                              'only those that have changed'))
    parser.add_argument('--jobs',
                        '-j',
                        type=int,
                        default=_availableProcessors(),
                        help='Number of threads to run concurrently')
    parser.add_argument('--verbose',
                        action='store_true',
                        help='Verbose logging')
    parser.add_argument('--results-directory',
                        default=os.path.join(rootDirectory, 'results'),
                        help='Directory to store the results of the runs')
    parser.add_argument('--overwrite-outs',
                        action='store_true',
                        help=('Overwrite all .out files if '
                              'a generator is present'))
    parser.add_argument('--only-pull-image',
                        action='store_true',
                        help=('Don\'t run tests: '
                              'only download the Docker container'))
    parser.add_argument('problem_paths',
                        metavar='PROBLEM',
                        type=str,
                        nargs='*')
    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s: %(message)s',
                        level=logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)

    if args.only_pull_image:
        container.getImageName(args.ci)
        sys.exit(0)

    anyFailure = False

    if os.path.isdir(args.results_directory):
        shutil.rmtree(args.results_directory)
    os.makedirs(args.results_directory)

    # Run all the tests in parallel, but set the CPU affinity mask to a unique
    # core for each thread in the pool. This mimics how the production
    # container works (except for I/O).
    futures: List[concurrent.futures.Future[Optional[TestResult]]] = []
    threadAffinityMapping: Dict[int, int] = {}
    threadAffinityMappingLock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(os.cpu_count() or 1, args.jobs),
            initializer=_threadInitializer,
            initargs=(threadAffinityMapping,
                      threadAffinityMappingLock)) as executor:
        for p in problems.problems(allProblems=args.all,
                                   rootDirectory=rootDirectory,
                                   problemPaths=args.problem_paths):
            if (p.shouldGenerateOutputs(rootDirectory=rootDirectory)
                    and args.overwrite_outs):
                logging.info('[  ] %-30s: Removing old .out files...', p.title)
                for filename in os.listdir(
                        os.path.join(rootDirectory, p.path, 'cases')):
                    if not filename.endswith('.out'):
                        continue
                    os.unlink(
                        os.path.join(rootDirectory, p.path, 'cases', filename))

            futures.append(
                executor.submit(_testProblem,
                                p,
                                resultsDirectory=args.results_directory,
                                rootDirectory=rootDirectory,
                                threadAffinityMapping=threadAffinityMapping,
                                ci=args.ci))

    # Once the results are gathered, display the results all at once. This
    # limits the interleaving to make the output less confusing.
    for future in concurrent.futures.as_completed(futures):
        futureResult = future.result()
        if futureResult is None:
            anyFailure = True
            continue

        p, report = futureResult

        problemResultsDirectory = os.path.join(args.results_directory, p.path)

        if report['state'] != 'passed':
            anyFailure = True

        if report['state'] == 'skipped':
            errorString = report['error'] or (
                'tests/tests.json, settings.json, outs, or testplan are '
                'probably missing or invalid.')
            problems.error(f'Skipped {p.title}: {errorString}',
                           filename=os.path.join(p.path, 'settings.json'),
                           ci=args.ci)
            continue

        for testResult in report.get('tests', []):
            if testResult['type'] == 'solutions':
                testedFile = os.path.normpath(
                    os.path.join(p.path, 'tests', testResult['filename']))

                expected = dict(testResult['solution'])
                del (expected['filename'])
                if not expected:
                    # If there are no constraints, by default expect the run to
                    # be accepted.
                    expected['verdict'] = 'AC'
                logsDirectory = os.path.join(problemResultsDirectory,
                                             str(testResult['index']))
            else:
                if testResult['type'] == 'invalid-inputs':
                    testedFile = os.path.normpath(
                        os.path.join(p.path,
                                     'tests',
                                     'invalid-inputs',
                                     testResult['filename']))
                    expected = {'verdict': 'WA'}
                else:
                    testedFile = os.path.normpath(
                        os.path.join(p.path,
                                     'cases',
                                     testResult['filename']))
                    expected = {'verdict': 'AC'}
                logsDirectory = os.path.join(problemResultsDirectory,
                                             str(testResult['index']),
                                             'validator')

            got = {
                'verdict': testResult.get('result', {}).get('verdict'),
                'score': testResult.get('result', {}).get('score'),
            }

            logging.info(
                f'    {testResult["type"][:10]:10} | '
                f'{testResult["filename"][:40]:40} | '
                f'{testResult["state"]:8} | '
                f'expected={expected} got={got} | '
                f'logs at {os.path.relpath(logsDirectory, rootDirectory)}')

            failureMessages: DefaultDict[
                str, List[str]] = collections.defaultdict(list)

            normalizedScore = decimal.Decimal(got.get('score', 0))
            scaledScore = round(normalizedScore, 15) * 100

            if testResult['state'] != 'passed':
                # Build a table that reports groups and case verdicts.
                groupReportTable = [
                    f'{"group":20} | {"case":20} | {"score":7} | {"verdict"}',
                    f'{"-"*20}-+-{"-"*20}-+-{"-"*7}-+-{"-"*7}',
                ]
                if 'compile_error' in testResult['result']:
                    failureMessage = f"{testedFile}:\n" + textwrap.indent(
                        testResult['result']['compile_error'], '    ')
                    failureMessages[testedFile].append(failureMessage)
                if testResult['result']['groups'] is not None:
                    for group in testResult['result']['groups']:
                        groupReportTable.append(
                            f'{group["group"][:20]:20} | {"":20} | '
                            f'{group["score"]*100:6.2f}% |')
                        for c in group['cases']:
                            groupReportTable.append(
                                f'{"":20} | {c["name"][:20]:20} | '
                                f'{c["score"]*100:6.2f}% | {c["verdict"]:3}')
                        groupReportTable.append(
                            f'{"-"*20}-+-{"-"*20}-+-{"-"*7}-+-{"-"*7}')

                    failureMessages[testedFile].append(
                        '\n'.join(groupReportTable))

                    failedCases = {
                        c['name']
                        for g in testResult['result']['groups']
                        for c in g['cases'] if c['verdict'] != 'AC'
                    }
                else:
                    failedCases = set()

                if os.path.isdir(logsDirectory):
                    for stderrFilename in sorted(os.listdir(logsDirectory)):
                        caseName = os.path.splitext(stderrFilename)[0]

                        if not stderrFilename.endswith('.err'):
                            continue
                        if caseName not in failedCases:
                            continue

                        if testResult['type'] == 'solutions':
                            associatedFile = testedFile
                        else:
                            associatedFile = os.path.join(
                                p.path, 'cases', f'{caseName}.in')

                        with open(os.path.join(logsDirectory, stderrFilename),
                                  'r') as out:
                            contents = out.read().strip()

                            if contents.startswith(_SANDBOX_DISABLED_WARNING):
                                contents = contents[
                                    len(_SANDBOX_DISABLED_WARNING):].strip()

                            if not contents:
                                continue

                            failureMessage = (
                                f'{stderrFilename}:'
                                f'\n{textwrap.indent(contents, "    ")}')

                            failureMessages[associatedFile].append(
                                failureMessage)
                else:
                    logging.warning('Logs directory %r not found.',
                                    logsDirectory)

            for (path, messages) in failureMessages.items():
                problems.error(
                    (f'Validation failed for problem: {p.title}\n'
                     f'Related file: {path}\n') + '\n'.join(messages),
                    filename=path,
                    ci=args.ci)

        logging.info(f'Results for {p.title}: {report["state"]}')
        logging.info(f'    Full logs and report in {problemResultsDirectory}')

    if anyFailure:
        logging.info('')
        logging.info('At least one problem failed.')
        sys.exit(1)


if __name__ == '__main__':
    _main()
