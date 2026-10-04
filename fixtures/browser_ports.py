"""Bound high loopback ports for browser fixtures without changing host policy."""
import errno
import secrets
import socket


def create_browser_server(factory, **kwargs):
    """Keep the bound server/socket; never release a probe and race its caller."""
    for _ in range(32):
        try:
            return factory(port=49152+secrets.randbelow(16384), **kwargs)
        except OSError as error:
            if error.errno not in {errno.EADDRINUSE, errno.EACCES}:
                raise
    raise OSError('No available high loopback port for browser acceptance')


def bound_browser_socket():
    def bind(port):
        sock = socket.socket()
        try:
            sock.bind(('127.0.0.1', port))
            return sock
        except OSError:
            sock.close()
            raise
    return create_browser_server(bind)
