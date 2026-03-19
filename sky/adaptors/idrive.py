"""iDrive e2 storage adaptor."""

import configparser
import contextlib
import os
import threading
import textwrap
from typing import Dict, Optional, Tuple

from sky import exceptions
from sky import sky_logging
from sky.adaptors import common
from sky.clouds import cloud
from sky.utils import annotations
from sky.utils import ux_utils

logger = sky_logging.init_logger(__name__)

IDRIVE_PROFILE_NAME = 'idrive'
IDRIVE_CREDENTIALS_PATH = '~/.idrive/e2.credentials'
IDRIVE_CONFIG_PATH = '~/.idrive/e2.config'
RCLONE_CONFIG_PATH = '~/.config/rclone/rclone.conf'
RCLONE_REMOTE_NAME = 'idrive'
DEFAULT_REGION = 'us-central-1'
NAME = 'IDrive'
_INDENT_PREFIX = '    '

_IMPORT_ERROR_MESSAGE = ('Failed to import dependencies for iDrive e2. '
                         'Try pip install "skypilot[idrive]"')

boto3 = common.LazyImport('boto3', import_error_message=_IMPORT_ERROR_MESSAGE)
botocore = common.LazyImport('botocore',
                             import_error_message=_IMPORT_ERROR_MESSAGE)

_LAZY_MODULES = (boto3, botocore)
_session_creation_lock = threading.RLock()


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _read_ini(path: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    config.read(_expand(path))
    return config


def _profile_section_name() -> str:
    return f'profile {IDRIVE_PROFILE_NAME}'


def _get_rclone_idrive_config() -> Optional[Tuple[str, str, str, str]]:
    rclone_path = _expand(RCLONE_CONFIG_PATH)
    if not os.path.isfile(rclone_path):
        return None

    config = _read_ini(RCLONE_CONFIG_PATH)
    if not config.has_section(RCLONE_REMOTE_NAME):
        return None

    section = config[RCLONE_REMOTE_NAME]
    access_key_id = section.get('access_key_id')
    secret_access_key = section.get('secret_access_key')
    endpoint = section.get('endpoint') or section.get('endpoint_url')
    region = (section.get('region') or DEFAULT_REGION).strip()

    if not access_key_id or not secret_access_key or not endpoint:
        return None

    return (access_key_id.strip(), secret_access_key.strip(),
            endpoint.strip(), region)


def _write_credential_files(access_key_id: str, secret_access_key: str,
                            endpoint: str, region: str) -> None:
    credentials_path = _expand(IDRIVE_CREDENTIALS_PATH)
    config_path = _expand(IDRIVE_CONFIG_PATH)

    os.makedirs(os.path.dirname(credentials_path), exist_ok=True)

    with open(credentials_path, 'w', encoding='utf-8') as file:
        file.write(
            textwrap.dedent(f"""\
                [{IDRIVE_PROFILE_NAME}]
                aws_access_key_id = {access_key_id}
                aws_secret_access_key = {secret_access_key}
                """))

    with open(config_path, 'w', encoding='utf-8') as file:
        file.write(
            textwrap.dedent(f"""\
                [{_profile_section_name()}]
                region = {region}
                endpoint_url = {endpoint}
                s3 =
                    addressing_style = path
                """))


def ensure_credential_files() -> bool:
    """Ensure AWS-style credential files exist for iDrive e2."""
    if idrive_profile_in_cred() and idrive_profile_in_config():
        return True

    rclone_config = _get_rclone_idrive_config()
    if rclone_config is None:
        return False

    access_key_id, secret_access_key, endpoint, region = rclone_config
    _write_credential_files(access_key_id, secret_access_key, endpoint, region)
    return True


@contextlib.contextmanager
def _load_idrive_credentials_env():
    """Context manager to temporarily change AWS credential file paths."""
    if not ensure_credential_files():
        with ux_utils.print_exception_no_traceback():
            raise ValueError('iDrive e2 credentials not found. Run '
                             '`sky check idrive` to verify they are '
                             'correctly set up.')

    prev_credentials_path = os.environ.get('AWS_SHARED_CREDENTIALS_FILE')
    prev_config_path = os.environ.get('AWS_CONFIG_FILE')
    os.environ['AWS_SHARED_CREDENTIALS_FILE'] = IDRIVE_CREDENTIALS_PATH
    os.environ['AWS_CONFIG_FILE'] = IDRIVE_CONFIG_PATH
    try:
        yield
    finally:
        if prev_credentials_path is None:
            del os.environ['AWS_SHARED_CREDENTIALS_FILE']
        else:
            os.environ['AWS_SHARED_CREDENTIALS_FILE'] = prev_credentials_path
        if prev_config_path is None:
            del os.environ['AWS_CONFIG_FILE']
        else:
            os.environ['AWS_CONFIG_FILE'] = prev_config_path


def get_idrive_credentials(boto3_session):
    """Get iDrive e2 credentials from the boto3 session."""
    with _load_idrive_credentials_env():
        credentials = boto3_session.get_credentials()
        if credentials is None:
            with ux_utils.print_exception_no_traceback():
                raise ValueError('iDrive e2 credentials not found. Run '
                                 '`sky check idrive` to verify they are '
                                 'correctly set up.')
        return credentials.get_frozen_credentials()


@annotations.lru_cache(scope='global')
def session():
    """Create an AWS session for iDrive e2."""
    with _session_creation_lock:
        with _load_idrive_credentials_env():
            return boto3.session.Session(profile_name=IDRIVE_PROFILE_NAME)


@annotations.lru_cache(scope='global')
def resource(resource_name: str, **kwargs):
    """Create an iDrive e2 resource."""
    session_ = session()
    credentials = get_idrive_credentials(session_)
    endpoint = get_endpoint()

    return session_.resource(
        resource_name,
        endpoint_url=endpoint,
        aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        region_name=get_region(),
        config=botocore.config.Config(s3={'addressing_style': 'path'}),
        **kwargs)


@annotations.lru_cache(scope='global')
def client(service_name: str, region: Optional[str] = None):
    """Create an iDrive e2 client of a certain service."""
    session_ = session()
    credentials = get_idrive_credentials(session_)
    endpoint = get_endpoint()

    return session_.client(
        service_name,
        endpoint_url=endpoint,
        aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        region_name=region or get_region(),
        config=botocore.config.Config(s3={'addressing_style': 'path'}),
    )


@common.load_lazy_modules(_LAZY_MODULES)
def botocore_exceptions():
    """AWS botocore exception."""
    from botocore import exceptions as boto_exceptions
    return boto_exceptions


def get_endpoint() -> str:
    """Read the iDrive endpoint from native config or rclone fallback."""
    config_path = _expand(IDRIVE_CONFIG_PATH)
    if os.path.isfile(config_path):
        config = _read_ini(IDRIVE_CONFIG_PATH)
        profile_section = _profile_section_name()
        if (config.has_section(profile_section) and
                config.has_option(profile_section, 'endpoint_url')):
            return config.get(profile_section, 'endpoint_url').strip()

    rclone_config = _get_rclone_idrive_config()
    if rclone_config is not None:
        _, _, endpoint, _ = rclone_config
        return endpoint

    with ux_utils.print_exception_no_traceback():
        raise ValueError('iDrive e2 config not found. Run `sky check idrive` '
                         'to verify setup.')


def get_region() -> str:
    """Read the iDrive region from native config or rclone fallback."""
    config_path = _expand(IDRIVE_CONFIG_PATH)
    if os.path.isfile(config_path):
        config = _read_ini(IDRIVE_CONFIG_PATH)
        profile_section = _profile_section_name()
        if (config.has_section(profile_section) and
                config.has_option(profile_section, 'region')):
            return config.get(profile_section, 'region').strip()

    rclone_config = _get_rclone_idrive_config()
    if rclone_config is not None:
        _, _, _, region = rclone_config
        return region

    return DEFAULT_REGION


def check_credentials(
        cloud_capability: cloud.CloudCapability) -> Tuple[bool, Optional[str]]:
    if cloud_capability == cloud.CloudCapability.STORAGE:
        return check_storage_credentials()
    raise exceptions.NotSupportedError(
        f'{NAME} does not support {cloud_capability}.')


def check_storage_credentials() -> Tuple[bool, Optional[str]]:
    """Check if the user has access credentials to iDrive e2."""
    if idrive_profile_in_cred() and idrive_profile_in_config():
        return True, None

    if _get_rclone_idrive_config() is not None:
        return True, None

    hints = []
    if not idrive_profile_in_cred():
        hints.append(f'[{IDRIVE_PROFILE_NAME}] profile is not set in '
                     f'{IDRIVE_CREDENTIALS_PATH}.')
    if not idrive_profile_in_config():
        hints.append(f'[{_profile_section_name()}] profile is not set in '
                     f'{IDRIVE_CONFIG_PATH}.')

    hint = ' '.join(hints)
    if hint:
        hint += ' Run one of the following setups:'
    else:
        hint = 'Run one of the following setups:'

    hint += f'\n{_INDENT_PREFIX}1. Create AWS-style credential files:'
    hint += f'\n{_INDENT_PREFIX}  $ pip install boto3'
    hint += (f'\n{_INDENT_PREFIX}  $ AWS_SHARED_CREDENTIALS_FILE='
             f'{IDRIVE_CREDENTIALS_PATH} aws configure --profile '
             f'{IDRIVE_PROFILE_NAME}')
    hint += (f'\n{_INDENT_PREFIX}  $ AWS_CONFIG_FILE={IDRIVE_CONFIG_PATH} '
             'aws configure set endpoint_url <IDRIVE_ENDPOINT_URL> --profile '
             f'{IDRIVE_PROFILE_NAME}')
    hint += (f'\n{_INDENT_PREFIX}  $ AWS_CONFIG_FILE={IDRIVE_CONFIG_PATH} '
             f'aws configure set region {DEFAULT_REGION} --profile '
             f'{IDRIVE_PROFILE_NAME}')
    hint += (f'\n{_INDENT_PREFIX}  $ AWS_CONFIG_FILE={IDRIVE_CONFIG_PATH} '
             f'aws configure set s3.addressing_style path --profile '
             f'{IDRIVE_PROFILE_NAME}')
    hint += f'\n{_INDENT_PREFIX}2. Or create an [{RCLONE_REMOTE_NAME}] remote '
    hint += (f'in {RCLONE_CONFIG_PATH} with access_key_id, '
             'secret_access_key, endpoint, and region. SkyPilot will '
             'bootstrap the AWS-style files from it when needed.')

    return False, hint


def idrive_profile_in_config() -> bool:
    """Check if the iDrive profile is set in the AWS config file."""
    conf_path = _expand(IDRIVE_CONFIG_PATH)
    if not os.path.isfile(conf_path):
        return False

    with open(conf_path, 'r', encoding='utf-8') as file:
        for line in file:
            if f'[{_profile_section_name()}]' in line:
                return True
    return False


def idrive_profile_in_cred() -> bool:
    """Check if the iDrive profile is set in the AWS credentials file."""
    cred_path = _expand(IDRIVE_CREDENTIALS_PATH)
    if not os.path.isfile(cred_path):
        return False

    with open(cred_path, 'r', encoding='utf-8') as file:
        for line in file:
            if f'[{IDRIVE_PROFILE_NAME}]' in line:
                return True
    return False


def get_credential_file_mounts() -> Dict[str, str]:
    """Return credential files that should be mounted to remote machines."""
    if not ensure_credential_files():
        return {}

    return {
        IDRIVE_CREDENTIALS_PATH: IDRIVE_CREDENTIALS_PATH,
        IDRIVE_CONFIG_PATH: IDRIVE_CONFIG_PATH,
    }
