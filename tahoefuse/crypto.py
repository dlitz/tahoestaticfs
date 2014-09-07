"""
Cache metadata and data of a directory tree.
"""

import os
import struct
import fcntl

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256, SHA512
from Crypto.Util import Counter
from Crypto import Random


class CryptFile(object):
    """
    File encrypted with a key in AES-CTR mode.
    """

    HEADER_SIZE = 16

    def __init__(self, path, key, mode='r+b'):
        if len(key) != 32:
            raise ValueError("Key must be 32 bytes")

        if mode in ('rb', 'r+b', 'w+b'):
            self.fp = open(path, mode)
        else:
            raise IOError("Unsupported mode %r" % (mode,))

        # BSD locking on the file; only one fd can write at a time
        if mode == 'rb':
            fcntl.flock(self.fp, fcntl.LOCK_SH)
        else:
            fcntl.flock(self.fp, fcntl.LOCK_EX)

        self.mode = mode
        self.key = key

        assert AES.block_size == 16

        if mode in 'w+b':
            # Generate new nonce
            nonce = Random.new().read(16)
            self.fp.write(nonce)
        else:
            # Read nonce from file
            nonce = self.fp.read(16)

        self.nonce = _unpack_uint128(nonce)
        self.offset = 0

    def seek(self, offset, whence=0):
        if whence == 0:
            pass
        elif whence == 1:
            offset = self.offset + offset
        elif whence == 2:
            offset = self._get_file_size() - self.HEADER_SIZE + offset
        else:
            raise IOError("Invalid whence")
        if offset < 0:
            raise IOError("Invalid offset")
        self.offset = offset

    def tell(self):
        return self.offset

    def _get_file_size(self):
        self.fp.seek(0, 2)
        return self.fp.tell()

    def _get_AES_at(self, offset, encrypt=True):
        start_block, start_off = divmod(offset, AES.block_size)
        ctr = Counter.new(128,
                          initial_value=_wrapsum_uint128(self.nonce,
                                                         start_block),
                          allow_wraparound=True)
        cryptor = AES.new(self.key, AES.MODE_CTR, counter=ctr)
        if encrypt:
            cryptor.encrypt('\x00' * start_off)
        else:
            cryptor.decrypt('\x00' * start_off)
        return cryptor

    def _read(self, size, offset):
        if size is None:
            size = self._get_file_size() - offset - self.HEADER_SIZE
        if size <= 0:
            return b""

        # Read and decrypt data
        decryptor = self._get_AES_at(offset, encrypt=False)
        self.fp.seek(self.HEADER_SIZE + offset)
        return decryptor.decrypt(self.fp.read(size))

    def _write(self, data, offset):
        # Synchronize encryptor at start position
        encryptor = self._get_AES_at(offset, encrypt=True)

        # Write output
        self.fp.seek(self.HEADER_SIZE + offset)
        if hasattr(data, 'next'):
            # streaming iterator
            for block in data:
                self.fp.write(encryptor.encrypt(block))
        else:
            self.fp.write(encryptor.encrypt(data))

    def read(self, size=None):
        data = self._read(size, self.offset)
        self.offset += len(data)
        return data

    def write(self, data):
        self.fp.seek(0, 2)
        file_size = self.fp.tell()
        if file_size < self.offset + self.HEADER_SIZE:
            # Write past end
            stream = NullStream(self.offset + self.HEADER_SIZE - file_size)
            self._write(iter(stream), file_size - self.HEADER_SIZE)

        self._write(data, self.offset)
        self.offset += len(data)

    def truncate(self, size):
        file_size = self._get_file_size()
        self.fp.truncate(size + self.HEADER_SIZE)

        if file_size < size + self.HEADER_SIZE:
            # Truncate increased file size: add null padding
            stream = NullStream(size + self.HEADER_SIZE - file_size)
            self._write(iter(stream), file_size - self.HEADER_SIZE)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        self.fp.close()
        self.key = None


class NullStream(object):
    def __init__(self, size, block_size=131072):
        self.size = size
        self.block_size = block_size

    def __iter__(self):
        remaining = self.size
        block = b'\x00' * self.block_size
        while remaining > 0:
            if remaining > self.block_size:
                yield block
                remaining -= self.block_size
            else:
                yield b'\x00' * remaining
                remaining = 0


def _pack_uint128(num):
    return (struct.pack('<Q', num & 0xffffffffffffffff)
            + struct.pack('<Q', (num >> 64) & 0xffffffffffffffff))


def _unpack_uint128(s):
    return (struct.unpack('<Q', s[:8])[0]
            | (struct.unpack('<Q', s[8:])[0] << 64))


def _wrapsum_uint128(a, b):
    return (a + b) % 0x100000000000000000000000000000000