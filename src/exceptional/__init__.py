from cStringIO import StringIO
import datetime
import gzip
import logging
import os
import re
import sys
import traceback
import types
import urllib
import urllib2

from pylons import config
from webob import Request, Response
try:
    import json
except ImportError:
    import simplejson as json

__version__ = '0.2.3'

EXCEPTIONAL_PROTOCOL_VERSION = 6
EXCEPTIONAL_API_ENDPOINT = "http://api.getexceptional.com/api/errors"

def memoize(func):
    """A simple memoize decorator (with no support for keyword arguments)."""

    cache = {}
    def wrapper(*args):
        if args in cache:
            return cache[args]
        cache[args] = value = func(*args)
        return value

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    if hasattr(func, '__module__'):
        wrapper.__module__ = func.__module__
    wrapper.clear = cache.clear

    return wrapper


class ExceptionalMiddleware(object):
    """
    Middleware to interface with the Exceptional service.

    Requires very little intervention on behalf of the user; you just need to
    add `exceptional.api_key` to your pylons settings.
    """

    def __init__(self, app, api_key, discreet=False):
        self.app = app
        self.active = False
        self.discreet = discreet

        try:
            self.api_key = api_key
            self.api_endpoint = EXCEPTIONAL_API_ENDPOINT + "?" + urllib.urlencode({
                    "api_key": self.api_key,
                    "protocol_version": EXCEPTIONAL_PROTOCOL_VERSION
                    })
            self.active = True
        except AttributeError:
            pass

    def _submit(self, exc, environ):
        """Submit the actual exception to getexceptional
        """
        info = {}
        conn = None

        try:
            info.update(self.environment_info())
            info.update(self.request_info(environ))
            info.update(self.exception_info(exc, sys.exc_info()[2]))

            payload = self.compress(json.dumps(info))
            req = urllib2.Request(self.api_endpoint, data=payload)
            req.headers['Content-Encoding'] = 'gzip'
            req.headers['Content-Type'] = 'application/json'

            conn = urllib2.urlopen(req)
            resp = conn.read()
        except Exception, e:
            raise Exception("Cannot submit %s because of %s" % (info, e), e)
        finally:
            if conn is not None:
                conn.close()

    def __call__(self, environ, start_response):
        req = Request(environ)
        response = Response()
        try:
            response = req.get_response(self.app)
        except Exception, e:
            error = traceback.format_exc()
            try:
                if self.active:
                    self._submit(e, environ)
            except:
                error2 = traceback.format_exc()
                error = "%s\nthen submission to getexceptional failed:\n%s" % (error, error2)
            response.status_int = 500

            if self.discreet:
                response.body = "An error has occured."
            else:
                response.body = "An error has occured; trace follows.\n%s" % error


        return response(environ, start_response)


    @staticmethod
    def compress(bytes):
        """Compress a bytestring using gzip."""

        stream = StringIO()
        # Use `compresslevel=1`; it's the least compressive but it's fast.
        gzstream = gzip.GzipFile(fileobj=stream, compresslevel=1, mode='wb')
        try:
            try:
                gzstream.write(bytes)
            finally:
                gzstream.close()
            return stream.getvalue()
        finally:
            stream.close()

    @memoize
    def environment_info(self):
        """
        Return a dictionary representing the server environment.

        The idea is that the result of this function will rarely (if ever)
        change for a given app instance. Ergo, the result can be cached between
        requests.
        """

        return {
                "application_environment": {
                    "framework": "pylons",
                    "env": dict(os.environ),
                    "language": "python",
                    "language_version": sys.version.replace('\n', ''),
                    "application_root_directory": self.project_root()
                    },
                "client": {
                    "name": "pylons-exceptional",
                    "version": __version__,
                    "protocol_version": EXCEPTIONAL_PROTOCOL_VERSION
                    }
                }

    def request_info(self, environ):
        """
        Return a dictionary of information for a given request.

        This will be run once for every request.
        """
        def _get_headers(env_dict):
            "Headers are upper-cased"
            h = {}
            try:
                for k in env_dict:
                    if type(env_dict[k]) == types.StringType and k.isupper():
                        h[k.replace("_", "-")] = env_dict[k]
            except:
                pass
            finally:
                return h

        # Process the session
        session = {}
        beaker = environ.get("beaker.session", "")
        for k in beaker:
            try:
                # Can it be json'ed?
                json.dumps(beaker[k])
                session[k] = beaker[k]
            except:
                "Ignore this session field"

        req_info = {}
        try:
            req_info = {
                 "request": {
                     "session": session,
                     "remote_ip": environ.get("REMOTE_ADDR", "Unknown"),
                     "parameters": environ.get("pylons.routes_dict", "Unknown"),
                     "action": environ.get("pylons.routes_dict").get("action"),
                     "controller": str(environ["pylons.controller"].__class__),
                     "url": environ.get("PATH_INFO", "Unknown"),
                     "request_method": environ.get("REQUEST_METHOD", "Unknown"),
                     "headers": _get_headers(environ),
                     }
                 }
        except:
            # Do you best but don't crash
            pass

        return req_info

    def exception_info(self, exception, tb, timestamp=None):
        backtrace = []
        for tb_part in traceback.format_tb(tb):
            backtrace.extend(tb_part.rstrip().splitlines())

        if timestamp is None:
            timestamp = datetime.datetime.utcnow()

        return {
                "exception": {
                    "occurred_at": timestamp.isoformat(),
                    "message": str(exception),
                    "backtrace": backtrace,
                    "exception_class": self.exception_class(exception)
                    }
                }

    def exception_class(self, exception):
        """Return a name representing the class of an exception."""

        cls = type(exception)
        if cls.__module__ == 'exceptions':  # Built-in exception.
            return cls.__name__
        return "%s.%s" % (cls.__module__, cls.__name__)

    @memoize
    def project_root(self):

        """
        Return the root of the current pylons project on the filesystem.
        """

        if "exceptional.root" in config.keys():
            return config["exceptional.root"]
        else:
            return "/I/don/t/know"

    @staticmethod
    def filter_params(params):
        """Filter sensitive information out of parameter dictionaries.
        """

        for key in params.keys():
            if "password" in key:
                del params[key]
        return params


class ExceptionalLogHandler(logging.Handler):
    """Handler for reporting to Exceptional

    The way you'll want to use it in code:

        import logging

        log = logging.getLogger("mylog")
        # ... <snip> all the other handlers you're adding to your logger... #
        exceptional_handler = ExceptionalLogHandler(api_key, debug_mode)
        exceptional_handler.setLevel(logging.CRITICAL)
        log.addHandler(exceptional_handler)

    After that, whenever you log something critical() or fatal() in your logger,
    ExceptionalLogHandler will report the exception being handled.

    I've disabled the ability to log errors in the process of trying to hit
    Exceptional. It's too easy for people to pass in the logger they're using,
    not set the Handler level high enough, and get caught in an infinite loop.
    But it can we re-enabled by removing the commented portions below.
    """

    def __init__(self, api_key, debug_mode=False, append_log_messages=False): # , log=None):
        """API key is the key to the Exceptional service and should be in the
        dogweb.ini config file. If debug is True, we'll just output to local
        logs and not really report things to Exceptional"""
        logging.Handler.__init__(self)
        self._api_key = api_key
        self._debug_mode = debug_mode
        self.append_log_messages = append_log_messages

        # self._log = log if log else logging

    def emit(self, record):
        if self._debug_mode:
            # self._log.info("ExceptionalLogHandler in debug mode, not reporting")
            pass

        if not self._debug_mode:
            try:
                exceptional = ExceptionalMiddleware(None, self._api_key)

                if exceptional.active:
                    # make an extra version of this that takes things implicitly
                    e = sys.exc_info()[1]
                    if self.append_log_messages:
                        e.args += record.message,
                    exceptional._submit(e, os.environ)
            except:
                # self._log.warning("ExceptionalLogHandler: Error submitting exception to getexceptional")
                # self._log.warning(traceback.format_exc())
                # print traceback.format_exc()
                pass


