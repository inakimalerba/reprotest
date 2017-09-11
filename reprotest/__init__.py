# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import argparse
import collections
import configparser
import getpass
import grp
import logging
import os
import pathlib
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import types

import pkg_resources

from reprotest.lib import adtlog
from reprotest.lib import adt_testbed
from reprotest import _contextlib
from reprotest import _shell_ast
from reprotest import presets


VIRT_PREFIX = "autopkgtest-virt-"

def get_server_path(server_name):
    return pkg_resources.resource_filename(
        __name__, os.path.join("virt", (VIRT_PREFIX + server_name) if server_name else ""))

def is_executable(parent, fn):
    path = os.path.join(parent, fn)
    return os.path.isfile(path) and os.access(path, os.X_OK)

all_servers = None
def get_all_servers():
    global all_servers
    if all_servers is None:
        server_dir = get_server_path(None)
        all_servers = sorted(fn[len(VIRT_PREFIX):] for fn in os.listdir(server_dir)
                             if is_executable(server_dir, fn) and fn.startswith(VIRT_PREFIX))
    return all_servers


# chroot is the only form of OS virtualization that's available on
# most POSIX OSes.  Linux containers (lxc) and namespaces are specific
# to Linux.  Some versions of BSD have jails (MacOS X?).  There are a
# variety of other options including Docker etc that use different
# approaches.

@_contextlib.contextmanager
def start_testbed(args, temp_dir, no_clean_on_error=False, host_distro='debian'):
    '''This is a simple wrapper around adt_testbed that automates the
    initialization and cleanup.'''
    # Find the location of reprotest using setuptools and then get the
    # path for the correct virt-server script.
    server_path = get_server_path(args[0])
    logging.info('STARTING VIRTUAL SERVER %r', [server_path] + args[1:])
    testbed = adt_testbed.Testbed([server_path] + args[1:], temp_dir, None,
            host_distro=host_distro)
    testbed.start()
    testbed.open()
    should_clean = True
    try:
        yield testbed
    except:
        if no_clean_on_error:
            should_clean = False
        raise
    finally:
        if should_clean:
            # TODO: we could probably do *some* level of cleanup even if
            # should_clean is False; investigate this further...
            testbed.stop()


class Build(collections.namedtuple('_Build', 'build_command setup cleanup env tree')):
    '''Holds the shell ASTs and various other data, used to execute each build.

    Fields:
        build_command (_shell_ast.Command): The build command itself, including
            all commands that accept other commands as arguments.  Examples:
            setarch.
        setup (_shell_ast.AndList): These are shell commands that change the
            shell environment and need to be run as part of the same script as
            the main build command but don't take other commands as arguments.
            These execute conditionally because if one command fails,
            the whole script should fail.  Examples: cd, umask.
        cleanup (_shell_ast.List): All commands that have to be run to return
            the testbed to its initial state, before the testbed does its own
            cleanup.  These are executed only if the build command fails,
            because otherwise the cleanup has to occur after the build artifact
            is copied out.  These execution unconditionally, one after another,
            because all cleanup commands should be attempted irrespective of
            whether others succeed.  Examples: fileordering.
        env (types.MappingProxyType): Immutable mapping of the environment.
        tree (str): Path to the source root where the build should take place.
    '''

    @classmethod
    def from_command(cls, build_command, env, tree):
        return cls(
            build_command = _shell_ast.SimpleCommand(
                "sh", "-ec", _shell_ast.Quote(build_command)),
            setup = _shell_ast.AndList(),
            cleanup = _shell_ast.List(),
            env = env,
            tree = tree,
        )

    def add_env(self, key, value):
        '''Helper function for adding a key-value pair to an immutable mapping.'''
        new_mapping = self.env.copy()
        new_mapping[key] = value
        return self._replace(env=types.MappingProxyType(new_mapping))

    def append_to_build_command(self, command):
        '''Passes the current build command as the last argument to a given
        _shell_ast.SimpleCommand.

        '''
        new_suffix = (command.cmd_suffix +
                      _shell_ast.CmdSuffix([self.build_command]))
        new_command = _shell_ast.SimpleCommand(command.cmd_prefix,
                                               command.cmd_name,
                                               new_suffix)
        return self._replace(build_command=new_command)

    def append_setup(self, command):
        '''Adds a command to the setup phase.

        '''
        new_setup = self.setup + _shell_ast.AndList([command])
        return self._replace(setup=new_setup)

    def append_setup_exec(self, *args):
        return self.append_setup_exec_raw(*map(_shell_ast.Quote, args))

    def append_setup_exec_raw(self, *args):
        return self.append_setup(_shell_ast.SimpleCommand.make(*args))

    def prepend_cleanup(self, command):
        '''Adds a command to the cleanup phase.

        '''
        # if this command fails, save the exit code but keep executing
        # we run with -e, so it would fail otherwise
        new_cleanup = (_shell_ast.List([_shell_ast.Term(
                            "{0} || __c=$?".format(command), ';')])
                       + self.cleanup)
        return self._replace(cleanup=new_cleanup)

    def prepend_cleanup_exec(self, *args):
        return self.prepend_cleanup_exec_raw(*map(_shell_ast.Quote, args))

    def prepend_cleanup_exec_raw(self, *args):
        return self.prepend_cleanup(_shell_ast.SimpleCommand.make(*args))

    def move_tree(self, source, target, set_tree):
        new_build = self.append_setup_exec(
            'mv', source, target).prepend_cleanup_exec(
            'mv', target, source)
        if set_tree:
            return new_build._replace(tree = os.path.join(target, ''))
        else:
            return new_build

    def to_script(self):
        '''Generates the shell code for the script.

        The build command is only executed if all the setup commands
        finish without errors.  The setup and build commands are
        executed in a subshell so that changes they make to the shell
        don't affect the cleanup commands.  (This avoids the problem
        with the disorderfs mount being kept open as a current working
        directory when the cleanup tries to unmount it.)

        '''
        subshell = _shell_ast.Subshell(self.setup +
                                       _shell_ast.AndList([self.build_command]))

        if self.cleanup:
            cleanup = """( __c=0; {0} exit $__c; )""".format(str(self.cleanup))
            return """\
if {0}; then
    {1};
else
    __x=$?;
    if {1}; then exit $__x; else
        echo >&2; "cleanup failed with exit code $?"; exit $__x;
    fi;
fi
""".format(str(subshell), str(cleanup))
        else:
            return str(subshell)


class VariationContext(collections.namedtuple('_VariationContext', 'verbosity user_groups default_faketime')):

    @classmethod
    def default(cls):
        return cls(0, frozenset(), 0)

    def guess_default_faketime(self, source_root):
        # Get the latest modification date of all the files in the source root.
        # This tries hard to avoid bad interactions with faketime and make(1) etc.
        # However if you're building this too soon after changing one of the source
        # files then the effect of this variation is not very great.
        filemtimes = (os.path.getmtime(os.path.join(root, f)) for root, dirs, files in os.walk(source_root) for f in files)
        return self._replace(default_faketime=int(max(filemtimes, default=0)))


def dirname(p):
    # works more intuitively for paths with a trailing /
    return os.path.normpath(os.path.dirname(os.path.normpath(p)))

def basename(p):
    # works more intuitively for paths with a trailing /
    return os.path.normpath(os.path.basename(os.path.normpath(p)))

# put build artifacts in ${dist}/source-root, to support tools that put artifacts in ..
VSRC_DIR = "source-root"


# time zone, locales, disorderfs, host name, user/group, shell, CPU
# number, architecture for uname (using linux64), umask, HOME, see
# also: https://tests.reproducible-builds.org/index_variations.html
# TODO: the below ideally should *read the current value*, and pick
# something that's different for the experiment.

# TODO: relies on a pbuilder-specific command to parallelize
# def cpu(script, env, tree):
#     return script, env, tree

def environment(ctx, build, vary):
    if not vary:
        return build
    return build.add_env('CAPTURE_ENVIRONMENT', 'i_capture_the_environment')

# TODO: this requires superuser privileges.
# def domain_host(ctx, script, env, tree):
#     return script, env, tree

# Note: this has to go before fileordering because we can't move mountpoints
# TODO: this variation makes it impossible to parallelise the build, for most
# of the current virtual servers. (It's theoretically possible to make it work)
def build_path_same(ctx, build, vary):
    if vary:
        return build
    const_path = os.path.join(dirname(build.tree), 'const_build_path')
    return build.move_tree(build.tree, const_path, True)
build_path_same.negative = True

def fileordering(ctx, build, vary):
    if not vary:
        return build

    old_tree = os.path.join(dirname(build.tree), basename(build.tree) + '-before-disorderfs', '')
    _ = build.move_tree(build.tree, old_tree, False)
    _ = _.append_setup_exec('mkdir', '-p', build.tree)
    _ = _.prepend_cleanup_exec('rmdir', build.tree)
    disorderfs = ['disorderfs'] + ([] if ctx.verbosity else ["-q"])
    _ = _.append_setup_exec(*(disorderfs + ['--shuffle-dirents=yes', old_tree, build.tree]))
    _ = _.prepend_cleanup_exec('fusermount', '-u', build.tree)
    # the "user_group" variation hacks PATH to run "sudo -u XXX" instead of various tools, pick it up here
    binpath = os.path.join(dirname(build.tree), 'bin')
    _ = _.prepend_cleanup_exec_raw('export', 'PATH="%s:$PATH"' % binpath)
    return _

# Note: this has to go after anything that might modify 'tree' e.g. build_path
def home(ctx, build, vary):
    if not vary:
        # choose an existent HOME, see Debian bug #860428
        return build.add_env('HOME', build.tree)
    else:
        return build.add_env('HOME', '/nonexistent/second-build')

# TODO: uname is a POSIX standard.  The related Linux command
# (setarch) only affects uname at the moment according to the docs.
# FreeBSD changes uname with environment variables.  Wikipedia has a
# reference to a setname command on another Unix variant:
# https://en.wikipedia.org/wiki/Uname
def kernel(ctx, build, vary):
    # set these two explicitly different. otherwise, when reprotest is
    # reprotesting itself, then one of the builds will fail its tests, because
    # its two child reprotests will see the same value for "uname" but the
    # tests expect different values.
    if not vary:
        return build.append_to_build_command(_shell_ast.SimpleCommand.make('linux64', '--uname-2.6'))
    else:
        return build.append_to_build_command(_shell_ast.SimpleCommand.make('linux32'))

# TODO: if this locale doesn't exist on the system, Python's
# locales.getlocale() will return (None, None) rather than this
# locale.  I imagine it will also probably cause false positives with
# builds being reproducible when they aren't because of locale-based
# issues if this locale isn't installed.  The right solution here is
# for this locale to be encoded into the dependencies so installing it
# installs the right locale.  A weaker but still reasonable solution
# is to figure out what locales are installed (how?) and use another
# locale if this one isn't installed.

# TODO: what exact locales and how to many test is probably a mailing
# list question.
def locales(ctx, build, vary):
    if not vary:
        return build.add_env('LANG', 'C.UTF-8').add_env('LANGUAGE', 'en_US:en')
    else:
        # if there is an issue with this being random, we could instead select it
        # based on a deterministic hash of the inputs
        loc = random.choice(['fr_CH.UTF-8', 'es_ES', 'ru_RU.CP1251', 'kk_KZ.RK1048', 'zh_CN'])
        return build.add_env('LANG', loc).add_env('LC_ALL', loc).add_env('LANGUAGE', '%s:fr' % loc)

# TODO: Linux-specific.  unshare --uts requires superuser privileges.
# How is this related to host/domainname?
# def namespace(ctx, script, env, tree):
#     # command1 = ['unshare', '--uts'] + command1
#     # command2 = ['unshare', '--uts'] + command2
#     return script, env, tree

def exec_path(ctx, build, vary):
    if not vary:
        return build
    return build.add_env('PATH', build.env['PATH'] + ':/i_capture_the_path')

# This doesn't require superuser privileges, but the chsh command
# affects all user shells, which would be bad.
# # def shell(ctx, script, env, tree):
#     return script, env, tree

def timezone(ctx, build, vary):
    # These time zones are theoretically in the POSIX time zone format
    # (http://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap08.html#tag_08),
    # so they should be cross-platform compatible.
    if not vary:
        return build.add_env('TZ', 'GMT+12')
    else:
        return build.add_env('TZ', 'GMT-14')

def faketime(ctx, build, vary):
    if not vary:
        return build
    lastmt = ctx.default_faketime
    now = time.time()
    if lastmt < now - 32253180:
        # if lastmt is far in the past, use that, it's a bit safer
        faket = '@%s' % lastmt
    else:
        # otherwise use a date far in the future
        faket = '+373days+7hours+13minutes'
    settime = _shell_ast.SimpleCommand.make('faketime', faket)
    # faketime's manpages are stupidly misleading; it also modifies file timestamps.
    # this is only mentioned in the README. we do not want this, it really really
    # messes with GNU make and other buildsystems that look at timestamps.
    return build.add_env('NO_FAKE_STAT', '1').append_to_build_command(settime)

def umask(ctx, build, vary):
    if not vary:
        return build.append_setup_exec('umask', '0022')
    else:
        return build.append_setup_exec('umask', '0002')

# Note: this needs to go before anything that might need to run setup commands
# as the other user (e.g. due to permissions).
def user_group(ctx, build, vary):
    if not vary:
        return build

    if not ctx.user_groups:
        logging.warn("IGNORING user_group variation, because no --user-groups were given. To suppress this warning, give --dont-vary user_group")
        return build

    olduser = getpass.getuser()
    oldgroup = grp.getgrgid(os.getgid()).gr_name
    user, group = random.choice(list(set(ctx.user_groups) - set([(olduser, oldgroup)])))
    sudobuild = _shell_ast.SimpleCommand.make('sudo', '-E', '-u', user, '-g', group)
    binpath = os.path.join(dirname(build.tree), 'bin')

    _ = build.append_to_build_command(sudobuild)
    # disorderfs needs to run as a different user.
    # we prefer that to running it as root, principle of least-privilege.
    _ = _.append_setup_exec('sh', '-ec', r'''
mkdir "{0}"
printf '#!/bin/sh\nsudo -u "{1}" -g "{2}" /usr/bin/disorderfs "$@"\n' > "{0}"/disorderfs
chmod +x "{0}"/disorderfs
printf '#!/bin/sh\nsudo -u "{1}" -g "{2}" /bin/mkdir "$@"\n' > "{0}"/mkdir
chmod +x "{0}"/mkdir
printf '#!/bin/sh\nsudo -u "{1}" -g "{2}" /bin/fusermount "$@"\n' > "{0}"/fusermount
chmod +x "{0}"/fusermount
'''.format(binpath, user, group))
    _ = _.append_setup_exec_raw('export', 'PATH="%s:$PATH"' % binpath)
    _ = _.append_setup_exec('sudo', 'chown', '-h', '-R', '--from=%s' % olduser, user, build.tree)
    # TODO: artifacts probably shouldn't be chown'd back
    _ = _.prepend_cleanup_exec('sudo', 'chown', '-h', '-R', '--from=%s' % user, olduser, build.tree)
    return _


# The order of the variations *is* important, because the command to
# be executed in the container needs to be built from the inside out.
VARIATIONS = collections.OrderedDict([
    ('environment', environment),
    ('build_path', build_path_same),
    ('user_group', user_group),
    # ('cpu', cpu),
    # ('domain_host', domain_host),
    ('fileordering', fileordering),
    ('home', home),
    ('kernel', kernel),
    ('locales', locales),
    # ('namespace', namespace),
    ('exec_path', exec_path),
    # ('shell', shell),
    ('time', faketime),
    ('timezone', timezone),
    ('umask', umask),
])


class BuildContext(collections.namedtuple('_BuildContext', 'testbed_root local_dist_root local_src build_name')):
    """

    The idiom os.path.join(x, '') is used here to ensure a trailing directory
    separator, which is needed by some things, notably VirtSubProc.
    """

    @property
    def testbed_src(self):
        return os.path.join(self.testbed_root, 'build-' + self.build_name, '')

    @property
    def testbed_dist(self):
        return os.path.join(self.testbed_root, 'artifacts-' + self.build_name, '')

    @property
    def local_dist(self):
        return os.path.join(self.local_dist_root, self.build_name)

    def make_build_commands(self, script, env):
        return Build.from_command(
            build_command = script,
            env = types.MappingProxyType(env),
            tree = self.testbed_src
        )

    def plan_variations(self, build, is_control, variation_context, variations):
        actions = [(not is_control and v in variations, v) for v in VARIATIONS]
        logging.info('build "%s": %s',
            self.build_name,
            ", ".join("%s %s" % ("FIX" if not vary else "vary", v) for vary, v in actions))
        for vary, v in actions:
            build = VARIATIONS[v](variation_context, build, vary)
        return build

    def copydown(self, testbed):
        logging.info("copying %s over to virtual server's %s", self.local_src, self.testbed_src)
        testbed.command('copydown', (os.path.join(self.local_src, ''), self.testbed_src))

    def copyup(self, testbed):
        logging.info("copying %s back from virtual server's %s", self.testbed_dist, self.local_dist)
        testbed.command('copyup', (self.testbed_dist, os.path.join(self.local_dist, '')))

    def run_build(self, testbed, build, artifact_pattern):
        logging.info("starting build with source directory: %s, artifact pattern: %s",
            self.testbed_src, artifact_pattern)
        # remove any existing artifact, in case the build script doesn't overwrite
        # it e.g. like how make(1) sometimes works.
        if re.search(r"""(^| )['"]*/""", artifact_pattern):
            raise ValueError("artifact_pattern is possibly dangerous; maybe use a relative path instead?")
        testbed.check_exec(
            ['sh', '-ec', 'cd "%s" && rm -rf %s' %
            (self.testbed_src, artifact_pattern)])
        # this dance is necessary because the cwd can't be cd'd into during the setup phase under some variations like user_group
        new_script = build.append_setup_exec_raw('export', 'REPROTEST_BUILD_PATH=%s' % build.tree).to_script()
        logging.info("executing: %s", new_script)
        argv = ['sh', '-ec', new_script]
        xenv = ['%s=%s' % (k, v) for k, v in build.env.items()]
        (code, _, _) = testbed.execute(argv, xenv=xenv, kind='build')
        if code != 0:
            testbed.bomb('"%s" failed with status %i' % (' '.join(argv), code), adtlog.AutopkgtestError)
        dist_base = os.path.join(self.testbed_dist, VSRC_DIR)
        testbed.check_exec(
            ['sh', '-ec', """mkdir -p "{0}"
cd "{1}" && cp --parents -a -t "{0}" {2}
cd "{0}" && touch -d@0 . .. {2}
""".format(dist_base, self.testbed_src, artifact_pattern)])


def run_or_tee(progargs, filename, store_dir, *args, **kwargs):
    if store_dir:
        tee = subprocess.Popen(['tee', filename], stdin=subprocess.PIPE, cwd=store_dir)
        r = subprocess.run(progargs, *args, stdout=tee.stdin, **kwargs)
        tee.communicate()
        return r
    else:
        return subprocess.run(progargs, *args, **kwargs)

def run_diff(dist_0, dist_1, diffoscope_args, store_dir):
    if diffoscope_args is None: # don't run diffoscope
        diffprogram = ['diff', '-ru', dist_0, dist_1]
        logging.info("Running diff: %r", diffprogram)
    else:
        diffprogram = ['diffoscope', dist_0, dist_1] + diffoscope_args
        logging.info("Running diffoscope: %r", diffprogram)

    retcode = run_or_tee(diffprogram, 'diffoscope.out', store_dir).returncode
    if retcode == 0:
        logging.info("No differences between %s, %s", dist_0, dist_1)
        if store_dir:
            shutil.rmtree(dist_1)
            os.symlink(basename(dist_0), dist_1)
    return retcode

def check(build_command, artifact_pattern, virtual_server_args, source_root,
          no_clean_on_error=False, store_dir=None, diffoscope_args=[],
          variations=VARIATIONS, variation_context=VariationContext.default(),
          testbed_pre=None, testbed_init=None, host_distro='debian'):
    # default argument [] is safe here because we never mutate it.
    if not source_root:
        raise ValueError("invalid source root: %s" % source_root)
    if os.path.isfile(source_root):
        source_root = os.path.normpath(os.path.dirname(source_root))

    if store_dir:
        store_dir = str(store_dir)
        if not os.path.exists(store_dir):
            os.makedirs(store_dir, exist_ok=False)
        elif os.listdir(store_dir):
            raise ValueError("store_dir must be empty: %s" % store_dir)

    logging.debug("virtual_server_args: %r", virtual_server_args)

    source_root = str(source_root)
    with tempfile.TemporaryDirectory() as temp_dir:
        if testbed_pre:
            new_source_root = os.path.join(temp_dir, "testbed_pre")
            shutil.copytree(source_root, new_source_root, symlinks=True)
            subprocess.check_call(["sh", "-ec", testbed_pre], cwd=new_source_root)
            source_root = new_source_root
        logging.debug("source_root: %s", source_root)

        variation_context = variation_context.guess_default_faketime(source_root)

        # TODO: an alternative strategy is to run the testbed many times, one for each build
        # not sure if it's worth implementing at this stage, but perhaps in the future.
        with start_testbed(virtual_server_args, temp_dir, no_clean_on_error,
                host_distro=host_distro) as testbed:

            if store_dir:
                result_dir = store_dir
            else:
                result_dir = os.path.join(temp_dir, 'artifacts')
                os.makedirs(result_dir)

            build_contexts = [BuildContext(testbed.scratch, result_dir, source_root, name)
                for name in ('control', 'experiment')]
            builds = [bctx.make_build_commands(
                    'cd "$REPROTEST_BUILD_PATH"; unset REPROTEST_BUILD_PATH; ' + build_command, os.environ)
                for bctx in build_contexts]

            logging.log(5, "builds: %r", builds)
            builds = [c.plan_variations(b, c.build_name == "control", variation_context, variations)
                for c, b in zip(build_contexts, builds)]
            logging.log(5, "builds: %r", builds)

            try:
                # run the scripts
                if testbed_init:
                    testbed.check_exec(["sh", "-ec", testbed_init])

                for bctx in build_contexts:
                    bctx.copydown(testbed)

                for bctx, build in zip(build_contexts, builds):
                    bctx.run_build(testbed, build, artifact_pattern)

                for bctx in build_contexts:
                    bctx.copyup(testbed)
            except Exception:
                traceback.print_exc()
                return 2

        retcodes = [
            run_diff(build_contexts[0].local_dist, bctx.local_dist, diffoscope_args, store_dir)
            for bctx in build_contexts[1:]]

        retcode = max(retcodes)
        if retcode == 0:
            print("=======================")
            print("Reproduction successful")
            print("=======================")
            print("No differences in %s" % artifact_pattern, flush=True)
            run_or_tee(['sh', '-ec', 'find %s -type f -exec sha256sum "{}" \;' % artifact_pattern],
                'SHA256SUMS', store_dir,
                cwd=os.path.join(build_contexts[0].local_dist, VSRC_DIR))
        else:
            # a slight hack, to trigger no_clean_on_error
            raise SystemExit(retcode)
        return retcode


def config_to_args(parser, filename):
    if not filename:
        return []
    elif os.path.isdir(filename):
        filename = os.path.join(filename, ".reprotestrc")
    config = configparser.ConfigParser(dict_type=collections.OrderedDict)
    config.read(filename)
    sections = {p.title: p for p in parser._action_groups[2:]}
    args = []
    for sectname, section in config.items():
        if sectname == 'basics':
            sectname = 'basic options'
        elif not sectname.endswith(' options'):
            sectname += ' options'
        items = list(section.items())
        if not items:
            continue
        sectacts = sections[sectname]._option_string_actions
        for key, val in items:
            key = "--" + key.replace("_", "-")
            val = val.strip()
            if key in sectacts.keys():
                if 'Append' in sectacts[key].__class__.__name__:
                    for v in val.split('\n'):
                        args.append('%s=%s' % (key, v))
                else:
                    args.append('%s=%s' % (key, val))
            else:
                raise ValueError("unexpected item in config: %s = %s" % (key, val))
    return args


def cli_parser():
    parser = argparse.ArgumentParser(
        prog='reprotest',
        usage='''%(prog)s --help [<virtual_server_name>]
       %(prog)s [options] [-c <build-command>] <source_root> [<artifact_pattern>]
                 [-- <virtual_server_args> [<virtual_server_args> ...]]
       %(prog)s [options] [-s <source_root>] <build_command> [<artifact_pattern>]
                 [-- <virtual_server_args> [<virtual_server_args> ...]]''',
        description='Build packages and check them for reproducibility.',
        formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)

    parser.add_argument('source_root|build_command', default=None, nargs='?',
        help='The first argument is treated either as a source_root (see the '
        '-s option) or as a build-command (see the -c option) depending on '
        'some heuristics. Specifically: if neither -c nor -s are given, then: '
        'if this exists as a file or directory and is not "auto", then this is '
        'treated as a source_root, else as a build_command. Otherwise, if one '
        'of -c or -s is given, then this is treated as the other one. If both '
        'are given, then this is a command-line syntax error and we exit code 2.'),
    parser.add_argument('artifact_pattern', default=None, nargs='?',
        help='Build artifact to test for reproducibility. May be a shell '
             'pattern such as "*.deb *.changes".'),
    parser.add_argument('virtual_server_args', default=None, nargs='*',
        help='Arguments to pass to the virtual_server, the first argument '
             'being the name of the server. If this itself contains options '
             '(of the form -xxx or --xxx), or if any of the previous arguments '
             'are omitted, you should put a "--" between these arguments and '
             'reprotest\'s own options. Default: "null", to run directly in '
             '/tmp. Choices: %s' %
             ', '.join(get_all_servers()))

    parser.add_argument('--help', default=None, const=True, nargs='?',
        choices=get_all_servers(), metavar='VIRTUAL_SERVER_NAME',
        help='Show this help message and exit. When given an argument, '
        'show instead the help message for that virtual server and exit. ')
    parser.add_argument('-f', '--config-file', default=None,
        help='File to load configuration from. (Default: %(default)s)')

    group1 = parser.add_argument_group('basic options')
    group1.add_argument('--verbosity', type=int, default=0,
        help='An integer.  Control which messages are displayed.')
    group1.add_argument('-v', '--verbose', dest='verbosity', action='count',
        help='Like --verbosity, but given multiple times without arguments.')
    group1.add_argument('--host-distro', default='debian',
        help='The distribution that will run the tests (Default: %(default)s)')
    group1.add_argument('-s', '--source-root', default=None,
        help='Root of the source tree, that is copied to the virtual server '
        'and made available during the build. If a file is given here, then '
        'all files in its parent directory are available during the build. '
        'Default: "." (current working directory).')
    group1.add_argument('-c', '--build-command', default=None,
        help='Build command to execute. If this is "auto" then reprotest will '
        'guess how to build the given source_root, in which case various other '
        'options may be automatically set-if-unset. Default: auto'),
    group1.add_argument('--store-dir', default=None, type=pathlib.Path,
        help='Save the artifacts in this directory, which must be empty or '
        'non-existent. Otherwise, the artifacts will be deleted and you only '
        'see their hashes (if reproducible) or the diff output (if not).')
    group1.add_argument('--variations', default=frozenset(VARIATIONS.keys()),
        type=lambda s: frozenset(sss for ss in s.split(',') for sss in ss.split()),
        help='Build variations to test as a whitespace-or-comma-separated '
        'list.  Default is to test all available variations: %s.' %
        ', '.join(VARIATIONS.keys()))
    group1.add_argument('--dont-vary', default=frozenset(),
        type=lambda s: frozenset(sss for ss in s.split(',') for sss in ss.split()),
        help='Build variations *not* to test as a whitespace-or-comma-separated '
        'list.  These take precedence over what you set for --variations. '
        'Default is nothing, i.e. test whatever you set for --variations.')
    group1.add_argument('--user-groups', default=frozenset(),
        type=lambda s: frozenset(tuple(x.split(':',1)) for x in s.split(',')),
        help='Comma-separated list of possible user:group combinations which '
        'will be randomly chosen to `sudo -i` to when varying user_group.')

    group2 = parser.add_argument_group('diff options')
    group2.add_argument('--diffoscope-arg', default=[], action='append',
        help='Give extra arguments to diffoscope when running it.')
    group2.add_argument('--no-diffoscope', action='store_true', default=False,
        help='Don\'t run diffoscope; instead run diff(1). Useful if you '
        'don\'t want to install diffoscope and/or just want a quick answer '
        'on whether the reproduction was successful or not, without spending '
        'time to compute all the detailed differences.')

    group3 = parser.add_argument_group('advanced options')
    group3.add_argument('--testbed-pre', default=None, metavar='COMMANDS',
        help='Shell commands to run before starting the test bed, in the '
        'context of the current system environment. This may be used to e.g. '
        'compute information needed by the build, where the computation needs '
        'packages you don\'t want installed in the testbed itself.')
    group3.add_argument('--testbed-init', default=None, metavar='COMMANDS',
        help='Shell commands to run after starting the test bed, but before '
        'applying variations. Used to e.g. install disorderfs in a chroot.')
    group3.add_argument('--auto-preset-expr', default="_", metavar='PYTHON_EXPRESSION',
        help='This may be used to transform the presets returned by the '
        'auto-detection feature. The value should be a python expression '
        'that transforms the _ variable, which is of type reprotest.presets.ReprotestPreset. '
        'See that class\'s documentation for ways you can write this '
        'expression. Default: %(default)s')
    group3.add_argument('--no-clean-on-error', action='store_true', default=False,
        help='Don\'t clean the virtual_server if there was an error. '
        'Useful for debugging, but WARNING: this is currently not '
        'implemented very well and may leave cruft on your system.')
    group3.add_argument('--dry-run', action='store_true', default=False,
        help='Don\'t run the builds, just print what would happen.')

    return parser


def command_line(parser, argv):
    # parse_known_args does not exactly do what we want - we want everything
    # after '--' to belong to virtual_server_args, but parse_known_args instead
    # treats them as any positional argument (e.g. ones that go before
    # virtual_server_args). so, work around that here.
    if '--' in argv:
        idx = argv.index('--')
        postargv = argv[idx:]
        argv = argv[:idx]
    else:
        postargv = []

    # work around python issue 14191; this allows us to accept command lines like
    # $ reprotest build stuff --option=val --option=val -- schroot unstable-amd64-sbuild
    # where optional args appear in between positional args, but there must be a '--'
    args, remainder = parser.parse_known_args(argv)
    remainder += postargv

    if remainder:
        if remainder[0] != '--':
            # however we disallow split command lines that don't have '--', e.g.:
            # $ reprotest build stuff --option=val --option=val schroot unstable-amd64-sbuild
            # since it's too complex to support that in a way that's not counter-intuitive
            parser.parse_args(argv)
            assert False # previous function should have raised an error
        args.virtual_server_args = (args.virtual_server_args or []) + remainder[1:]
    args.virtual_server_args = args.virtual_server_args or ["null"]

    if args.help:
        if args.help:
            parser.print_help()
            sys.exit(0)
        else:
            sys.exit(subprocess.call([get_server_path(args.help), "-h"]))

    return args


def run(argv, check):
    # Argparse exits with status code 2 if something goes wrong, which
    # is already the right status exit code for reprotest.
    parser = cli_parser()
    parsed_args = command_line(parser, argv)
    config_args = config_to_args(parser, parsed_args.config_file)
    # Command-line arguments override config file settings.
    parsed_args = command_line(parser, config_args + argv)

    verbosity = parsed_args.verbosity
    adtlog.verbosity = verbosity
    logging.basicConfig(
        format='%(message)s', level=30-10*verbosity, stream=sys.stdout)
    logging.debug('%r', parsed_args)

    # Decide which form of the CLI we're using
    build_command, source_root = None, None
    first_arg = parsed_args.__dict__['source_root|build_command']
    if parsed_args.build_command:
        if parsed_args.source_root:
            print("Both -c and -s were given; abort")
            sys.exit(2)
        else:
            source_root = first_arg
    else:
        if parsed_args.source_root:
            build_command = first_arg
        elif not first_arg:
            print("No <source_root> or <build_command> provided. See --help for options.")
            sys.exit(2)
        elif first_arg == "auto":
            build_command = first_arg
            if parsed_args.artifact_pattern:
                logging.warn("old CLI form `reprotest auto <source_root>` detected, "
                    "setting source_root to the second argument: %s", parsed_args.artifact_pattern)
                logging.warn("to avoid this warning, use instead `reprotest <source_root>` "
                    "or (if really necessary) `reprotest -s <source_root> auto <artifact>`")
                source_root = parsed_args.artifact_pattern
                parsed_args.artifact_pattern = None
        elif os.path.exists(first_arg):
            source_root = first_arg
        else:
            build_command = first_arg
    build_command = build_command or parsed_args.build_command or "auto"
    source_root = source_root or parsed_args.source_root or '.'

    # Args that might be affected by presets
    virtual_server_args = parsed_args.virtual_server_args
    artifact_pattern = parsed_args.artifact_pattern
    testbed_pre = parsed_args.testbed_pre
    testbed_init = parsed_args.testbed_init
    diffoscope_args = parsed_args.diffoscope_arg

    # Do presets
    if build_command == 'auto':
        auto_preset_expr = parsed_args.auto_preset_expr
        values = presets.get_presets(source_root, virtual_server_args[0])
        values = eval(auto_preset_expr, {'_': values}, {})
        logging.info("preset auto-selected: %r", values)
        build_command = values.build_command
        artifact_pattern = artifact_pattern or values.artifact_pattern
        testbed_pre = testbed_pre or values.testbed_pre
        testbed_init = testbed_init or values.testbed_init
        if diffoscope_args is not None:
            diffoscope_args = values.diffoscope_args + diffoscope_args

    # Variations args
    variations = parsed_args.variations - parsed_args.dont_vary
    _ = VariationContext.default()
    _ = _._replace(verbosity=verbosity)
    _ = _._replace(user_groups=_.user_groups | parsed_args.user_groups)
    variation_context = _

    # Remaining args
    host_distro = parsed_args.host_distro
    store_dir = parsed_args.store_dir
    no_clean_on_error = parsed_args.no_clean_on_error
    if parsed_args.no_diffoscope:
        diffoscope_args = None

    if not artifact_pattern:
        print("No <artifact> to test for differences provided. See --help for options.")
        sys.exit(2)

    check_args_keys = (
        "build_command", "artifact_pattern", "virtual_server_args", "source_root",
        "no_clean_on_error", "store_dir", "diffoscope_args",
        "variations", "variation_context",
        "testbed_pre", "testbed_init", "host_distro")
    l = locals()
    check_args = collections.OrderedDict([(k, l[k]) for k in check_args_keys])
    if parsed_args.dry_run:
        return check_args
    else:
        return check(**check_args)

def main():
    r = run(sys.argv[1:], check)
    if isinstance(r, collections.OrderedDict):
        print("check(%s)" % ", ".join("%s=%r" % (k, v) for k, v in r.items()))
    else:
        return r
