#!/usr/bin/python3
import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import re
import subprocess
import sys

from typing import List, Optional

import container
import problems

_SUPPORTED_GENERATORS = frozenset(('png', 'testplan'))


def _getSolution(p: problems.Problem, *, rootDirectory: str,
                 ci: bool) -> Optional[str]:
    """Gets the solution for the problem."""
    solutions = [
        f for f in os.listdir(os.path.join(rootDirectory, p.path, 'solutions'))
        if f.startswith('solution.')
    ]

    if not solutions:
        return None
    if len(solutions) != 1:
        problems.fatal(f'Found more than one solution! {solutions}',
                       filename=os.path.join(p.path, 'settings.json'),
                       ci=ci)

    return os.path.join(rootDirectory, p.path, 'solutions', solutions[0])


def _getInputs(p: problems.Problem, *, rootDirectory: str,
               ci: bool) -> List[str]:
    """Gets the list of .in files for the problem."""
    inFilenames = [
        f for subdirectory in ('cases', 'examples', 'statements')
        for f in problems.enumerateFullPath(
            os.path.join(rootDirectory, p.path, subdirectory))
        if f.endswith('.in')
    ]
    if not inFilenames:
        problems.fatal(f'No test cases found for {p.title}!',
                       filename=os.path.join(p.path, 'settings.json'),
                       ci=ci)
    return inFilenames


def _generateTestplan(p: problems.Problem, *, rootDirectory: str, force: bool,
                      ci: bool) -> bool:
    """Generate testplan files for the provided problem."""
    logging.info('%-30s: Generating testplan for problem', p.title)

    if 'cases' not in p.config:
        return True

    testplan = os.path.join(rootDirectory, p.path, 'testplan')

    logging.info('%-30s: Generating testplan from settings.json.', p.title)

    if os.path.isfile(testplan):
        problems.fatal('testplan cannot exist when settings.json has cases!',
                       filename=os.path.relpath(testplan, rootDirectory),
                       ci=ci)

    with open(testplan, 'w') as tp:
        for group in p.config['cases']:
            for case in group['cases']:
                tp.write("{} {}\n".format(case['name'], case['weight']))

    return True


def _generateImages(p: problems.Problem, *, rootDirectory: str, force: bool,
                    ci: bool) -> bool:
    """Generate .png files for the provided problem."""
    logging.info('%-30s: Generating images for problem', p.title)

    if p.config.get('misc', {}).get('languages') != 'karel':
        logging.warning(
            '%-30s: Not a karel problem! Skipping generating images.', p.title)
        return True

    solutionPath = _getSolution(p, rootDirectory=rootDirectory, ci=ci)
    if solutionPath is None:
        logging.warning(
            '%-30s: No solution found! Skipping generating images.', p.title)
        return True
    relativeSolutionPath = os.path.relpath(solutionPath, rootDirectory)

    inFilenames = _getInputs(p, rootDirectory=rootDirectory, ci=ci)

    anyProblemFailure = False
    with container.Compile(sourcePath=solutionPath, ci=ci) as c:
        logging.info('%-30s: Generating pngs for problem', p.title)

        for inFilename in inFilenames:
            relativeInFilename = os.path.relpath(inFilename, rootDirectory)
            outFilename = f'{os.path.splitext(inFilename)[0]}.out'

            logging.debug('%-30s: Generating .pngs for %s', p.title,
                          inFilename)
            dimMatch = re.search(r'\.(\d*)x(\d*)\.in', inFilename)
            if dimMatch:
                dimOpts = [
                    '--height',
                    dimMatch.group(1), '--width',
                    dimMatch.group(2)
                ]
            else:
                dimOpts = []

            try:
                c.run_command([
                    '/opt/nodejs/lib/node_modules/karel/cmd/kareljs',
                    'draw',
                    '--output=-',
                ] + dimOpts,
                              stdinPath=inFilename,
                              stdoutPath=f'{inFilename}.png',
                              timeout=datetime.timedelta(seconds=10))
            except subprocess.CalledProcessError as cpe:
                anyProblemFailure = True
                problems.error((f'failed generating '
                                f'input .png for {relativeInFilename}:\n' +
                                cpe.stderr.decode("utf-8")),
                               filename=relativeInFilename,
                               ci=ci)
                continue

            try:
                c.run_command([
                    '/opt/nodejs/lib/node_modules/karel/cmd/kareljs',
                    'draw',
                    '--output=-',
                    '--run',
                    os.path.join('/src', c.containerSourceFilename),
                ] + dimOpts,
                              stdinPath=inFilename,
                              stdoutPath=f'{outFilename}.png',
                              timeout=datetime.timedelta(seconds=10))
            except subprocess.CalledProcessError as cpe:
                anyProblemFailure = True
                problems.error((f'{relativeSolutionPath} failed generating '
                                f'output .png with {relativeInFilename}:\n' +
                                cpe.stderr.decode("utf-8")),
                               filename=relativeSolutionPath,
                               ci=ci)

    if anyProblemFailure:
        logging.warning('%-30s: Failed generating some .png files', p.title)
        return False

    logging.info('%-30s: Success generating .png files', p.title)
    return True


def _main() -> None:
    parser = argparse.ArgumentParser('Generate resources')
    parser.add_argument(
        '--all',
        action='store_true',
        help='Consider all problems, instead of only those that have changed')
    parser.add_argument('--ci',
                        action='store_true',
                        help='Signal that this is being run from the CI.')
    parser.add_argument('--force',
                        action='store_true',
                        help='Force re-generating all resources')
    parser.add_argument('--jobs',
                        '-j',
                        default=min(32, (os.cpu_count() or 2) + 4),
                        help='Number of threads to run concurrently')
    parser.add_argument('--generate',
                        default=_SUPPORTED_GENERATORS,
                        type=lambda x: set(x.split(',')),
                        help=('Comma-separated list of artifacts to generate. '
                              'Should be a subset of {png,testplan}. '
                              'Generates everything by default.'))
    parser.add_argument('--verbose',
                        action='store_true',
                        help='Verbose logging')
    parser.add_argument('problem_paths',
                        metavar='PROBLEM',
                        type=str,
                        nargs='*')
    args = parser.parse_args()

    if args.generate - _SUPPORTED_GENERATORS:
        logging.error('Provided generators not supported: %r',
                      args.generate - _SUPPORTED_GENERATORS)
        sys.exit(1)

    logging.basicConfig(format='%(asctime)s: %(message)s',
                        level=logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)

    rootDirectory = problems.repositoryRoot()

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.jobs) as executor:
        futures: List[concurrent.futures.Future[bool]] = []

        for p in problems.problems(allProblems=args.all,
                                   rootDirectory=rootDirectory,
                                   problemPaths=args.problem_paths):
            if 'testplan' in args.generate:
                futures.append(
                    executor.submit(_generateTestplan,
                                    p,
                                    rootDirectory=rootDirectory,
                                    force=args.force,
                                    ci=args.ci))
            if 'png' in args.generate:
                futures.append(
                    executor.submit(_generateImages,
                                    p,
                                    rootDirectory=rootDirectory,
                                    force=args.force,
                                    ci=args.ci))

        if not all(future.result()
                   for future in concurrent.futures.as_completed(futures)):
            logging.error('Some resources failed to generate')
            sys.exit(1)


if __name__ == '__main__':
    _main()
