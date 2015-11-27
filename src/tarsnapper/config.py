"""Deal with jobs defined in a config file. The format is YAML that looks
like this:

    # Global values, valid for all jobs unless overridden:
    deltas: 1d 7d 30d
    delta-names:
      important: 1h 1d 30d 90d 360d
    target: /localmachine/$name-$date

    jobs:
      images:
        source: /home/michael/Images

      some-other-job:
        sources:
          - /var/dir/1
          - /etc/google
        target: /custom-target-$date
        deltas: 1h 6h 1d 7d 24d 180d

      important-job:
        source: /important/
        delta: important

      yet-another-job:
        source: /foo/
        deltas: 1h 1d;12h 7d;1d 120d

"""

from datetime import timedelta
from string import Template
import yaml


__all__ = ('Job', 'load_config', 'load_config_from_file', 'ConfigError',)


class ConfigError(Exception):
    pass


class Job(object):
    """Represent a single backup job.
    """

    def __init__(self, **initial):
        self.name = initial.get('name')
        self.aliases = initial.get('aliases')
        self.target = initial.get('target')
        self.dateformat = initial.get('dateformat')
        self.deltas = initial.get('deltas')
        self.sources = initial.get('sources')
        self.excludes = initial.get('excludes', [])
        self.force = initial.get('force')
        self.exec_before = initial.get('exec_before')
        self.exec_after = initial.get('exec_after')


def require_placeholders(text, placeholders, what):
    """Ensure that ``text`` contains the given placeholders.

    Raises a ``ConfigError`` using ``what`` in the message, or returns
    the unmodified text.
    """
    if not text is None:
        for var in placeholders:
            if Template(text).safe_substitute({var: 'foo'}) == text:
                raise ConfigError(('%s must make use of the following '
                                   'placeholders: %s') % (
                                       what, ", ".join(placeholders)))
    return text


def str_to_timedelta(text):
    """Parse a string to a timedelta value.
    """
    if text.endswith('s'):
        return timedelta(seconds=int(text[:-1]))
    elif text.endswith('h'):
        return timedelta(seconds=int(text[:-1]) * 3600)
    elif text.endswith('d'):
        return timedelta(days=int(text[:-1]))
    raise ValueError(text)


def parse_deltas(delta_string):
    """Parse the given string into a list of (``timedelta``, ``timedelta``) instances.
    representing time and step size
    """
    if delta_string is None:
        return None

    deltas = []
    for delta_item in delta_string.split(' '):
        delta_item = delta_item.strip()
        if not delta_item:
            continue

        if delta_item.find(';') == -1:
            time = delta_item
            step = delta_item
        else:
            items = delta_item.split(';')
            if len(items) != 2:
                raise ConfigError('Not a valid delta: %s' % e)
            time = items[0]
            step = items[1]

        try:
            delta_time =  str_to_timedelta(time)
            delta_step =  str_to_timedelta(step)
            deltas.append((delta_time, delta_step))
        except ValueError, e:
            raise ConfigError('Not a valid delta: %s' % e)

    if deltas and len(deltas) < 2:
        raise ConfigError('At least two deltas are required')
    return deltas

def parse_named_deltas(named_delta_dict):
    named_deltas = {}
    for name, deltas in named_delta_dict.iteritems():
        if deltas is None:
            raise ConfigError(('%s: No deltas specified') % name)
        named_deltas[name] = parse_deltas(deltas)
    return named_deltas

def load_config(text):
    """Load the config file and return a dict of jobs, with the local
    and global configurations merged.
    """
    config = yaml.load(text)

    default_dateformat = config.pop('dateformat', None)
    default_deltas = parse_deltas(config.pop('deltas', None))
    default_target = require_placeholders(config.pop('target', None),
                                          ['name', 'date'], 'The global target')

    named_deltas = parse_named_deltas(config.pop('delta-names', {}))

    read_jobs = {}
    jobs_section = config.pop('jobs', None)
    if not jobs_section:
        raise ConfigError('config must define at least one job')
    for job_name, job_dict in jobs_section.iteritems():
        job_dict = job_dict or {}
        # sources
        if 'sources' in job_dict and 'source' in job_dict:
            raise ConfigError(('%s: Use either the "source" or "sources" '+
                              'option, not both') % job_name)
        if 'source' in job_dict:
            sources = [job_dict.pop('source')]
        else:
            sources = job_dict.pop('sources', None)
        # aliases
        if 'aliases' in job_dict and 'alias' in job_dict:
            raise ConfigError(('%s: Use either the "alias" or "aliases" '+
                              'option, not both') % job_name)
        if 'alias' in job_dict:
            aliases = [job_dict.pop('alias')]
        else:
            aliases = job_dict.pop('aliases', None)
        # excludes
        if 'excludes' in job_dict and 'exclude' in job_dict:
            raise ConfigError(('%s: Use either the "excludes" or "exclude" '+
                              'option, not both') % job_name)
        if 'exclude' in job_dict:
            excludes = [job_dict.pop('exclude')]
        else:
            excludes = job_dict.pop('excludes', [])
        # deltas
        if 'deltas' in job_dict and 'delta' in job_dict:
            raise ConfigError(('%s: Use either the "deltas" or "delta" '+
                              'option, not both') % job_name)
        if 'delta' in job_dict:
            delta_name = job_dict.pop('delta', None)
            if delta_name not in named_deltas:
                raise ConfigError(('%s: Named delta "%s" not defined')
                                  % (job_name,delta_name))
            deltas = named_deltas[delta_name]
        else:
            deltas = parse_deltas(job_dict.pop('deltas', None)) or default_deltas
        new_job = Job(**{
            'name': job_name,
            'sources': sources,
            'aliases': aliases,
            'excludes': excludes,
            'target': job_dict.pop('target', default_target),
            'force': job_dict.pop('force', False),
            'deltas': deltas,
            'dateformat': job_dict.pop('dateformat', default_dateformat),
            'exec_before': job_dict.pop('exec_before', None),
            'exec_after': job_dict.pop('exec_after', None),
        })
        if not new_job.target:
            raise ConfigError('%s does not have a target name' % job_name)
        # Note: It's ok to define jobs without sources or deltas. Those
        # can only be used for selected commands, then.
        require_placeholders(new_job.target, ['date'], '%s: target')
        if job_dict:
            raise ConfigError('%s has unsupported configuration values: %s' % (
                job_name, ", ".join(job_dict.keys())))

        read_jobs[job_name] = new_job

    # Return jobs, and all global keys not popped
    return read_jobs, config


def load_config_from_file(filename):
    f = open(filename, 'rb')
    try:
        return load_config(f.read())
    finally:
        f.close()
