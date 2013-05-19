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
import getpass
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
        self.git = 'git'
        self.meta = '.kaleido'
        self.working_copy = '.'
        self.working_copy_root = None
        self.sync_interval = 0.1
        self.local_polling = False
        self.remote_polling = False
        self.beacon_listen = False
        self.beacon_address = ('127.0.0.1', 50000)
        self.beacon_keepalive = 20.
        self.beacon_timeout = 60.
        self.reconnect_interval = 60.
        self.control_address = ('127.0.0.1', 0)
        self.update_only = False
        self.allow_destructive = False
        self.command_after_sync = None
        self.quiet = False

    def meta_path(self):
        return os.path.join(self.working_copy_root, self.meta)

    def msg_prefix(self):
        if self.working_copy_root != None:
            s = self.working_copy_root + ': '
        else:
            s = os.path.abspath(self.working_copy) + ': '
        if len(s) < 20:
            s = '%-20s' % s
        else:
            s = s[:7] + '...' + s[-10:]
        return s


class GitUtil:
    def __init__(self, options):
        self.options = options
        self.common_args = []

    def set_common_args(self, common_args):
        self.common_args = common_args[:]

    def _copy_output(self, src, dest, tee=None):
        while True:
            s = src.readline(4096).decode(sys.getdefaultencoding())
            if not s: break
            if dest: dest.write(s)
            if tee: tee.write(self.options.msg_prefix() + '  ' + s)
        src.close()

    def call(self, args, must_succeed=True):
        #if not self.options.quiet:
        #    print(self.options.msg_prefix() + '  $ ' + ' '.join([self.options.git] + self.common_args + args))
        p = subprocess.Popen([self.options.git] + self.common_args + args,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.close()

        threads = []
        stdout_buf = io.StringIO()
        stderr_buf = None
        tee_stdout = None if self.options.quiet else sys.stdout
        tee_stderr = None if self.options.quiet else sys.stderr
        threads.append(threading.Thread(target=self._copy_output, args=(p.stdout, stdout_buf, tee_stdout)))
        threads.append(threading.Thread(target=self._copy_output, args=(p.stderr, stderr_buf, tee_stderr)))
        list([t.start() for t in threads])
        ret = p.wait()
        list([t.join() for t in threads])

        if must_succeed and ret != 0:
            raise RuntimeError('git returned %d' % ret)

        return (ret, stdout_buf.getvalue())

    def execute(self, args, must_succeed=True):
        #if not self.options.quiet:
        #    print(self.options.msg_prefix() + '  $ ' + ' '.join([self.options.git] + self.common_args + args))
        ret = subprocess.call([self.options.git] + self.common_args + args)

        if must_succeed and ret != 0:
            raise RuntimeError('git returned %d' % ret)

        return (ret, '')

    def detect_git_version(self):
        version = self.call(['--version'])[1]
        version = version.split(' ')[2]
        version = tuple([int(x) for x in version.split('.')[:3]])
        return version

    def detect_working_copy_root(self):
        path = os.path.abspath(self.options.working_copy)
        while path != os.path.dirname(path):
            if os.path.exists(os.path.join(path, self.options.meta)):
                self.options.working_copy_root = path
                return
            path = os.path.dirname(path)
        raise Exception('cannot find %s' % self.options.meta)

    def get_path_args(self):
        return ['--git-dir=' + self.options.meta_path(), '--work-tree=' + self.options.working_copy_root]

    def list_git_branches(self):
        ret = self.call(['branch', '--no-color'], False)
        if ret[0] != 0:
            return
            yield
        for line in ret[1].splitlines():
            yield line[2:].strip()

    def get_last_commit_id(self):
        ret = self.call(['log', '-1'], False)
        if ret[0] != 0:
            return None
        for line in ret[1].splitlines():
            if line.startswith('commit '):
                return line[7:].strip()
        return None

    def get_last_commit_time(self):
        ret = self.call(['log', '-1'], False)
        if ret[0] != 0:
            return 0.
        for line in ret[1].splitlines():
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
    def __init__(self, options, event):
        self.options = options
        self.use_polling = self.options.local_polling
        self.running = False
        self.exiting = False
        self.flag = False
        self.event = event

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
        self.event.set()
        if not self.use_polling:
            if platform.platform().startswith('Linux'):
                print(self.options.msg_prefix() + 'monitoring local changes in %s' % self.options.working_copy_root)
                self.p = subprocess.Popen(['inotifywait', '--monitor', '--recursive', '--quiet',
                                           '-e', 'modify', '-e', 'attrib', '-e', 'close_write',
                                           '-e', 'move', '-e', 'create', '-e', 'delete',
                                           '--format', '%w%f',
                                           self.options.working_copy_root],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                self.p.stdin.close()
                self.t = threading.Thread(target=self._inotifywait_handler, args=())
                self.t.start()
            elif platform.platform().startswith('Windows'):
                print(self.options.msg_prefix() + 'monitoring local changes in %s' % self.options.working_copy_root)
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
            print(self.options.msg_prefix() + \
                  'monitoring local changes in %s (polling)' % self.options.working_copy_root)
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
        meta_path = self.options.meta_path()
        while not self.exiting:
            path = self.p.stdout.readline(4096).strip().decode(sys.getdefaultencoding())
            if not path: break
            try:
                if path.startswith(meta_path):
                    continue
                if os.path.dirname(path).endswith('.git') and os.path.basename(path) == 'index.lock':
                    continue
                #print(self.options.msg_prefix() + path)
                self.flag = True
                self.event.set()
            except ValueError:
                pass
        self.p.stdout.close()

    def _ReadDirectoryChanges_handler(self):
        meta_path = self.options.meta_path()
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
                if os.path.dirname(path).endswith('.git') and os.path.basename(path) == 'index.lock':
                    continue
                #print(self.options.msg_prefix() + path)
                self.flag = True
                self.event.set()
                break


class RemoteChangeMonitor:
    def __init__(self, options, event):
        self.options = options
        self.use_polling = self.options.remote_polling
        self.beacon_listen = self.options.beacon_listen
        self.beacon_address = self.options.beacon_address
        self.control_address = self.options.control_address
        self.running = False
        self.exiting = False
        self.flag = False
        self.event = event
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
            self.s_control_client.sendto(b'w', self.control_address)

    def start(self):
        assert not self.running
        self.exiting = False
        self.flag = True    # assume changes initially
        self.event.set()
        self.need_to_send_signal = False
        if self.beacon_address == None:
            self.use_polling = True
        if not self.use_polling:
            self.peers = []
            if self.beacon_listen:
                self.s_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.s_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.s_server.bind(self.beacon_address)
                self.s_server.listen(5)
                print(self.options.msg_prefix() + 'beacon server listening at %s:%d' % self.beacon_address)
            self.s_control_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.s_control_server.bind(self.control_address)
            self.control_address = self.s_control_server.getsockname()
            self.s_control_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.t = threading.Thread(target=self._handler, args=())
            self.t.start()
        self.running = True

    def stop(self):
        assert self.running
        self.exiting = True
        if not self.use_polling:
            self.s_control_client.sendto(b'w', self.control_address)
            self.t.join()
            if self.beacon_listen:
                self.s_server.close()
            for p in self.peers:
                p.socket.close()
            self.s_control_client.close()
            self.s_control_server.close()
        self.running = False

    class _Peer:
        def __init__(self, socket, addr, buf, last_recv, last_send):
            self.socket = socket
            self.addr = addr
            self.buf = buf
            self.last_recv = last_recv
            self.last_send = last_send

    def _find_peer_idx(self, s):
        for idx, p in enumerate(self.peers):
            if p.socket == s:
                return idx
        return -1

    def _handler(self):
        last_connect_attempt = 0
        while True:
            if self.exiting:
                can_exit = True
                if self.need_to_send_signal:
                    can_exit = False
                else:
                    for p in self.peers:
                        if p.buf:
                            can_exit = False
                            break
                if can_exit:
                    break

            now = time.time()
            if not self.beacon_listen and not self.peers and \
               now - last_connect_attempt >= self.options.reconnect_interval:
                last_connect_attempt = now
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setblocking(0)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print(self.options.msg_prefix() + 'connecting to beacon server %s:%d' % self.beacon_address)
                try:
                    s.connect(self.beacon_address)
                except socket.error:
                    pass
                self.peers.append(self._Peer(s, self.beacon_address, b'', now, now))
                self.flag = True    # assume changes because we may have missed signals
                self.event.set()

            socks_to_read = [self.s_control_server] + [p.socket for p in self.peers]
            if self.beacon_listen: socks_to_read += [self.s_server]
            socks_to_write = []
            socks_to_check_error = [p.socket for p in self.peers]
            for idx, p in enumerate(self.peers):
                if p.buf: socks_to_write.append(p.socket)
            peers_to_close = []

            if socks_to_read or socks_to_write:
                rlist, wlist, elist = select.select(socks_to_read, socks_to_write, socks_to_check_error,
                                                    self.options.beacon_keepalive)
            else:
                rlist, wlist, elist = [], [], []

            now = time.time()
            for s in rlist:
                if s == self.s_control_server:
                    # no-op; the purpose of the control message is just to escape select()
                    msg = s.recvfrom(1)
                elif self.beacon_listen and s == self.s_server:
                    s_new_client, addr = self.s_server.accept()
                    now = time.time()
                    print(self.options.msg_prefix() + 'new peer from %s:%d' % addr)
                    try:
                        s_new_client.setblocking(0)
                        s_new_client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                        s_new_client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except socket.error:
                        pass
                    self.peers.append(self._Peer(s_new_client, addr, b'', now, now))
                else:
                    idx = self._find_peer_idx(s)
                    assert idx != -1
                    p = self.peers[idx]
                    try:
                        msg = s.recv(1)
                        if msg:
                            p.last_recv = now
                    except socket.error:
                        msg = None
                    if msg == b'k':
                        # keepalive
                        #print(self.options.msg_prefix() + 'keepalive from %s:%d' % p.addr)
                        pass
                    elif msg == b'c':
                        # changes
                        self.flag = True
                        self.event.set()
                        if self.beacon_listen:
                            # broadcast except the source
                            print(self.options.msg_prefix() + \
                                  'notifying %d peers for remote changes' % (len(self.peers) - 1))
                            for p2 in self.peers:
                                if p != p2:
                                    p2.buf = b'c'
                                    p2.last_send = now
                    else:
                        if msg:
                            print(self.options.msg_prefix() + 'unexpected response from %s:%d' % p.addr)
                        peers_to_close.append(p)

            for s in wlist:
                idx = self._find_peer_idx(s)
                assert idx != -1
                p = self.peers[idx]
                try:
                    wrote_len = p.socket.send(p.buf)
                    p.buf = p.buf[wrote_len:]
                except socket.error:
                    pass

            for s in elist:
                idx = self._find_peer_idx(s)
                assert idx != -1
                p = self.peers[idx]
                peers_to_close.append(p)

            if self.need_to_send_signal:
                # broadcast
                print(self.options.msg_prefix() + 'notifying %d peers for local changes' % len(self.peers))
                for p in self.peers:
                    p.buf = b'c'
                    p.last_send = now
                self.need_to_send_signal = False

            for p in self.peers:
                if now - p.last_recv > self.options.beacon_timeout:
                    print(self.options.msg_prefix() + 'connection to %s:%d timeout' % p.addr)
                    peers_to_close.append(p)
                elif now - p.last_send > self.options.beacon_keepalive:
                    if not p.buf:
                        p.buf = b'k'
                    p.last_send = now

            for p in peers_to_close:
                if p in self.peers:
                    print(self.options.msg_prefix() + 'connection to %s:%d closed' % p.addr)
                    self.peers.remove(p)
                    p.socket.close()


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
        self.gu.call(['config', 'core.hideDotFiles', 'false'])
        self.gu.call(['commit', '--author=%s <%s@%s>' % (getpass.getuser(), getpass.getuser(), platform.node()),
                      '--message=', '--allow-empty-message', '--allow-empty'])
        meta_path = self.options.meta_path()
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
        self.gu.call(['config', 'core.hideDotFiles', 'false'])
        self.gu.call(['config', 'remote.origin.url', url])
        meta_path = self.options.meta_path()
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write(self.options.meta + '\n')
        open(os.path.join(meta_path, 'git-daemon-export-ok'), 'wt')
        open(os.path.join(meta_path, 'inbox-id'), 'wt').write(inbox_id + '\n')
        self.gu.call(['checkout'])
        return True

    def beacon(self):
        event = threading.Event()
        remote_change_monitor = RemoteChangeMonitor(self.options, event)
        remote_change_monitor.start()
        try:
            while True:
                time.sleep(60)
        finally:
            remote_change_monitor.stop()

    def serve(self, address, port):
        self.gu.detect_working_copy_root()
        base_path = os.path.abspath(self.options.working_copy_root)
        meta_path = self.options.meta_path()
        self.gu.set_common_args(self.gu.get_path_args())
        self.gu.execute(['daemon', '--reuseaddr', '--strict-paths', '--verbose',
                         '--enable=upload-pack', '--enable=upload-archive', '--enable=receive-pack',
                         '--listen=' + address, '--port=' + str(port), '--base-path=' + base_path, meta_path])
        return True

    def squash(self):
        if not self.options.allow_destructive:
            raise Exception('destructive operations are not allowed')

        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        has_origin = self.gu.call(['config', '--get', 'remote.origin.url'], False)[0] == 0

        if has_origin:
            raise Exception('squash must be done at the root working copy with no origin')

        tree_id = self.gu.call(['commit-tree', 'HEAD^{tree}'])[1].strip()
        self.gu.call(['branch', 'new_master', tree_id])
        self.gu.call(['checkout', 'new_master'])
        self.gu.call(['branch', '-M', 'new_master', 'master'])
        self.gu.call(['gc', '--aggressive'], False)

        event = threading.Event()
        remote_change_monitor = RemoteChangeMonitor(self.options, event)
        remote_change_monitor.start()
        try:
            remote_change_monitor.after_sync()
        finally:
            remote_change_monitor.stop()
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
        meta_path = self.options.meta_path()
        inbox_id = open(os.path.join(meta_path, 'inbox-id'), 'rt').read().strip()

        git_strategy_option = ['--strategy-option', 'theirs'] if self.gu.detect_git_version() >= (1, 7, 0) else []

        self.gu.set_common_args(self.gu.get_path_args())
        has_origin = self.gu.call(['config', '--get', 'remote.origin.url'], False)[0] == 0

        if not sync_forever:
            # disable local notification
            self.options.local_polling = True
            # disable beacon server
            self.options.beacon_listen = False

        event = threading.Event()
        local_change_monitor = LocalChangeMonitor(self.options, event)
        local_change_monitor.start()
        remote_change_monitor = RemoteChangeMonitor(self.options, event)
        remote_change_monitor.start()

        try:
            prev_last_change = self.gu.get_last_commit_time()
            last_diff = 0

            while True:
                event.wait(self._no_change_notifications[-1])
                event.clear()

                local_op = local_change_monitor.may_have_changes()
                remote_op = remote_change_monitor.may_have_changes()

                changed = False

                if local_op:
                    local_change_monitor.before_sync()

                    last_commit_id = self.gu.get_last_commit_id()

                    # try to add local changes
                    if self.options.update_only:
                        self.gu.call(['add', '--update'], False)
                    else:
                        self.gu.call(['add', '--all'], False)

                    # commit local changes to local master
                    self.gu.call(['commit',
                                  '--author=%s <%s@%s>' % (getpass.getuser(), getpass.getuser(), platform.node()),
                                  '--message=', '--allow-empty-message'], False)

                    if last_commit_id != self.gu.get_last_commit_id():
                        changed = True

                if remote_op:
                    remote_change_monitor.before_sync()

                    last_commit_id = self.gu.get_last_commit_id()

                    # fetch remote master to local sync_inbox_origin for local merge
                    if has_origin:
                        self.gu.call(['fetch', '--force', 'origin', 'master:sync_inbox_origin'], False)

                    # merge local sync_inbox_* into local master
                    for branch in self.gu.list_git_branches():
                        if not branch.startswith('sync_inbox_'):
                            continue
                        has_common_ancestor = self.gu.call(['merge-base', 'master', branch], False)[0] == 0
                        if has_common_ancestor:
                            # typical merge---merge local master with the origin
                            self.gu.call(['merge', '--strategy=recursive'] + git_strategy_option + [branch], False)
                            self.gu.call(['branch', '--delete', branch], False)
                        elif branch == 'sync_inbox_origin':
                            # the origin has been squashed; apply it locally
                            if not self.options.allow_destructive:
                                print(self.options.msg_prefix() + \
                                      'ignored squash with destructive operations disallowed')
                            else:
                                succeeding = True
                                if succeeding:
                                    succeeding = self.gu.call(['branch', 'new_master', branch], False)[0] == 0
                                if succeeding:
                                    # this may fail without --force if some un-added file is now included in the tree
                                    succeeding = self.gu.call(['checkout', '--force', 'new_master'], False)[0] == 0
                                if succeeding:
                                    succeeding = self.gu.call(['branch', '-M', 'new_master', 'master'], False)[0] == 0
                                if succeeding:
                                    self.gu.call(['gc', '--aggressive'], False)
                                if not succeeding:
                                    print(self.options.msg_prefix() + 'failed to propagate squash')
                        else:
                            # ignore squash from non-origin sources
                            # branch -D is destructive,
                            # but this is quite safe when performed only on a local copy of others' branch
                            self.gu.call(['branch', '-D', branch], False)

                    if last_commit_id != self.gu.get_last_commit_id():
                        changed = True

                # figure out if there was indeed local changes
                if changed:
                    # push local master to remote sync_inbox_ID for remote merge
                    if has_origin:
                        self.gu.call(['push', '--force', 'origin', 'master:sync_inbox_%s' % inbox_id], False)

                    # send beacon signal
                    remote_change_monitor.after_sync()

                    last_change = self.gu.get_last_commit_time()
                else:
                    last_change = prev_last_change
                    pass

                now = time.time()
                # detect and print the last change time
                if prev_last_change != last_change or not sync_forever:
                    # new change
                    diff_msg = TimeUtil.get_timediff_str(now - last_change)
                    diff_msg = diff_msg + ' ago' if diff_msg else 'now'
                    print(self.options.msg_prefix() + \
                          'last change at %s (%s)' % (email.utils.formatdate(last_change, True), diff_msg))
                    prev_last_change = last_change

                if not changed:
                    # no change
                    for timespan in self._no_change_notifications:
                        if last_diff < timespan and now - prev_last_change >= timespan:
                            last_diff = now - prev_last_change
                            print(self.options.msg_prefix() + 'no changes in ' + TimeUtil.get_timediff_str(last_diff))
                            break
                else:
                    last_diff = now - prev_last_change

                if changed:
                    if self.options.command_after_sync != None:
                        os.system(self.options.command_after_sync)

                if sync_forever:
                    time.sleep(self.options.sync_interval)
                    continue
                else:
                    break
        #except KeyboardInterrupt:
        #    pass
        finally:
            remote_change_monitor.stop()
            local_change_monitor.stop()

        return True

    def fix_git(self, path):
        if not platform.platform().startswith('Linux') and not platform.platform().startswith('Windows'):
            raise Exception('not supported platform')

        native_git_path = os.path.join(path, '.git')
        fixed_git_path = os.path.join(path, '.kaleido-git')
        if not os.path.exists(native_git_path):
            raise Exception('.git does not exist')
        if os.path.exists(fixed_git_path):
            raise Exception('.kaleido-git already exists')

        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())

        # rename .git to ours so that git does not think this path as a submodule
        os.rename(native_git_path, fixed_git_path)
        # add a new entry to exclude list
        if '.kaleido-git' + '\n' not in open(os.path.join(fixed_git_path, 'info', 'exclude'), 'rt').readlines():
            open(os.path.join(fixed_git_path, 'info', 'exclude'), 'at').write('.kaleido-git' + '\n')

        # remove the previous "commit" entry that may exist (sync is required to commit changes)
        self.gu.call(['rm', '--cached', '-r', '--ignore-unmatch', path], False)

        # add a symlink from .git to .kaleido-git to make git continue to work
        if platform.platform().startswith('Linux'):
            os.symlink(fixed_git_path, native_git_path)
        elif platform.platform().startswith('Windows'):
            subprocess.call(['cmd', '/c', 'mklink', '/j', native_git_path, fixed_git_path])

        return True

    def unfix_git(self, path):
        if not platform.platform().startswith('Linux') and not platform.platform().startswith('Windows'):
            raise Exception('not supported platform')

        native_git_path = os.path.join(path, '.git')
        fixed_git_path = os.path.join(path, '.kaleido-git')
        if not os.path.exists(native_git_path):
            raise Exception('.git does not exist')
        if not os.path.exists(fixed_git_path):
            raise Exception('.kaleido-git does not exist')

        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())

        # remove previous entries in kaleido repository (sync is required to commit changes)
        self.gu.call(['rm', '--cached', '-r', '--ignore-unmatch', path], False)

        # remove the symlink and restore the original .git directory name
        if platform.platform().startswith('Linux'):
            os.unlink(native_git_path)
        elif platform.platform().startswith('Windows'):
            os.rmdir(native_git_path)

        os.rename(fixed_git_path, native_git_path)

        return True

    def git_command(self, args):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        return self.gu.execute(args)[0]


def print_help():
    options = Options()
    print('usage: %s [OPTIONS] ' \
          '{ init | clone REPOSITORY | beacon | serve ADDRESS:PORT | ' \
            'squash | sync | sync-forever | ' \
            'fix-git PATH | unfix-git PATH | ' \
            '| GIT-COMMAND }' % sys.argv[0])
    print()
    print('Options:')
    print('  -h                  Show this help message and exit')
    print('  -g GIT              Set the git executable path [default: %s]' % options.git)
    print('  -m META             Set the git metadata directory name [default: %s]' % options.meta)
    print('  -w WORKING_COPY     Set the working copy path [default: %s]' % options.working_copy)
    print('  -i INTERVAL         Set the minimum sync interval in sync-forever [default: %s sec]' % \
          options.sync_interval)
    print('  -p                  Force using polling to detect local changes')
    print('  -P                  Force using polling to detect remote changes')
    print('  -b ADDRESS:PORT     Specify the beacon TCP address [default: %s:%d]' % options.beacon_address)
    print('  -B                  Enable the beacon server in sync-forever')
    print('  -t TIMEOUT          Set the beacon connection timeout [default: %s sec]' % options.beacon_timeout)
    print('  -k INTERVAL         Set the beacon connection keepalive interval [default: %s sec]' % \
          options.beacon_keepalive)
    print('  -T INTERVAL         Set the minimum interval for beacon reconnection [default: %s sec]' % \
          options.reconnect_interval)
    print('  -y ADDRESS:PORT     Specify the internal control UDP address [default: %s:%d]' % options.control_address)
    print('  -u                  Do not add new files automatically in sync-forever')
    print('  -D                  Allow destructive operations')
    print('  -c COMMAND          Run a user command after sync')
    print('  -q                  Be less verbose')
    print()
    print('Commands:')
    print('  init                Initialize a new kaleido sync')
    print('  clone REPOSITORY    Clone the existing kaleido sync')
    print('  beacon              Run the beacon server')
    print('  serve ADDRESS:PORT  Run the git daemon')
    print('  squash              Purge the sync history')
    print('  sync                Sync once')
    print('  sync-forever        Sync continuously')
    print('  fix-git PATH        Fix the nested git repository to work with kaleido sync')
    print('  unfix-git PATH      Restore the original nested git repository')
    print('  GIT-COMMAND         Execute a custom git command')

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
            options.sync_interval = float(args[1])
            args = args[2:]
        elif args[0] == '-p':
            options.local_polling = True
            args = args[1:]
        elif args[0] == '-P':
            options.remote_polling = True
            args = args[1:]
        elif args[0] == '-b':
            address, port = args[1].split(':', 1)
            options.beacon_address = (address, int(port))
            args = args[2:]
        elif args[0] == '-B':
            options.beacon_listen = True
            args = args[1:]
        elif args[0] == '-t':
            options.beacon_timeout = float(args[1])
            args = args[2:]
        elif args[0] == '-k':
            options.beacon_keepalive = float(args[1])
            args = args[2:]
        elif args[0] == '-T':
            options.reconnect_interval = float(args[1])
            args = args[2:]
        elif args[0] == '-y':
            address, port = args[1].split(':', 1)
            options.control_address = (address, int(port))
            args = args[2:]
        elif args[0] == '-u':
            options.update_only = True
            args = args[1:]
        elif args[0] == '-D':
            options.allow_destructive = True
            args = args[1:]
        elif args[0] == '-c':
            options.command_after_sync = args[1]
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
            raise Exception('%s directory already exists' % options.meta)
        ret = True if Kaleido(options).init() else False
    elif command == 'clone':
        if len(args) < 1:
            raise Exception('too few arguments')
        elif os.path.exists(os.path.join(options.working_copy, options.meta)):
            raise Exception('%s directory already exists' % options.meta)
        path = args[0]
        ret = True if Kaleido(options).clone(path) else False
    elif command == 'serve':
        if len(args) < 1:
            raise Exception('too few arguments')
        address, port = args[0].split(':', 1)
        ret = True if Kaleido(options).serve(address, int(port)) else False
    elif command == 'beacon':
        options.beacon_listen = True
        options.working_copy = 'beacon'
        ret = True if Kaleido(options).beacon() else False
    elif command == 'squash':
        ret = True if Kaleido(options).squash() else False
    elif command == 'sync':
        ret = True if Kaleido(options).sync() else False
    elif command == 'sync-forever':
        ret = True if Kaleido(options).sync_forever() else False
    elif command == 'fix-git':
        if len(args) < 1:
            raise Exception('too few arguments')
        path = args[0]
        ret = True if Kaleido(options).fix_git(path) else False
    elif command == 'unfix-git':
        if len(args) < 1:
            raise Exception('too few arguments')
        path = args[0]
        ret = True if Kaleido(options).unfix_git(path) else False
    else:
        ret = Kaleido(options).git_command([command] + args)

    return ret

if __name__ == '__main__':
    sys.exit(main())
