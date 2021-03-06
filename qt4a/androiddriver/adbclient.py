# -*- coding: UTF-8 -*-
# 
# Tencent is pleased to support the open source community by making QTA available.
# Copyright (C) 2016THL A29 Limited, a Tencent company. All rights reserved.
# Licensed under the BSD 3-Clause License (the "License"); you may not use this 
# file except in compliance with the License. You may obtain a copy of the License at
# 
# https://opensource.org/licenses/BSD-3-Clause
# 
# Unless required by applicable law or agreed to in writing, software distributed 
# under the License is distributed on an "AS IS" basis, WITHOUT WARRANTIES OR CONDITIONS
# OF ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.
# 

'''ADB客户端，用于与ADB守护进程通信
'''

# 2013/9/16 apple 创建
# 2014/11/6 apple 接口修改，与adb命令保持一致

import os
import time
import socket, select
import struct
import threading
import cStringIO
from util import logger, TimeoutError

SYNC_DATA_MAX = 64 * 1024

class AdbError(RuntimeError):
    pass

class Pipe(object):
    '''模拟实现内存管道
    '''
    def __init__(self):
        self._buffer = cStringIO.StringIO()
        self._max_buffer_size = 4096 * 16
        self._lock = threading.Lock()
        self._pos = 0  # 当前读指针位置
        
    def write(self, s):
        with self._lock:
            self._buffer.seek(0, 2)  # 将指针置于尾部
            self._buffer.write(s)
    
    def readline(self):
        wait = False
        while True:
            if wait: time.sleep(0.1)
            with self._lock:
                self._buffer.seek(0, 2)
                buffer_size = self._buffer.tell()
                if buffer_size <= self._pos:
                    wait = True
                    continue
            with self._lock:
                self._buffer.seek(self._pos)
                ret = self._buffer.readline()
                if len(ret) == 0:
                    wait = True
                    continue
                else:
                    self._pos = self._buffer.tell()
                    self._buffer.seek(0, 2)
                    buffer_size = self._buffer.tell()
                    if buffer_size >= self._max_buffer_size:
                        # 创建新的缓冲区
                        self._buffer.seek(self._pos)
                        buffer = self._buffer.read()
                        self._buffer.close()
                        self._buffer = cStringIO.StringIO()
                        self._buffer.write(buffer)
                        self._pos = 0
                    return ret
    
    def read(self):
        '''读取管道中的所有数据
        '''
        with self._lock:
            self._buffer.seek(self._pos)
            return self._buffer.read()
    
class ADBPopen(object):
    '''与Popen兼容
    '''
    class StdinPipe(object):
        '''
        '''
        def __init__(self, sock):
            self._sock = sock
            
        def write(self, s):
            # print 'send', repr(s)
            self._sock.send(s)
        
        def flush(self):
            pass

    def __init__(self, sock, timeout=None):
        self._sock = sock
        self._stdin = self.StdinPipe(sock)
        self._stdout = Pipe()
        self._stderr = Pipe()
        self._running = True
        self._timeout = timeout
        if self._timeout == None: self._timeout = 0xFFFFFFFF
        self._thread = threading.Thread(target=self._work_thread, args=(), name=self.__class__.__name__)
        self._thread.setDaemon(True)
        self._thread.start()
        
    @property
    def stdin(self):
        return self._stdin
    
    @property
    def stdout(self):
        return self._stdout
    
    @property
    def stderr(self):
        return self._stderr
    
    @property
    def pid(self):
        return self._thread.ident
    
    def _work_thread(self):
        time0 = time.time()
        while self._running and time.time() - time0 < self._timeout:
            infds, outfds, errfds = select.select([self._sock, ], [], [], 1)
            if len(infds) > 0:
                try:
                    buff = self._sock.recv(4096)
                    if len(buff) == 0: 
                        self._sock.close()
                        self._running = False
                        return
                    self._stdout.write(buff)
                except socket.error, e:
                    logger.info("接收返回数据错误： %s" % (e))
#                    import traceback
#                    traceback.print_exc()
                    self._stdout.write(' ')  # 通知接收方退出
                    self._sock.close()
                    self._running = False
                    return
        self._sock.close()
        
    def poll(self):
        '''是否存在
        '''
        if self._thread.is_alive():
            return None
        else:
            return 0
    
    def terminate(self):
        '''结束
        '''
        self._running = False
        time.sleep(1)  # 等待线程退出
    
    def communicate(self):
        '''
        '''
        while True:
            if self.poll() == 0: 
                if self._running: raise TimeoutError('execute timeout')
                return self.stdout.read(), self.stderr.read()
            time.sleep(0.1)
            
class ADBClient(object):
    '''
    '''
    instance_dict = {}
    
    def __init__(self, server_addr='127.0.0.1', server_port=5037):
        self._server_addr = server_addr
        self._server_port = server_port
        self._sock = None
        
    @staticmethod
    def get_client(host, port=5037):
        '''根据主机名获取ADBClient实例
        '''
        return ADBClient(host, port)

    def call(self, cmd, *args, **kwds):
        '''调用命令字
        '''
        cmd = cmd.replace('-', '_')
        method = getattr(self, cmd)
        # print args
        sync = True
        if kwds.has_key('sync'): sync = kwds.pop('sync')
        if kwds.has_key('timeout') and not cmd in ('shell', 'install', 'uninstall', 'wait_for_device', 'reboot'): kwds.pop('timeout')
        if sync: 
            ret = None
            retry_count = kwds.pop('retry_count')
            i = 0
            socket_error_count = 0
            while i < retry_count:
                try:
                    ret = method(*args, **kwds)
                    break
                except socket.error, e:
                    logger.exception(u'执行%s %s error' % (cmd, ' '.join(args)))
                    socket_error_count += 1
                    if socket_error_count <= 10: i -= 1
                    time.sleep(1)
                except AdbError, e:
                    if 'device not found' in str(e):
                        return '', 'error: device not found'
                    elif 'cannot bind to socket' in str(e):
                        return '', str(e)
                    elif 'device offline' in str(e):
                        return '', 'error: device offline'
                    elif 'Bad response' in str(e) or 'Device or resource busy' in str(e):
                        # 需要重试
                        logger.exception(u'执行%s%s %r' % (cmd, ' '.join(args), e))
                    else:
                        raise RuntimeError(u'执行%s %s 命令失败：%s' % (cmd, ' '.join(args), e))
                    time.sleep(1)
                    if i >= retry_count - 1: raise e
                except RuntimeError, e:
                    logger.exception(u'执行%s%s %r' % (cmd, ' '.join(args), e))
                    if 'device not found' in str(e):
                        self.wait_for_device(args[0], retry_count=1, timeout=300)
                        self._sock = None
                        return self.call(cmd, *args, **kwds)
                i += 1
                self._sock = None
            if ret == None: raise TimeoutError(u'执行%s %s 命令失败' % (cmd, ' '.join(args)))

            if isinstance(ret, basestring):
                return ret, ''
            else:
                return ret
        else:
            self._transport(args[0])  # 异步操作的必然需要发送序列号
            if cmd == 'shell':
                self._send_command('shell:' + ' '.join(args[1:]))
                return ADBPopen(self._sock)
            
        
    def _connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for i in range(3):
            try:
                self._sock.connect((self._server_addr, self._server_port))
                return True
            except socket.error:
                pass
        return False

    def _check_status(self):
        '''检查返回状态
        '''
        stat = self._sock.recv(4)
        # print stat
        if stat == "OKAY":
            return True
        elif stat == "FAIL":
            size = int(self._sock.recv(4), 16)
            val = self._sock.recv(size)
            raise AdbError(val)
        else:
            raise AdbError("Bad response: %r" % (stat,))

    def _send_command(self, cmd):
        data = "%04x%s" % (len(cmd), cmd)
        if not self._sock: self._connect()
        # logger.debug('send: %r' % data)
        self._sock.send(data)
        return self._check_status()

    def _recv(self, size=None):
        '''从socket读取数据
        '''
        result = ''
        if size != None:
            while len(result) < size:
                result += self._sock.recv(size - len(result))
        else:
            data = self._sock.recv(4096)
            while data:
                result += data
                data = self._sock.recv(4096)
        return result

    def send_command(self, cmd):
        self._send_command(cmd)
        size = int(self._sock.recv(4), 16)
        resp = self._sock.recv(size)
        # logger.debug('recv: %r' % resp[:200])
        self._sock.close()
        self._sock = None
        return resp

    def _transport(self, device_id):
        self._send_command('host:transport:%s' % device_id)

#    def send_command2(self, device_id, cmd):
#        '''
#        '''
#        self._transport(device_id)
#        self._send_command(cmd)
#        data = self._sock.recv(4096)
#        # print 'data', data
#        result = ''
#        while data:
#            result += data
#            data = self._sock.recv(4096)
#        self._sock.close()
#        self._sock = None
#        return result

    def devices(self):
        '''adb devices
        '''
        result = self.send_command('host:devices')
        return result

    def shell(self, device_id, cmd, **kwds):
        '''adb shell
        '''
        cmd_line = 'shell:%s' % cmd
        self._transport(device_id)
        self._send_command(cmd_line)
        return ADBPopen(self._sock, timeout=kwds['timeout']).communicate()

    def _sync_read_mode(self, remote_path):
        '''
        '''
        data = 'STAT' + struct.pack('I', len(remote_path)) + remote_path
        self._sock.send(data)
        result = self._sock.recv(16)
        if result[:4] != 'STAT':
            raise AdbError('sync_read_mode error')
        mode, size, time = struct.unpack('III', result[4:])
        return mode, size, time

    def pull(self, device_id, src_file, dst_file):
        '''adb pull
        '''
        time0 = time.time()
        self._transport(device_id)
        self._send_command('sync:')
        mode, fsize, ftime = self._sync_read_mode(src_file)
        if mode == 0:
            self._sock.close()
            self._sock = None
            raise AdbError('remote object %r does not exist' % src_file)
        data = 'RECV' + struct.pack('I', len(src_file)) + src_file
        self._sock.send(data)
        f = open(dst_file, 'wb')
        data_size = 0
        last_data = ''
        while True:
            result = self._sock.recv(8)
            if len(result) != 8:
                logger.warn('返回数据错误：%r' % result)
                
            last_data += result
            if len(last_data) < 8: 
                continue
            else: 
                result = last_data[:8]
                last_data = last_data[8:]

            psize = struct.unpack('I', result[4:])[0]  # 每个分包大小
            # print psize
            if result[:4] == 'DONE': break
            elif result[:4] == 'FAIL':
                raise AdbError(self._sock.recv(psize))
            elif result[:4] != 'DATA':
                raise AdbError('pull_file error')

            # print fsize
            result = self._recv(psize - len(last_data))
            result = last_data + result
            if len(result) >= psize:
                last_data = result[psize:]
                result = result[:psize]
            else:
                raise ValueError('数据长度不一致,期望值:%d 实际值:%d' % (psize, len(result)))
            f.write(result)
            data_size += len(result)
            
        f.close()
        self._sock.send('QUIT' + struct.pack('I', 0))
        time_cost = time.time() - time0
        self._sock.close()
        self._sock = None
        if data_size > 0:
            return '%d KB/s (%d bytes in %fs)' % (int(data_size / 1000 / time_cost) if time_cost > 0 else 65535, data_size, time_cost)
        else:
            return ''
        
    def push(self, device_id, src_file, dst_file):
        '''adb push
        '''
        time0 = time.time()
        try:
            st = os.stat(src_file)
        except WindowsError, e:
            if e[0] == 2:
                raise AdbError("cannot stat '%s': No such file or directory" % src_file)
            else: raise e
        self._transport(device_id)
        self._send_command('sync:')
        mode, fsize, ftime = self._sync_read_mode(dst_file)
#         print mode
#         print st.st_mode
        s = '%s,%d' % (dst_file, st.st_mode)
        data = 'SEND' + struct.pack('I', len(s)) + s
        self._sock.send(data)
        f = open(src_file, 'rb')
        data = f.read(SYNC_DATA_MAX)
        data_size = 0
        while data:
            # print 'send', len(data)
            self._sock.send('DATA' + struct.pack('I', len(data)) + data)
            data_size += len(data)
            data = f.read(SYNC_DATA_MAX)
        f.close()
        data = 'DONE' + struct.pack('I', st.st_mtime)
        self._sock.send(data)
        result = self._sock.recv(8)
        if result[:4] == 'OKAY':
            self._sock.close()
            self._sock = None
            time_cost = time.time() - time0
            return '%d KB/s (%d bytes in %fs)' % (int(data_size / 1000 / time_cost) if time_cost > 0 else 0, data_size, time_cost)
        elif result[:4] == 'FAIL':
            msg_len = struct.unpack('I', result[4:])[0]
            error_msg = self._sock.recv(msg_len)
            raise AdbError(error_msg)
        else:
            raise RuntimeError('unexpect data: %r' % result)
        
    def install(self, device_id, apk_path, args='', **kwds):
        '''adb install
        '''
        if not os.path.exists(apk_path):
            raise AdbError(r'can\'t find %r to install' % apk_path)
        apk_name = os.path.split(apk_path)[-1]
        dst_path = '/data/local/tmp/%s' % apk_name
        self.push(device_id, apk_path, dst_path)
        cmdline = 'pm install ' + (args + ' ' if args else '') + dst_path
        return self.shell(device_id, cmdline, **kwds)

    def uninstall(self, device_id, package_name, **kwds):
        '''adb uninstall
        '''
        cmd = 'pm uninstall %s' % package_name
        return self.shell(device_id, cmd, **kwds)

    def forward(self, device_id, local, remote):
        '''adb forward
        '''
        self._send_command('host-serial:%s:forward:%s;%s' % (device_id, local, remote))
        return ''
    
    def get_state(self, device_id):
        '''获取设备状态
        '''
        return self.send_command('host-serial:%s:get-state' % (device_id))
    
    def reboot(self, device_id, **kwds):
        '''重启设备
        '''
        self._transport(device_id)
        self._sock.settimeout(kwds['timeout'])
        try:
            self.send_command('reboot:')
        except socket.error, e:
            raise e
        except:
            pass
        return True
    
    def wait_for_device(self, device_id, **kwds):
        '''等待设备
        '''
        self._send_command('host-serial:%s:wait-for-any' % (device_id))
        return ADBPopen(self._sock, timeout=kwds['timeout']).communicate()
    
    def snapshot_screen(self, device_id):
        '''截屏
        return: Image.Image
        '''
        self._transport(device_id)
        self._send_command('framebuffer:')
        
        fb_desc = self._sock.recv(13 * 4)
        version = struct.unpack_from('I', fb_desc, 0)[0]
        bpp = struct.unpack_from('I', fb_desc, 4)[0]
        size = struct.unpack_from('I', fb_desc, 8)[0]
        width = struct.unpack_from('I', fb_desc, 12)[0]
        height = struct.unpack_from('I', fb_desc, 16)[0]
        red_offset = struct.unpack_from('I', fb_desc, 20)[0]
        red_length = struct.unpack_from('I', fb_desc, 24)[0]  # @UnusedVariable
        blue_offset = struct.unpack_from('I', fb_desc, 28)[0]
        blue_length = struct.unpack_from('I', fb_desc, 32)[0]  # @UnusedVariable
        green_offset = struct.unpack_from('I', fb_desc, 36)[0]
        green_length = struct.unpack_from('I', fb_desc, 40)[0]  # @UnusedVariable
        alpha_offset = struct.unpack_from('I', fb_desc, 44)[0]
        alpha_length = struct.unpack_from('I', fb_desc, 48)[0]

        if version != 1:
            raise AdbError("unsupported version of framebuffer: %s" % version)
        # detect order
        util_map = { red_offset: 'R', blue_offset: 'B', green_offset: 'G'}
        keys = util_map.keys()
        keys.sort()
        raw_mode = ''.join([util_map[it] for it in keys])
        
        # detect mode
        if alpha_length and alpha_offset:
            mode = 'RGBA'
            if bpp != 32:
                raise AdbError("unsupported RGBA mode, bpp is %s" % bpp)
            raw_mode += 'A'
            
        elif alpha_offset:
            mode = 'RGBX'
            if bpp != 32:
                raise AdbError("unsupported RGBX mode, bpp is %s" % bpp)
            raw_mode += 'X'
            
        else:
            mode = 'RGB'
            if bpp == 16:
                raw_mode += ';16'
            elif bpp == 24:
                pass
            else:
                raise AdbError("unsupported RGB mode, bpp is %s" % bpp)

        data = ''
        while len(data) < size:
            data += self._sock.recv(4096)

        from PIL import Image
        return Image.frombuffer(mode, (width, height), data, 'raw', raw_mode, 0, 1)
    
if __name__ == '__main__':
    client = ADBClient('127.0.0.1', 5037)
    
