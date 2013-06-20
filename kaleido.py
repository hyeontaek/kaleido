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
if platform.platform().startswith('Windows'):
    import win32file
    import win32con


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
        self.ignore_git_ignore = False
        self.allow_destructive = False
        self.command_after_sync = None
        self.quiet = False

    def meta_path(self):
        return os.path.join(self.working_copy_root, self.meta)

    def msg_prefix(self):
        if self.working_copy_root != None:
            prefix = self.working_copy_root + ': '
        else:
            prefix = os.path.abspath(self.working_copy) + ': '
        if len(prefix) < 20:
            prefix = '%-20s' % prefix
        else:
            prefix = prefix[:7] + '...' + prefix[-10:]
        return prefix


class GitUtil:
    def __init__(self, options):
        self.options = options
        self.common_args = []

    def set_common_args(self, common_args):
        self.common_args = common_args[:]

    def _copy_output(self, src, dest, tee=None):
        while True:
            line = src.readline(4096).decode(sys.getdefaultencoding())
            if not line:
                break
            if dest:
                dest.write(line)
            if tee:
                # TODO: the following may cause UnicodeEncodeError on Windows
                #       when the line contains characters incompatible to the console's coding
                tee.write(self.options.msg_prefix() + '  ' + line)
        src.close()

    def call(self, args, must_succeed=True):
        #if not self.options.quiet:
        #    print(self.options.msg_prefix() + '  $ ' + ' '.join([self.options.git] + self.common_args + args))
        proc = subprocess.Popen([self.options.git] + self.common_args + args,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdin.close()

        threads = []
        stdout_buf = io.StringIO()
        stderr_buf = None
        tee_stdout = None if self.options.quiet else sys.stdout
        tee_stderr = None if self.options.quiet else sys.stderr
        threads.append(threading.Thread(target=self._copy_output, args=(proc.stdout, stdout_buf, tee_stdout)))
        threads.append(threading.Thread(target=self._copy_output, args=(proc.stderr, stderr_buf, tee_stderr)))
        for thread in threads:
            thread.start()
        ret = proc.wait()
        for thread in threads:
            thread.join()

        if must_succeed and ret != 0:
            raise RuntimeError('git returned %d' % ret)

        return (ret, stdout_buf.getvalue())

    def call_generic(self, args, must_succeed=True):
        #if not self.options.quiet:
        #    print(self.options.msg_prefix() + '  $ ' + ' '.join(args))
        proc = subprocess.Popen(args,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdin.close()

        threads = []
        stdout_buf = io.StringIO()
        stderr_buf = None
        tee_stdout = None if self.options.quiet else sys.stdout
        tee_stderr = None if self.options.quiet else sys.stderr
        threads.append(threading.Thread(target=self._copy_output, args=(proc.stdout, stdout_buf, tee_stdout)))
        threads.append(threading.Thread(target=self._copy_output, args=(proc.stderr, stderr_buf, tee_stderr)))
        for thread in threads:
            thread.start()
        ret = proc.wait()
        for thread in threads:
            thread.join()

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
        version = tuple([int(field) for field in version.split('.')[:3]])
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
            yield   # empty generator
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
        t_str = ''
        for timeunit, name_s, name_p in TimeUtil._time_units:
            if diff >= timeunit:
                coeff = int(diff / timeunit)
                diff -= coeff * timeunit
                t_str += '%d %s ' % (coeff, name_s if coeff == 1 else name_p)
        return t_str.rstrip()


class RestartableTimer:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args if args != None else ()
        self.kwargs = kwargs if kwargs != None else {}
        self.fire_at = -1
        self.good_to_start = threading.Event()
        self.thread = threading.Thread(target=self._main)
        self.thread.daemon = True
        self.thread.start()

    def start(self):
        #print('timer start')
        now = time.time()
        self.fire_at = now + self.interval
        self.good_to_start.set()

    def cancel(self):
        self.fire_at = -1
        self.good_to_start.clear()

    def _main(self):
        while True:
            self.good_to_start.wait()
            while True:
                fire_at = self.fire_at
                now = time.time()
                if fire_at > now:
                    time.sleep(fire_at - now)
                else:
                    break
            self.good_to_start.clear()
            if fire_at >= 0:
                #print('timer fired')
                self.good_to_start.clear()
                self.function(*self.args, **self.kwargs)


class LocalChangeMonitor:
    def __init__(self, options, event):
        self.options = options
        self.use_polling = self.options.local_polling
        self.running = False
        self.exiting = False
        self.flag = False
        self.event = event
        self.proc = None
        self.thread = None
        self.handle = None
        self.flag_set_timer = RestartableTimer(0.1, self._on_flag_set_timer)

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
                self.proc = subprocess.Popen(['inotifywait', '--monitor', '--recursive', '--quiet',
                                              '-e', 'attrib', '-e', 'close_write',
                                              '-e', 'move', '-e', 'create', '-e', 'delete',
                                              '--format', '%w%f',
                                              self.options.working_copy_root],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                self.proc.stdin.close()
                self.thread = threading.Thread(target=self._inotifywait_handler, args=())
                self.thread.start()
            elif platform.platform().startswith('Windows'):
                print(self.options.msg_prefix() + 'monitoring local changes in %s' % self.options.working_copy_root)
                # FILE_LIST_DIRECTORY = 1 (for the second argument)
                self.handle = win32file.CreateFile(self.options.working_copy_root, 1,
                                                   win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE |
                                                   win32con.FILE_SHARE_DELETE,
                                                   None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS,
                                                   None)
                self.thread = threading.Thread(target=self._read_directory_changes_handler, args=())
                self.thread.start()
            else:
                # TODO: support Kevent for BSD
                self.use_polling = True
        if self.use_polling:
            print(self.options.msg_prefix() + \
                  'monitoring local changes in %s (polling)' % self.options.working_copy_root)
        self.running = True

    def stop(self):
        assert self.running
        self.exiting = True
        self.running = False
        if not self.use_polling:
            if platform.platform().startswith('Linux'):
                self.proc.terminate()
                # the following is skipped for faster termination
                #self.proc.wait()
                #self.thread.join()
            elif platform.platform().startswith('Windows'):
                win32file.CloseHandle(self.handle)
                # the following is skipped for faster termination
                #self.thread.join()

    def _on_flag_set_timer(self):
        self.flag = True
        self.event.set()

    def _inotifywait_handler(self):
        meta_path = self.options.meta_path()
        while not self.exiting:
            path = self.proc.stdout.readline(4096).strip().decode(sys.getdefaultencoding())
            if not path:
                break
            try:
                if path.startswith(meta_path):
                    continue
                if os.path.isdir(path):
                    continue
                if os.path.basename(path) == '.git':
                    continue
                #print(self.options.msg_prefix() + path)
                self.flag_set_timer.start()
            except ValueError:
                pass
        self.proc.stdout.close()

    def _read_directory_changes_handler(self):
        meta_path = self.options.meta_path()
        while not self.exiting:
            results = win32file.ReadDirectoryChangesW(self.handle, 1024, True,
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
                if os.path.isdir(path):
                    continue
                if os.path.basename(path) == '.git':
                    continue
                #print(self.options.msg_prefix() + path)
                self.flag_set_timer.start()
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
        self.peers = None
        self.thread = None
        self.sock_server = None
        self.sock_control_server = None
        self.sock_control_client = None

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
            self.sock_control_client.sendto(b'w', self.control_address)

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
                self.sock_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock_server.bind(self.beacon_address)
                self.sock_server.listen(5)
                print(self.options.msg_prefix() + 'beacon server listening at %s:%d' % self.beacon_address)
            self.sock_control_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock_control_server.bind(self.control_address)
            self.control_address = self.sock_control_server.getsockname()
            self.sock_control_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.thread = threading.Thread(target=self._handler, args=())
            self.thread.start()
        self.running = True

    def stop(self):
        assert self.running
        self.exiting = True
        if not self.use_polling:
            self.sock_control_client.sendto(b'w', self.control_address)
            self.thread.join()
            if self.beacon_listen:
                self.sock_server.close()
            for peer in self.peers:
                peer.sock.close()
            self.sock_control_client.close()
            self.sock_control_server.close()
        self.running = False

    class _Peer:
        def __init__(self, sock, addr, buf, last_recv, last_send, connecting):
            self.sock = sock 
            self.addr = addr
            self.buf = buf
            self.last_recv = last_recv
            self.last_send = last_send
            self.connecting = connecting

    def _find_peer_idx(self, sock):
        for idx, peer in enumerate(self.peers):
            if peer.sock == sock:
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
                    for peer in self.peers:
                        if peer.buf:
                            can_exit = False
                            break
                if can_exit:
                    break

            now = time.time()
            if not self.beacon_listen and not self.peers and \
               now - last_connect_attempt >= self.options.reconnect_interval:
                last_connect_attempt = now
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                print(self.options.msg_prefix() + 'connecting to beacon server %s:%d' % self.beacon_address)
                try:
                    sock.connect(self.beacon_address)
                except socket.error:
                    pass
                self.peers.append(self._Peer(sock, self.beacon_address, b'k', now, now, True))
                self.flag = True    # assume changes because we may have missed signals
                self.event.set()

            socks_to_read = [self.sock_control_server] + [peer.sock for peer in self.peers]
            if self.beacon_listen:
                socks_to_read += [self.sock_server]
            socks_to_write = []
            socks_to_check_error = [peer.sock for peer in self.peers]
            for idx, peer in enumerate(self.peers):
                if peer.buf:
                    socks_to_write.append(peer.sock)
            peers_to_close = []

            if socks_to_read or socks_to_write:
                rlist, wlist, elist = select.select(socks_to_read, socks_to_write, socks_to_check_error,
                                                    self.options.beacon_keepalive)
            else:
                rlist, wlist, elist = [], [], []

            now = time.time()
            for sock in rlist:
                if sock == self.sock_control_server:
                    # no-op; the purpose of the control message is just to escape select()
                    msg = sock.recvfrom(1)
                elif self.beacon_listen and sock == self.sock_server:
                    s_new_client, addr = self.sock_server.accept()
                    now = time.time()
                    print(self.options.msg_prefix() + 'connection from %s:%d established' % addr)
                    try:
                        s_new_client.setblocking(0)
                        s_new_client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except socket.error:
                        pass
                    self.peers.append(self._Peer(s_new_client, addr, b'k', now, now, False))
                else:
                    idx = self._find_peer_idx(sock)
                    assert idx != -1
                    peer = self.peers[idx]
                    try:
                        msg = sock.recv(1)
                        if msg:
                            peer.last_recv = now
                    except socket.error:
                        msg = None
                    if msg and peer.connecting:
                        peer.connecting = False
                        print(self.options.msg_prefix() + 'connection to %s:%d established' % peer.addr)
                    if msg == b'k':
                        # keepalive
                        #print(self.options.msg_prefix() + 'keepalive from %s:%d' % peer.addr)
                        pass
                    elif msg == b'c':
                        # changes
                        self.flag = True
                        self.event.set()
                        if self.beacon_listen:
                            # broadcast except the source
                            if not self.options.quiet:
                                print(self.options.msg_prefix() + \
                                      'notifying %d peers for remote changes' % (len(self.peers) - 1))
                            for peer2 in self.peers:
                                if peer != peer2:
                                    peer2.buf = b'c'
                                    peer2.last_send = now
                    else:
                        if msg:
                            print(self.options.msg_prefix() + 'unexpected response from %s:%d' % peer.addr)
                        peers_to_close.append(peer)

            for sock in wlist:
                idx = self._find_peer_idx(sock)
                assert idx != -1
                peer = self.peers[idx]
                try:
                    wrote_len = peer.sock.send(peer.buf)
                    peer.buf = peer.buf[wrote_len:]
                except socket.error:
                    pass

            for sock in elist:
                idx = self._find_peer_idx(sock)
                assert idx != -1
                peer = self.peers[idx]
                peers_to_close.append(peer)

            if self.need_to_send_signal:
                # broadcast
                if not self.options.quiet:
                    print(self.options.msg_prefix() + 'notifying %d peers for local changes' % len(self.peers))
                for peer in self.peers:
                    peer.buf = b'c'
                    peer.last_send = now
                self.need_to_send_signal = False

            for peer in self.peers:
                if now - peer.last_recv > self.options.beacon_timeout:
                    print(self.options.msg_prefix() + 'connection to %s:%d timeout' % peer.addr)
                    peers_to_close.append(peer)
                elif now - peer.last_send > self.options.beacon_keepalive:
                    if not peer.buf:
                        peer.buf = b'k'
                    peer.last_send = now

            for peer in peers_to_close:
                if peer in self.peers:
                    print(self.options.msg_prefix() + 'connection to %s:%d closed' % peer.addr)
                    if peer.buf and self.beacon_listen:
                        # if we could not send a signal because the socket is closed,
                        # retry again with a new connection socket
                        # we do not need this on a beacon server because the clients will assume
                        # remote changes when they reconnect to the server
                        self.need_to_send_signal = True
                    self.peers.remove(peer)
                    peer.sock.close()


class Kaleido:
    def __init__(self, options):
        self.options = options
        self.gu = GitUtil(self.options)

    def _reset_config(self):
        # turn off cr/lf conversion
        self.gu.call(['config', 'core.autocrlf', 'false'])

    def init(self):
        inbox_id = '%d_%d' % (time.time(), random.randint(0, 999999))
        self.gu.set_common_args([])
        self.gu.call(['init', '--bare', os.path.join(self.options.working_copy, self.options.meta)])
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        self.gu.call(['config', 'core.bare', 'false'])
        self._reset_config()
        self.gu.call(['commit', '--author=%s <%s@%s>' % (getpass.getuser(), getpass.getuser(), platform.node()),
                      '--message=', '--allow-empty-message', '--allow-empty'])
        meta_path = self.options.meta_path()
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write(self.options.meta + '\n')
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write('.git' + '\n')
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
        self._reset_config()
        self.gu.call(['config', 'remote.origin.url', url])
        meta_path = self.options.meta_path()
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write(self.options.meta + '\n')
        open(os.path.join(meta_path, 'info', 'exclude'), 'at').write('.git' + '\n')
        open(os.path.join(meta_path, 'git-daemon-export-ok'), 'wt')
        open(os.path.join(meta_path, 'inbox-id'), 'wt').write(inbox_id + '\n')
        self.gu.call(['checkout'])
        return True

    def reset_config(self):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        self._reset_config()

    def beacon(self):
        event = threading.Event()
        remote_change_monitor = RemoteChangeMonitor(self.options, event)
        remote_change_monitor.start()
        try:
            while True:
                time.sleep(60)
        finally:
            remote_change_monitor.stop()
        return True

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
        # TODO: delete all non-master branch
        self.gu.call(['repack', '-A', '-d'])
        self.gu.call(['prune'])

        event = threading.Event()
        remote_change_monitor = RemoteChangeMonitor(self.options, event)
        remote_change_monitor.start()
        try:
            remote_change_monitor.after_sync()
        finally:
            remote_change_monitor.stop()
        return True

    def sync(self):
        return self._sync(False)

    def sync_forever(self):
        return self._sync(True)

    _no_change_notifications = [
            (24 * 60 * 60), (12 * 60 * 60), ( 6 * 60 * 60),
            (     60 * 60), (     30 * 60), (     10 * 60),
            (          60), (          30), (          10),
        ]

    def _add_changes(self):
        # this function basically does git add --all, except
        #   it uses .kaleido-ignore to ignore files in addition to .gitignore files
        #   ignore all git submodules (by avoid using directory names when adding/removing)
        #self.gu.call(['add', '--all'], False)

        info_exclude_path = os.path.join(self.options.working_copy_root, '.kaleido/info/exclude')
        exclude_args = ['--exclude=.kaleido', '--exclude=.git', '--exclude-from=' + info_exclude_path]
        if not self.options.ignore_git_ignore:
            exclude_args.append('--exclude-standard')
        kaleido_ignore_path = os.path.join(self.options.working_copy_root, '.kaleido-ignore')
        if os.path.exists(kaleido_ignore_path):
            exclude_args.append('--exclude-from=' + kaleido_ignore_path)

        # find new or modified files
        to_add = self.gu.call(['ls-files', '--modified', '--others', '-z'] + exclude_args + \
                              [self.options.working_copy_root], False)[1]
        for path in to_add.split('\0'):
            if not path:
                continue
            if os.path.isdir(os.path.join(self.options.working_copy_root, path)):
                continue
            self.gu.call(['add', '--force', path], False)

        # find removed files
        to_rm = self.gu.call(['ls-files', '--deleted', '-z'] + exclude_args + \
                             [self.options.working_copy_root], False)[1]
        for path in to_rm.split('\0'):
            if not path:
                continue
            if os.path.isdir(os.path.join(self.options.working_copy_root, path)):
                continue
            self.gu.call(['rm', '--cached', path], False)

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

        self.repair_keleido_git_links()

        try:
            prev_last_change = self.gu.get_last_commit_time()
            last_diff = 0
            first_sync = True

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
                    self._add_changes()

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
                        remote_head = self.gu.call(['merge-base', branch, branch], False)[1]
                        has_common_ancestor, common_ancestor = self.gu.call(['merge-base', 'master', branch], False)
                        if has_common_ancestor == 0:    # 0 is the exit code for a success
                            if remote_head == common_ancestor:
                                # optimization; if the local master has the remote master as a common ancestor,
                                # the remote master is just lagging behind and will catch up the local master
                                # then, the local side just waits for the remote to pull more commits
                                pass
                            else:
                                # typical merge---merge local master with the origin
                                self.gu.call(['merge', '--strategy=recursive'] + git_strategy_option + [branch], False)
                            # we should not delete the merged branch because later pushes onto this branch will be
                            # efficiently done; if we delete the branch, each push will incur (almost) the full repository
                            # retransmission
                            # self.gu.call(['branch', '--delete', branch], False)
                            pass
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
                                    # TODO: delete all non-master branch
                                    self.gu.call(['repack', '-A', '-d'], False)
                                    self.gu.call(['prune'], False)
                                if not succeeding:
                                    print(self.options.msg_prefix() + 'failed to propagate squash')
                        else:
                            # ignore squash from non-origin sources
                            # # branch -D is destructive,
                            # # but this is quite safe when performed only on a local copy of others' branch
                            # # do not delete branch to possibly help later push become efficient
                            # we should not delete the merged branch (see above)
                            # self.gu.call(['branch', '-D', branch], False)
                            pass

                    if last_commit_id != self.gu.get_last_commit_id():
                        changed = True

                if local_op or remote_op:
                    self.repair_keleido_git_links()

                # push if there are any changes or in the first sync trial (the last run may have missed a push)
                if changed or first_sync:
                    # push local master to remote sync_inbox_ID for remote merge
                    if has_origin:
                        self.gu.call(['push', '--force', 'origin', 'master:sync_inbox_%s' % inbox_id], False)

                    # send beacon signal
                    remote_change_monitor.after_sync()

                    last_change = self.gu.get_last_commit_time()
                else:
                    last_change = prev_last_change

                # detect and print the last change time
                now = time.time()
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
                            if not self.options.quiet:
                                print(self.options.msg_prefix() + 'no changes in ' + TimeUtil.get_timediff_str(last_diff))
                            break
                else:
                    last_diff = now - prev_last_change

                if changed:
                    if self.options.command_after_sync != None:
                        os.system(self.options.command_after_sync)

                if sync_forever:
                    time.sleep(self.options.sync_interval)
                    first_sync = False
                    continue
                else:
                    break
        #except KeyboardInterrupt:
        #    pass
        finally:
            remote_change_monitor.stop()
            local_change_monitor.stop()

        return True

    def link_kaleido_git(self, path):
        if not platform.platform().startswith('Linux') and not platform.platform().startswith('Windows'):
            raise Exception('not supported platform')

        native_git_path = os.path.join(path, '.git')
        fixed_git_path = os.path.join(path, '.kaleido-git')

        if platform.platform().startswith('Linux'):
            os.symlink('.kaleido-git', native_git_path)
        elif platform.platform().startswith('Windows'):
            self.gu.call_generic(['cmd', '/c', 'mklink', '/j', native_git_path, fixed_git_path])

    def unlink_kaleido_git(self, path):
        if not platform.platform().startswith('Linux') and not platform.platform().startswith('Windows'):
            raise Exception('not supported platform')

        native_git_path = os.path.join(path, '.git')

        if platform.platform().startswith('Linux'):
            os.unlink(native_git_path)
        elif platform.platform().startswith('Windows'):
            os.rmdir(native_git_path)

    def repair_keleido_git_links(self):
        try:
            for root, dirs, _ in os.walk(self.options.working_copy_root):
                if not os.path.exists(os.path.join(root, '.kaleido-git')):
                    continue

                if os.path.exists(os.path.join(root, '.git')):
                    self.unlink_kaleido_git(root)
                    dirs.remove('.git')

                self.link_kaleido_git(root)
                dirs.remove('.kaleido-git')
        except OSError:
            pass

    def track_git(self, path):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())

        for root, dirs, _ in os.walk(path):
            if not os.path.exists(os.path.join(root, '.git')):
                continue
            if os.path.exists(os.path.join(root, '.kaleido-git')):
                # already tracking - try to untrack first
                self._untrack_git(root)

            # do not recurse into it
            dirs.remove('.git')

            native_git_path = os.path.join(root, '.git')
            fixed_git_path = os.path.join(root, '.kaleido-git')

            # rename .git to ours so that git does not think this path as a submodule
            os.rename(native_git_path, fixed_git_path)

            # add a symlink from .git to .kaleido-git to make git continue to work
            self.link_kaleido_git(root)

            # add .kaleido-git/index
            # once this is done, the working copy will be tracked even though it has .git symlink
            self.gu.call(['add', os.path.join(fixed_git_path, 'index')])

        return True

    def untrack_git(self, path):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())

        self._untrack_git(path)

        return True

    def _untrack_git(self, path):
        for root, dirs, _ in os.walk(path):
            if not os.path.exists(os.path.join(root, '.git')):
                continue
            if not os.path.exists(os.path.join(root, '.kaleido-git')):
                # not tracking
                continue

            # do not recurse into them
            dirs.remove('.git')
            dirs.remove('.kaleido-git')

            native_git_path = os.path.join(root, '.git')
            fixed_git_path = os.path.join(root, '.kaleido-git')

            # remove the symlink and restore the original .git directory name
            self.unlink_kaleido_git(root)

            # restore original .git name
            os.rename(fixed_git_path, native_git_path)

            # exclude all files of .kaleido-git as well as its working copy
            self.gu.call(['rm', '-r', '--cached', '--ignore-unmatch', root])

    def git_command(self, args):
        self.gu.detect_working_copy_root()
        self.gu.set_common_args(self.gu.get_path_args())
        return self.gu.execute(args)[0]


def print_help():
    options = Options()
    print('usage: %s [OPTIONS] ' \
          '{ init | clone REPOSITORY | reset-config | beacon | serve ADDRESS:PORT | ' \
            'squash | sync | sync-forever | ' \
            'track-git PATH | untrack-git PATH | ' \
            '[--] GIT-COMMAND }' % sys.argv[0])
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
    print('  -i                  Ignore standard git ignore files')
    print('  -D                  Allow destructive operations')
    print('  -c COMMAND          Run a user command after sync')
    print('  -q                  Be less verbose')
    print()
    print('Commands:')
    print('  init                Initialize a new kaleido sync')
    print('  clone REPOSITORY    Clone the existing kaleido sync')
    print('  reset-config        Reset the git config of the kaleido sync')
    print('  beacon              Run the beacon server')
    print('  serve ADDRESS:PORT  Run the git daemon')
    print('  squash              Purge the sync history')
    print('  sync                Sync once')
    print('  sync-forever        Sync continuously')
    print('  track-git PATH      Include git repositories under PATH for sync')
    print('  untrack-git PATH    Exclude git repositories under PATH for sync')
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
            del args[0:2]
        elif args[0] == '-m':
            options.meta = args[1]
            del args[0:2]
        elif args[0] == '-w':
            options.working_copy = args[1]
            del args[0:2]
        elif args[0] == '-i':
            options.sync_interval = float(args[1])
            del args[0:2]
        elif args[0] == '-p':
            options.local_polling = True
            del args[0:1]
        elif args[0] == '-P':
            options.remote_polling = True
            del args[0:1]
        elif args[0] == '-b':
            address, port = args[1].split(':', 1)
            options.beacon_address = (address, int(port))
            del args[0:2]
        elif args[0] == '-B':
            options.beacon_listen = True
            del args[0:1]
        elif args[0] == '-t':
            options.beacon_timeout = float(args[1])
            del args[0:2]
        elif args[0] == '-k':
            options.beacon_keepalive = float(args[1])
            del args[0:2]
        elif args[0] == '-T':
            options.reconnect_interval = float(args[1])
            del args[0:2]
        elif args[0] == '-y':
            address, port = args[1].split(':', 1)
            options.control_address = (address, int(port))
            del args[0:2]
        elif args[0] == '-i':
            options.ignore_git_ignore = True
            del args[0:1]
        elif args[0] == '-D':
            options.allow_destructive = True
            del args[0:1]
        elif args[0] == '-c':
            options.command_after_sync = args[1]
            del args[0:2]
        elif args[0] == '-q':
            options.quiet = True
            del args[0:1]
        elif args[0] == '--':
            del args[0:1]
            break
        else:
            break

    command = args[0]
    del args[0:1]

    if command == 'init':
        if os.path.exists(os.path.join(options.working_copy, options.meta)):
            raise Exception('%s directory already exists' % options.meta)
        ret = 0 if Kaleido(options).init() else 1
    elif command == 'clone':
        if len(args) < 1:
            raise Exception('too few arguments')
        elif os.path.exists(os.path.join(options.working_copy, options.meta)):
            raise Exception('%s directory already exists' % options.meta)
        path = args[0]
        ret = 0 if Kaleido(options).clone(path) else 1
    elif command == 'reset-config':
        ret = 0 if Kaleido(options).reset_config() else 1
    elif command == 'serve':
        if len(args) < 1:
            raise Exception('too few arguments')
        address, port = args[0].split(':', 1)
        ret = 0 if Kaleido(options).serve(address, int(port)) else 1
    elif command == 'beacon':
        options.beacon_listen = True
        options.working_copy = 'beacon'
        ret = 0 if Kaleido(options).beacon() else 1
    elif command == 'squash':
        ret = 0 if Kaleido(options).squash() else 1
    elif command == 'sync':
        ret = 0 if Kaleido(options).sync() else 1
    elif command == 'sync-forever':
        ret = 0 if Kaleido(options).sync_forever() else 1
    elif command == 'track-git':
        if len(args) < 1:
            raise Exception('too few arguments')
        path = args[0]
        ret = 0 if Kaleido(options).track_git(path) else 1
    elif command == 'untrack-git':
        if len(args) < 1:
            raise Exception('too few arguments')
        path = args[0]
        ret = 0 if Kaleido(options).untrack_git(path) else 1
    else:
        ret = Kaleido(options).git_command([command] + args)

    return ret

if __name__ == '__main__':
    sys.exit(main())
