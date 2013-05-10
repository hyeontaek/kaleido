#!/usr/bin/env python

# git-sync: a file synchronizer using git as transport
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

import BaseHTTPServer
import optparse
import os
import platform
import random
import subprocess
import sys
import StringIO
import time
import threading

META_DEFAULT='.sync'
#META_DEFAULT='.git'    # for debugging; unsafe
WORKING_COPY_DEFAULT='.'

if platform.platform().startswith('Linux') or platform.platform().startswith('FreeBSD'):
    GIT_DEFAULT = '/usr/bin/git'
elif platform.platform().startswith('Windows'):
    if platform.architecture()[0] == '64bit':
        GIT_DEFAULT= r'C:\Program Files (x86)\Git\bin\git.exe'
    else:
        GIT_DEFAULT = r'C:\Program Files\Git\bin\git.exe'
else:
    assert False

def copy_output(src, dst, tee=None):
    while True:
        s = src.readline(4096)
        if not s: break
        dst.write(s)
        if tee: tee.write(s)
    src.close()

def run(git_path, args, print_stdout=True, print_stderr=True, fatal=False):
    p = subprocess.Popen([git_path] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    p.stdin.close()

    threads = []
    stdout_buf = StringIO.StringIO()
    stderr_buf = StringIO.StringIO()
    tee_stdout = sys.stdout if print_stdout else None
    tee_stderr = sys.stderr if print_stderr else None
    threads.append(threading.Thread(target=copy_output, args=(p.stdout, stdout_buf, tee_stdout)))
    threads.append(threading.Thread(target=copy_output, args=(p.stderr, stderr_buf, tee_stderr)))
    list([t.start() for t in threads])
    ret = p.wait()
    list([t.join() for t in threads])

    if fatal and ret != 0:
        raise RuntimeError('git returned %d' % ret)
    
    return (ret == 0, stdout_buf.getvalue(), stderr_buf.getvalue())

def detect_git_version(git_path):
    _, version, _ = run(git_path, ['--version'], False, False, fatal=True)

    version = version.split(' ')[2]
    version = tuple([int(x) for x in version.split('.')[:4]])
    return version

def list_git_branches(git_path, git_common_options):
    for line in run(git_path, git_common_options + ['branch', '--no-color'], False, False)[1].splitlines():
        yield line[2:].strip()

def get_path_args(directory, meta):
    return ['--git-dir=' + os.path.join(directory, meta), '--work-tree=' + directory]

def init(options, command, args):
    if os.path.exists(os.path.join(options.working_copy, options.meta)):
        print('error: meta directory already exists')
        return False
    git_common_options = get_path_args(options.working_copy, options.meta)
    run(options.git, ['init', '--bare', os.path.join(options.working_copy, options.meta)], fatal=True) # this does not use git_common_options
    run(options.git, git_common_options + ['config', 'core.bare', 'false'], fatal=True)
    run(options.git, git_common_options + ['commit', '--author="sync <sync@sync>"', '--message=""', '--allow-empty'], fatal=True)
    open(os.path.join(options.working_copy, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
    open(os.path.join(options.working_copy, options.meta, 'git-daemon-export-ok'), 'wb')
    inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
    open(os.path.join(options.working_copy, options.meta, 'inbox-id'), 'wb').write(inbox_id + '\n')
    return True

def clone(options, command, args):
    if os.path.exists(os.path.join(options.working_copy, options.meta)):
        print('error: meta directory already exists')
        return False
    url = args[0].rstrip('/') + '/' + options.meta
    if url.find('://') == -1:
        url = os.path.abspath(url)
    git_common_options = get_path_args(options.working_copy, options.meta)
    run(options.git, git_common_options + ['clone', '--bare', url, os.path.join(options.working_copy, options.meta)], fatal=True)
    run(options.git, git_common_options + ['config', 'core.bare', 'false'], fatal=True)
    run(options.git, git_common_options + ['config', 'remote.origin.url', url], fatal=True)
    open(os.path.join(options.working_copy, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
    open(os.path.join(options.working_copy, options.meta, 'git-daemon-export-ok'), 'wb')
    inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
    open(os.path.join(options.working_copy, options.meta, 'inbox-id'), 'wb').write(inbox_id + '\n')
    run(options.git, git_common_options + ['checkout'], fatal=True)
    return True

def serve(options, command, args):
    address_arg = args[0]
    address, port = address_arg.split(':', 1) if address_arg.find(':') != -1 else ('0.0.0.0', address_arg)
    dirs = []
    if not options.quiet:
        print('served:')
    for name in ['.'] + os.listdir(options.working_copy):
        path = os.path.abspath(os.path.join(options.working_copy, name, options.meta))
        if os.path.isdir(path):
            dirs.append(path)
            if not options.quiet:
                print(name)
    git_common_options = get_path_args(options.working_copy, options.meta)
    ret = run(options.git, git_common_options + ['daemon', '--reuseaddr', '--strict-paths',
            '--enable=upload-pack', '--enable=upload-archive', '--enable=receive-pack',
            '--listen=' + address, '--port=' + port,
            '--base-path=' + os.path.abspath(options.working_copy)] + dirs)
    return ret[0]

def sync(options, command, args):
    sync_forever = command == 'sync-forever'
    inbox_id = open(os.path.join(options.working_copy, options.meta, 'inbox-id'), 'rb').read().strip()

    version = detect_git_version(options.git)
    git_strategy_option = ['-X', 'theirs'] if version >= (1, 7, 0, 0) else []

    git_common_options = get_path_args(options.working_copy, options.meta)
    git_pushable = run(options.git, git_common_options + ['config', '--get', 'remote.origin.url'])[0]

    try:
        while True:
            # merge local sync_inbox_* into local master
            for branch in list_git_branches(options.git, git_common_options):
                if not branch.startswith('sync_inbox_'):
                    continue
                run(options.git, git_common_options + ['merge', '--quiet', '--strategy=recursive'] + git_strategy_option + [branch], print_stdout=(not options.quiet))

            # commit local changes to local master
            run(options.git, git_common_options + ['add', '--update'], print_stdout=(not options.quiet))
            run(options.git, git_common_options + ['commit', '--quiet', '--author="sync <sync@sync>"', '--message=""'], print_stdout=(not options.quiet))

            if git_pushable:
                # push local master to remote sync_inbox_ID for remote merge
                run(options.git, git_common_options + ['push', '--quiet', 'origin', 'master:sync_inbox_%s' % inbox_id], print_stdout=(not options.quiet))
                # fetch remote master to local sync_inbox_origin for local merge
                run(options.git, git_common_options + ['fetch', '--quiet', 'origin', 'master:sync_inbox_origin'], print_stdout=(not options.quiet))

            if sync_forever:
                time.sleep(1)
                continue
            break
    except KeyboardInterrupt:
        pass
    return True

def git(options, command, args):
    git_args = get_path_args('.', options.meta) + args
    return run(options.git, git_args)[0]

def main():
    usage = 'usage: %prog [OPTIONS] {init | clone <repository> | serve [<address>:]<port> | sync | sync-forever | git [<git-command>]}'
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('-g', '--git', dest='git', default=GIT_DEFAULT, help='git executable path')
    parser.add_option('-m', '--meta', dest='meta', default=META_DEFAULT, help='git repository directory name')
    parser.add_option('-w', '--working-copy', dest='working_copy', default=WORKING_COPY_DEFAULT, help='working copy path')
    parser.add_option('-q', '--quiet', dest='quiet', action='store_true', default=False, help='less verbose')
    (options, args) = parser.parse_args()

    if len(args) == 0:
        parser.error('too few arguments')
        parser.print_help()
        return 1

    command = args[0]
    args = args[1:]

    if command == 'init':
        ret = init(options, command, args)
    elif command == 'clone':
        if len(args) < 1:
            parser.error('too few arguments')
            parser.print_help()
            ret = False
        else:
            ret = clone(options, command, args)
    elif command == 'serve':
        if len(args) < 1:
            parser.error('too few arguments')
            parser.print_help()
            ret = False
        else:
            ret = serve(options, command, args)
    elif command == 'sync' or command == 'sync-forever':
        if not os.path.exists(os.path.join(options.working_copy, options.meta)):
            print('error: meta directory does not exist')
            ret = False
        else:
            ret = sync(options, command, args)
    elif command == 'git':
        ret = git(options, command, args)
    else:
        print('error: invalid command')
        parser.print_help()
        ret = False

    return 0 if ret else 1

if __name__ == '__main__':
    sys.exit(main())

