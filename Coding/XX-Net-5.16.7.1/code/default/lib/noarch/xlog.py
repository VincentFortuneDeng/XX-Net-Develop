import os
import sys
import time
from datetime import datetime
import traceback
import threading
import json
import re
import shutil
from os.path import join

from six import string_types

import utils

CRITICAL = 50
FATAL = CRITICAL
ERROR = 40
WARNING = 30
WARN = WARNING
INFO = 20
DEBUG = 10
NOTSET = 0

# full_log set by server, upload full log for debug (maybe next time start session), remove old log file on reset log
full_log = False

# keep log set by UI, keep all logs, never delete old log, also upload log to server.


class Logger():
    def __init__(self, name, buffer_size=0, file_name=None, roll_num=1,
                 log_path=None, save_start_log=0, save_warning_log=False,
                 save_persistent_log=False):
        self.name = str(name)
        self.file_max_size = 1024 * 1024
        self.buffer_lock = threading.RLock()
        self.buffer = {}  # id => line
        self.buffer_size = buffer_size
        self.last_no = 0
        self.min_level = NOTSET
        self.log_fd = None
        self.set_color()
        self.roll_num = roll_num
        if file_name:
            self.set_file(file_name)

        self.log_path = log_path
        self.save_start_log = save_start_log
        self.save_warning_log = save_warning_log
        self.save_persistent_log = save_persistent_log
        self.start_log_num = 0
        self.boot_started_at = int(time.time() * 1000) / 1000.0
        self.startup_verbose_ended_at = None if save_start_log else self.boot_started_at
        self.persistent_log_fn = None
        self.persistent_fd = None
        self.persistent_file_max_size = 10 * 1024 * 1024
        self.error_context_last = {}

        if log_path and os.path.exists(join(log_path, "keep_log.txt")):
            self.info("keep log")
            self.keep_log = True
        else:
            self.keep_log = False

        if log_path and (save_warning_log or save_persistent_log):
            self.persistent_log_fn = os.path.join(log_path, "%s.log" % name)
            self.persistent_fd = open(self.persistent_log_fn, "ab")
            self._roll_persistent_log_if_needed()

    def set_buffer(self, buffer_size):
        with self.buffer_lock:
            self.buffer_size = buffer_size
            buffer_len = len(self.buffer)
            if buffer_len > self.buffer_size:
                for i in range(self.last_no - buffer_len, self.last_no - self.buffer_size):
                    try:
                        del self.buffer[i]
                    except:
                        pass

    def reset_log_files(self):
        with self.buffer_lock:
            if not self.persistent_log_fn:
                return
            try:
                if self.persistent_fd:
                    self.persistent_fd.flush()
                    self.persistent_fd.close()
            except Exception:
                pass
            self.persistent_fd = None
            try:
                self.persistent_fd = open(self.persistent_log_fn, "ab")
                self._roll_persistent_log_if_needed()
            except Exception:
                self.persistent_fd = None

    def keep_logs(self):
        self.keep_log = True
        # self.debug("keep log for %s", self.name)
        if not self.log_path:
            return

        with open(join(self.log_path, "keep_log.txt"), "w") as fd:
            fd.write(" ")

    def setLevel(self, level):
        if level == "DEBUG":
            self.min_level = DEBUG
        elif level == "INFO":
            self.min_level = INFO
        elif level == "WARN":
            self.min_level = WARN
        elif level == "ERROR":
            self.min_level = ERROR
        elif level == "FATAL":
            self.min_level = FATAL
        else:
            print(("log level not support:%s", level))

    def set_color(self):
        self.err_color = None
        self.warn_color = None
        self.debug_color = None
        self.reset_color = None
        self.set_console_color = lambda x: None
        if hasattr(sys.stderr, 'isatty') and sys.stderr.isatty():
            if os.name == 'nt':
                self.err_color = 0x04
                self.warn_color = 0x06
                self.debug_color = 0x002
                self.reset_color = 0x07

                import ctypes
                SetConsoleTextAttribute = ctypes.windll.kernel32.SetConsoleTextAttribute
                GetStdHandle = ctypes.windll.kernel32.GetStdHandle
                self.set_console_color = lambda color: SetConsoleTextAttribute(GetStdHandle(-11), color)

            elif os.name == 'posix':
                self.err_color = '\033[31m'
                self.warn_color = '\033[33m'
                self.debug_color = '\033[32m'
                self.reset_color = '\033[0m'

                self.set_console_color = lambda color: sys.stderr.write(color)

    def set_file(self, file_name):
        self.log_filename = file_name
        if os.path.isfile(file_name):
            self.file_size = os.path.getsize(file_name)
            if self.file_size > self.file_max_size:
                self.roll_log()
                self.file_size = 0
        else:
            self.file_size = 0

        self.log_fd = open(file_name, "a+")

    def _roll_persistent_log_if_needed(self, incoming_size=0):
        try:
            file_size = os.path.getsize(self.persistent_log_fn) \
                if os.path.isfile(self.persistent_log_fn) else 0
            if file_size and file_size + incoming_size > self.persistent_file_max_size:
                if self.persistent_fd:
                    self.persistent_fd.close()
                    self.persistent_fd = None
                os.replace(self.persistent_log_fn, self.persistent_log_fn + ".1")
                self.persistent_fd = open(self.persistent_log_fn, "ab")
        except Exception:
            if self.persistent_fd is None:
                try:
                    self.persistent_fd = open(self.persistent_log_fn, "ab")
                except Exception:
                    pass

    def _persistent_record_bytes(self, string):
        raw = string.encode("utf-8")
        limit = min(64 * 1024, self.persistent_file_max_size)
        if len(raw) <= limit:
            return raw
        marker = ("\n... %d bytes omitted ...\n" % len(raw)).encode("ascii")
        remaining = max(0, limit - len(marker))
        head = raw[:remaining // 2]
        tail = raw[len(raw) - (remaining - len(head)):]
        while head:
            try:
                head.decode("utf-8")
                break
            except UnicodeDecodeError:
                head = head[:-1]
        while tail:
            try:
                tail.decode("utf-8")
                break
            except UnicodeDecodeError:
                tail = tail[1:]
        return head + marker + tail

    def _write_persistent(self, string):
        data = self._persistent_record_bytes(string)
        self._roll_persistent_log_if_needed(len(data))
        if self.persistent_fd:
            self.persistent_fd.write(data)
            self.persistent_fd.flush()

    def _write_error_context(self, fmt):
        if not self.persistent_log_fn:
            return
        now = time.time()
        key = (self.name, fmt)
        last_written = self.error_context_last.get(key)
        if last_written is not None and now - last_written < 60:
            return

        candidates = [line for _, line in sorted(self.buffer.items())
                      if " - [DEBUG] " in line or " - [INFO] " in line][-40:]
        if not candidates:
            return
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
        begin = "%s - [ERROR] ERROR_CONTEXT_BEGIN\n" % time_str
        end = "%s - [ERROR] ERROR_CONTEXT_END\n" % time_str
        limit = min(16 * 1024, self.persistent_file_max_size)
        fixed_size = len(begin.encode("utf-8")) + len(end.encode("utf-8"))
        selected = []
        selected_size = 0
        for line in reversed(candidates):
            line_size = len(line.encode("utf-8"))
            if fixed_size + selected_size + line_size > limit:
                continue
            selected.append(line)
            selected_size += line_size
        if not selected:
            return
        selected.reverse()
        self._write_persistent(begin + "".join(selected) + end)
        self.error_context_last[key] = now

    def roll_log(self):
        for i in range(self.roll_num, 1, -1):
            new_name = "%s.%d" % (self.log_filename, i)
            old_name = "%s.%d" % (self.log_filename, i - 1)
            if not os.path.isfile(old_name):
                continue

            # self.info("roll_log %s -> %s", old_name, new_name)
            shutil.move(old_name, new_name)

        shutil.move(self.log_filename, self.log_filename + ".1")

    def log(self, level, console_color, html_color, fmt, *args, **kwargs):
        args = utils.bytes2str_only(args)
        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
        string = '%s - [%s] %s\n' % (time_str, level, fmt % args)
        self.buffer_lock.acquire()
        try:
            try:
                console_string = '%s [%s][%s] %s\n' % (time_str, self.name, level, fmt % args)

                self.set_console_color(console_color)
                sys.stderr.write(console_string)
                self.set_console_color(self.reset_color)
            except:
                pass

            if self.log_fd:
                self.log_fd.write(string)
                try:
                    self.log_fd.flush()
                except:
                    pass

                self.file_size += len(string)
                if self.file_size > self.file_max_size:
                    self.log_fd.close()
                    self.log_fd = None
                    self.roll_log()
                    self.log_fd = open(self.log_filename, "w")
                    self.file_size = 0

            persist = kwargs.get("persist", False)
            in_startup = self.save_start_log and self.start_log_num < self.save_start_log
            if self.persistent_log_fn and (
                    in_startup or persist or self.keep_log or full_log or
                    level in ["WARN", "WARNING", "ERROR", "CRITICAL"]):
                self._write_persistent(string)
                if level in ["ERROR", "CRITICAL"] and kwargs.get("include_context", True):
                    self._write_error_context(fmt)

            if in_startup:
                self.start_log_num += 1
                if self.start_log_num >= self.save_start_log:
                    self.startup_verbose_ended_at = time.time()

            if self.buffer_size:
                self.last_no += 1
                self.buffer[self.last_no] = string
                buffer_len = len(self.buffer)
                if buffer_len > self.buffer_size:
                    del self.buffer[self.last_no - self.buffer_size]
        except Exception as e:
            string = '%s - [%s]LOG_EXCEPT: %s, Except:%s<br> %s' % \
                     (time.ctime()[4:-5], level, fmt % args, e, traceback.format_exc())
            self.last_no += 1
            self.buffer[self.last_no] = string
            buffer_len = len(self.buffer)
            if buffer_len > self.buffer_size:
                del self.buffer[self.last_no - self.buffer_size]
        finally:
            self.buffer_lock.release()

    def debug(self, fmt, *args, **kwargs):
        if self.min_level > DEBUG:
            return
        self.log('DEBUG', self.debug_color, '21610b', fmt, *args, **kwargs)

    def info(self, fmt, *args, **kwargs):
        if self.min_level > INFO:
            return
        self.log('INFO', self.reset_color, '000000', fmt, *args, **kwargs)

    def warning(self, fmt, *args, **kwargs):
        if self.min_level > WARN:
            return
        self.log('WARNING', self.warn_color, 'FF8000', fmt, *args, **kwargs)

    def warn(self, fmt, *args, **kwargs):
        self.warning(fmt, *args, **kwargs)

    def error(self, fmt, *args, **kwargs):
        if self.min_level > ERROR:
            return
        self.log('ERROR', self.err_color, 'FE2E2E', fmt, *args, **kwargs)

    def exception(self, fmt, *args, **kwargs):
        self.error(fmt, *args, **kwargs)
        self.error("Except stack:%s", traceback.format_exc(), include_context=False, **kwargs)

    def critical(self, fmt, *args, **kwargs):
        if self.min_level > CRITICAL:
            return
        self.log('CRITICAL', self.err_color, 'D7DF01', fmt, *args, **kwargs)

    # =================================================================
    def export_view(self, view, max_bytes):
        if view not in ("startup", "warning") or \
                not isinstance(max_bytes, int) or max_bytes <= 0:
            self.warning("invalid log export view:%r max_bytes:%r", view, max_bytes)
            return ""
        if not self.persistent_log_fn:
            return ""

        record_start = re.compile(
            rb'(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} - '
            rb'\[(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\] ')
        records = []
        with self.buffer_lock:
            for suffix in (".1", ""):
                try:
                    with open(self.persistent_log_fn + suffix, "rb") as log_file:
                        raw = log_file.read()
                except (IOError, OSError):
                    continue
                complete_end = raw.rfind(b"\n") + 1
                raw = raw[:complete_end]
                starts = list(record_start.finditer(raw))
                for index, match in enumerate(starts):
                    end = starts[index + 1].start() if index + 1 < len(starts) else len(raw)
                    try:
                        records.append(raw[match.start():end].decode("utf-8"))
                    except UnicodeDecodeError:
                        continue

        grouped = []
        index = 0
        while index < len(records):
            text = records[index]
            if " - [ERROR] ERROR_CONTEXT_BEGIN\n" not in text:
                grouped.append(text)
                index += 1
                continue
            block = [text]
            index += 1
            while index < len(records):
                block.append(records[index])
                finished = " - [ERROR] ERROR_CONTEXT_END\n" in records[index]
                index += 1
                if finished:
                    grouped.append("".join(block))
                    break

        selected = []
        for text in grouped:
            if view == "warning":
                if not re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} - '
                                r'\[(?:WARNING|WARN|ERROR|CRITICAL)\] ', text):
                    continue
            else:
                try:
                    timestamp = datetime.strptime(text[:23], "%Y-%m-%d %H:%M:%S.%f").timestamp()
                except (ValueError, OSError):
                    continue
                end_time = self.startup_verbose_ended_at or time.time()
                if not self.boot_started_at <= timestamp <= end_time:
                    continue
                if len(selected) >= self.save_start_log:
                    break
            selected.append(text)

        result = []
        result_size = 0
        for text in reversed(selected):
            size = len(text.encode("utf-8"))
            if size > max_bytes:
                continue
            if result_size + size > max_bytes:
                break
            result.append(text)
            result_size += size
        result.reverse()
        return "".join(result)

    def get_last_lines(self, max_lines):
        self.buffer_lock.acquire()
        buffer_len = len(self.buffer)
        if buffer_len > max_lines:
            first_no = self.last_no - max_lines
        else:
            first_no = self.last_no - buffer_len + 1

        jd = {}
        if buffer_len > 0:
            for i in range(first_no, self.last_no + 1):
                jd[i] = utils.to_str(self.buffer[i])
        self.buffer_lock.release()
        return json.dumps(jd)

    def get_new_lines(self, from_no):
        self.buffer_lock.acquire()
        jd = {}
        first_no = self.last_no - len(self.buffer) + 1
        if from_no < first_no:
            from_no = first_no

        if self.last_no >= from_no:
            for i in range(from_no, self.last_no + 1):
                jd[i] = utils.to_str(self.buffer[i])
        self.buffer_lock.release()
        return json.dumps(jd)


class LogThrottle(object):
    # (F-phase 2026-10-27) Rate limit for hot warn lines that are real
    # signals but flood the warning log (F-phase findings: retry_task_cb
    # bursts >200/hour during front outages). allow() returns
    # (allowed, suppressed_count): the first hit in a window logs
    # immediately, later hits are only counted, and the first hit after the
    # window reports how many were hidden. Callers keep their control flow;
    # only the log line is rate-limited.
    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.hits = {}  # key => [last_logged_ts, suppressed_count]
        self.sealed = False

    def allow(self, key):
        now = time.time()
        with self.lock:
            if self.sealed:
                return False, 0
            entry = self.hits.get(key)
            if entry is not None and now - entry[0] < self.interval:
                entry[1] += 1
                return False, entry[1]

            suppressed = entry[1] if entry is not None else 0
            self.hits[key] = [now, 0]
            if len(self.hits) > 512:
                self._prune(now)
            return True, suppressed

    def flush(self):
        with self.lock:
            suppressed = 0
            key_count = 0
            for entry in self.hits.values():
                if entry[1]:
                    suppressed += entry[1]
                    key_count += 1
            self.hits = {}
            self.sealed = True
            return suppressed, key_count

    def reset(self):
        with self.lock:
            self.hits = {}
            self.sealed = False

    def _prune(self, now):
        # called with lock held; drop expired keys so long-tail keys
        # cannot grow the dict unbounded (same policy as the 512-cap
        # used by pipe_socks warn_send_block).
        for k in list(self.hits):
            if now - self.hits[k][0] >= self.interval:
                del self.hits[k]


class null():
    @staticmethod
    def debug(fmt, *args, **kwargs):
        pass

    @staticmethod
    def info(fmt, *args, **kwargs):
        pass

    @staticmethod
    def warn(fmt, *args, **kwargs):
        pass

    @staticmethod
    def exception(fmt, *args, **kwargs):
        pass


loggerDict = {}


def getLogger(name=None, buffer_size=0, file_name=None, roll_num=1,
              log_path=None, save_start_log=0, save_warning_log=False,
              save_persistent_log=False):
    global loggerDict, default_log
    if name is None:
        for n in loggerDict:
            name = n
            break
    if name is None:
        name = u"default"

    if not isinstance(name, string_types):
        raise TypeError('A logger name must be string or Unicode')
    if isinstance(name, bytes):
        name = name.decode('utf-8')

    if name in loggerDict:
        return loggerDict[name]
    else:
        logger_instance = Logger(name, buffer_size, file_name, roll_num, log_path,
                                 save_start_log, save_warning_log, save_persistent_log)
        loggerDict[name] = logger_instance
        default_log = logger_instance
        return logger_instance


def reset_log_files():
    for name, log in loggerDict.items():
        log.reset_log_files()


def keep_log(temp=False):
    global full_log
    if temp:
        full_log = True
    else:
        for name, log in loggerDict.items():
            log.keep_logs()


default_log = getLogger()


def debug(fmt, *args, **kwargs):
    default_log.debug(fmt, *args, **kwargs)


def info(fmt, *args, **kwargs):
    default_log.info(fmt, *args, **kwargs)


def warning(fmt, *args, **kwargs):
    default_log.warn(fmt, *args, **kwargs)


def warn(fmt, *args, **kwargs):
    default_log.warn(fmt, *args, **kwargs)


def error(fmt, *args, **kwargs):
    default_log.error(fmt, *args, **kwargs)


def exception(fmt, *args, **kwargs):
    error(fmt, *args, **kwargs)
    error("Except stack:%s", traceback.format_exc(), include_context=False, **kwargs)


def critical(fmt, *args, **kwargs):
    default_log.critical(fmt, *args, **kwargs)
