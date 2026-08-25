# -*- coding: utf-8 -*-
"""统一 MySQL 访问层：线程安全、有界连接池和账号查询。"""
from __future__ import annotations

import hashlib
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

import pymysql

from . import config


class MySQLPool:
    def __init__(self):
        self._items: queue.Queue = queue.Queue(maxsize=max(1, config.MYSQL_POOL_MAX))
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def _new(self):
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=5, autocommit=False)
        with self._lock:
            self._created += 1
        return conn, time.monotonic()

    @contextmanager
    def connection(self) -> Iterator:
        if self._closed:
            raise RuntimeError("MySQL pool is closed")
        conn = None
        born = 0.0
        try:
            conn, born = self._items.get(timeout=config.MYSQL_POOL_TIMEOUT)
        except queue.Empty:
            with self._lock:
                if self._created < max(1, config.MYSQL_POOL_MAX):
                    conn, born = self._new()
                else:
                    raise TimeoutError("MySQL connection pool exhausted")
        if conn is None:
            raise RuntimeError("MySQL pool failed to lease a connection")
        try:
            if time.monotonic() - born > config.MYSQL_POOL_RECYCLE:
                try: conn.close()
                except Exception: pass
                with self._lock: self._created -= 1
                conn, born = self._new()
            conn.ping(reconnect=True)
            yield conn
        except Exception:
            try: conn.rollback()
            except Exception: pass
            raise
        finally:
            try: conn.rollback()
            except Exception: pass
            if not self._closed:
                try: self._items.put_nowait((conn, born))
                except queue.Full:
                    try: conn.close()
                    except Exception: pass
                    with self._lock: self._created -= 1
            else:
                try: conn.close()
                except Exception: pass
                with self._lock: self._created -= 1

    @contextmanager
    def transaction(self) -> Iterator:
        with self.connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self):
        self._closed = True
        while True:
            try:
                conn, _ = self._items.get_nowait()
            except queue.Empty:
                break
            try: conn.close()
            except Exception: pass


_pool: MySQLPool | None = None
_pool_lock = threading.Lock()


def pool() -> MySQLPool:
    global _pool
    with _pool_lock:
        if _pool is None or _pool._closed:
            _pool = MySQLPool()
        current = _pool
        assert current is not None
    return current


@contextmanager
def connection():
    with pool().connection() as conn:
        yield conn


@contextmanager
def transaction():
    with pool().transaction() as conn:
        yield conn


def close_pool():
    global _pool
    with _pool_lock:
        if _pool:
            _pool.close()
            _pool = None


def _conn():
    """兼容旧调用方；新代码应使用 connection()/transaction()。"""
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                           user="root", password=config.MYSQL_PASSWORD,
                           database=config.MYSQL_DB, charset="utf8mb4", connect_timeout=5)


def _hash(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_user(username: str, password: str) -> dict | None:
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT password_hash, salt, role, display_name, status FROM sys_user WHERE username=%s", (username,))
                row = cur.fetchone()
        if not row: return None
        pw_hash, salt, role, display_name, status = row
        if status != 1 or _hash(password, salt) != pw_hash: return None
        return {"username": username, "role": role, "display_name": display_name or username}
    except pymysql.MySQLError:
        return None


_sessions: dict[str, dict] = {}

def issue_token(user: dict) -> str:
    token = uuid.uuid4().hex
    _sessions[token] = user
    return token

def resolve_token(token: str) -> dict | None:
    return _sessions.get(token)

def revoke_token(token: str) -> None:
    _sessions.pop(token, None)
