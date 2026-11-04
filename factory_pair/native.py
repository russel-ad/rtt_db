"""ctypes boundary; all methods/callbacks run on the BLE asyncio owner."""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path

PTR=C.POINTER(C.c_uint8)
WRITE=C.CFUNCTYPE(C.c_int,PTR,C.c_uint16)
RANDOM=C.CFUNCTYPE(C.c_int,PTR,C.c_uint16)
RECEIVE=C.CFUNCTYPE(None,C.c_int,PTR,C.c_uint16)
DONE=C.CFUNCTYPE(None,C.c_uint32,C.c_int,C.c_uint8)


class NativeLink:
    def __init__(self,write,receive,done):
        base=Path(__file__).resolve().parent/'native'
        target=base/'factory_mlink.dll'
        report=json.loads((base/'build.json').read_text(encoding='utf-8'))
        if hashlib.sha256(target.read_bytes()).hexdigest()!=report['dll_sha256']:
            raise RuntimeError('出厂配对协议 DLL 校验失败，请重新安装完整工具')
        self.lib=C.CDLL(str(target))
        self.lib.fp_create.argtypes=[WRITE,RANDOM,RECEIVE,DONE]; self.lib.fp_create.restype=C.c_void_p
        for name in ('fp_destroy','fp_hello','fp_up'):
            getattr(self.lib,name).argtypes=[C.c_void_p]
        self.lib.fp_receive.argtypes=[C.c_void_p,PTR,C.c_uint16,C.c_uint32]
        self.lib.fp_poll.argtypes=[C.c_void_p,C.c_uint32]
        self.lib.fp_send.argtypes=[C.c_void_p,PTR,C.c_uint16,C.c_uint32]
        @WRITE
        def tx(p,n):
            try: return write(C.string_at(p,n))
            except Exception: return -1
        @RANDOM
        def rand(p,n): C.memmove(p,os.urandom(n),n); return 0
        @RECEIVE
        def rx(result,p,n): receive(result,C.string_at(p,n))
        @DONE
        def completed(tag,result,reason): done(tag,result,reason)
        self.callbacks=(tx,rand,rx,completed)
        self.handle=self.lib.fp_create(*self.callbacks)
        if not self.handle: raise RuntimeError('无法初始化出厂配对协议')

    @property
    def up(self): return bool(self.handle and self.lib.fp_up(self.handle))
    def hello(self): return self.lib.fp_hello(self.handle)
    def poll(self,now): return self.lib.fp_poll(self.handle,now & 0xffffffff)
    def receive(self,data,now):
        self.lib.fp_receive(self.handle,(C.c_uint8*len(data)).from_buffer_copy(data),len(data),now & 0xffffffff)
    def send(self,data,tag):
        return self.lib.fp_send(self.handle,(C.c_uint8*len(data)).from_buffer_copy(data),len(data),tag)
    def close(self):
        if self.handle: self.lib.fp_destroy(self.handle); self.handle=None
