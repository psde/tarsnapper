import sys, os
from os import path
import uuid
import subprocess
import re
from string import Template
from datetime import datetime, timedelta
import logging
import argparse

import expire, config
from config import Job


class ArgumentError(Exception):
    pass


class TarsnapError(Exception):
    pass


class TarsnapBackend(object):
    """The code that calls the tarsnap executable.

    One of the reasons this is designed as a class is to allow the backend
    to mimimize the calls to "tarsnap --list-archives" by caching the result.
    """

    def __init__(self, log, options, dryrun=False):
        """
        ``options`` - options to pass to each tarsnap call
        (a list of key value pairs).

        In ``dryrun`` mode, will class will only pretend to make and/or
        delete backups. This is a global option rather than a method
        specific one, because once the cached list of archives is tainted
        with simulated data, you don't really want to run in non-dry mode.
        """
        self.log = log
        self.options = options
        self.dryrun = dryrun
        self._archive_list = None

    def _call(self, *arguments):
        """
        ``arguments`` is a single list of strings.
        """
        call_with = ['tarsnap']
        call_with.extend(arguments)
        for key, value in self.options:
            call_with.extend(["--%s" % key, value])
        self.log.debug("Executing: %s" % " ".join(call_with))
        p = subprocess.Popen(call_with, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        p.wait()
        if p.returncode != 0:
            raise TarsnapError('%s' % p.stderr.read())
        return p.stdout

    def get_archives(self):
        """A list of archives as returned by --list-archives. Queried
        the first time it is accessed, and then subsequently cached.
        """
        if self._archive_list is None:
            response = self._call('--list-archives')
            self._archive_list = [l.rstrip() for l in response.readlines()]
        return self._archive_list
    archives = property(get_archives)

    def get_backups(self, job):
        """Return a dict of backups that exist for the given job, by
        parsing the list of archives.
        """
        unique = uuid.uuid4().hex
        target = Template(job.target).substitute({'name': job.name, 'date': unique})
        regex = re.compile("^%s$" % re.escape(target).replace(unique, '(?P<date>.*?)'))

        backups = {}
        for backup_path in self.get_archives():
            match = regex.match(backup_path)
            if not match:
                continue
            date = parse_date(match.groupdict()['date'], job.dateformat)
            backups[backup_path] = date

        return backups

    def expire(self, job):
        """Have tarsnap delete those archives which we need to expire
        according to the deltas defined.

        If a dry run is wanted, set ``dryrun`` to a dict of the backups to
        pretend that exist (they will always be used, and not matched).
        """

        backups = self.get_backups(job)
        self.log.info('%d backups are matching' % len(backups))

        # Determine which backups we need to get rid of, which to keep
        to_keep = expire.expire(backups, job.deltas)
        self.log.info('%d of those can be deleted' % (len(backups)-len(to_keep)))

        # Delete all others
        for name, _ in backups.items():
            if not name in to_keep:
                self.log.info('Deleting %s' % name)
                if not self.dryrun:
                    self._call('-d', '-f', name)
                self.archives.remove(name)
            else:
                self.log.debug('Keeping %s' % name)

    def make(self, job):
        now = datetime.utcnow()
        date_str = now.strftime(job.dateformat or DEFAULT_DATEFORMAT)
        target = Template(job.target).safe_substitute(
            {'date': date_str, 'name': job.name})

        if job.name:
            self.log.info('Creating backup %s: %s' % (job.name, target))
        else:
            self.log.info('Creating backup: %s' % target)

        if not self.dryrun:
            self._call('-c', '-f', target, *job.sources)
        # Add the new backup the list of archives, so we have an up-to-date
        # list without needing to query again.
        self.archives.append(target)

        return target, now


DATE_FORMATS = (
    '%Y%m%d-%H%M%S',
    '%Y%m%d-%H%M',
)
DEFAULT_DATEFORMAT = '%Y%m%d-%H%M%S'

def parse_date(string, dateformat=None):
    """Parse a date string using either a list of builtin formats,
    or the given one.
    """
    for to_try in ([dateformat] if dateformat else DATE_FORMATS):
        try:
            return datetime.strptime(string, to_try)
        except ValueError:
            pass
    else:
        raise ValueError('"%s" is not a supported date format' % string)


def timedelta_string(value):
    """Parse a string to a timedelta value.
    """
    try:
        return config.str_to_timedelta(value)
    except ValueError, e:
        raise argparse.ArgumentTypeError('invalid delta value: %r (suffix d, s allowed)' % e)


class Command(object):

    BackendClass = TarsnapBackend

    def __init__(self, args, log):
        self.args = args
        self.log = log
        self.backend = self.BackendClass(
            self.log, self.args.tarsnap_options,
            dryrun=getattr(self.args, 'dryrun', False))

    @classmethod
    def setup_arg_parser(self, parser):
        pass

    @classmethod
    def validate_args(self, args):
        pass

    def run(self, job):
        raise NotImplementedError()


class ListCommand(Command):

    help = 'list all the existing backups'
    description = 'For each job, output a sorted list of existing backups.'

    def run(self, job):
        backups = self.backend.get_backups(job)

        self.log.info('%s' % job.name)

        # Sort backups by time
        # TODO: This duplicates code from the expire module. Should
        # the list of backups always be returned sorted instead?
        backups = [(name, time) for name, time in backups.items()]
        backups.sort(cmp=lambda x, y: -cmp(x[1], y[1]))
        for backup, _ in backups:
            print "  %s" % backup


class ExpireCommand(Command):

    help = 'delete old backups, but don\'t create a new one'
    description = 'For each job defined, determine which backups can ' \
                  'be deleted according to the deltas, and then delete them.'

    @classmethod
    def setup_arg_parser(self, parser):
        parser.add_argument('--dry-run', dest='dryrun', action='store_true',
                            help='only simulate, don\'t delete anything')

    def expire(self, job):
        self.backend.expire(job)

    def run(self, job):
        self.expire(job)


class MakeCommand(ExpireCommand):

    help = 'create a new backup, and afterwards expire old backups'
    description = 'For each job defined, make a new backup, then ' \
                  'afterwards delete old backups no longer required. '\
                  'If you need only the latter, see the separate ' \
                  '"expire" command.'

    @classmethod
    def setup_arg_parser(self, parser):
        parser.add_argument('--dry-run', dest='dryrun', action='store_true',
                            help='only simulate, make no changes',)
        parser.add_argument('--no-expire', dest='no_expire',
                            action='store_true', default=None,
                            help='don\'t expire, only make backups')

    @classmethod
    def validate_args(self, args):
        if not args.config and not args.target:
            raise ArgumentError('Since you are not using a config file, '\
                                'you need to give --target')
        if not args.config and not args.deltas and not args.no_expire:
            raise ArgumentError('Since you are not using a config file, and '\
                                'have not specified --no-expire, you will '
                                'need to give --deltas')
        if not args.config and not args.sources:
            raise ArgumentError('Since you are not using a config file, you '
                                'need to specify at least one source path '
                                'using --sources')

    def run(self, job):
        # Determine whether we can run this job. If any of the sources
        # are missing, or any source directory is empty, we skip this job.
        sources_missing = False
        for source in job.sources:
            if not path.exists(source):
                sources_missing = True
                break
            if path.isdir(source) and not os.listdir(source):
                # directory is empty
                sources_missing = True
                break

        # Do a new backup
        skipped = False

        if sources_missing:
            if job.name:
                self.log.info(("Not backing up '%s', because not all given "
                               "sources exist") % job_name)
            else:
                self.log.info("Not making backup, because not all given "
                              "sources exist")
            skipped = True
        else:
            self.backend.make(job)

        # Expire old backups, but only bother if either we made a new
        # backup, or if expire was explicitly requested.
        if not skipped and not self.args.no_expire:
            self.expire(job)


COMMANDS = {
    'make': MakeCommand,
    'expire': ExpireCommand,
    'list': ListCommand,
}


def parse_args(argv):
    """Parse the command line.
    """
    parser = argparse.ArgumentParser(
        description='An interface to tarsnap to manage backups.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-q', action='store_true', dest='quiet', help='be quiet')
    group.add_argument('-v', action='store_true', dest='verbose', help='be verbose')
    parser.add_argument('-o', metavar=('name', 'value'), nargs=2,
                        dest='tarsnap_options', default=[], action='append',
                        help='option to pass to tarsnap')
    parser.add_argument('--config', '-c', help='use the given config file')

    group = parser.add_argument_group(
        description='Instead of using a configuration file, you may define '\
                    'a single job on the command line:')
    group.add_argument('--target', help='target filename for the backup')
    group.add_argument('--sources', nargs='+', help='paths to backup',
                        default=[])
    group.add_argument('--deltas', '-d', metavar='DELTA',
                        type=timedelta_string,
                        help='generation deltas', nargs='+')
    group.add_argument('--dateformat', '-f', help='dateformat')

    # This will allow the user to break out of an nargs='*' to start
    # with the subcommand. See http://bugs.python.org/issue9571.
    parser.add_argument('-', dest='__dummy', action="store_true",
                        help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(
        title="commands", description="commands may offer additional options")
    for cmd_name, cmd_klass in COMMANDS.iteritems():
        subparser = subparsers.add_parser(cmd_name, help=cmd_klass.help,
                                          description=cmd_klass.description,
                                          add_help=False)
        subparser.set_defaults(command=cmd_klass)
        group = subparser.add_argument_group(
            title="optional arguments for this command")
        # We manually add the --help option so that we can have a
        # custom group title, but only show a single group.
        group.add_argument('-h', '--help', action='help',
                           default=argparse.SUPPRESS,
                           help='show this help message and exit')
        cmd_klass.setup_arg_parser(group)

        # Unfortunately, we need to redefine the jobs argument for each
        # command, rather than simply having it once, globally.
        subparser.add_argument(
            'jobs', metavar='job', nargs='*',
            help='only process the given job as defined in the config file')

    # This would be in a group automatically, but it would be shown as
    # the very first thing, while it really should be the last (which
    # explicitely defining the group causes to happen).
    #
    # Also, note that we define this argument for each command as well,
    # and the command specific one will actually be parsed. This is
    # because while argparse allows us to *define* this argument globally,
    # and renders the usage syntax correctly as well, it isn't actually
    # able to parse the thing it correctly (see
    # http://bugs.python.org/issue9540).
    group = parser.add_argument_group(title='positional arguments')
    group.add_argument(
        '__not_used', metavar='job', nargs='*',
        help='only process the given job as defined in the config file')

    args = parser.parse_args(argv)

    # Do some argument validation that would be to much to ask for
    # argparse to handle internally.
    if args.config and (args.target or args.dateformat or args.deltas or
                        args.sources):
        raise ArgumentError('If --config is used, then --target, --deltas, '
                            '--sources and --dateformat are not available')
    if args.jobs and not args.config:
        raise ArgumentError(('Specific jobs (%s) can only be given if a '
                            'config file is used') % ", ".join(args.jobs))
    # The command may want to do some validation regarding it's own options.
    args.command.validate_args(args)

    return args


def main(argv):
    try:
        args = parse_args(argv)
    except ArgumentError, e:
        print "Error: %s" % e
        return 1

    # Setup logging
    level = logging.WARNING if args.quiet else (
        logging.DEBUG if args.verbose else logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger()
    log.setLevel(level)
    log.addHandler(ch)

    # Build a list of jobs, process them.
    if args.config:
        try:
            jobs = config.load_config_from_file(args.config)
        except config.ConfigError, e:
            log.fatal('Error loading config file: %s' % e)
            return 1
    else:
        # Only a single job, as given on the command line
        jobs = {None: Job(**{'target': args.target, 'dateformat': args.dateformat,
                       '      deltas': args.deltas, 'sources': args.sources})}

    # Validate the requested list of jobs to run
    if args.jobs:
        unknown = set(args.jobs) - set(jobs.keys())
        if unknown:
            log.fatal('Error: not defined in the config file: %s' % ", ".join(unknown))
            return 1
        jobs_to_run = dict([(n, j) for n, j in jobs.iteritems() if n in args.jobs])
    else:
        jobs_to_run = jobs

    command = args.command(args, log)
    try:
        for job in jobs_to_run.values():
            command.run(job)
    except TarsnapError, e:
        log.fatal("tarsnap execution failed:\n%s" % e)
        return 1


def run():
    sys.exit(main(sys.argv[1:]) or 0)


if __name__ == '__main__':
    run()