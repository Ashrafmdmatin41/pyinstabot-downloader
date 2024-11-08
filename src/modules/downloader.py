# pylint: disable=duplicate-code
"""
This module interacts with the instagram api and uploads content to a temporary directory.
Supports downloading the content of the post by link and getting information about the account.
https://github.com/subzeroid/instagrapi
"""
import os
import time
from pathlib import Path
from urllib3.exceptions import ReadTimeoutError
from requests.exceptions import ConnectionError as RequestsConnectionError
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ClientRequestTimeout, MediaNotFound, MediaUnavailable, PleaseWaitFewMinutes, ChallengeRequired
from logger import log
from .exceptions import WrongVaultInstance, FailedCreateDownloaderInstance, FailedAuthInstagram, FailedDownloadPost


class Downloader:
    """
    An Instagram API instance is created by this class and contains a set of all the necessary methods
    to upload content from Instagram to a temporary directory.
    """
    def __init__(
        self,
        configuration: dict = None,
        vault: object = None
    ) -> None:
        """
        The method for create a new Instagram API client instance.

        Args:
            :param configuration (dict): dictionary with configuration parameters for Instagram API communication.
                :param username (str): username for authentication in the instagram api.
                :param password (str): password for authentication in the instagram api.
                :param session-file (str): path to the session file for authentication in the instagram api.
                :param delay-requests (int): delay between requests.
                :param 2fa-enabled (bool): two-factor authentication enabled.
                :param 2fa-seed (str): seed for two-factor authentication (secret key).
                :param locale (str): locale for requests.
                :param country-code (str): country code for requests.
                :param timezone-offset (int): timezone offset for requests.
                :param user-agent (str): user agent for requests.
                :param proxy-dsn (str): proxy dsn for requests.
                :param request-timeout (int): request timeout for requests.
            :param vault (object): instance of vault for reading configuration downloader-api.

        Returns:
            None

        Attributes:
            :attribute configuration (dict): dictionary with configuration parameters for instagram api communication.
            :attribute client (object): instance of the instagram api client.

        Examples:
            >>> configuration = {
            ...     'username': 'my_username',
            ...     'password': 'my_password',
            ...     'session-file': 'data/session.json',
            ...     'delay-requests': 1,
            ...     '2fa-enabled': False,
            ...     '2fa-seed': 'my_seed_secret',
            ...     'locale': 'en_US',
            ...     'country-code': '1',
            ...     'timezone-offset': 10800,
            ...     'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            ...     'proxy-dsn': 'http://localhost:8080'
            ...     'request-timeout': 10
            ... }
            >>> vault = Vault()
            >>> downloader = Downloader(configuration, vault)
        """
        if not vault:
            raise WrongVaultInstance("Wrong vault instance, you must pass the vault instance to the class argument.")

        if configuration:
            self.configuration = configuration
        elif not configuration:
            self.configuration = vault.kv2engine.read_secret(path='configuration/downloader-api')
        else:
            raise FailedCreateDownloaderInstance(
                "Failed to initialize the Downloader instance."
                "Please check the configuration in class argument or the secret with the configuration in the Vault."
            )

        log.info('[Downloader]: Creating a new instance...')
        self.client = Client()

        log.info('[Downloader]: Configuring client settings...')
        self.client.delay_range = [1, int(self.configuration['delay-requests'])]
        self.client.request_timeout = int(self.configuration['request-timeout'])
        self.client.set_locale(locale=self.configuration['locale'])
        self.client.set_country_code(country_code=int(self.configuration['country-code']))
        self.client.set_timezone_offset(seconds=int(self.configuration['timezone-offset']))
        self.client.set_user_agent(user_agent=self.configuration['user-agent'])
        self.client.set_proxy(dsn=self.configuration.get('proxy-dsn', None))
        log.info('[Downloader]: Client settings: %s', self.client.get_settings())

        auth_status = self.login()
        if auth_status == 'logged_in':
            log.info('[Downloader]: Instance created successfully with account %s', self.configuration['username'])
        else:
            raise FailedAuthInstagram("Failed to authenticate the Instaloader instance.")

        self.download_methods = {
            (1, 'any'): self.client.photo_download,
            (2, 'feed'): self.client.video_download,
            (2, 'clips'): self.client.clip_download,
            (2, 'igtv'): self.client.igtv_download,
            (8, 'any'): self.client.album_download
        }

    @staticmethod
    def exceptions_handler(method) -> None:
        """
        Decorator for handling exceptions in the Downloader class.

        Args:
            :param method (function): method to be wrapped.
        """
        def wrapper(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except LoginRequired:
                log.error('[Downloader]: Instagram API login required. Re-authentication...')
                self.login(method='relogin')
            except ChallengeRequired:
                log.error('[Downloader]: Instagram API requires challenge. Need manually pass in browser. Retry after 1 hour')
                time.sleep(3600)
                self.login()
            except PleaseWaitFewMinutes:
                log.error('[Downloader]: Device or IP address has been restricted. Just wait a one hour and try again')
                time.sleep(3600)
                self.login(method='relogin')
            except (ReadTimeoutError, RequestsConnectionError, ClientRequestTimeout):
                log.error('[Downloader]: Timeout error downloading post content. Retry after 1 minute')
                time.sleep(60)
            return method(self, *args, **kwargs)
        return wrapper

    @exceptions_handler
    def login(self, method: str = 'session') -> str | None:
        """
        The method for authentication in Instagram API.

        Args:
            :param method (str): the type of authentication in the Instagram API. Default: 'session'.
                possible values:
                    'session' - authentication by existing session file. Or create session file for existing device.
                    'relogin' - authentication as an existing device. This will create a new session file and clear the old attributes.

        Returns:
            (str) logged_in
                or
            None
        """
        log.info('[Downloader]: Authentication in the Instagram API with type: %s', method)

        # 2FA authentication settings
        if self.configuration['2fa-enabled']:
            totp_code = self.client.totp_generate_code(seed=self.configuration['2fa-seed'])
            log.info('[Downloader]: Two-factor authentication is enabled. TOTP code: %s', totp_code)
            login_args = {
                'username': self.configuration['username'],
                'password': self.configuration['password'],
                'verification_code': totp_code
            }
        else:
            login_args = {
                'username': self.configuration['username'],
                'password': self.configuration['password']
            }

        # Login to the Instagram API
        if method == 'session' and os.path.exists(self.configuration['session-file']):
            log.info('[Downloader]: Loading session file with creation date %s', time.ctime(os.path.getctime(self.configuration['session-file'])))
            self.client.load_settings(self.configuration['session-file'])
            self.client.login(**login_args)
        elif method == 'relogin':
            log.info('[Downloader]: Relogin to the Instagram API...')
            old_session = self.client.get_settings()
            self.client.set_settings({})
            self.client.set_uuids(old_session["uuids"])
            self.client.login(**login_args)
            self.client.dump_settings(self.configuration['session-file'])
        else:
            log.info('[Downloader]: Creating a new session file...')
            self.client.login(**login_args)
            self.client.dump_settings(self.configuration['session-file'])

        # Check the status of the authentication
        log.info('[Downloader]: Checking the status of the authentication...')
        self.client.get_timeline_feed()
        log.info('[Downloader]: Authentication in the Instagram API was successful.')

        return 'logged_in'

    @exceptions_handler
    def get_post_content(self, shortcode: str = None, error_count: int = 0) -> dict | None:
        """
        The method for getting the content of a post from a specified Post ID.

        Args:
            :param shortcode (str): the ID of the record for downloading content.
            :param error_count (int): the number of errors that occurred during the download.

        Returns:
            (dict) {
                    'post': shortcode,
                    'owner': owner,
                    'type': typename,
                    'status': 'completed'
                }
        """
        if error_count > 3:
            log.error('[Downloader]: The number of errors exceeded the limit: %s', error_count)
            raise FailedDownloadPost("The number of errors exceeded the limit.")

        log.info('[Downloader]: Downloading the contents of the post %s...', shortcode)
        try:
            media_pk = self.client.media_pk_from_code(code=shortcode)
            media_info = self.client.media_info(media_pk=media_pk).dict()
            media_type = media_info['media_type']
            product_type = media_info.get('product_type')
            key = (media_type, 'any' if media_type in (1, 8) else product_type)
            download_method = self.download_methods.get(key)

            path = Path(f"data/{media_info['user']['username']}")
            os.makedirs(path, exist_ok=True)
            status = None

            if download_method:
                download_method(media_pk=media_pk, folder=path)
                status = "completed"
            else:
                log.error('[Downloader]: The media type is not supported for download: %s', media_info)
                status = "not_supported"

            if os.listdir(path):
                log.info('[Downloader]: The contents of the post %s have been successfully downloaded', shortcode)
                response = {
                    'post': shortcode,
                    'owner': media_info['user']['username'],
                    'type': media_info['product_type'] if media_info['product_type'] else 'photo',
                    'status': status if status else 'completed'
                }
            else:
                log.error('[Downloader]: Temporary directory is empty: %s', path)
                response = {
                    'post': shortcode,
                    'owner': media_info['user']['username'],
                    'type': media_info['product_type'] if media_info['product_type'] else 'photo',
                    'status': status if status else 'failed'
                }

        except (MediaUnavailable, MediaNotFound) as error:
            log.warning('[Downloader]: Post %s not found, perhaps it was deleted. Message will be marked as processed: %s', shortcode, error)
            response = {
                'post': shortcode,
                'owner': 'undefined',
                'type': 'undefined',
                'status': 'source_not_found'
            }

        return response
