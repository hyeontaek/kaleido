# kaleido: a multi-way file synchronizer using git as transport
# Copyright (C) 2011,2013 Hyeontaek Lim
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import email.utils
import io
import os
import platform
import random
import subprocess
import sys
import time
import threading

META_DEFAULT='.kaleido'
#META_DEFAULT='.git'    # for debugging; unsafe

WORKING_COPY_DEFAULT='.'

if platform.platform().startswith('Linux') or platform.platform().startswith('FreeBSD'):
    GIT_DEFAULT = 'git'
elif platform.platform().startswith('Windows'):
    if platform.architecture()[0] == '64bit':
        GIT_DEFAULT= r'C:\Program Files (x86)\Git\bin\git.exe'
    else:
        GIT_DEFAULT = r'C:\Program Files\Git\bin\git.exe'
else:
    assert False, 'not support platform'

INTERVAL_DEFAULT='1'

class Option:
    def __init__(self):
        self.git = GIT_DEFAULT
        self.meta = META_DEFAULT
        self.working_copy = WORKING_COPY_DEFAULT
        self.interval = INTERVAL_DEFAULT
        self.quiet = False

def copy_output(src, dst, tee=None):
    while True:
        s = src.readline(4096).decode(sys.getdefaultencoding())
        if not s: break
        dst.write(s)
        if tee: tee.write(s)
    src.close()

def run(git_path, args, print_stdout=True, print_stderr=True, fatal=False):
    p = subprocess.Popen([git_path] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    p.stdin.close()

    threads = []
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    tee_stdout = sys.stdout if print_stdout else None
    tee_stderr = sys.stderr if print_stderr else None
    threads.append(threading.Thread(target=copy_output, args=(p.stdout, stdout_buf, tee_stdout)))
    threads.append(threading.Thread(target=copy_output, args=(p.stderr, stderr_buf, tee_stderr)))
    list([t.start() for t in threads])
    ret = p.wait()
    list([t.join() for t in threads])

    if fatal and ret != 0:
        raise RuntimeError('%s returned %d' % (git_path, ret))
    
    return (ret == 0, stdout_buf.getvalue(), stderr_buf.getvalue())

def invoke(git_path, args):
    ret = subprocess.call([git_path] + args)
    return ret == 0

_time_units = [
        (24 * 60 * 60, 'day', 'days'),
        (     60 * 60, 'hour', 'hours'),
        (          60, 'minute', 'minutes'),
        (           1, 'second', 'seconds'),
    ]
def get_timediff_str(diff):
    t = ''
    for timeunit, name_s, name_p in _time_units:
        if diff >= timeunit:
            c = int(diff / timeunit)
            diff -= c * timeunit
            t += '%d %s ' % (c, name_s if c == 1 else name_p)
    return t.rstrip()

def detect_git_version(options):
    _, version, _ = run(options.git, ['--version'], print_stdout=False, print_stderr=False, fatal=True)

    version = version.split(' ')[2]
    version = tuple([int(x) for x in version.split('.')[:3]])
    return version

def detect_working_copy(options):
    path = os.path.abspath(os.path.join(options.working_copy))
    while path != os.path.dirname(path):
        if os.path.exists(os.path.join(path, options.meta)):
            options.working_copy = path
            return True
        path = os.path.dirname(path)
    print('error: cannot find %s' % options.meta)
    return False

def list_git_branches(git_path, git_common_options):
    for line in run(git_path, git_common_options + ['branch', '--no-color'], print_stdout=False, print_stderr=False)[1].splitlines():
        yield line[2:].strip()

def get_path_args(options):
    return ['--git-dir=' + os.path.join(options.working_copy, options.meta), '--work-tree=' + options.working_copy]

def init(options):
    inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
    git_common_options = get_path_args(options)
    run(options.git, ['init', '--bare', os.path.join(options.working_copy, options.meta)], fatal=True) # this does not use git_common_options
    run(options.git, git_common_options + ['config', 'core.bare', 'false'], fatal=True)
    run(options.git, git_common_options + ['commit', '--author="%s <%s@kaleido>"' % (inbox_id, inbox_id), '--message=', '--allow-empty-message', '--allow-empty'], fatal=True)
    open(os.path.join(options.working_copy, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
    open(os.path.join(options.working_copy, options.meta, 'git-daemon-export-ok'), 'wt')
    open(os.path.join(options.working_copy, options.meta, 'inbox-id'), 'wt').write(inbox_id + '\n')
    return True

def clone(options, args):
    url = args[0].rstrip('/') + '/' + options.meta
    if url.find('://') == -1:
        url = os.path.abspath(url)
    inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
    git_common_options = get_path_args(options)
    run(options.git, git_common_options + ['clone', '--bare', url, os.path.join(options.working_copy, options.meta)], fatal=True)
    run(options.git, git_common_options + ['config', 'core.bare', 'false'], fatal=True)
    run(options.git, git_common_options + ['config', 'remote.origin.url', url], fatal=True)
    open(os.path.join(options.working_copy, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
    open(os.path.join(options.working_copy, options.meta, 'git-daemon-export-ok'), 'wt')
    open(os.path.join(options.working_copy, options.meta, 'inbox-id'), 'wt').write(inbox_id + '\n')
    run(options.git, git_common_options + ['checkout'], fatal=True)
    return True

def serve(options, args):
    address_arg = args[0]
    address, port = address_arg.split(':', 1) if address_arg.find(':') != -1 else ('0.0.0.0', address_arg)
    base_path = os.path.abspath(options.working_copy)
    meta_path = os.path.abspath(os.path.join(options.working_copy, options.meta))
    git_common_options = get_path_args(options)
    ret = run(options.git, git_common_options + ['daemon', '--reuseaddr', '--strict-paths',
            '--enable=upload-pack', '--enable=upload-archive', '--enable=receive-pack',
            '--listen=' + address, '--port=' + port, '--base-path=' + base_path, meta_path], fatal=True)
    return ret[0]

def squash(options):
    git_common_options = get_path_args(options)
    has_origin = run(options.git, git_common_options + ['config', '--get', 'remote.origin.url'], print_stdout=False)[0]

    if has_origin:
        print('squash must be done at the root working copy with no origin')
        return False

    tree_id = run(options.git, git_common_options + ['commit-tree', 'HEAD^{tree}'], print_stdout=False, fatal=True)[1].strip()
    run(options.git, git_common_options + ['branch', 'new_master', tree_id], fatal=True)
    run(options.git, git_common_options + ['checkout', 'new_master'], fatal=True)
    run(options.git, git_common_options + ['branch', '-M', 'new_master', 'master'], fatal=True)
    run(options.git, git_common_options + ['gc', '--aggressive'])
    return True

def sync(options, command):
    sync_forever = (command == 'sync-forever')
    inbox_id = open(os.path.join(options.working_copy, options.meta, 'inbox-id'), 'rt').read().strip()

    git_common_options = get_path_args(options)
    has_origin = run(options.git, git_common_options + ['config', '--get', 'remote.origin.url'], print_stdout=False)[0]

    git_strategy_option = ['--strategy-option', 'theirs'] if detect_git_version(options) >= (1, 7, 0) else []

    no_change_notifications = [
            (24 * 60 * 60), (12 * 60 * 60), ( 6 * 60 * 60),
            (     60 * 60), (     30 * 60), (     10 * 60),
            (          60), (          30), (          10),
        ]

    try:
        prev_last_change = None
        last_diff = 0

        while True:
            # commit local changes to local master
            run(options.git, git_common_options + ['add', '--update'], print_stdout=(not options.quiet))
            if not run(options.git, git_common_options + ['diff', '--quiet', '--cached'])[0]:
                # there seems some change to commit
                run(options.git, git_common_options + ['commit', '--quiet', '--author="%s <%s@kaleido>"' % (inbox_id, inbox_id), '--message=', '--allow-empty-message'], print_stdout=(not options.quiet))

            # fetch remote master to local sync_inbox_origin for local merge
            if has_origin:
                run(options.git, git_common_options + ['fetch', '--quiet', '--force', 'origin', 'master:sync_inbox_origin'], print_stdout=(not options.quiet))

            # merge local sync_inbox_* into local master
            for branch in list_git_branches(options.git, git_common_options):
                if not branch.startswith('sync_inbox_'):
                    continue
                has_common_ancestor = run(options.git, git_common_options + ['merge-base', 'master', branch], print_stdout=False)[0]
                if has_common_ancestor:
                    # merge local master with the origin
                    run(options.git, git_common_options + ['merge', '--quiet', '--strategy=recursive'] + git_strategy_option + [branch], print_stdout=(not options.quiet))
                    run(options.git, git_common_options + ['branch', '--delete', branch], print_stdout=False)
                elif branch == 'sync_inbox_origin':
                    # the origin has been squashed; apply it locally
                    run(options.git, git_common_options + ['branch', 'new_master', branch], print_stdout=(not options.quiet), fatal=True)
                    # this may fail without --force if some un-added file is now included in the tree
                    run(options.git, git_common_options + ['checkout', '--force', 'new_master'], print_stdout=(not options.quiet), fatal=True)
                    run(options.git, git_common_options + ['branch', '-M', 'new_master', 'master'], print_stdout=(not options.quiet), fatal=True)
                    run(options.git, git_common_options + ['gc', '--aggressive'], print_stdout=(not options.quiet))

            # push local master to remote sync_inbox_ID for remote merge
            if has_origin:
                run(options.git, git_common_options + ['push', '--quiet', '--force', 'origin', 'master:sync_inbox_%s' % inbox_id], print_stdout=(not options.quiet))

            # detect the last change time
            if not options.quiet:
                for line in run(options.git, git_common_options + ['log', '-1'], print_stdout=False)[1].splitlines():
                    if line.startswith('Date: '):
                        last_change_str = line[6:].strip()
                        last_change = time.mktime(email.utils.parsedate(last_change_str))
                        break
                now = time.time()
                if prev_last_change != last_change:
                    # new change
                    diff_msg = get_timediff_str(now - last_change)
                    diff_msg = diff_msg + ' ago' if diff_msg else 'now'
                    print('last change: %s (%s)' % (email.utils.formatdate(last_change, True), diff_msg))
                    prev_last_change = last_change
                else:
                    # no change
                    no_change_msg = ''
                    for timespan in no_change_notifications:
                        if last_diff < timespan and now - last_change >= timespan:
                            no_change_msg = 'no changes in ' + get_timediff_str(timespan)
                            break
                    if no_change_msg:
                        print(no_change_msg)
                last_diff = now - last_change

            if sync_forever:
                time.sleep(float(options.interval))
                continue
            else:
                break
    except KeyboardInterrupt:
        pass
    return True

def git_command(options, command, args):
    git_args = get_path_args(options) + [command] + args
    return invoke(options.git, git_args)

def print_help():
    print('usage: %s [OPTIONS] {init | clone <repository> | serve [<address>:]<port> | squash | sync | sync-forever | <git-command>}' % sys.argv[0])
    print()
    print('Options:')
    print('  -h                show this help message and exit')
    print('  -g GIT            git executable path [default: %s]' % GIT_DEFAULT)
    print('  -m META           git repository directory name [default: %s]' % META_DEFAULT)
    print('  -w WORKING_COPY   working copy path [default: %s]' % WORKING_COPY_DEFAULT)
    print('  -i INTERVAL       interval between sync in sync-forever [default: %s]' % INTERVAL_DEFAULT)
    print('  -q                less verbose when syncing')

def main():
    options = Option()

    args = sys.argv[1:]

    if len(args) == 0:
        print_help()
        return 1

    while len(args) >= 1:
        if args[0] == '-h':
            print_help()
            return 1
        elif args[0] == '-g':
            options.git = args[1]
            args = args[2:]
        elif args[0] == '-m':
            options.meta = args[1]
            args = args[2:]
        elif args[0] == '-w':
            options.working_copy = args[1]
            args = args[2:]
        elif args[0] == '-i':
            options.interval = args[1]
            args = args[2:]
        elif args[0] == '-q':
            options.quiet = True
            args = args[1:]
        else:
            break

    command = args[0]
    args = args[1:]

    if command == 'init':
        if os.path.exists(os.path.join(options.working_copy, options.meta)):
            print('error: %s directory already exists' % options.meta)
            ret = False
        else:
            ret = init(options)
    elif command == 'clone':
        if len(args) < 1:
            print('error: too few arguments')
            print_help()
            ret = False
        elif os.path.exists(os.path.join(options.working_copy, options.meta)):
            print('error: %s directory already exists' % options.meta)
            ret = False
        else:
            ret = clone(options, args)
    elif command == 'serve':
        if len(args) < 1:
            print('error: too few arguments')
            print_help()
            ret = False
        elif not detect_working_copy(options):
            ret = False
        else:
            ret = serve(options, args)
    elif command == 'squash':
        if not detect_working_copy(options):
            ret = False
        else:
            ret = squash(options)
    elif command == 'sync' or command == 'sync-forever':
        if not detect_working_copy(options):
            ret = False
        else:
            ret = sync(options, command)
    else:
        if not detect_working_copy(options):
            ret = False
        else:
            ret = git_command(options, command, args)

    return 0 if ret else 1

if __name__ == '__main__':
    sys.exit(main())

