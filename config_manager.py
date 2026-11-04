import os
import json
import sys

DEFAULT_CONFIG = {
    "jk_chip": [
        "nRF52840_xxAA",
        "STM32L412CB",
        "stm32f103C8",
        "stm32f103zet6",
        "nRF52832_xxAA",
        "nRF52832_xxAB"
    ],
    "jk_interface": [
        "SWD"
    ],
    "jk_con_reset": True,
    "jk_speed": 4000,
    "hw_sel": "1",
    "filter": "",
    "filter_en": False,
    "font": [
        "Consolas",
        "Calibri"
    ],
    "font_size": "12",
    "tx_line": "\n",
    "user_input_data": [
        "123"
    ],
    "curves_name": "X&&Y&&Z",
    "y_range": [
        -100,
        100
    ],
    "y_label_text": "m/s^2",
    "ser_baud": [
        2000000,
        115200,
        941176,
        1152000,
        9600,
        19200
    ],
    "ser_com": "COM1",
    "ser_des": "通信端口 (COM1)",
    "char_format": "asc",
    "update_flag": True,
    "line_break": "\n",
    "rtt_block_address": [
        "",
        ""
    ]
}

def get_app_dir():
    return os.path.dirname(os.path.realpath(sys.argv[0]))

def get_config_path():
    return os.path.join(get_data_dir(), 'config.json')

def get_data_dir():
    directory = os.environ.get('RTT_DATA_DIR') or get_app_dir()
    os.makedirs(directory, exist_ok=True)
    return directory

def get_log_dir():
    return os.path.join(get_data_dir(), 'aaa_log')

def load_config():
    config_path = get_config_path()
    if not os.path.exists(config_path):
        # A user may place their previous portable config beside the stable
        # launcher. Import it once; leave device ledgers and caches in place.
        legacy = os.path.join(os.environ.get('RTT_UPDATE_ROOT', get_app_dir()), 'config.json')
        if os.path.abspath(legacy) != os.path.abspath(config_path) and os.path.isfile(legacy):
            with open(legacy, 'r', encoding='utf-8-sig') as f:
                save_config(json.load(f))
        else:
            save_config(DEFAULT_CONFIG)
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(config):
    import tempfile
    path = get_config_path()
    descriptor, temporary = tempfile.mkstemp(prefix='.config-', dir=get_data_dir())
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def ensure_log_dir():
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    return log_dir

def initialize_app_environment():
    load_config()  # This will create the config file if it doesn't exist
    return ensure_log_dir()

def ensure_log_dir():
    log_dir = get_log_dir()
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    return log_dir
