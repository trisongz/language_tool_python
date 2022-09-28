import os
import re
import json
import httpx
import atexit
import socket
import asyncio
import aiohttp
import requests
import threading
import subprocess
import contextlib
import http.client
import urllib.parse

from typing import Dict, List, Any

from language_tool_python.config_file import LanguageToolConfig, ServerConfig
from language_tool_python.download_lt import download_lt
from language_tool_python.language_tag import LanguageTag
from language_tool_python.match import Match
from language_tool_python.utils import (
    correct,
    parse_url, get_locale_language, get_language_tool_directory, get_server_cmd,
    FAILSAFE_LANGUAGE, startupinfo,
    LanguageToolError, ServerError, JavaError, PathError
)

from language_tool_python.logs import logger

REMOTE_SERVER = os.getenv('LTP_SERVER', 'https://languagetool.org/api/')
SERVER_MODE = os.getenv('LTP_SERVER_MODE', 'false').lower() == 'true'
DEBUG_MODE = os.getenv('LTP_DEBUG', 'false').lower() == 'true'

# Keep track of running server PIDs in a global list. This way,
# we can ensure they're killed on exit.
RUNNING_SERVER_PROCESSES: List[subprocess.Popen] = []

class LanguageTool:
    """Main class used for checking text against different rules. 
    LanguageTool v2 API documentation: https://languagetool.org/http-api/swagger-ui/#!/default/post_check
    """
    _MIN_PORT = 8081
    _MAX_PORT = 8999
    _TIMEOUT = 5 * 60
    _remote = False
    _port = _MIN_PORT
    _server: subprocess.Popen = None
    _consumer_thread: threading.Thread = None
    _PORT_RE = re.compile(r"(?:https?://.*:|port\s+)(\d+)", re.I)
    
    def __init__(
        self, 
        language: str = 'en-US', 
        motherTongue: str = None,
        remote_server: str = None, 
        newSpellings: Any = None,
        new_spellings_persist: bool = True,
        host: str = None, 
        config: Dict[str, Any] = None
    ):
        self._new_spellings = None
        self._new_spellings_persist = new_spellings_persist
        self._host = host or socket.gethostbyname('localhost')
        if remote_server:
            assert config is None, "cannot pass config file to remote server"
        self._server_config = None
        if SERVER_MODE:
            logger.info('Running in Server Mode')
            self._server_config = ServerConfig()
            if not config: config = self._server_config.to_config()

        self.config = LanguageToolConfig(config) if config else None
        if remote_server is not None:
            self._remote = True
            self._url = parse_url(remote_server)
            self._url = urllib.parse.urljoin(self._url, 'v2/')
            self._update_remote_server_config(self._url)
        elif not self._server_is_alive():
            self._start_server_on_free_port()
        if language is None:
            try:
                language = get_locale_language()
            except ValueError:
                language = FAILSAFE_LANGUAGE
        if newSpellings:
            self._new_spellings = newSpellings
            self._register_spellings(self._new_spellings)
        self._language = LanguageTag(language, self._get_languages())
        self.motherTongue = motherTongue
        self.disabled_rules = set()
        self.enabled_rules = set()
        self.disabled_categories = set()
        self.enabled_categories = set()
        self.enabled_rules_only = False
        self.preferred_variants = set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        self.close()

    def __repr__(self):
        return '{}(language={!r}, motherTongue={!r})'.format(
            self.__class__.__name__, self.language, self.motherTongue)

    def close(self):
        if self._server_is_alive():
            logger.warning(f'Killing server process {self._server.pid}')
            self._terminate_server()
        if not self._new_spellings_persist and self._new_spellings:
            self._unregister_spellings()
            self._new_spellings = []

    @property
    def language(self):
        """The language to be used."""
        return self._language

    @language.setter
    def language(self, language):
        self._language = LanguageTag(language, self._get_languages())
        self.disabled_rules.clear()
        self.enabled_rules.clear()

    @property
    def motherTongue(self):
        """The user's mother tongue or None.
        The mother tongue may also be used as a source language for
        checking bilingual texts.
        """
        return self._motherTongue
    
    @motherTongue.setter
    def motherTongue(self, motherTongue):
        self._motherTongue = (None if motherTongue is None
                              else LanguageTag(motherTongue, self._get_languages()))
    @property
    def _spell_checking_categories(self):
        return {'TYPOS'}

    def check(self, text: str) -> List[Match]:
        """Match text against enabled rules."""
        url = urllib.parse.urljoin(self._url, 'check')
        response = self._query_server(url, self._create_params(text))
        matches = response['matches']
        return [Match(match) for match in matches]

    def _create_params(self, text: str) -> Dict[str, str]:
        params = {'language': str(self.language), 'text': text}
        if self.motherTongue is not None:
            params['motherTongue'] = self.motherTongue
        if self.disabled_rules:
            params['disabledRules'] = ','.join(self.disabled_rules)
        if self.enabled_rules:
            params['enabledRules'] = ','.join(self.enabled_rules)
        if self.enabled_rules_only:
            params['enabledOnly'] = 'true'
        if self.disabled_categories:
            params['disabledCategories'] = ','.join(self.disabled_categories)
        if self.enabled_categories:
            params['enabledCategories'] = ','.join(self.enabled_categories)
        if self.preferred_variants:
            params['preferredVariants'] = ','.join(self.preferred_variants)
        return params

    def correct(self, text: str) -> str:
        """Automatically apply suggestions to the text."""
        return correct(text, self.check(text))
    
    def enable_spellchecking(self):
        """Enable spell-checking rules."""
        self.disabled_categories.difference_update(self._spell_checking_categories)

    def disable_spellchecking(self):
        """Disable spell-checking rules."""
        self.disabled_categories.update(self._spell_checking_categories)

    @staticmethod
    def _get_valid_spelling_file_path() -> str:
        library_path = get_language_tool_directory()
        spelling_file_path = os.path.join(library_path, "org/languagetool/resource/en/hunspell/spelling.txt")
        if not os.path.exists(spelling_file_path):
            raise FileNotFoundError(f"Failed to find the spellings file at {spelling_file_path}\n Please file an issue at https://github.com/jxmorris12/language_tool_python/issues")

        return spelling_file_path

    def _register_spellings(self, spellings):
        spelling_file_path = self._get_valid_spelling_file_path()
        with open(spelling_file_path, "a+", encoding='utf-8') as spellings_file:
            spellings_file.write("\n" + "\n".join(list(spellings)))
        if DEBUG_MODE:
            logger.info(f"Registered new spellings at {spelling_file_path}")

    def _unregister_spellings(self):
        spelling_file_path = self._get_valid_spelling_file_path()
        with open(spelling_file_path, 'r+', encoding='utf-8') as spellings_file:
            spellings_file.seek(0, os.SEEK_END)
            for _ in range(len(self._new_spellings)):
                while spellings_file.read(1) != '\n':
                    spellings_file.seek(spellings_file.tell() - 2, os.SEEK_SET)
                spellings_file.seek(spellings_file.tell() - 2, os.SEEK_SET)
            spellings_file.seek(spellings_file.tell() + 1, os.SEEK_SET)
            spellings_file.truncate()
        if DEBUG_MODE:
            logger.info(f"Unregistered new spellings at {spelling_file_path}")

    def _get_languages(self) -> set:
        """Get supported languages (by querying the server)."""
        self._start_server_if_needed()
        url = urllib.parse.urljoin(self._url, 'languages')
        languages = set()
        for e in self._query_server(url, num_tries=1):
            languages.add(e.get('code'))
            languages.add(e.get('longCode'))
        languages.add("auto")
        return languages

    def _start_server_if_needed(self):
        # Start server.
        if not self._server_is_alive() and self._remote is False:
            self._start_server_on_free_port()

    def _update_remote_server_config(self, url):
        self._url = url
        self._remote = True

    def _query_server(self, url, params=None, num_tries=2):
        if DEBUG_MODE:
            logger.info(f'_query_server url: {url} | params: {params}')
        for n in range(num_tries):
            try:
                with requests.get(url, params=params, timeout=self._TIMEOUT) as response:
                    try:
                        return response.json()
                    except json.decoder.JSONDecodeError as e:
                        if DEBUG_MODE:
                            logger.info(f'URL {url} and params {params} returned invalid JSON response:')
                            logger.info(f'{response}')
                            logger.info(response.content)
                        raise LanguageToolError(response.content.decode()) from e
            except (IOError, http.client.HTTPException) as e:
                if self._remote is False:
                    self._terminate_server()
                    self._start_local_server()
                if n + 1 >= num_tries:
                    raise LanguageToolError(f'{self._url}: {e}') from e

    def _start_server_on_free_port(self):
        while True:
            self._url = f'http://{self._host}:{self._port}/v2/'
            try:
                self._start_local_server()
                break
            except ServerError:
                if self._MIN_PORT <= self._port < self._MAX_PORT:
                    self._port += 1
                else:
                    raise

    def _start_local_server(self):
        # Before starting local server, download language tool if needed.
        download_lt()
        err = None
        try:
            if DEBUG_MODE:
                if self._port:
                    logger.info(f'language_tool_python initializing with port: {self._port}')
                if self.config:
                    logger.info(f'language_tool_python initializing with temporary config file: {self.config.path}')
            server_cmd = get_server_cmd(
                self._port, 
                self.config,
                options = self._server_config.get_server_options() if self._server_config else None,
            )
        except PathError as e:
            # Can't find path to LanguageTool.
            err = e
        else:
            # Need to PIPE all handles: http://bugs.python.org/issue3905
            self._server = subprocess.Popen(
                server_cmd,
                stdin = subprocess.PIPE,
                stdout = subprocess.PIPE,
                stderr = subprocess.PIPE,
                universal_newlines = True,
                startupinfo = startupinfo
            )
            global RUNNING_SERVER_PROCESSES
            RUNNING_SERVER_PROCESSES.append(self._server)

            match = None
            while True:
                line = self._server.stdout.readline()
                if not line:
                    break
                match = self._PORT_RE.search(line)
                if match:
                    port = int(match.group(1))
                    if port != self._port:
                        raise LanguageToolError(f'requested port {self._port}, but got {port}')
                    break
            if not match:
                err_msg = self._terminate_server()
                match = self._PORT_RE.search(err_msg)
                if not match:
                    raise LanguageToolError(err_msg)
                port = int(match.group(1))
                if port != self._port:
                    raise LanguageToolError(err_msg)

        if self._server:
            self._consumer_thread = threading.Thread(
                target = lambda: _consume(self._server.stdout)
            )
            self._consumer_thread.daemon = True
            self._consumer_thread.start()
        else:
            # Couldn't start the server, so maybe there is already one running.
            raise ServerError('Server running; don\'t start a server here.')

    def _server_is_alive(self):
        return self._server and self._server.poll() is None

    def _terminate_server(self):
        LanguageToolError_message = ''
        with contextlib.suppress(OSError):
            self._server.terminate()
        with contextlib.suppress(IOError, ValueError):
            LanguageToolError_message = self._server.communicate()[1].strip()
        with contextlib.suppress(IOError):
            self._server.stdout.close()
        with contextlib.suppress(IOError):
            self._server.stdin.close()
        with contextlib.suppress(IOError):
            self._server.stderr.close()
        self._server = None
        return LanguageToolError_message

class AsyncLanguageTool(LanguageTool):
    """Asynchronous LanguageTool client.

    This class is a wrapper around LanguageTool that allows for asynchronous
    requests to the LanguageTool server. This is useful for applications that
    need to make multiple requests to the server at the same time.

    Note that this class is not thread-safe. It is intended to be used in
    asynchronous applications that use a single thread for all requests.

    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = httpx.AsyncClient()
        
        #self._loop = asyncio.get_event_loop()
        #self._session = aiohttp.ClientSession(loop=self._loop)
    
    async def __aexit__(
        self, exc_type, exc_val, exc_tb
    ) -> None:
        if self._session:
            await self._session.aclose()
            self._session = None

    def __exit__(self, exc_type, exc_val, exc_tb):
        #if self._session:
        #    self._session.close()
        #    # self._session.close()
        # self._session.close()
        self.close()

    def __del__(self):
        # self._session.close()
        self.close()
    
    async def correct(self, text: str) -> str:
        """Automatically apply suggestions to the text."""
        return correct(text, await self.check(text))

    async def check(self, text: str) -> List[Match]:
        """Match text against enabled rules."""
        url = urllib.parse.urljoin(self._url, 'check')
        response = await self._async_query_server(url, self._create_params(text))
        matches = response['matches']
        return [Match(match) for match in matches]

    async def _async_query_server(self, url, params=None, num_tries=2):
        if DEBUG_MODE:
            logger.info(f'_query_server url: {url} | params: {params}')
        for n in range(num_tries):
            try:
                # async with self._session.get(url, params=params, timeout=self._TIMEOUT) as response:
                response = await self._session.get(url, params=params, timeout=self._TIMEOUT)
                try:
                    return await response.json()
                except json.decoder.JSONDecodeError as e:
                    if DEBUG_MODE:
                        logger.info(f'URL {url} and params {params} returned invalid JSON response:')
                        logger.info(f'{response}')
                        logger.info(response.content)
                    raise LanguageToolError(response.content.decode()) from e
            except (IOError, http.client.HTTPException, httpx.HTTPError) as e:
                if self._remote is False:
                    self._terminate_server()
                    self._start_local_server()
                if n + 1 >= num_tries:
                    raise LanguageToolError(f'{self._url}: {e}') from e

class LanguageToolPublicAPI(LanguageTool):
    """Language tool client of the official API."""
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, 
            remote_server = REMOTE_SERVER, 
            **kwargs
        )

class AsyncLanguageToolPublicAPI(AsyncLanguageTool):
    """Asynchronous Language tool client of the official API."""
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, 
            remote_server = REMOTE_SERVER, 
            **kwargs
        )

@atexit.register
def terminate_server():
    """Terminate the server."""
    for proc in RUNNING_SERVER_PROCESSES:
        proc.terminate()


def _consume(stdout):
    """Consume/ignore the rest of the server output.
    Without this, the server will end up hanging due to the buffer
    filling up.
    """
    while stdout.readline():
        pass

def run_server():
    """Run the server."""
    import time
    lt = LanguageTool()
    while True:
        try:
            time.sleep(1)
        except Exception as e:
            logger.error(f'Error: {e}')
            lt.close()
            break


if __name__ == '__main__':
    # Start the server.
    run_server()

