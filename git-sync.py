#!/usr/bin/env python

# git-sync: a file synchronizer using git as transport
# Copyright (C) 2011 Hyeontaek Lim
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

import io
import optparse
import os
import platform
import random
import subprocess
import sys
import time
import threading

META_DEFAULT='.sync'
#META_DEFAULT='.git'    # for debugging; unsafe

if platform.platform().startswith('Linux') or platform.platform().startswith('FreeBSD'):
    GIT_PATH = 'git'
elif platform.platform().startswith('Windows'):
    if platform.architecture()[0] == '64bit':
        GIT_PATH = r'C:\Program Files (x86)\Git\bin\git.exe'
    else:
        GIT_PATH = r'C:\Program Files\Git\bin\git.exe'
else:
    assert False

def move_output(src, dst, tee=None):
    while True:
        s = src.readline(4096)
        if not s: break
        dst.write(s)
        if tee: tee.write(s)
    src.close()

def run_git(args, print_stdout=True, print_stderr=True, fatal=False):
    p = subprocess.Popen([GIT_PATH] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)

    p.stdin.close()

    threads = []
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    tee_stdout = sys.stdout if print_stdout else None
    tee_stderr = sys.stderr if print_stderr else None
    threads.append(threading.Thread(target=move_output, args=(p.stdout, stdout_buf, tee_stdout)))
    threads.append(threading.Thread(target=move_output, args=(p.stderr, stderr_buf, tee_stderr)))
    list([t.start() for t in threads])
    ret = p.wait()
    list([t.join() for t in threads])

    if fatal and ret != 0:
        raise RuntimeError('git returned %d' % ret)
    
    return (ret == 0, stdout_buf.getvalue(), stderr_buf.getvalue())

def detect_git_version():
    _, version, _ = run_git(['--version'], False, False, fatal=True)

    version = version.split(' ')[2]
    version = tuple([int(X) for x in version.split('.')[:4]])
    return version

def list_git_branches():
    for line in run_git(['branch', '--no-color'], False, False)[1].splitlines():
        yield line[2:].strip()

def update_env(directory, meta):
    os.environ['GIT_DIR'] = os.path.join(directory, meta)
    os.environ['GIT_WORK_TREE'] = directory

def main():
    usage = 'usage: %prog [--meta <directory>] {--custom <git-command> | [{--init | --clone <repository>}] [--serve [<address>:]<port>] [--no-sync | --sync-forever] <directory>}'
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('-m', '--meta', dest='meta', default=META_DEFAULT)
    parser.add_option('-U', '--usercmd', dest='usercmd', action='store_true', default=False)
    parser.add_option('-i', '--init', dest='init', action='store_true', default=False)
    parser.add_option('-c', '--clone', dest='clone', default=None)
    parser.add_option('-s', '--serve', dest='serve', default=None)
    parser.add_option('-0', '--no-sync', dest='no_sync', action='store_true', default=False)
    parser.add_option('-f', '--sync-forever', dest='sync_forever', action='store_true', default=False)
    (options, args) = parser.parse_args()

    if options.usercmd:
        args = []
        for idx, arg in enumerate(sys.argv):
            if arg == '-U' or arg == '--custom':
                args = sys.argv[idx + 1:]
                break
        return 0 if run_git(args)[0] else 1

    if len(args) != 1:
        parser.error('directory must be given')
        parser.print_help()
        return 1

    if options.init and options.clone != None:
        parser.error('--init and --clone are mutually exclusive')
        parser.print_help()
        return 1

    if options.no_sync and options.sync_forever:
        parser.error('--no-sync and --sync-forever are mutually exclusive')
        parser.print_help()
        return 1

    directory = args[0]

    if options.init:
        if os.path.exists(os.path.join(directory, options.meta)):
            print('warning: meta directory already exists; ignoring --init')
        else:
            run_git(['init', '--bare', os.path.join(directory, options.meta)], fatal=True)
            update_env(directory, options.meta)
            run_git(['config', 'core.bare', 'false'], fatal=True)
            run_git(['commit', '--author="sync <sync@sync>"', '--message=""', '--allow-empty'], True, fatal=True)
            open(os.path.join(directory, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
            open(os.path.join(directory, options.meta, 'git-daemon-export-ok'), 'w')
            inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
            open(os.path.join(directory, options.meta, 'inbox-id'), 'w').write(inbox_id)
            return 0

    elif options.clone != None:
        if os.path.exists(os.path.join(directory, options.meta)):
            print('warning: meta directory already exists; ignoring --clone')
        else:
            url = options.clone.rstrip('/') + '/' + options.meta
            update_env(directory, options.meta)
            run_git(['clone', '--bare', url, os.path.join(directory, options.meta)], fatal=True)
            run_git(['config', 'core.bare', 'false'], fatal=True)
            run_git(['config', 'remote.origin.url', url], fatal=True)
            open(os.path.join(directory, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
            open(os.path.join(directory, options.meta, 'git-daemon-export-ok'), 'w')
            inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
            open(os.path.join(directory, options.meta, 'inbox-id'), 'w').write(inbox_id)
            run_git(['checkout'], fatal=True)
            return 0

    if not os.path.exists(os.path.join(directory, options.meta)):
        print('error: meta directory does not exist')
        return 1
    inbox_id = open(os.path.join(directory, options.meta, 'inbox-id'), 'r').read()

    update_env(directory, options.meta)

    t_serve = None
    if options.serve != None:
        if options.serve.find(':') != -1:
            address, _, port = options.serve.partition(':')
        else:
            address = '0.0.0.0'
            port = options.serve
        t_serve = threading.Thread(
                target=run_git,
                args=(['daemon', '--strict-paths', '--reuseaddr',
                        '--enable=upload-pack', '--enable=upload-archive', '--enable=receive-pack',
                        '--listen=' + address, '--port=' + port,
                        '--base-path=' + os.path.abspath(directory),
                        os.path.abspath(os.path.join(directory, options.meta))],)
            )
        t_serve.start()

    version = detect_git_version()
    if version >= (1, 7, 0, 0):
        git_strategy_option = ['-X', 'theirs']
    else:
        git_strategy_option = []
    
    if run_git(['config', '--get', 'remote.origin.url'], False)[0]:
        git_pushable = True
    else:
        git_pushable = False

    while options.sync_forever:
        # merge local sync_inbox_* into local master
        for branch in list_git_branches():
            if not branch.startswith('sync_inbox_'):
                continue
            run_git(['merge', '--quiet', '--strategy=recursive'] + git_strategy_option + [branch])

        # commit local changes to local master
        run_git(['add', '--all'])
        run_git(['commit', '--author="sync <sync@sync>"', '--message=""'], False)

        if git_pushable:
            # push local master to remote sync_inbox_ID for remote merge
            run_git(['push', '--quiet', 'origin', 'master:sync_inbox_%d' % inbox_id], False)
            # fetch remote master to local sync_inbox_origin for local merge
            run_git(['fetch', '--quiet', 'origin', 'master:sync_inbox_origin'])

        time.sleep(1)

    return 0

if __name__ == '__main__':
    sys.exit(main())

