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

if platform.platform().startswith('Linux') or platform.platform().startswith('FreeBSD'):
    GIT_DEFAULT = 'git'
elif platform.platform().startswith('Windows'):
    if platform.architecture()[0] == '64bit':
        GIT_DEFAULT= r'C:\Program Files (x86)\Git\bin\git.exe'
    else:
        GIT_DEFAULT = r'C:\Program Files\Git\bin\git.exe'
else:
    assert False

def move_output(src, dst, tee=None):
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
    threads.append(threading.Thread(target=move_output, args=(p.stdout, stdout_buf, tee_stdout)))
    threads.append(threading.Thread(target=move_output, args=(p.stderr, stderr_buf, tee_stderr)))
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

def list_git_branches(git_path, git_common_optionss):
    for line in run(git_path, git_common_optionss + ['branch', '--no-color'], False, False)[1].splitlines():
        yield line[2:].strip()

def get_path_args(directory, meta):
    return ['--git-dir=' + os.path.join(directory, meta), '--work-tree=' + directory]

def main():
    usage = 'usage: %prog [--meta <directory>] {--usercmd <git-command> | [{--init | --clone <repository>}] [--distribute [<address>:]<port> | --serve [<address>:]<port> | --sync-forever] <directory>}'
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('-g', '--git', dest='git', default=GIT_DEFAULT, help='git executable path')
    parser.add_option('-m', '--meta', dest='meta', default=META_DEFAULT, help='git repository directory name')
    parser.add_option('-U', '--usercmd', dest='usercmd', action='store_true', default=False, help='custom user command')
    parser.add_option('-i', '--init', dest='init', action='store_true', default=False, help='init a sync')
    parser.add_option('-c', '--clone', dest='clone', default=None, help='clone a sync')
    parser.add_option('-d', '--distribute', dest='distribute', default=None, help='distribute git-sync via HTTP')
    parser.add_option('-s', '--serve', dest='serve', default=None, help='serve git repositories using git daemon')
    parser.add_option('-f', '--sync-forever', dest='sync_forever', action='store_true', default=False, help='sync forever')
    parser.add_option('-q', '--quiet', dest='quiet', action='store_true', default=False, help='less verbose')
    (options, args) = parser.parse_args()

    if options.usercmd:
        args = get_path_args('.', options.meta)
        for idx, arg in enumerate(sys.argv):
            if arg == '-U' or arg == '--custom':
                args += sys.argv[idx + 1:]
                break
        return 0 if run(options.git, args)[0] else 1

    if len(args) == 0:
        args = ['.']
    elif len(args) >= 2:
        parser.error('too many arguments are given')
        parser.print_help()
        return 1

    if options.init and options.clone != None:
        parser.error('--init and --clone are mutually exclusive')
        parser.print_help()
        return 1

    if options.distribute and options.serve and options.sync_forever:
        parser.error('--distribute and --serve and --sync-forever are mutually exclusive')
        parser.print_help()
        return 1

    directory = args[0]
    path_args = get_path_args(directory, options.meta)

    git_common_options = path_args
    git_common_options_clean = []

    if options.init:
        if os.path.exists(os.path.join(directory, options.meta)):
            print('warning: meta directory already exists; ignoring --init')
        else:
            run(options.git, git_common_options_clean + ['init', '--bare', os.path.join(directory, options.meta)], fatal=True)
            run(options.git, git_common_options + ['config', 'core.bare', 'false'], fatal=True)
            run(options.git, git_common_options + ['commit', '--author="sync <sync@sync>"', '--message=""', '--allow-empty'], fatal=True)
            open(os.path.join(directory, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
            open(os.path.join(directory, options.meta, 'git-daemon-export-ok'), 'wb')
            inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
            open(os.path.join(directory, options.meta, 'inbox-id'), 'wb').write(inbox_id)
            return 0

    elif options.clone != None:
        if os.path.exists(os.path.join(directory, options.meta)):
            print('warning: meta directory already exists; ignoring --clone')
        else:
            url = options.clone.rstrip('/') + '/' + options.meta
            if url.find('://') == -1:
                url = os.path.abspath(url)
            run(options.git, git_common_options + ['clone', '--bare', url, os.path.join(directory, options.meta)], fatal=True)
            run(options.git, git_common_options + ['config', 'core.bare', 'false'], fatal=True)
            run(options.git, git_common_options + ['config', 'remote.origin.url', url], fatal=True)
            open(os.path.join(directory, options.meta, 'info', 'exclude'), 'at').write(options.meta + '\n')
            open(os.path.join(directory, options.meta, 'git-daemon-export-ok'), 'wb')
            inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
            open(os.path.join(directory, options.meta, 'inbox-id'), 'wb').write(inbox_id)
            run(options.git, git_common_options + ['checkout'], fatal=True)
            return 0

    if options.distribute != None:
        if options.distribute.find(':') != -1:
            address, _, port = options.distribute.partition(':')
        else:
            address = '0.0.0.0'
            port = options.distribute
        class Handler(BaseHTTPServer.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/':
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html')
                    self.end_headers()
                    self.wfile.write('<html><body><a href="git-sync.py">git-sync.py</a></body></html>')
                elif self.path == '/git-sync.py':
                    try:
                        f = open(sys.argv[0], 'rb')
                        s = f.read()
                    except IOError, OSError:
                        self.send_response(500)
                        self.end_headers()
                    else:
                        self.send_response(200)
                        self.send_header('Content-type', 'text/x-python')
                        self.end_headers()
                        self.wfile.write(s)
                else:
                    self.send_response(404)
                    self.end_headers()

        try:
            BaseHTTPServer.HTTPServer((address, int(port)), Handler).serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if options.serve != None:
        if options.serve.find(':') != -1:
            address, _, port = options.serve.partition(':')
        else:
            address = '0.0.0.0'
            port = options.serve
        dirs = []
        if not options.quiet:
            print('served:')
        for name in ['.'] + os.listdir(directory):
            path = os.path.abspath(os.path.join(directory, name, options.meta))
            if os.path.isdir(path):
                dirs.append(path)
                if not options.quiet:
                    print(name)
        ret = run(options.git, git_common_options + ['daemon', '--reuseaddr', '--strict-paths',
                '--enable=upload-pack', '--enable=upload-archive', '--enable=receive-pack',
                '--listen=' + address, '--port=' + port,
                '--base-path=' + os.path.abspath(directory)] + dirs)
        return 0 if ret[0] else 1

    if not os.path.exists(os.path.join(directory, options.meta)):
        print('error: meta directory does not exist')
        return 1
    inbox_id = open(os.path.join(directory, options.meta, 'inbox-id'), 'rb').read()

    version = detect_git_version(options.git)
    if version >= (1, 7, 0, 0):
        git_strategy_option = ['-X', 'theirs']
    else:
        git_strategy_option = []
    
    if run(options.git, git_common_options + ['config', '--get', 'remote.origin.url'], False)[0]:
        git_pushable = True
    else:
        git_pushable = False

    while True:
        # merge local sync_inbox_* into local master
        for branch in list_git_branches(options.git, git_common_options):
            if not branch.startswith('sync_inbox_'):
                continue
            run(options.git, git_common_options + ['merge', '--quiet', '--strategy=recursive'] + git_strategy_option + [branch], print_stdout=(not options.quiet))

        # commit local changes to local master
        run(options.git, git_common_options + ['add', '--all'], print_stdout=(not options.quiet))
        run(options.git, git_common_options + ['commit', '--quiet', '--author="sync <sync@sync>"', '--message=""'], print_stdout=(not options.quiet))

        if git_pushable:
            # push local master to remote sync_inbox_ID for remote merge
            run(options.git, git_common_options + ['push', '--quiet', 'origin', 'master:sync_inbox_%s' % inbox_id], print_stdout=(not options.quiet))
            # fetch remote master to local sync_inbox_origin for local merge
            run(options.git, git_common_options + ['fetch', '--quiet', 'origin', 'master:sync_inbox_origin'], print_stdout=(not options.quiet))

        if options.sync_forever:
            time.sleep(1)
            continue
        else:
            break

    return 0

if __name__ == '__main__':
    sys.exit(main())

