import contextlib
import datetime
import logging
import subprocess
import os.path

from types import TracebackType
from typing import AnyStr, Iterator, IO, Optional, Type, Sequence

import problems

_LANGUAGE_MAPPING = {
    'cpp': 'cpp17-gcc',
}


@contextlib.contextmanager
def _maybe_open(path: Optional[str],
                mode: str) -> Iterator[Optional[IO[AnyStr]]]:
    """A contextmanager that can open a file, or return None.

    This is useful to provide arguments to subprocess.call() and its friends.
    """
    if path is None:
        yield None
    else:
        with open(path, mode) as f:
            yield f


def getImageName(ci: bool) -> str:
    """Ensures the container image is present in the expected version."""
    if ci:
        # Since this is running on GitHub, downloading the image from the
        # GitHub container registry is significantly faster.
        imageName = 'docker.pkg.github.com/omegaup/quark/omegaup-runner-ci'
    else:
        # This does not require authentication.
        imageName = 'omegaup/runner-ci'

    taggedContainerName = f'{imageName}:v1.9.27'
    if not subprocess.check_output(
        ['docker', 'image', 'ls', '-q', taggedContainerName],
            universal_newlines=True).strip():
        logging.info('Downloading Docker image %s...', taggedContainerName)
        subprocess.check_call(['docker', 'pull', taggedContainerName])
    return taggedContainerName


class Compile:
    """Use the omegaUp container to compile and run programs.

    This is intended to be used as a context manager:

    with Compile(sourcePath='myprogram.cpp', ci=True) as c:
      c.run(stdinPath='myinput.in', stdoutPath='myoutput.out')
    """
    def __init__(
        self,
        sourcePath: str,
        ci: bool,
    ):
        self.containerId = ''
        self.containerSourceFilename = ''
        self.sourcePath = sourcePath
        self.ci = ci

    def __enter__(self) -> 'Compile':
        extension = os.path.splitext(self.sourcePath)[1][1:]
        self.language = _LANGUAGE_MAPPING.get(extension, extension)
        self.containerSourceFilename = f'Main.{extension}'
        self.containerId = subprocess.run([
            'docker',
            'run',
            '--rm',
            '--detach',
            '--entrypoint',
            '/usr/bin/sleep',
            '--volume',
            (f'{os.path.abspath(self.sourcePath)}:'
             f'/src/{self.containerSourceFilename}'),
            getImageName(self.ci),
            'infinity',
        ],
                                          universal_newlines=True,
                                          stdout=subprocess.PIPE,
                                          check=True).stdout.strip()

        try:
            self.run_command([
                '/var/lib/omegajail/bin/omegajail',
                '--homedir',
                '/src',
                '--homedir-writable',
                '--compile',
                self.language,
                '--compile-source',
                self.containerSourceFilename,
                '--compile-target',
                'Main',
            ])
        except subprocess.CalledProcessError as cpe:
            problems.error((f'Failed to compile {self.sourcePath}:\n' +
                            cpe.stderr.decode("utf-8")),
                           filename=self.sourcePath,
                           ci=self.ci)
            # If the container errored out before returning, __exit__() won't
            # be called, and the container will leak. Explicitly clean up
            # before re-raising the exception to avoid that.
            self.__cleanup()
            raise

        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc_value: Optional[BaseException],
                 traceback: Optional[TracebackType]) -> None:
        self.__cleanup()

    def run(
        self,
        stdinPath: str,
        stdoutPath: str,
        *,
        timeout: datetime.timedelta = datetime.timedelta(seconds=5)
    ) -> None:
        """Run a single invocation of the compiled binary."""
        self.run_command(args=[
            '/var/lib/omegajail/bin/omegajail',
            '--homedir',
            '/src',
            '--run',
            self.language,
            '--run-target',
            'Main',
        ],
                         stdinPath=stdinPath,
                         stdoutPath=stdoutPath,
                         timeout=timeout)

    def run_command(
        self,
        args: Sequence[str],
        *,
        stdinPath: Optional[str] = None,
        stdoutPath: Optional[str] = None,
        timeout: datetime.timedelta = datetime.timedelta(seconds=10)
    ) -> None:
        """Run an arbitrary command in the container."""
        logging.debug('Invoking command in container: "%s"', ' '.join(args))

        with _maybe_open(stdinPath,
                         'rb') as stdin, _maybe_open(stdoutPath,
                                                     'wb') as stdout:
            subprocess.run(
                ['docker', 'exec', '--interactive', self.containerId] +
                list(args),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                timeout=timeout.total_seconds(),
                check=True)

    def __cleanup(self) -> None:
        # The output is the same container id, so avoid printing it because
        # it's just noise.
        subprocess.check_call([
            'docker',
            'container',
            'kill',
            self.containerId,
        ],
                              stdout=subprocess.DEVNULL)
