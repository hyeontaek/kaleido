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
import select
import socket
import subprocess
import sys
import time
import threading
try:
    import win32file
    import win32con
except ImportError: pass


class Options:
    def __init__(self):
        if platform.platform().startswith('Linux') or platform.platform().startswith('FreeBSD'):
            self.git = 'git'
        elif platform.platform().startswith('Windows'):
            if platform.architecture()[0] == '64bit':
                self.git = r'C:\Program Files (x86)\Git\bin\git.exe'
            else:
                self.git = r'C:\Program Files\Git\bin\git.exe'
        else:
            assert False, 'Not support platform'
        self.meta = '.kaleido'
        self.working_copy = '.'
        self.interval = 0.1
        self.local_polling = False
        self.remote_polling = False
        self.beacon_listen = False
        self.beacon_address = ('127.0.0.1', 50000)
        self.quiet = False


class GitUtil:
    def __init__(self, options):
        self.options = options
        self.common_args = []

    def set_common_args(self, common_args):
        self.common_args = common_args[:]

    @staticmethod
    def _copy_output(src, dest, tee=None):
        while True:
            s = src.readline(4096).decode(sys.getdefaultencoding())
            if not s: break
            dest.write(s)
            if tee: tee.write('  ' + s)
        src.close()

    def call(self, args, must_succeed=True):
        #if not self.options.quiet:
        #    print('  $ ' + ' '.join([self.options.git] + self.common_args + args))
        p = subprocess.Popen([self.options.git] + self.common_args + args,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.close()

        threads = []
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        tee_stdout = None #sys.stdout
        tee_stderr = None if self.options.quiet else sys.stderr
        threads.append(threading.Thread(target=GitUtil._copy_output, args=(p.stdout, stdout_buf, tee_stdout)))
        threads.append(threading.Thread(target=GitUtil._copy_output, args=(p.stderr, stderr_buf, tee_stderr)))
        list([t.start() for t in threads])
        ret = p.wait()
        list([t.join() for t in threads])

        if must_succeed and ret != 0:
            raise RuntimeError('git returned %d' % ret)
        
        return (ret, stdout_buf.getvalue(), stderr_buf.getvalue())

    def execute(self, args, must_succeed=True):
        #if not self.options.quiet:
        #    print('  $ ' + ' '.join([self.options.git] + self.common_args + args))
        ret = subprocess.call([self.options.git] + self.common_args + args)

        if must_succeed and ret != 0:
            raise RuntimeError('git returned %d' % ret)

        return (ret, '', '')

    def detect_git_version(self):
        version = self.call(['--version'])[1]
        version = version.split(' ')[2]
        version = tuple([int(x) for x in version.split('.')[:3]])
        return version

    def detect_working_copy_root(self):
        path = os.path.abspath(os.path.join(self.options.working_copy))
        while path != os.path.dirname(path):
            if os.path.exists(os.path.join(path, self.options.meta)):
                self.options.working_copy_root = path
                return
            path = os.path.dirname(path)
        raise Exception('error: cannot find %s' % self.options.meta)

    def get_path_args(self):
        return ['--git-dir=' + os.path.join(self.options.working_copy_root, self.options.meta),
                '--work-tree=' + self.options.working_copy_root]

    def list_git_branches(self):
        for line in self.call(['branch', '--no-color'])[1].splitlines():
            yield line[2:].strip()

    def get_last_commit_id(self):
        for line in self.call(['log', '-1'])[1].splitlines():
            if line.startswith('commit '):
                return line[7:].strip()
        return None

    def get_last_commit_time(self):
        for line in self.call(['log', '-1'])[1].splitlines():
            if line.startswith('Date: '):
                last_change_str = line[6:].strip()
                return time.mktime(email.utils.parsedate(last_change_str))
        return 0.


class TimeUtil:
    _time_units = [
            (24 * 60 * 60, 'day', 'days'),
            (     60 * 60, 'hour', 'hours'),
            (          60, 'minute', 'minutes'),
            (           1, 'second', 'seconds'),
        ]

    @staticmethod
    def get_timediff_str(diff):
        t = ''
        for timeunit, name_s, name_p in TimeUtil._time_units:
            if diff >= timeunit:
                c = int(diff / timeunit)
                diff -= c * timeunit
                t += '%d %s ' % (c, name_s if c == 1 else name_p)
        return t.rstrip()


class LocalChangeMonitor:
    def __init__(self, options):
        self.options = options
        self.use_polling = self.options.local_polling
        self.running = False
        self.exiting = False
        self.flag = False

    def __del__(self):
        if self.running:
            self.stop()

    def may_have_changes(self):
        return self.flag

    def before_sync(self):
        if not self.use_polling:
            self.flag = False

    def start(self):
        assert not self.running
        self.exiting = False
        self.flag = True    # assume changes initially
        if not self.use_polling:
            if platform.platform().startswith('Linux'):
                print('monitoring local changes in %s' % self.options.working_copy_root)
                self.p = subprocess.Popen(['inotifywait', '--monitor', '--recursive', '--quiet',
                                           '-e', 'modify', '-e', 'attrib', '-e', 'close_write',
                                           '-e', 'move', '-e', 'create', '-e', 'delete',
                                           self.options.working_copy_root],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                self.p.stdin.close()
                self.t = threading.Thread(target=self._inotifywait_handler, args=())
                self.t.start()
            elif platform.platform().startswith('Windows'):
                print('monitoring local changes in %s' % self.options.working_copy_root)
                FILE_LIST_DIRECTORY = 1
                self.h = win32file.CreateFile(self.options.working_copy_root, FILE_LIST_DIRECTORY,
                                              win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE |
                                              win32con.FILE_SHARE_DELETE,
                                              None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
                self.t = threading.Thread(target=self._ReadDirectoryChanges_handler, args=())
                self.t.start()
            else:
                self.use_polling = True
        if self.use_polling:
            print('monitoring local changes in %s (polling)' % self.options.working_copy_root)
        # TODO: support Kevent for BSD
        self.running = True

    def stop(self):
        assert self.running
        self.exiting = True
        self.running = False
        if not self.use_polling:
            if platform.platform().startswith('Linux'):
                self.p.terminate()
                # the following is skipped for faster termination
                #self.p.wait()
                #self.t.join()
            elif platform.platform().startswith('Windows'):
                win32file.CloseHandle(self.h)
                # the following is skipped for faster termination
                #self.t.join()

    def _inotifywait_handler(self):
        #try:
            meta_path = os.path.join(self.options.working_copy_root, self.options.meta)
            while not self.exiting:
                s = self.p.stdout.readline(4096).decode(sys.getdefaultencoding())
                if not s: break
                try:
                    directory, event, filename = s.split(' ', 3)
                    if directory.startswith(meta_path):
                        continue
                    #sys.stdout.write(s)
                    self.flag = True
                except ValueError:
                    pass
            self.p.stdout.close()
        #except Exception as e:
        #    print(str(e))

    def _ReadDirectoryChanges_handler(self):
        #try:
            meta_path = os.path.join(self.options.working_copy_root, self.options.meta)
            while not self.exiting:
                results = win32file.ReadDirectoryChangesW(self.h, 1024, True,
                                                          win32con.FILE_NOTIFY_CHANGE_FILE_NAME |
                                                          win32con.FILE_NOTIFY_CHANGE_DIR_NAME |
                                                          win32con.FILE_NOTIFY_CHANGE_ATTRIBUTES |
                                                          win32con.FILE_NOTIFY_CHANGE_SIZE |
                                                          win32con.FILE_NOTIFY_CHANGE_LAST_WRITE |
                                                          win32con.FILE_NOTIFY_CHANGE_SECURITY,
                                                          None, None)
                for _, name in results:
                    path = os.path.join(self.options.working_copy_root, name)
                    if path.startswith(meta_path):
                        continue
                    #print(path)
                    self.flag = True
                    break
        #except Exception as e:
        #    print(str(e))


class RemoteChangeMonitor:
    # reducing this helps the program terminate quickly, but increases CPU load at idle
    _POLL_TIMEOUT = 100

    def __init__(self, options):
        self.options = options
        self.use_polling = self.options.remote_polling
        self.listen = self.options.beacon_listen
        self.address = self.options.beacon_address
        self.running = False
        self.exiting = False
        self.flag = False
        self.need_to_send_signal = False

    def __del__(self):
        if self.running:
            self.stop()

    def may_have_changes(self):
        return self.flag

    def before_sync(self):
        if not self.use_polling:
            self.flag = False

    def after_sync(self):
        if not self.use_polling:
            self.need_to_send_signal = True

    def start(self):
        assert not self.running
        self.exiting = False
        self.flag = True    # assume changes initially
        self.need_to_send_signal = False
        if self.address == None:
            self.use_polling = True
        if not self.use_polling:
            self.sb_peers = []
            if self.listen:
                self.s_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.s_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.s_server.bind(self.address)
                self.s_server.listen(5)
                print('beacon server listening at %s:%d' % self.address)
            self.t = threading.Thread(target=self._handler, args=())
            self.t.start()
        self.running = True

    def stop(self):
        assert self.running
        if not self.use_polling:
            while self.need_to_send_signal:
                time.sleep(0.1)
            pending_write = True
            while pending_write:
                pending_write = False
                for c in self.sb_peers:
                    if c[1]:
                        pending_write = True
                        break
                if not pending_write:
                    break
                time.sleep(0.1)
        self.exiting = True
        self.running = False
        if not self.use_polling:
            self.t.join()
            if self.listen:
                self.s_server.close()
            for c in self.sb_peers:
                c[0].close()

    def _handler(self):
        #try:
            while not self.exiting:
                if not self.listen and not self.sb_peers:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setblocking(0)
                    try:
                        print('connecting to beacon server %s:%d' % self.address)
                        s.connect(self.address)
                    except socket.error:
                        pass
                    self.sb_peers.append([s, b''])
                    self.flag = True    # assume changes because we may have missed signals

                if self.need_to_send_signal:
                    # broadcast
                    print('notifying %d peers for local changes' % len(self.sb_peers))
                    for c in self.sb_peers:
                        c[1] = b's'
                    self.need_to_send_signal = False

                socks_to_read = [c[0] for c in self.sb_peers] + ([self.s_server] if self.listen else [])
                socks_to_write = []
                for idx, c in enumerate(self.sb_peers):
                    if c[1]: socks_to_write.append(c[0])

                rlist, wlist, _ = select.select(socks_to_read, socks_to_write, [], 0.1)

                for s in rlist:
                    if self.listen and s == self.s_server:
                        s_new_client, _ = self.s_server.accept()
                        s_new_client.setblocking(0)
                        self.sb_peers.append([s_new_client, b''])
                    else:
                        try:
                            msg = s.recv(1)
                        except socket.error:
                            msg = None
                        if not msg:
                            for idx, c in enumerate(self.sb_peers):
                                if c[0] == s:
                                    del self.sb_peers[idx]
                                    s.close()
                                    break
                        else:
                            self.flag = True
                            if self.listen:
                                # broadcast except the source
                                print('notifying %d peers for remote changes' % (len(self.sb_peers) - 1))
                                for c in self.sb_peers:
                                    if c[0] != s:
                                        c[1] = b's'
                for s in wlist:
                    for idx, c in enumerate(self.sb_peers):
                        if c[0] == s:
                            wrote_len = c[0].send(c[1])
                            if wrote_len:
                                c[1] = b''
        #except Exception as e:
        #    print(str(e))


class Kaleido:
    def __init__(self, options):
        self.options = options
        self.gu = GitUtil(self.options)

    def init(self):
        inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
        self.gu.set_common_args([])
        self.gu.call(['init', '--bare', os.path.join(self.options.working_copy, self.options.meta)])
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        self.gu.call(['config', 'core.bare', 'false'])
        self.gu.call(['commit', '--author="%s <%s@kaleido>"' % (inbox_id, inbox_id),
                         '--message=', '--allow-empty-message', '--allow-empty'])
        meta_path = os.path.join(self.options.working_copy_root, self.options.meta)
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write(self.options.meta + '\n')
        open(os.path.join(meta_path, 'git-daemon-export-ok'), 'wt')
        open(os.path.join(meta_path, 'inbox-id'), 'wt').write(inbox_id + '\n')
        return True

    def clone(self, path):
        url = path.rstrip('/') + '/' + self.options.meta
        if url.find(':') == -1:
            url = os.path.abspath(url)
        inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
        self.gu.set_common_args([])
        self.gu.call(['clone', '--bare', url, os.path.join(self.options.working_copy, self.options.meta)])
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        self.gu.call(['config', 'core.bare', 'false'])
        self.gu.call(['config', 'remote.origin.url', url])
        meta_path = os.path.join(self.options.working_copy_root, self.options.meta)
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write(self.options.meta + '\n')
        open(os.path.join(meta_path, 'git-daemon-export-ok'), 'wt')
        open(os.path.join(meta_path, 'inbox-id'), 'wt').write(inbox_id + '\n')
        self.gu.call(['checkout'])
        return True

    def serve(self, address, port):
        self.gu.detect_working_copy_root()
        base_path = os.path.abspath(self.options.working_copy_root)
        meta_path = os.path.abspath(os.path.join(self.options.working_copy_root, self.options.meta))
        self.gu.set_common_args(self.gu.get_path_args())
        self.gu.execute(['daemon', '--reuseaddr', '--strict-paths', '--verbose',
                         '--enable=upload-pack', '--enable=upload-archive', '--enable=receive-pack',
                         '--listen=' + address, '--port=' + str(port), '--base-path=' + base_path, meta_path])
        return True

    def squash(self):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        has_origin = self.gu.call(['config', '--get', 'remote.origin.url'], False)[0] == 0

        if has_origin:
            raise Exception('squash must be done at the root working copy with no origin')

        tree_id = self.gu.call(['commit-tree', 'HEAD^{tree}'])[1].strip()
        self.gu.call(['branch', 'new_master', tree_id])
        self.gu.call(['checkout', 'new_master'])
        self.gu.call(['branch', '-M', 'new_master', 'master'])
        self.gu.call(['gc', '--aggressive'])
        return True

    def sync(self):
        self._sync(False)

    def sync_forever(self):
        self._sync(True)

    _no_change_notifications = [
            (24 * 60 * 60), (12 * 60 * 60), ( 6 * 60 * 60),
            (     60 * 60), (     30 * 60), (     10 * 60),
            (          60), (          30), (          10),
        ]

    def _sync(self, sync_forever):
        self.gu.detect_working_copy_root()
        meta_path = os.path.join(self.options.working_copy_root, self.options.meta)
        inbox_id = open(os.path.join(meta_path, 'inbox-id'), 'rt').read().strip()

        git_strategy_option = ['--strategy-option', 'theirs'] if self.gu.detect_git_version() >= (1, 7, 0) else []

        self.gu.set_common_args(self.gu.get_path_args())
        has_origin = self.gu.call(['config', '--get', 'remote.origin.url'], False)[0] == 0

        if not sync_forever:
            # disable local notification and remote beaon server
            self.options.local_polling = True
            if self.options.remote_polling and self.options.beacon_listen:
                self.options.beacon_listen = False
                self.options.beacon_address = None

        local_change_monitor = LocalChangeMonitor(self.options)
        local_change_monitor.start()
        remote_change_monitor = RemoteChangeMonitor(self.options)
        remote_change_monitor.start()

        try:
            prev_last_change = None
            last_diff = 0

            while True:
                local_op = local_change_monitor.may_have_changes()
                remote_op = remote_change_monitor.may_have_changes()
                changed = False

                if local_op:
                    local_change_monitor.before_sync()

                    last_commit_id = self.gu.get_last_commit_id()

                    # try to add local changes
                    #print('local->local:  add')
                    self.gu.call(['add', '--update'], False)

                    # commit local changes to local master
                    #print('local->local:  commit')
                    self.gu.call(['commit', '--author="%s <%s@kaleido>"' % (inbox_id, inbox_id),
                                     '--message=', '--allow-empty-message'], False)

                    if last_commit_id != self.gu.get_last_commit_id():
                        changed = True

                if remote_op:
                    remote_change_monitor.before_sync()

                    last_commit_id = self.gu.get_last_commit_id()

                    # fetch remote master to local sync_inbox_origin for local merge
                    #print('remote->local: fetch')
                    if has_origin:
                        self.gu.call(['fetch', '--force', 'origin', 'master:sync_inbox_origin'], False)

                    # merge local sync_inbox_* into local master
                    #print('local->local:  merge')
                    for branch in self.gu.list_git_branches():
                        if not branch.startswith('sync_inbox_'):
                            continue
                        has_common_ancestor = self.gu.call(['merge-base', 'master', branch], False)[0] == 0
                        if has_common_ancestor:
                            # merge local master with the origin
                            self.gu.call(['merge', '--strategy=recursive'] + git_strategy_option + [branch], False)
                            self.gu.call(['branch', '--delete', branch], False)
                        elif branch == 'sync_inbox_origin':
                            # the origin has been squashed; apply it locally
                            self.gu.call(['branch', 'new_master', branch])
                            # this may fail without --force if some un-added file is now included in the tree
                            self.gu.call(['checkout', '--force', 'new_master'])
                            self.gu.call(['branch', '-M', 'new_master', 'master'])
                            self.gu.call(['gc', '--aggressive'])
                        else:
                            # ignore squash from non-origin sources
                            self.gu.call(['branch', '-D', branch], False)

                    if last_commit_id != self.gu.get_last_commit_id():
                        changed = True

                # figure out if there was indeed local changes
                if changed:
                    # push local master to remote sync_inbox_ID for remote merge
                    #print('local->remote: push')
                    if has_origin:
                        self.gu.call(['push', '--force', 'origin', 'master:sync_inbox_%s' % inbox_id])

                    # send beacon signal
                    remote_change_monitor.after_sync()

                if changed or not sync_forever:
                    # detect and print the last change time
                    last_change = self.gu.get_last_commit_time()
                    now = time.time()
                    if prev_last_change != last_change:
                        # new change
                        diff_msg = TimeUtil.get_timediff_str(now - last_change)
                        diff_msg = diff_msg + ' ago' if diff_msg else 'now'
                        print('last change at %s (%s)' % (email.utils.formatdate(last_change, True), diff_msg))
                        prev_last_change = last_change
                    else:
                        # no change
                        for timespan in self._no_change_notifications:
                            if last_diff < timespan and now - last_change >= timespan:
                                print('no changes in ' + TimeUtil.get_timediff_str(timespan))
                                break
                    last_diff = now - last_change

                if sync_forever:
                    time.sleep(self.options.interval)
                    continue
                else:
                    break
        #except KeyboardInterrupt:
        #    pass
        finally:
            remote_change_monitor.stop()
            local_change_monitor.stop()

        return True

    def git_command(self, args):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        return self.gu.execute(args)[0]


def print_help():
    options = Options()
    print('usage: %s [OPTIONS] ' \
          '{init | clone <REPOSITORY> | serve <ADDRESS>:<PORT> | squash | sync | sync-forever | <git-command>}' \
          % sys.argv[0])
    print()
    print('Options:')
    print('  -h               show this help message and exit')
    print('  -g GIT           git executable path [default: %s]' % options.git)
    print('  -m META          git metadata directory name [default: %s]' % options.meta)
    print('  -w WORKING_COPY  working copy path [default: %s]' % options.working_copy)
    print('  -i INTERVAL      minimum sync interval in sync-forever [default: %f]' % options.interval)
    print('  -p               force using local polling in sync-forever')
    print('  -P               force using remote polling in sync-forever')
    print('  -b               serve beacon clients')
    print('  -B <ADDRESS>:<PORT>\n' \
          '                   specify beacon address [default: %s:%d]' % options.beacon_address)
    print('  -q               less verbose')

def main():
    options = Options()

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
            options.interval = float(args[1])
            args = args[2:]
        elif args[0] == '-p':
            options.local_polling = True
            args = args[1:]
        elif args[0] == '-P':
            options.remote_polling = True
            args = args[1:]
        elif args[0] == '-b':
            options.beacon_listen = True
            args = args[1:]
        elif args[0] == '-B':
            address, port = args[1].split(':', 1)
            options.beacon_address = (address, int(port))
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
            raise Exception('error: %s directory already exists' % options.meta)
        ret = True if Kaleido(options).init() else False
    elif command == 'clone':
        if len(args) < 1:
            raise Exception('error: too few arguments')
        elif os.path.exists(os.path.join(options.working_copy, options.meta)):
            raise Exception('error: %s directory already exists' % options.meta)
        path = args[0]
        ret = True if Kaleido(options).clone(path) else False
    elif command == 'serve':
        if len(args) < 1:
            raise Exception('error: too few arguments')
        address, port = args[0].split(':', 1)
        ret = True if Kaleido(options).serve(address, int(port)) else False
    elif command == 'squash':
        ret = True if Kaleido(options).squash() else False
    elif command == 'sync':
        ret = True if Kaleido(options).sync() else False
    elif command == 'sync-forever':
        ret = True if Kaleido(options).sync_forever() else False
    else:
        ret = Kaleido(options).git_command([command] + args)

    return ret

if __name__ == '__main__':
    sys.exit(main())

